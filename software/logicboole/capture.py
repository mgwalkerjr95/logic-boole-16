# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""A finished capture, and the files it can be written to.

    .lb16   this project's own file: the app opens and saves it.  Small, because it holds only the changes
    .sr     a sigrok session, for PulseView and sigrok-cli and their protocol decoders
    .vcd    value change dump, for GTKWave and most other waveform viewers
    .csv    a line for every change: the time in seconds and the 16 levels

Capture.load() reads .lb16 and .sr back, and Capture.measure() reads times off a capture: periods, pulse lengths,
changes.
"""
import heapq
import json
import math
import os
import re
import statistics
import struct
import zipfile
import zlib
from array import array
from bisect import bisect_left
from itertools import accumulate, islice

from . import words as W

CHANNELS = ["A%d" % i for i in range(8)] + ["B%d" % i for i in range(8)]
MAGIC = b"LB16CAP1"
LB16_SAMPLES = 1 << 32                      # an .lb16 file gives the sample of each change in 32 bits
SR_EXTRA = "logicboole"                     # the part of a sigrok session file in which this software keeps more
NAME = re.compile(r"[A-Za-z0-9_.+#/-]{1,24}")
RESERVED = {"in", "then", "for", "pulse"}    # words of the trigger's text


class TooLong(ValueError):
    """An .lb16 file cannot hold the capture: it changes after sample 2^32."""


def parse_names(text):
    """Channel names as text, "A0=SDA,A1=SCL", as {channel number: name}.  A name is up to 24 letters, digits and
    _ . + # / -, and is not another channel's id."""
    out = {}
    for part in text.replace(",", " ").split():
        ch, eq, name = part.partition("=")
        if ch.upper() not in CHANNELS or not eq:
            raise ValueError("\"%s\": a channel's name is given as A0=SDA." % part)
        k = CHANNELS.index(ch.upper())
        if not NAME.fullmatch(name):
            raise ValueError("\"%s\": a name is 1 to 24 letters, digits and _ . + # / -, with no space." % name)
        if name.upper() in CHANNELS and name.upper() != CHANNELS[k] or name.lower() in RESERVED:
            raise ValueError("%s cannot be a channel's name: it means something else in a trigger." % name)
        if k in out:
            raise ValueError("%s is named twice." % CHANNELS[k])
        out[k] = name
    return out


class Capture:
    """What the 16 inputs did, as runs: values[i] (bit 0 is A0 ... bit 15 is B7) held for lengths[i] samples.

    rate          samples a second
    samples       how many samples the capture holds
    thresholds    (pod A, pod B) in volts at the probe, or None if not known
    trigger       the number of the trigger sample, if the capture waited for a trigger and found it, else None
    overflow      the capture ended early because the computer did not take the data fast enough: it is whole
                  up to its last sample
    stopped       the computer ended it early ("stopped": asked to; "size": it had grown too large; "timeout":
                  the trigger did not come in the time given), else None
    problems      what was found wrong with it, as text.  An empty list means every check passed
    info          what else is known: the time it was taken, the format on USB, the link's speed ...
    """

    def __init__(self, rate, values, lengths, thresholds=None, overflow=False, stopped=None, problems=(),
                 info=None, trigger=None):
        self.rate, self._runs = rate, (values, lengths)
        self.samples = sum(lengths)
        self.trigger = trigger
        self.thresholds, self.overflow, self.stopped = thresholds, overflow, stopped
        self.problems, self.info = list(problems), dict(info or {})
        self.names = list(CHANNELS)
        self.words = self.format = None     # the board's words as they came (bytes) and their format, if kept
        self._edges = None                  # edges(), kept once worked out

    @classmethod
    def from_words(cls, data, fmt, rate, samples=None, **more):
        """From the board's words (bytes, low byte first); fmt is "rle" or "raw".  samples: how many of the
        samples in them count (the raw format fills its last word)."""
        cap = cls(rate, (), (), **more)
        cap.words, cap.format, cap._runs = data, fmt, None
        cap.samples = W.samples_held(data, fmt) if samples is None else samples
        return cap

    def _get_runs(self):
        if self._runs is None:              # worked out when first asked for: a long raw capture takes a while
            if self.words is not None:
                self._runs = W.runs_of(self.words, self.format, self.samples)
            else:                           # read from a file, which holds the edges
                self._runs = _runs_of_edges(self._edges, self.samples)
        return self._runs

    @property
    def values(self):
        return self._get_runs()[0]

    @property
    def lengths(self):
        return self._get_runs()[1]

    @property
    def ok(self):
        return not self.problems

    def set_names(self, names):
        """Gives channels names: text as parse_names() takes it, or {channel number: name}.  Two channels may not
        have one name."""
        given = parse_names(names) if isinstance(names, str) else dict(names)
        new = list(self.names)
        for k, name in given.items():
            new[k] = name
        seen = {}
        for k, name in enumerate(new):
            if name != CHANNELS[k] and name.lower() in seen:
                raise ValueError("%s and %s would both be named %s." % (CHANNELS[seen[name.lower()]], CHANNELS[k], name))
            seen[name.lower()] = k
        self.names = new

    def channel(self, name):
        """The number of a channel (0 for A0 ... 15 for B7), from its id or from the name the capture gives it."""
        key = str(name).strip()
        if key.upper() in CHANNELS:
            return CHANNELS.index(key.upper())
        found = [k for k, n in enumerate(self.names) if n.lower() == key.lower()]
        if len(found) == 1:
            return found[0]
        named = ["%s (%s)" % (n, c) for n, c in zip(self.names, CHANNELS) if n != c]
        raise ValueError("%s is not a channel: they are A0 ... A7 and B0 ... B7%s."
                         % (key, ", or by name " + ", ".join(named) if named else ""))

    def label(self, k):
        """A channel for reading: "A0", or "A0 (SDA)" when it has a name."""
        return CHANNELS[k] if self.names[k] == CHANNELS[k] else "%s (%s)" % (CHANNELS[k], self.names[k])

    @property
    def seconds(self):
        return self.samples / self.rate

    def starts(self):
        """The sample number at which each run begins."""
        return array("Q", accumulate(self.lengths, initial=0))[:-1] if self.lengths else array("Q")

    def edges(self):
        """For each of the 16 channels: (its level at sample 0, the sample numbers at which it changes), the
        numbers in an array.  Worked out once and kept: 4 bytes an edge (8 in a capture of more than 2^32
        samples)."""
        if self._edges is None:
            kind = "I" if self.samples <= LB16_SAMPLES else "Q"
            out = [array(kind) for _ in CHANNELS]
            values, lengths = self.values, self.lengths
            if not values:
                self._edges = [(0, e) for e in out]
                return self._edges
            first = prev = values[0]
            at = 0
            for v, n in zip(values, lengths):
                d = v ^ prev
                while d:
                    low = d & -d
                    out[low.bit_length() - 1].append(at)
                    d ^= low
                prev = v
                at += n
            self._edges = [(first >> ch & 1, e) for ch, e in enumerate(out)]
        return self._edges

    def summary(self):
        """What each channel did, as a list of dicts: channel, changes, first (seconds, or None), starts (0/1),
        and name if the channel has one."""
        out = []
        for ch, (initial, e) in enumerate(self.edges()):
            out.append({"channel": CHANNELS[ch], "changes": len(e), "first": e[0] / self.rate if e else None,
                        "starts": initial})
            if self.names[ch] != CHANNELS[ch]:
                out[-1]["name"] = self.names[ch]
        return out

    def measure(self, channels=None, start=None, end=None, listed=0):
        """Times read off the capture, as a dict.  A time is a whole number of samples, so it is right to within
        one sample (sample_seconds) at each of its ends.

        channels    ids ("A0", "B3") or the names the capture gives them ("SCL"); not given: the channels that
                    change
        start, end  seconds from the capture's start, to look at a part of it; not given: all of it
        listed      how many of these channels' changes to list, in the order they came

        The dict: sample_rate, sample_seconds, start, end, channels, changes and changes_total.  For each
        channel: channel, name (the channel's id if it has no name), starts (its level at the start: 0 or 1),
        changes, rising, falling, period (from one
        rising edge to the next), high and low (how long it stayed there between two changes), each as min,
        median and max in seconds or None if there was none, frequency (1 / the median period),
        average_frequency (the rising edges from the first to the last, over the time between them: precise for a
        steady clock, where frequency is held to whole samples, but lowered by any gap in a bus that sends in
        bursts) and duty (the part of the time it was high, from its first rising edge to its last).  Each of changes: time (seconds),
        sample, channel, level (what it changed to)."""
        first = 0 if start is None else max(0, round(start * self.rate))
        last = self.samples if end is None else min(self.samples, round(end * self.rate))
        if last < first:
            raise ValueError("The end of the part to measure is before its start.")
        edges = self.edges()
        if channels is None:
            picked = [ch for ch, (_, e) in enumerate(edges) if bisect_left(e, last) > bisect_left(e, first)]
        else:
            picked = [self.channel(name) for name in channels]

        def spread(lengths):
            if not lengths:
                return None
            return {"min": min(lengths) / self.rate, "median": statistics.median(lengths) / self.rate,
                    "max": max(lengths) / self.rate}

        out, lists = [], []
        for ch in picked:
            initial, e = edges[ch]
            i0, i1 = bisect_left(e, first), bisect_left(e, last)
            w, level = e[i0:i1], initial ^ (i0 & 1)         # the changes in the part, and the level before them
            rising = w[level::2]
            gaps = [b - a for a, b in zip(w, w[1:])]        # how long each level lasted, from the first change on
            highs, lows = gaps[level::2], gaps[1 - level::2]
            periods = [b - a for a, b in zip(rising, rising[1:])]
            period = spread(periods)
            out.append({"channel": CHANNELS[ch], "name": self.names[ch], "starts": level, "changes": len(w),
                        "rising": len(rising),
                        "falling": len(w) - len(rising), "period": period,
                        "frequency": self.rate / statistics.median(periods) if periods else None,
                        "average_frequency": len(periods) * self.rate / (rising[-1] - rising[0]) if periods else None,
                        "high": spread(highs), "low": spread(lows),
                        "duty": sum(highs[:len(rising) - 1]) / (rising[-1] - rising[0]) if period else None})
            lists.append([(s, ch, level ^ 1 ^ (k & 1)) for k, s in enumerate(islice(w, listed))])
        return {"sample_rate": self.rate, "sample_seconds": 1 / self.rate, "start": first / self.rate,
                "end": last / self.rate, "channels": out, "changes_total": sum(c["changes"] for c in out),
                "changes": [{"time": s / self.rate, "sample": s, "channel": CHANNELS[ch], "level": v}
                            for s, ch, v in islice(heapq.merge(*lists), listed)]}

    # ---- files ----

    def save(self, path, names=None):
        """Writes the capture; the file's ending picks the format.  names: 16 channel names, if not A0 ... B7."""
        kind = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        write = {"lb16": self._lb16, "sr": self._sr, "vcd": self._vcd, "csv": self._csv}.get(kind)
        if write is None:
            raise ValueError("%s: a capture is saved as .lb16, .sr, .vcd or .csv" % path)
        write(path, list(names or self.names))

    def _lb16(self, path, names):
        edges = self.edges()
        for _, e in edges:
            if e and e[-1] >= LB16_SAMPLES:
                raise TooLong("%s: an .lb16 file holds 2^32 samples at most (%s at this rate), and this capture "
                              "has %d: save it as .sr, .vcd or .csv" % (path, _duration(LB16_SAMPLES / self.rate),
                                                                         self.samples))
        head = {"sampleRate": self.rate, "numSamples": self.samples, "trigger": self.trigger or 0,
                "source": self.info.get("source", "Logic Boole 16"), "info": self._file_info(), "gaps": [],
                "channels": [{"initial": initial, "edges": len(e)} for initial, e in edges],
                "names": [n if n != c else "" for n, c in zip(names, CHANNELS)], "analyzers": []}
        text = json.dumps(head).encode()
        text += b" " * (-(12 + len(text)) % 4)
        with open(path, "wb") as f:
            f.write(MAGIC + struct.pack("<I", len(text)) + text)
            for _, e in edges:
                out = e if e.typecode == "I" and W.LITTLE else array("I", e)     # (the kept edges stay as they are)
                if not W.LITTLE:
                    out.byteswap()
                out.tofile(f)

    def _file_info(self):
        info = {"thresholds": self.thresholds, "overflow": self.overflow, "stopped": self.stopped,
                "problems": self.problems, "trigger_sample": self.trigger}
        info.update(self.info)
        return info

    def _sr(self, path, names):
        rate = self.rate
        text = ("%d MHz" % (rate / 1e6) if rate % 1e6 == 0 else "%d kHz" % (rate / 1e3) if rate % 1e3 == 0
                else "%d Hz" % round(rate))
        meta = ["[global]", "sigrok version=0.5.2", "", "[device 1]", "capturefile=logic-1", "total probes=16",
                "samplerate=" + text, "total analog=0"]
        meta += ["probe%d=%s" % (i + 1, n) for i, n in enumerate(names)] + ["unitsize=2", ""]
        if self.format == "raw":            # the words are the samples already
            raw = memoryview(self.words)[:2 * self.samples]
            pieces = (raw[i:i + (4 << 20)] for i in range(0, len(raw), 4 << 20))
        else:
            pieces = W.sample_bytes(self.values, self.lengths)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            z.writestr("version", "2")
            z.writestr("metadata", "\n".join(meta))
            for i, piece in enumerate(pieces):
                z.writestr("logic-1-%d" % (i + 1), piece)
            # What a sigrok session has no place for (the trigger sample, the thresholds, the checks ...), as an
            # .lb16 file's info, in a part of the file that sigrok's programs do not look at: sigrok-cli 0.7.2
            # reads, exports and decodes a session with it exactly as one without it
            z.writestr(SR_EXTRA, json.dumps(self._file_info()))

    def _vcd(self, path, names):
        # Times are counted in the largest unit a VCD file may name (1, 10 or 100 of a unit) that the sample
        # period is a whole number of: 1 ns at 200 MS/s, 10 ns at 100 MS/s and at 50 MS/s, 1 us at 1 MS/s.
        # The standard design's periods are whole 5 ns.  The fast design's are not whole in any unit (1.333 ns at
        # 750 MS/s): those are counted in femtoseconds, which is right to 3 parts in 10 million.
        step, exponent = round(1e15 / self.rate), 0             # femtoseconds
        while step % 10 == 0 and exponent < 15:
            step, exponent = step // 10, exponent + 1
        scale = "%d %s" % (10 ** (exponent % 3), ("fs", "ps", "ns", "us", "ms", "s")[exponent // 3])
        ids = [chr(33 + i) for i in range(16)]
        with open(path, "w") as f:
            f.write("$version Logic Boole 16 $end\n$timescale %s $end\n$scope module logic $end\n" % scale)
            for i, n in zip(ids, names):
                f.write("$var wire 1 %s %s $end\n" % (i, n.replace(" ", "_")))
            f.write("$upscope $end\n$enddefinitions $end\n")
            prev, at = None, 0
            for v, n in zip(self.values, self.lengths):
                if prev is None:
                    f.write("#0\n$dumpvars\n%s$end\n" % "".join("%d%s\n" % (v >> ch & 1, ids[ch]) for ch in range(16)))
                elif v != prev:
                    d = v ^ prev
                    f.write("#%d\n%s" % (at * step, "".join("%d%s\n" % (v >> ch & 1, ids[ch])
                                                             for ch in range(16) if d >> ch & 1)))
                prev = v
                at += n
            f.write("#%d\n" % (at * step))

    def _csv(self, path, names):
        # The times have a figure more after the point than a sample period needs, however far into the capture
        # (8 at 10 MS/s, 10 at 200 MS/s): each time reads back as its own sample
        places = max(0, math.ceil(math.log10(self.rate)) + 1)
        with open(path, "w") as f:
            f.write("time_s,%s\n" % ",".join(names))
            prev, at = None, 0
            for v, n in zip(self.values, self.lengths):
                if v != prev:
                    f.write("%.*f,%s\n" % (places, at / self.rate, ",".join(str(v >> ch & 1) for ch in range(16))))
                prev = v
                at += n

    @classmethod
    def load(cls, path):
        """Reads an .lb16 file (this software's, or the app's), or a sigrok session (.sr) such as PulseView and
        this software save.  A file that is neither, or is cut short, raises NotACapture (a ValueError) that says
        so.  An .lb16 file's edges are read as they are, with nothing made of each one: the runs (values,
        lengths) are made from them only if they are asked for."""
        if os.path.isdir(path):
            raise NotACapture("%s is a folder, not a capture file." % path)
        with open(path, "rb") as f:
            first = f.read(12)
            if first[:4] == b"PK\x03\x04":  # a zip file: a sigrok session
                f.close()
                return cls._load_sr(path)
            if first[:8] != MAGIC:
                raise NotACapture("%s is not a capture file: this software reads .lb16 files (its own and the "
                                  "app's) and sigrok sessions (.sr)." % path)
            try:
                return cls._load_lb16(path, f, struct.unpack_from("<I", first, 8)[0])
            except (struct.error, EOFError, KeyError, IndexError, TypeError, ValueError, OverflowError) as e:
                if isinstance(e, NotACapture):
                    raise
                raise NotACapture("%s is not a capture file: it begins as an .lb16 file, and its %s."
                                  % (path, "data ends early" if isinstance(e, (EOFError, struct.error))
                                     else "header is not one")) from None

    @classmethod
    def _load_lb16(cls, path, f, n):
        size = os.fstat(f.fileno()).st_size
        if 12 + n > size:
            raise EOFError
        head = json.loads(f.read(n).decode("utf-8"))
        rate, samples, channels = head["sampleRate"], head["numSamples"], head["channels"]
        if (not isinstance(rate, (int, float)) or isinstance(rate, bool) or not rate > 0
                or not isinstance(samples, int) or samples < 0 or not isinstance(channels, list)):
            raise ValueError("the header")
        edges = []
        for c in channels[:16]:             # a channel beyond the 16th is left out
            initial, count = c["initial"], c["edges"]
            if initial not in (0, 1) or not isinstance(count, int) or count < 0:
                raise ValueError("a channel")
            if f.tell() + 4 * count > size:
                raise EOFError
            e = array("I")
            e.fromfile(f, count)            # straight into the array: nothing is made of each edge
            if not W.LITTLE:
                e.byteswap()
            if e and e[-1] >= samples:
                raise NotACapture("%s is not a capture file: a channel changes after its last sample." % path)
            edges.append((int(initial), e))
        while len(edges) < 16:
            edges.append((0, array("I")))
        info = head.get("info")
        info = dict(info) if isinstance(info, dict) else {}
        info.update(_older_info(info, head))
        info.pop("meta", None)              # (what it held is now at the top of info)
        cap = cls(rate, (), (), info=info, **_settings(info, samples))
        cap._runs, cap._edges, cap.samples = None, edges, samples
        given = list(head.get("names") or ())
        cap.names = [given[i] if i < len(given) and isinstance(given[i], str) and given[i] else c
                     for i, c in enumerate(CHANNELS)]
        return cap

    @classmethod
    def _load_sr(cls, path):
        """A sigrok session: a zip file with "metadata" (the rate, the probes' names, the bytes in a sample) and
        the samples in "logic-1-1", "logic-1-2" ... (or one "logic-1").  Its first 16 probes become A0 ... B7;
        a probe beyond them is left out."""
        try:
            with zipfile.ZipFile(path) as z:
                meta = _sr_metadata(z.read("metadata").decode("utf-8", "replace"))
                name = meta.get("capturefile", "logic-1")
                parts = sorted((n for n in z.namelist() if re.fullmatch(re.escape(name) + r"(-\d+)?", n)),
                               key=lambda n: int(n[len(name) + 1:] or 0))
                unit = int(meta.get("unitsize", 1))
                if not parts or not 1 <= unit <= 8:
                    raise NotACapture("%s: a sigrok session with no logic samples in it" % path)
                values, lengths = _runs_of_samples((z.read(n) for n in parts), unit)
                extra = None                # what this software keeps beside the samples (_sr())
                if SR_EXTRA in z.namelist():
                    try:
                        extra = json.loads(z.read(SR_EXTRA).decode("utf-8"))
                    except ValueError:
                        pass
        except (zipfile.BadZipFile, KeyError, ValueError, EOFError, zlib.error) as e:
            if isinstance(e, NotACapture):
                raise
            raise NotACapture("%s is not a capture file: it is a zip file, and not a sigrok session that can be "
                              "read." % path) from None
        rate = _sr_rate(meta.get("samplerate", ""))
        if rate is None:
            raise NotACapture("%s: the sigrok session gives no sample rate" % path)
        if isinstance(extra, dict):
            info = dict(extra)
            cap = cls(rate, values, lengths, info=info, **_settings(info, sum(lengths)))
        else:
            cap = cls(rate, values, lengths, info={"source": "sigrok session"})
        for i in range(16):
            given = meta.get("probe%d" % (i + 1))
            if given:
                cap.names[i] = given
        return cap


class NotACapture(ValueError):
    """A file that Capture.load() cannot read as a capture."""


def writable(path):
    """Refuses a file that a capture cannot be saved in (ValueError, saying why): for a check before the capture
    is taken, not after.  Its ending must be one that save() writes, its folder must be there, and this user must
    be able to write in it."""
    kind = path.rsplit(".", 1)[-1].lower() if "." in os.path.basename(path) else ""
    if kind not in ("lb16", "sr", "vcd", "csv"):
        raise ValueError("%s: a capture is saved as .lb16, .sr, .vcd or .csv" % path)
    folder = os.path.dirname(os.path.abspath(path))
    if os.path.isdir(path):
        raise ValueError("%s is a folder: give the name of a file in it." % path)
    if not os.path.isdir(folder):
        raise ValueError("%s cannot be saved: there is no folder %s." % (path, folder))
    if not os.access(path if os.path.exists(path) else folder, os.W_OK):
        raise ValueError("%s cannot be saved: this user may not write %s." % (path, path if os.path.exists(path)
                                                                                else "in " + folder))


# What the app wrote in an .lb16 file's "info" before it wrote this software's layout: the capture's answer from
# the app's server, under "meta", with these names
META_KEYS = {"thresholds": "thresholds", "overflow": "overflow", "stopped": "stopped", "problems": "problems",
             "trigger_sample": "trigger_sample", "trigger": "trigger", "clock": "clock", "glitch_ns": "glitch_ns",
             "pi": "pi", "usb": "usb", "words": "words", "asked": "samples_asked"}


def _settings(info, samples):
    """The capture's settings and checks, taken out of a file's info, as Capture() takes them.  A file may come
    from another program: what is not of the right kind is left out."""
    def number(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    thresholds, at = info.pop("thresholds", None), info.pop("trigger_sample", None)
    stopped, problems = info.pop("stopped", None), info.pop("problems", None)
    return {"thresholds": thresholds if isinstance(thresholds, (list, tuple)) and len(thresholds) == 2
            and all(map(number, thresholds)) else None,
            "overflow": bool(info.pop("overflow", False)),
            "stopped": stopped if isinstance(stopped, str) else None,
            "problems": [str(p) for p in problems] if isinstance(problems, list) else (),
            "trigger": at if isinstance(at, int) and not isinstance(at, bool) and 0 <= at < samples else None}


def _older_info(info, head):
    """What a file in the app's earlier layout says, in this software's: from info["meta"] what this software
    keeps at the top of info, and the trigger sample from the header's "trigger" (where the app put 0 for a
    capture with no trigger sample).  Nothing that the top of info has already is changed."""
    out = {}
    meta = info.get("meta")
    if isinstance(meta, dict):
        for old, new in META_KEYS.items():
            if old in meta and new not in info:
                out[new] = meta[old]
    if "trigger_sample" not in info and "trigger_sample" not in out:
        at = head.get("trigger")
        out["trigger_sample"] = at if isinstance(at, int) and not isinstance(at, bool) and at > 0 else None
    if "source" not in info and isinstance(head.get("source"), str):
        out["source"] = head["source"]
    return out


def _runs_of_edges(edges, samples):
    """Runs (values, lengths) from each channel's (level at sample 0, the samples at which it changes): the
    channels' edges are merged in order, each as one number (its sample times 16, plus the channel), one at a
    time."""
    value = 0
    for ch, (initial, _) in enumerate(edges):
        value |= initial << ch
    busy = [(ch, e) for ch, (_, e) in enumerate(edges) if len(e)]
    values, lengths, last = array("H"), array("Q"), 0
    if len(busy) == 1:                      # one channel changes: its edges are in order already
        ch, e = busy[0]
        bit = 1 << ch
        for s in e:
            if s != last:
                values.append(value)
                lengths.append(s - last)
                last = s
            value ^= bit
    elif busy:
        for code in heapq.merge(*(map((16).__mul__, e) if ch == 0 else map((ch).__add__, map((16).__mul__, e))
                                  for ch, e in busy)):
            s = code >> 4
            if s != last:
                values.append(value)
                lengths.append(s - last)
                last = s
            value ^= 1 << (code & 15)
    if samples > last:
        values.append(value)
        lengths.append(samples - last)
    return values, lengths


def _duration(seconds):
    for unit, scale in (("min", 60.0), ("s", 1.0), ("ms", 1e-3)):
        if seconds >= scale:
            return "%.3g %s" % (seconds / scale, unit)
    return "%.3g us" % (seconds * 1e6)


def _sr_metadata(text):
    """The [device 1] section of a sigrok session's metadata, as a dict."""
    out, section = {}, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif "=" in line and section == "[device 1]":
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _sr_rate(text):
    """"200 MHz", "12.5 kHz", "100 Hz" or "1 GHz" as samples a second, or None."""
    m = re.fullmatch(r"\s*([0-9.]+)\s*([kMG]?)Hz\s*", text)
    if not m:
        return None
    return float(m.group(1)) * {"": 1, "k": 1e3, "M": 1e6, "G": 1e9}[m.group(2)]


def _runs_of_samples(pieces, unit):
    """Runs (values, lengths) of samples of `unit` bytes each, low byte first, arriving in pieces.  Only the low 16
    bits count.  Each piece is compared with itself moved on by one sample, and the bytes of that difference that
    are not 0 are found by a regular expression, which runs in C: a long quiet stretch costs little."""
    nonzero = re.compile(b"[^\x00]")
    values, lengths = array("H"), array("Q")
    last, value, begun, at, carry = bytes(unit), None, 0, 0, b""
    for piece in pieces:
        piece = carry + piece
        cut = len(piece) - len(piece) % unit
        piece, carry = piece[:cut], piece[cut:]
        if not piece:
            continue
        before = last + piece[:-unit]
        diff = (int.from_bytes(piece, "little") ^ int.from_bytes(before, "little")).to_bytes(len(piece), "little")
        changes, pos = [0] if value is None else [], 0      # the first sample begins the first run
        while True:
            m = nonzero.search(diff, pos)
            if m is None:
                break
            c = m.start() - m.start() % unit
            if c:
                changes.append(c)
            elif value is not None:
                changes.append(0)
            pos = c + unit
        for c in changes:
            v = int.from_bytes(piece[c:c + min(unit, 2)], "little")
            if v == value:                  # only a probe beyond the 16th changed
                continue
            here = at + c // unit
            if value is not None:
                values.append(value)
                lengths.append(here - begun)
            value, begun = v, here
        last = piece[-unit:]
        at += len(piece) // unit
    if value is not None:
        values.append(value)
        lengths.append(at - begun)
    return values, lengths
