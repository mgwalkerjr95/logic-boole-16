# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""The fast design: fewer inputs, more samples a second.

The board's FPGA is loaded with one of two designs.  The one it is used with takes all 16 inputs at up to 200 MS/s
and has the trigger, the glitch filter, the capture clock and the Pi's lines.  The fast design (gateware/fast_top.v)
takes samples and sends them, nothing else, and takes them from the inputs' pads 750 million times a second:

    4 inputs     A0-A3, A4-A7, B0-B3 or B4-B7      750 MS/s    a sample every 1.33 ns
    8 inputs     pod A or pod B                    375 MS/s    every 2.67 ns
    16 inputs    all                               187.5 MS/s  every 5.33 ns

Per cycle of its 187.5 MHz clock the design keeps 16 bits, a frame: four samples of 4 inputs, two of 8 or one of 16,
the earliest in the lowest bits.  The stream is the standard design's with a frame where that has a sample (words.py:
raw, two frames a word; run-length, a frame and how many more like it followed), so the same code reads the words,
and what is here turns frames into samples and back.

A capture made with it is an ordinary Capture: the inputs that were not taken read low in it.
"""
from array import array

ID = b"\x4C\x42\x46"                        # the fast design's registers 00 ... 02 (the standard design: 4C 42 16)
ALL = ["A%d" % i for i in range(8)] + ["B%d" % i for i in range(8)]


class Mode:
    """One choice of inputs.  register: what goes into the design's register 08.  per: samples in a frame.
    bits: inputs in a sample.  shift: where the first of them is among the 16 (A0 is 0, B7 is 15)."""

    def __init__(self, name, register, per, shift):
        self.name, self.register, self.per, self.shift = name, register, per, shift
        self.bits = 16 // per
        self.mask = (1 << self.bits) - 1
        self.channels = ALL[shift:shift + self.bits]
        self.same = sum(1 << (self.bits * t) for t in range(per))      # times a sample: a frame of that sample

    def rate(self, fastest):
        """Samples a second.  fastest: the design's rate with 4 inputs (its registers 2C and 2D)."""
        return fastest * self.per / 4

    def __repr__(self):
        return "Mode(%s)" % self.name


MODES = {m.name: m for m in (
    Mode("A0-A3", 2 | 0 << 2, 4, 0), Mode("A4-A7", 2 | 1 << 2, 4, 4), Mode("B0-B3", 2 | 2 << 2, 4, 8),
    Mode("B4-B7", 2 | 3 << 2, 4, 12), Mode("A", 1 | 0 << 2, 2, 0), Mode("B", 1 | 1 << 2, 2, 8),
    Mode("all", 0, 1, 0))}
BY_REGISTER = {m.register: m for m in MODES.values()}
FASTEST = 750e6                             # samples a second with 4 inputs, as the design is built
OTHER_NAMES = {"A0-A7": "A", "B0-B7": "B", "PODA": "A", "PODB": "B", "ALL": "all", "16": "all", "A0-B7": "all"}


def mode_of(text):
    """The Mode for "A0-A3", "a4..a7", "B", "pod b", "all" ...; a Mode is passed through."""
    if isinstance(text, Mode):
        return text
    name = str(text).strip().upper().replace(" ", "").replace("...", "-").replace("..", "-")
    name = OTHER_NAMES.get(name, name)
    if name not in MODES:
        raise ValueError("The fast design takes four inputs (A0-A3, A4-A7, B0-B3 or B4-B7), a pod (A or B) or all "
                         "16 (all): not \"%s\"." % text)
    return MODES[name]


def sample_runs(frames, counts, mode, samples=None, limit=None):
    """Runs of frames (frames[i] came counts[i] times in a row) as runs of samples: (values, lengths), a value
    being all 16 inputs with the mode's inputs in their own places and the others low.  samples cuts the result to
    that many.  limit: stop once there are about that many runs (10 bytes each), so that a busy signal cannot
    fill the computer's memory; the result is then whole up to where it stops, and len(values) >= limit says so."""
    per, bits, shift, mask, same = mode.per, mode.bits, mode.shift, mode.mask, mode.same
    values, lengths = array("H"), array("Q")
    last = None
    for f, n in zip(frames, counts):
        if limit is not None and len(values) >= limit:
            break
        first = f & mask
        if f == first * same:               # every sample of the frame alike: one run, however many frames
            v = first << shift
            if v == last:
                lengths[-1] += n * per
            else:
                values.append(v)
                lengths.append(n * per)
                last = v
            continue
        parts = [(f >> (bits * t) & mask) << shift for t in range(per)]
        if limit is not None:
            n = min(n, (limit - len(values)) // per + 1)
        for _ in range(n):
            for v in parts:
                if v == last:
                    lengths[-1] += 1
                else:
                    values.append(v)
                    lengths.append(1)
                    last = v
    over = 0 if samples is None else sum(lengths) - samples
    while over > 0:
        if lengths[-1] > over:
            lengths[-1] -= over
            break
        over -= lengths.pop()
        values.pop()
    return values, lengths


def frame_runs(runs, mode):
    """The other way, for the stand-in board and the tests: runs of samples, as (value of all 16 inputs, how
    many), to runs of frames as (frame, how many).  Samples that do not fill a last frame are left out, as the
    design takes whole frames."""
    per, bits, shift, mask, same = mode.per, mode.bits, mode.shift, mode.mask, mode.same
    part, have = 0, 0                       # the frame being filled, and how many samples it has
    for value, n in runs:
        s = value >> shift & mask
        while n > 0:
            if have == 0 and n >= per:      # whole frames of this sample
                yield s * same, n // per
                n %= per
                continue
            part |= s << (bits * have)
            have += 1
            n -= 1
            if have == per:
                yield part, 1
                part, have = 0, 0
