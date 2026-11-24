# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""What is on each channel of a saved capture: steady, a clock, PWM, or part of an I2C bus, an SPI bus or a serial
line.  This is the app's auto-detect (web/detect.js) for the command line and the MCP server, with the same rules,
so that both find the same buses.

The buses are found by trying them and keeping only the combinations whose framing is right throughout: an SPI
chip select whose low stretches each hold whole bytes of clock pulses and no clock edge outside them; an I2C pair
on which every start is followed by whole bytes of nine clock pulses up to its stop; a serial line that rests
high, whose pulses are whole bits of one of the usual speeds, and that decodes with no framing error.  A bus that
is shown has decoded cleanly; it is still a guess about the wiring, to be checked with the person who knows it.
"""
import math
import statistics
from bisect import bisect_left, bisect_right

from .capture import CHANNELS

LOOK = 200000                               # edges of a channel that are looked at
BAUDS = (300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400, 57600, 76800, 115200, 230400, 250000,
         460800, 500000, 921600, 1000000, 1500000, 2000000, 3000000)


def channel_stats(edges, samples, rate, ch):
    """One channel: how many edges, how much of the time it is high, and whether it repeats (a clock or PWM)."""
    initial, E = edges[ch]
    n = len(E)
    st = {"ch": ch, "edges": n, "kind": "steady", "level": initial, "high": initial, "freq": 0.0, "duty": 0.0,
          "min_width": 0}
    if not n:
        return st
    hi, lvl, prev, m, min_w = 0, initial, 0, min(n, LOOK), math.inf
    for i in range(m):
        if lvl:
            hi += E[i] - prev
        if i and E[i] - prev < min_w:
            min_w = E[i] - prev
        prev = E[i]
        lvl ^= 1
    span = samples if m == n else E[m - 1]
    if m == n and lvl:
        hi += samples - prev
    st["high"] = hi / span if span else initial
    st["min_width"] = min_w if math.isfinite(min_w) else 0
    st["kind"] = "data"
    if m >= 8:                              # a clock or PWM: every period (rising edge to rising edge) is the same
        pmin, pmax, psum, np_, wmin, wmax, wsum = math.inf, 0, 0, 0, math.inf, 0, 0
        i = 1 if initial else 0             # the index of the first rising edge
        while i + 2 < m:
            p, w = E[i + 2] - E[i], E[i + 1] - E[i]
            pmin, pmax, wmin, wmax = min(pmin, p), max(pmax, p), min(wmin, w), max(wmax, w)
            psum, wsum, np_ = psum + p, wsum + w, np_ + 1
            i += 2
        if np_ >= 3:
            mean = psum / np_
            # ... and it runs from one end of the capture to the other: a burst of clock pulses is a bus, not a clock
            throughout = E[0] <= 2 * mean and (m < n or samples - E[n - 1] <= 2 * mean)
            if throughout and pmax - pmin <= 2 + mean * 0.01:
                st["freq"], st["duty"] = rate / mean, wsum / psum
                st["kind"] = "clock" if wmax - wmin <= 2 + mean * 0.01 else "pwm"
    return st


def _level(edges, ch, s):
    initial, E = edges[ch]
    return initial ^ (bisect_right(E, s) & 1)


def _inside_low(cs, s, margin):
    """Is sample s inside a low stretch of the chip select cs, give or take margin samples at its ends?"""
    initial, E = cs
    i = bisect_right(E, s)
    if (initial ^ (i & 1)) == 0:
        return True
    return (i < len(E) and E[i] - s <= margin) or (i > 0 and s - E[i - 1] <= margin)


def find_spi(edges, stats, free):
    """SPI: a chip select whose low stretches hold every clock edge, 8 clock pulses or a multiple of that in each."""
    found = []

    def busy(i):
        return i in free and stats[i]["kind"] == "data"

    for k in range(16):
        if not busy(k) or stats[k]["edges"] < 16:
            continue
        clk_initial, CE = edges[k]
        nc = min(len(CE), LOOK)
        for c in range(16):
            if (c == k or not busy(c) or stats[c]["edges"] < 2 or stats[c]["edges"] * 8 > stats[k]["edges"]
                    or stats[c]["high"] < 0.5):
                continue
            cs_initial, SE = edges[c]
            ok, whole, nbytes, first_fall = True, 0, 0, -1
            i = 0 if cs_initial else 1      # whole low stretches only: from a falling edge to the next rising one
            while i + 1 < len(SE) and ok:
                a, b = bisect_left(CE, SE[i]), bisect_left(CE, SE[i + 1])
                if b > nc:                  # beyond the part that is looked at
                    break
                n = b - a
                if n < 16 or n % 16:
                    ok = False
                else:
                    whole += 1
                    nbytes += n // 16
                    if first_fall < 0:
                        first_fall = SE[i]
                i += 2
            if not ok or not whole:
                continue
            start = 0 if cs_initial else (SE[0] if SE else math.inf)       # and no clock edge while it is high
            for i in range(bisect_left(CE, start), nc):
                j = bisect_right(SE, CE[i])
                if (cs_initial ^ (j & 1)) == 1:
                    ok = False
                    break
            if not ok:
                continue
            margin = stats[k]["min_width"] * 2
            data = []
            for d in range(16):
                if d in (k, c) or not busy(d) or stats[d]["edges"] < 2:
                    continue
                DE = edges[d][1]
                nd = min(len(DE), LOOK)
                inside = sum(1 for i in range(nd) if _inside_low(edges[c], DE[i], margin))
                if inside >= nd * 0.95:
                    data.append(d)
            if not data:
                continue
            data = sorted(sorted(data, key=lambda x: -stats[x]["edges"])[:2])
            cpol = _level(edges, k, first_fall)
            lead = trail = 0                # data that changes after the second edge of each clock pulse: CPHA 0
            for d in data:
                DE = edges[d][1]
                for i in range(min(len(DE), 5000)):
                    j = bisect_right(CE, DE[i]) - 1
                    s = bisect_right(SE, DE[i])
                    if j < 0 or (s > 0 and CE[j] < SE[s - 1]):     # before the first clock pulse
                        trail += 1
                    elif (clk_initial ^ ((j + 1) & 1)) != cpol:
                        lead += 1
                    else:
                        trail += 1
            found.append({"type": "spi", "score": nbytes, "channels": [k, c] + data,
                          "cfg": {"clk": k, "cs": c, "mosi": data[0], "miso": data[1] if len(data) > 1 else -1,
                                  "cpol": cpol, "cpha": 1 if lead > trail else 0}})
    return found


def i2c_stats(edges, samples, sda, scl, limit=math.inf):
    """The app's I2C decoder, counting only: errors, stray clock pulses, bytes and whole messages."""
    (sda_l, a), (scl_l, c) = edges[sda], edges[scl]
    ia = ic = events = 0
    txn, addressed, bits = False, False, 0
    counts = {"errors": 0, "stray": 0, "bytes": 0, "rows": 0}

    def emit():
        nonlocal addressed, bits
        counts["bytes"] += 1
        addressed, bits = True, 0

    def close():
        nonlocal txn, bits
        if bits == 9:
            emit()
        if bits > 1:                        # a byte cut short
            counts["errors"] += 1
        if not addressed:
            counts["errors"] += 1
        counts["rows"] += 1
        txn, bits = False, 0

    while (ia < len(a) or ic < len(c)) and events < limit:
        events += 1
        sa = a[ia] if ia < len(a) else math.inf
        sc = c[ic] if ic < len(c) else math.inf
        if sc <= sa:                        # the clock first when both change in one sample
            ic += 1
            scl_l ^= 1
            if scl_l:
                if txn:
                    bits += 1
                elif counts["rows"]:
                    counts["stray"] += 1    # clock pulses between a stop and the next start
            elif txn and bits == 9:
                emit()
        else:
            ia += 1
            sda_l ^= 1
            if not scl_l:
                continue                    # data changing while the clock is low: nothing special
            if not sda_l:                   # falling while the clock is high: a start
                if txn:
                    close()
                txn, addressed, bits = True, False, 0
            elif txn:                       # rising while the clock is high: a stop
                close()
    # a message still open where the capture ends is cut, and not counted
    return {"errors": counts["errors"], "stray": counts["stray"], "bytes": counts["bytes"],
            "messages": counts["rows"]}


def find_i2c(edges, samples, stats, free):
    """I2C: a pair of lines on which every start is followed by whole bytes of nine clock pulses up to the stop."""
    found = []

    def busy(i):
        return i in free and stats[i]["kind"] == "data"

    for scl in range(16):
        if not busy(scl) or stats[scl]["edges"] < 18:
            continue
        for sda in range(16):
            if sda == scl or not busy(sda) or stats[sda]["edges"] < 4:
                continue
            st = i2c_stats(edges, samples, sda, scl, LOOK)
            if st["messages"] < 1 or st["errors"] or st["stray"] or st["bytes"] < st["messages"]:
                continue
            found.append({"type": "i2c", "score": st["bytes"], "channels": [sda, scl], "cfg": {"sda": sda, "scl": scl}})
    return found


def estimate_baud(edges, rate, ch):
    """The speed of a serial line from its pulse widths: every pulse is a whole number of bits long."""
    E = edges[ch][1]
    n = min(len(E), 20000)
    if n < 6:
        return None
    w = [E[i] - E[i - 1] for i in range(1, n)]
    u = sorted(w)[int(len(w) * 0.02)]       # near the shortest, ignoring a stray glitch
    if u < 2:
        return None
    fit = 0.0
    for _ in range(2):                      # fit the bit time to every pulse that is 1 ... 10 bits long
        sw = sk = good = short = 0
        for x in w:
            k = math.floor(x / u + 0.5)     # as JavaScript's Math.round: halves go up
            if k < 1:
                continue
            if k <= 10:
                short += 1
                if abs(x / u - k) < 0.25:
                    sw, sk, good = sw + x, sk + k, good + 1
        if not sk:
            return None
        u = sw / sk
        fit = good / short if short else 0.0
    raw = rate / u
    best = min(BAUDS, key=lambda b: abs(math.log(raw / b)))
    standard = abs(raw / best - 1) < 0.045
    return {"baud": best if standard else math.floor(raw + 0.5), "raw": raw, "standard": standard, "fit": fit}


def uart_stats(edges, samples, rate, rx, baud, nbits=8, parity="none", limit=LOOK):
    """The app's serial decoder (decodeUART: resting high, nbits data bits the lowest first, a parity bit if parity
    is "even" or "odd", 1 stop bit) on the first `limit` edges, counting only: bytes, framing errors and parity
    errors, with the values of the bytes that had neither ("values")."""
    initial, E = edges[rx]
    bit = rate / baud
    par = 0 if parity == "none" else 1
    out = {"bytes": 0, "framing": 0, "parity": 0, "values": []}
    if not bit >= 3:
        return out

    def lvl(s):
        return initial ^ (bisect_right(E, s) & 1)

    i, p = 0, 0
    while True:
        i = bisect_left(E, p, i)
        while i < len(E) and (initial ^ ((i + 1) & 1)) == 1:      # an edge away from idle
            i += 1
        if i >= len(E) or i >= limit:
            break
        s0 = E[i]
        if s0 + bit * (1.5 + nbits + par) >= samples:             # the capture ends inside this byte
            break
        if lvl(math.floor(s0 + bit / 2)) == 1:                    # shorter than half a bit: a glitch
            p = s0 + 1
            continue
        v = ones = 0
        for k in range(nbits):
            b = lvl(math.floor(s0 + bit * (1.5 + k)))
            ones += b
            v |= b << k
        wrong = False
        if par and (ones + lvl(math.floor(s0 + bit * (1.5 + nbits)))) & 1 != (1 if parity == "odd" else 0):
            out["parity"] += 1
            wrong = True
        if lvl(math.floor(s0 + bit * (1.5 + nbits + par))) != 1:
            out["framing"] += 1
            wrong = True
        out["bytes"] += 1
        if not wrong:
            out["values"].append(v)
        p = math.floor(s0 + bit * (1 + nbits + par + 0.5))        # the middle of the stop bit
    return out


# The serial formats tried, in turn: 8 data bits with no parity (8N1), with even parity (8E1), with odd (8O1).
# Seven data bits with even parity (7E1) decode as 8N1, with the parity bit as the eighth: seven_even() tells them
# apart by that bit, which then always makes the number of ones even
FORMATS = ((8, "none"), (8, "even"), (8, "odd"))


def seven_even(values):
    """Whether bytes decoded as 8N1 (those with no error) are 7 data bits and an even parity bit: at least 16 of
    them, each with an even number of ones, and the top bit (the parity bit) set in some."""
    if any(bin(v).count("1") & 1 for v in values):
        return False
    return len(values) >= 16 and any(v & 0x80 for v in values)


def find_uart(edges, samples, rate, stats, free):
    """Serial: a line that rests high, whose pulses are all whole bits of one of the usual speeds, and that decodes
    in one of FORMATS (or as 7E1) with hardly a framing or parity error: 2 % of the bytes at the most."""
    found = []
    for rx in range(16):
        if rx not in free or stats[rx]["kind"] != "data" or stats[rx]["edges"] < 8 or stats[rx]["high"] < 0.5:
            continue
        est = estimate_baud(edges, rate, rx)
        if not est or not est["standard"] or est["fit"] < 0.97:
            continue
        for nbits, parity in FORMATS:
            st = uart_stats(edges, samples, rate, rx, est["baud"], nbits, parity)
            if st["bytes"] < 2 or st["framing"] > st["bytes"] * 0.02 or st["parity"] > st["bytes"] * 0.02:
                continue
            if parity == "none" and seven_even(st["values"]):
                nbits, parity = 7, "even"
                st = uart_stats(edges, samples, rate, rx, est["baud"], nbits, parity)
            found.append({"type": "uart", "score": st["bytes"], "channels": [rx],
                          "cfg": {"rx": rx, "baud": est["baud"], "bits": nbits, "parity": parity}})
            break
    return found


def analyze(cap):
    """The app's detect(): (the buses found, best first and no channel used twice; each channel's stats)."""
    edges = cap.edges()
    stats = [channel_stats(edges, cap.samples, cap.rate, ch) for ch in range(16)]
    free, buses = set(range(16)), []
    for finder in (lambda: find_spi(edges, stats, free), lambda: find_i2c(edges, cap.samples, stats, free),
                   lambda: find_uart(edges, cap.samples, cap.rate, stats, free)):
        for f in sorted(finder(), key=lambda f: -f["score"]):
            if all(c in free for c in f["channels"]):
                free.difference_update(f["channels"])
                buses.append(f)
    return buses, stats, edges


def _clock_rate(edges, rate, ch):
    """A bus clock's rate inside its bursts: 1 / the median time from one rising edge to the next."""
    initial, E = edges[ch]
    rising = E[(1 if initial else 0):LOOK:2]
    periods = [b - a for a, b in zip(rising, rising[1:])]
    return rate / statistics.median(periods) if periods else None


def detect(cap):
    """What is on each channel, as a dict: buses (each with its channels, what it carried, the words that decode
    it with `decode`, and names to give its channels) and channels (each one's kind: steady, clock, pwm, data, or
    the bus it is part of)."""
    buses, stats, edges = analyze(cap)
    taken, out_buses, given = {}, [], {}

    def name(ch, want):
        n, k = want, 2
        while n.lower() in given:
            n, k = "%s%d" % (want, k), k + 1
        given[n.lower()] = ch
        return n

    for f in buses:
        cfg, kind = f["cfg"], f["type"]
        if kind == "i2c":
            roles = [("sda", cfg["sda"], "SDA"), ("scl", cfg["scl"], "SCL")]
            entry = {"bus": "i2c", "sda": CHANNELS[cfg["sda"]], "scl": CHANNELS[cfg["scl"]], "bytes": f["score"],
                     "clock_hz": _clock_rate(edges, cap.rate, cfg["scl"]),
                     "decode": "i2c scl=%s sda=%s" % (CHANNELS[cfg["scl"]], CHANNELS[cfg["sda"]])}
        elif kind == "spi":
            roles = [("clk", cfg["clk"], "SPI_CLK"), ("cs", cfg["cs"], "SPI_CS"), ("mosi", cfg["mosi"], "SPI_MOSI")]
            if cfg["miso"] >= 0:
                roles.append(("miso", cfg["miso"], "SPI_MISO"))
            entry = {"bus": "spi"}
            entry.update((role, CHANNELS[ch]) for role, ch, _ in roles)
            entry.update(cpol=cfg["cpol"], cpha=cfg["cpha"], mode=cfg["cpol"] * 2 + cfg["cpha"], bytes=f["score"],
                         clock_hz=_clock_rate(edges, cap.rate, cfg["clk"]))
            entry["decode"] = "spi %s cpol=%d cpha=%d" % (" ".join("%s=%s" % (role, CHANNELS[ch])
                                                                    for role, ch, _ in roles), cfg["cpol"], cfg["cpha"])
        else:
            roles = [("rx", cfg["rx"], "UART")]
            entry = {"bus": "uart", "rx": CHANNELS[cfg["rx"]], "baudrate": cfg["baud"], "bytes": f["score"],
                     "data_bits": cfg["bits"], "parity": cfg["parity"],
                     "decode": "uart rx=%s baudrate=%d%s%s" % (CHANNELS[cfg["rx"]], cfg["baud"],
                                                             " data_bits=%d" % cfg["bits"] if cfg["bits"] != 8 else "",
                                                             " parity=" + cfg["parity"] if cfg["parity"] != "none"
                                                             else "")}
        entry["names"] = ",".join("%s=%s" % (CHANNELS[ch], name(ch, want)) for _, ch, want in roles)
        for _, ch, _ in roles:
            taken[ch] = kind
        out_buses.append(entry)
    channels = []
    for ch, st in enumerate(stats):
        c = {"channel": CHANNELS[ch], "changes": st["edges"]}
        if cap.names[ch] != CHANNELS[ch]:
            c["name"] = cap.names[ch]
        if ch in taken:
            c["kind"] = taken[ch]
        else:
            c["kind"] = st["kind"]
            if st["kind"] == "steady":
                c["level"] = st["level"]
            elif st["kind"] in ("clock", "pwm"):
                c["frequency"], c["duty"] = st["freq"], st["duty"]
            else:
                c["high"] = st["high"]
        channels.append(c)
    return {"sample_rate": cap.rate, "samples": cap.samples, "seconds": cap.seconds, "buses": out_buses,
            "channels": channels}
