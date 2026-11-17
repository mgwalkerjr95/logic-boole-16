# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""The trigger, the capture clock and the glitch filter: the text that describes each, and the FPGA's registers.

A trigger is one to four stages.  They are met one after the other, each by a later sample than the one before,
and the sample that meets the last stage is the trigger sample.  As text, with "then" between the stages:

    B0=r                            B0 rises (f: falls, e: either)
    A0=1 A1=0                       A0 is high and A1 is low
    A2=f A0=1                       A2 falls while A0 is high
    A0=1 for 10us                   A0 has been high for 10 us, and still is
    A0=1 pulse 100ns..1us           a pulse has ended: A0 was high for 100 ns to 1 us, and is low now
    A0=0 pulse ..50ns               a low pulse of 50 ns at the most ("1us..": of 1 us at the least)
    in=r                            the TRIG IN pin rises (in=f: falls, in=e: either)
    B7=f then B0=r then B1=1 for 2ms

The FPGA looks at the samples it keeps, so a time becomes a number of samples at the capture's sample rate, and
a number with no unit is a number of samples.  A capture clock is "A0=r", "B3=f" or "A0=e": a sample is then
kept at each such edge of that input in place of every so many nanoseconds.  A glitch filter is a time: a
change of an input counts once the input has held its new level for that long.

docs/protocol.md describes the registers.
"""
import math
import struct

from .capture import CHANNELS

STAGES = 4                                  # how many the FPGA's design has (its register 2D says so too)
PERIOD = 5e-9                               # seconds: the glitch filter counts in the FPGA's samples, 5 ns apart
FILTER_MAX = 250                            # samples: the longest glitch filter, 1.25 us
UNITS = (("ns", 1e-9), ("us", 1e-6), ("ms", 1e-3), ("s", 1.0))
EDGES = {"r": (1, 0), "f": (0, 1), "e": (1, 1)}
NO_LIMIT = 0xFFFFFFFF


class TriggerError(ValueError):
    pass


def channel(name):
    """The number of a channel, 0 (A0) ... 15 (B7), from its name."""
    try:
        return CHANNELS.index(name.strip().upper())
    except ValueError:
        raise TriggerError("%s is not a channel: they are A0 ... A7 and B0 ... B7." % name.strip()) from None


def seconds(text):
    """A time as text ("100ns", "2.5us", "1ms", "0.5s") in seconds, or None for a number with no unit."""
    for unit, scale in UNITS:
        if text.endswith(unit):
            try:
                return float(text[:-len(unit)]) * scale
            except ValueError:
                break
    return None


def _samples(text, rate):
    """A time, or a number of samples, as a number of samples."""
    t = seconds(text)
    if t is not None and rate is None:
        raise TriggerError("With a capture clock there is no sample rate: give %s as a number of samples." % text)
    try:
        n = round(t * rate) if t is not None else int(text)
    except ValueError:
        raise TriggerError("%s is neither a time (100ns, 2us, 1ms) nor a number of samples." % text) from None
    if not 0 <= n < NO_LIMIT:
        raise TriggerError("%s is too long: 4 billion samples is the most." % text)
    return n


def parse(text, rate=None):
    """A trigger as a list of stages, from its text.  rate: the capture's samples a second, which times need.

    A stage is a dict: kind (0 levels and edges, 1 a pulse has ended, 2 the levels have lasted, 3 TRIG IN),
    mask and value (the inputs that must be at a level, and the levels), rise and fall (the inputs of which one
    must have just risen or fallen), min and max (samples), in_rise and in_fall (TRIG IN)."""
    stages, parts = [], [[]]
    for w in text.replace(",", " ").split():
        if w.lower() == "then":
            parts.append([])
        else:
            parts[-1].append(w)
    for words in parts:
        part = " ".join(words)
        if not words:
            raise TriggerError("The trigger has an empty stage: \"%s\"." % text.strip())
        s = {"kind": 0, "mask": 0, "value": 0, "rise": 0, "fall": 0, "min": 0, "max": 0, "in_rise": 0, "in_fall": 0}
        i = 0
        while i < len(words):
            w = words[i]
            i += 1
            if w in ("for", "pulse"):
                if s["kind"] or i >= len(words):
                    raise TriggerError("\"%s\" is followed by one time, once in a stage: \"%s\"." % (w, part.strip()))
                arg = words[i]
                i += 1
                if w == "for":
                    s["kind"], s["min"] = 2, max(1, _samples(arg, rate))
                else:
                    low, dots, high = arg.partition("..")
                    if not dots:
                        low = high = arg
                    s["kind"] = 1
                    s["min"] = max(1, _samples(low, rate)) if low else 1
                    s["max"] = _samples(high, rate) if high else NO_LIMIT
                    if s["max"] < s["min"]:
                        raise TriggerError("The pulse's longest time is shorter than its shortest: \"%s\"." % arg)
                continue
            name, eq, what = w.partition("=")
            what = what.lower()
            if not eq or what not in ("0", "1", "r", "f", "e"):
                raise TriggerError("\"%s\" is not understood: a stage is made of things like A0=1, B3=r, "
                                   "\"for 10us\" and \"pulse 1us..2us\"." % w)
            if name.lower() == "in":
                if what in "01":
                    raise TriggerError("TRIG IN is used by its edges: in=r, in=f or in=e.")
                s["in_rise"], s["in_fall"] = EDGES[what]
                continue
            bit = 1 << channel(name)
            if (s["mask"] | s["rise"] | s["fall"]) & bit:
                raise TriggerError("%s is named twice in one stage." % name.upper())
            if what in "01":
                s["mask"] |= bit
                s["value"] |= bit * int(what)
            else:
                s["rise"] |= bit * EDGES[what][0]
                s["fall"] |= bit * EDGES[what][1]
        edges = s["rise"] | s["fall"]
        if s["in_rise"] or s["in_fall"]:
            if s["kind"] or edges:
                raise TriggerError("A stage with TRIG IN can name levels with it, nothing else: \"%s\"." % part.strip())
            s["kind"] = 3
        elif s["kind"] and (edges or not s["mask"]):
            raise TriggerError("\"for\" and \"pulse\" go with levels (A0=1), not with edges: \"%s\"." % part.strip())
        elif not s["mask"] and not edges:
            raise TriggerError("The stage \"%s\" names no input." % part.strip())
        stages.append(s)
    if len(stages) > STAGES:
        raise TriggerError("A trigger has %d stages at the most; this one has %d." % (STAGES, len(stages)))
    return stages


def stages_of(trigger, rate=None):
    """What capture() is given as its trigger, as a list of stages: text, one stage or a list of them, or None."""
    if not trigger:
        return []
    if isinstance(trigger, str):
        return parse(trigger, rate)
    stages = [trigger] if isinstance(trigger, dict) else list(trigger)
    blank = {"kind": 0, "mask": 0, "value": 0, "rise": 0, "fall": 0, "min": 0, "max": 0, "in_rise": 0, "in_fall": 0}
    stages = [dict(blank, **s) for s in stages]
    if len(stages) > STAGES:
        raise TriggerError("A trigger has %d stages at the most." % STAGES)
    for s in stages:
        if s["value"] & ~s["mask"]:
            raise TriggerError("A stage's value has a bit outside its mask: it would never be met.")
    return stages


def describe(stages, rate=None):
    """A trigger back as text.  Times are given as times if the rate is known."""
    def span(n):
        if rate is None:
            return str(n)
        t = n / rate
        for unit, scale in reversed(UNITS):
            if t >= scale or unit == "ns":
                return "%.6g%s" % (t / scale, unit)

    out = []
    for s in stages:
        words = []
        for ch, name in enumerate(CHANNELS):
            bit = 1 << ch
            if s["mask"] & bit:
                words.append("%s=%d" % (name, bool(s["value"] & bit)))
            elif (s["rise"] | s["fall"]) & bit:
                words.append("%s=%s" % (name, "e" if s["rise"] & s["fall"] & bit else "r" if s["rise"] & bit else "f"))
        if s["kind"] == 3:
            words.append("in=%s" % ("e" if s["in_rise"] and s["in_fall"] else "r" if s["in_rise"] else "f"))
        elif s["kind"] == 2:
            words.append("for %s" % span(s["min"]))
        elif s["kind"] == 1:
            words.append("pulse %s..%s" % (span(s["min"]) if s["min"] > 1 else "",
                                          span(s["max"]) if s["max"] != NO_LIMIT else ""))
        out.append(" ".join(words))
    return " then ".join(out)


def stage_registers(s):
    """One stage as the FPGA's registers: its mode (one byte) and its 16 bytes."""
    mode = s["kind"] | (4 if s["in_rise"] else 0) | (8 if s["in_fall"] else 0)
    return mode, struct.pack("<HHHHII", s["mask"], s["value"], s["rise"], s["fall"], s["min"], s["max"])


def clock_register(clock):
    """A capture clock ("A0=r", "B3=f", "A0=e"; a channel alone means its rising edges) as register 30, and as
    text.  None: (0, None), the capture is timed by the sample rate."""
    if not clock:
        return 0, None
    name, eq, edge = clock.strip().partition("=")
    edge = edge.lower() or "r"
    if edge not in EDGES:
        raise TriggerError("A capture clock is an input and an edge: A0=r, A0=f or A0=e.")
    ch = channel(name)
    rise, fall = EDGES[edge]
    return 1 | rise << 1 | fall << 2 | ch << 4, "%s=%s" % (CHANNELS[ch], edge)


def filter_samples(glitch):
    """The glitch filter as 16 numbers of samples, from what capture() is given: None (off), a time in seconds
    for every input, or a dict of channel names ("A0"), pods ("A", "B") or "all" and their times.  A change
    counts once that many samples in a row, 5 ns apart, show the new level."""
    out = [0] * 16
    if not glitch:
        return out
    if not isinstance(glitch, dict):
        glitch = {"all": glitch}
    for name, t in glitch.items():
        n = round(float(t) / PERIOD)
        if t and not 2 <= n <= FILTER_MAX:
            raise TriggerError("A glitch filter is 10 ns ... %.4g us, in steps of 5 ns."
                               % (FILTER_MAX * PERIOD * 1e6))
        key = name.strip().upper()
        which = (range(16) if key == "ALL" else range(8) if key == "A" else range(8, 16) if key == "B"
                 else (channel(key),))
        for ch in which:
            out[ch] = n if t else 0
    return out


def parse_filter(text):
    """The command line's glitch filter, "20ns" or "A0=20ns,B=1us", as what capture() takes."""
    out = {}
    for part in text.replace(",", " ").split():
        name, eq, t = part.partition("=")
        if not eq:
            name, t = "all", part
        value = seconds(t)
        if value is None:
            if t.strip() not in ("0", "off"):
                raise TriggerError("%s is not a time: a glitch filter is 20ns, 1us and the like, or off." % t)
            value = 0.0
        out[name] = value
    return out


def ring_words(before, fmt, most):
    """How many words the FPGA's FIFO keeps while it waits, for that many samples before the trigger: a
    run-length word holds a sample at least, a raw word two."""
    need = before if fmt == "rle" else math.ceil(before / 2) + 1
    return max(0, min(most, need))
