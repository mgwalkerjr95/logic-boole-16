# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""Checks the whole path from the FPGA to the computer, with nothing connected to the probes.

selftest() is for the standard design and selftest_fast(), at the end of this file, for the fast one.

The FPGA sends its test pattern in place of the probes (a counter of the samples), in both formats and from
1 MS/s to 200 MS/s, and every word that arrives is compared with it.  Some captures end at their sample count
and some are ended by the computer.  Three are meant to overflow: they make 100 million words a second or more,
which is all the board passes on and more than USB carries, and each must then end early and still be whole up to
its last sample.  Two short ones at 200 MS/s make words that fast too, but few enough for the board's own buffers:
those must arrive whole.

Five captures have sizes at which the end of the data arrives in each of the ways USB has for it: the last
packet full or not, the FX3's last buffer full or not, and the computer's read exactly full.

The last captures wait for a trigger on the test pattern.  Which sample of the pattern must be the trigger sample
is known, so the capture must have that sample where it says its trigger is, the asked number of samples from
there on, and from before it as many as were asked for or as the board had.  The last one's trigger never comes:
the wait is ended, and what the board saw last must arrive.

On a USB 2.0 link (480 Mbit/s) more of the captures overflow: all that make more words a second than that link
carries.  That is no fault of the board.  What arrives of them is compared like the rest, and a capture with a
trigger must still have the right trigger sample.
"""
import time

from . import trigger as T, words as W

# format, rate, the pattern's shift, seconds, and whether the computer ends the capture (no sample count).  A rate
# of 200 MS/s divided by an odd number takes its samples at the two edges of the FPGA's clock in turn.
CASES = (("raw", 1e6, 0, 1.0, False), ("raw", 25e6, 0, 2.0, False), ("raw", 100e6, 0, 1.0, False),
         ("rle", 1e6, 4, 1.0, False), ("rle", 100e6, 8, 3.0, False), ("rle", 200e6, 8, 3.0, False),
         ("rle", 200e6, 15, 3.0, False), ("rle", 50e6, 0, 1.0, False), ("rle", 200e6, 2, 1.0, False),
         ("rle", 40e6, 0, 0.5, False), ("raw", 200e6 / 3, 0, 1.0, False), ("rle", 200e6 / 3, 1, 1.0, False),
         ("raw", 100e6 / 3, 3, 1.0, False),
         ("rle", 100e6, 0, 0.2, False), ("rle", 200e6, 0, 0.2, False), ("raw", 200e6, 0, 0.2, False),
         ("raw", 100e6, 0, 0.7, True), ("rle", 200e6, 8, 0.7, True), ("rle", 20e6, 0, 0.4, True))

# format and a number of samples at 200 MS/s, the pattern not shifted: 30,000 words and 15,000 words, which the
# FPGA's FIFO and the FX3's buffers hold even if the computer reads nothing
WHOLE = (("raw", 60000), ("rle", 15000))

# format and a number of samples at 10 MS/s (the pattern not shifted, so a run-length word holds one sample):
# 51200 bytes are three of the FX3's buffers and two full packets; 65536 bytes are four buffers and nothing more;
# 1048576 bytes fill one of the computer's reads exactly; 51204 bytes end with a packet of 4 bytes
ENDS = (("raw", 25600), ("rle", 12800), ("raw", 32768), ("raw", 524288), ("raw", 25602))

# words a second above which a capture overflows on a USB 2.0 link: one carried 11.1 million for 20 seconds, and
# overflowed at 12.5 million
USB2_WORDS = 11.5e6


def _is(value):
    return {"mask": 0xFFFF, "value": value}


# what it shows, format, rate, the pattern's shift, samples from the trigger on, samples asked for from before
# it, the trigger's stages (logicboole/trigger.py), and which sample of the pattern must be the trigger sample
TRIGGERS = (
    ("a value", "rle", 100e6, 8, 1000000, 100000, [_is(0x0400)], 0x0400 << 8),
    ("a value", "raw", 50e6, 0, 200001, 5001, [_is(0x8000)], 0x8000),
    ("the capture's first sample", "rle", 100e6, 4, 100000, 1000, [_is(0)], 0),
    ("levels that have lasted 700 samples", "rle", 200e6, 6, 500000, 2000,
     [{"kind": 2, "mask": 0xFFF0, "value": 0x0230, "min": 700}], (0x0230 << 6) + 699),
    ("a pulse of 65536 samples has ended", "rle", 100e6, 8, 100000, 70000,
     [{"kind": 1, "mask": 0x0F00, "value": 0x0300, "min": 65536, "max": 65536}], 0x0400 << 8),
    ("four stages in turn", "raw", 200e6, 3, 400000, 10000,
     [_is(0x0100), {"fall": 0x0001}, {"kind": 2, "mask": 0x0003, "value": 0x0003, "min": 5}, {"rise": 0x0010}],
     0x0110 << 3),
    ("all the board keeps from before", "rle", 200e6, 3, 2000000, 1 << 30, [_is(0x7000)], 0x7000 << 3),
    ("a trigger that never comes", "rle", 100e6, 8, 1000, 300000,       # the pattern's pulses are 256 samples
     [{"kind": 1, "mask": 0x0001, "value": 0x0001, "min": 1, "max": 255}], None))


def words_a_second(fmt, rate, shift):
    return rate / 2 if fmt == "raw" else rate / (1 << shift)


def selftest(board, log=print, scale=1.0):
    """Runs the captures on a connected board and returns True if every word of every one was right.
    scale shortens them (the stand-in board is slow at making samples)."""
    passed = True
    usb2 = board.status()["speed"] == 2
    slow = 0                                # captures that overflowed because the link is USB 2.0
    cases = (CASES + tuple((fmt, 200e6, 0, n, "whole") for fmt, n in WHOLE)
             + tuple((fmt, 10e6, 0, n, "count") for fmt, n in ENDS))
    for fmt, rate, shift, seconds, how in cases:
        if how in ("whole", "count"):       # a number of samples, not seconds
            cap = board.capture(rate=rate, samples=seconds, fmt=fmt, test=shift)
        elif how:
            end = time.monotonic() + seconds * scale
            cap = board.capture(rate=rate, samples=0, fmt=fmt, test=shift, cancel=lambda: time.monotonic() >= end)
        else:
            cap = board.capture(rate=rate, seconds=seconds * scale, fmt=fmt, test=shift)
        problems = list(cap.problems)
        if how == "whole" and cap.overflow:
            problems.append("it overflowed, and is small enough for the board's own buffers")
        passed = passed and not problems
        meant = how != "whole" and words_a_second(fmt, rate, shift) >= 100e6
        link = usb2 and how != "whole" and not meant and words_a_second(fmt, rate, shift) > USB2_WORDS
        slow += bool(cap.overflow and link)
        note = ("; OVERFLOW after %.4f s, as meant" % cap.seconds if cap.overflow and meant
                else "; OVERFLOW after %.4f s: more than USB 2.0 carries" % cap.seconds if cap.overflow and link
                else "; OVERFLOW after %.4f s" % cap.seconds if cap.overflow
                else "; ended by the computer" if how is True else "")
        log("%-4s %11.10g S/s  pattern shifted by %2d  %11d samples  %10d words  %6.1f MB/s  %s"
            % (fmt, cap.rate, shift, cap.samples, cap.info["words"],
               cap.info["words"] * 4 / max(cap.info["read_seconds"], 1e-9) / 1e6,
               "WRONG: " + "; ".join(problems) if problems else "every word right" + note))
    r = board.registers()
    if not r["stages"]:
        log("The FPGA's design has no trigger: the captures with one were left out.")
        return passed
    for name, fmt, rate, shift, samples, before, stages, want in TRIGGERS:
        cap = board.capture(rate=rate, samples=samples, fmt=fmt, test=shift, trigger=stages, samples_before=before,
                            timeout=None if want is not None else 0.3 if scale >= 1 else 0.05)
        problems = list(cap.problems)
        at = cap.trigger
        link = usb2 and words_a_second(fmt, rate, shift) > USB2_WORDS
        if cap.overflow and not link:
            problems.append("overflow after the trigger")
        slow += bool(cap.overflow and link)
        if want is None:
            if at is not None or cap.stopped != "timeout":
                problems.append("it triggered, and should not have")
            elif cap.samples != before:
                problems.append("%d samples from the end of the wait, not %d" % (cap.samples, before))
            note = "the wait was ended, and the last %d samples arrived" % cap.samples
        elif at is None:
            problems.append("no trigger")
        else:
            around = 65536 << shift         # the pattern comes round
            first = W.pattern_origin(cap.words, cap.format, shift)
            if (first + at - want) % around:
                problems.append("the trigger sample is sample %d of the pattern, not %d"
                                % ((first + at) % around, want))
            if cap.samples - at != samples and not cap.overflow:
                problems.append("%d samples from the trigger on, not %d" % (cap.samples - at, samples))
            # before the trigger: what was asked for (a raw word may add a sample), unless the capture had not
            # run that long or the FPGA's FIFO, which keeps the words while it waits, holds less
            words = T.ring_words(before, fmt, (1 << r["fifo_bits"]) - 8)
            least = min(before, want, (words - 1) << shift if fmt == "rle" else 2 * (words - 1))
            if not least <= at <= before + (fmt == "raw"):
                problems.append("%d samples before the trigger, %d asked for" % (at, before))
            note = "the trigger sample is the right one, with %d samples before it" % at
            if cap.overflow and link:
                note += "; OVERFLOW after the trigger: more than USB 2.0 carries"
        passed = passed and not problems
        log("%-4s %11.10g S/s  pattern shifted by %2d  %11d samples  %10d words  trigger: %s.  %s"
            % (fmt, cap.rate, shift, cap.samples, cap.info["words"], name,
               "WRONG: " + "; ".join(problems) if problems else "every word right; " + note))
    if usb2:
        log("The link is USB 2.0 (480 Mbit/s): %d of the captures make more words a second than it carries and "
            "overflowed.  A USB 3 port and cable carry them." % slow)
    return passed


# ---- the fast design -----------------------------------------------------------------------------------------------

# Its stream is the standard design's with a frame (the samples of 5.33 ns) for a sample, so these are counted in
# frames, of which there are 187.5 million a second whichever inputs are taken.  Inputs, format, the pattern's
# shift, seconds, and whether the computer ends the capture.
FAST_CASES = (("A0-A3", "rle", 8, 2.0, False), ("B4-B7", "rle", 2, 1.0, False), ("A", "rle", 15, 2.0, False),
              ("B", "rle", 4, 1.0, False), ("all", "rle", 3, 1.0, False), ("A4-A7", "rle", 8, 0.7, True),
              ("B0-B3", "rle", 0, 0.2, False), ("A0-A3", "raw", 0, 0.5, False), ("B", "raw", 0, 0.5, False),
              ("all", "raw", 3, 0.5, False), ("A0-A3", "raw", 0, 0.5, True))
# as WHOLE and ENDS above: what the board's own buffers hold, and sizes at which the data ends in each of USB's ways
FAST_WHOLE = (("raw", 60000), ("rle", 15000))
FAST_ENDS = (("raw", 25600), ("rle", 12800), ("raw", 32768), ("raw", 524288), ("raw", 25602))
FRAMES = 187.5e6                            # a second
EDGE = 90e6                                 # words a second from which a capture is at the edge of what USB carries


def selftest_fast(board, log=print, scale=1.0):
    """The same for the fast design, which is loaded first: its test pattern (a counter of the frames) in both
    formats and with each choice of inputs, every word compared.  Returns True if every word of every capture was
    right.  The raw format makes 93.75 million words a second, about what the board and USB carry at the most; a
    raw capture that overflows is whole up to its last frame and is not counted as wrong, and how many did is
    said at the end."""
    from . import fast as F
    passed, edge = True, 0
    usb2 = board.status()["speed"] == 2
    cases = (FAST_CASES + tuple(("A0-A3", fmt, 0, n, "whole") for fmt, n in FAST_WHOLE)
             + tuple(("A", fmt, 0, n, "count") for fmt, n in FAST_ENDS))
    for inputs, fmt, shift, seconds, how in cases:
        per = F.mode_of(inputs).per
        if how in ("whole", "count"):       # a number of frames, not seconds
            cap = board.capture_fast(inputs, samples=seconds * per, fmt=fmt, test=shift)
        elif how:
            end = time.monotonic() + seconds * scale
            cap = board.capture_fast(inputs, samples=0, fmt=fmt, test=shift, cancel=lambda: time.monotonic() >= end)
        else:
            cap = board.capture_fast(inputs, seconds=seconds * scale, fmt=fmt, test=shift)
        problems = list(cap.problems)
        if how == "whole" and cap.overflow:
            problems.append("it overflowed, and is small enough for the board's own buffers")
        a_second = words_a_second(fmt, FRAMES, shift)
        if cap.overflow and how != "whole" and a_second < EDGE and not usb2:
            problems.append("it overflowed at %.3g million words a second" % (a_second / 1e6))
        passed = passed and not problems
        edge += bool(cap.overflow and EDGE <= a_second < 100e6 and how != "whole")
        note = ("; OVERFLOW after %.4f s" % cap.seconds if cap.overflow
                else "; ended by the computer" if how is True else "")
        log("%-5s %-4s %6.5g MS/s  pattern shifted by %2d  %11d frames  %10d words  %6.1f MB/s  %s"
            % (inputs, fmt, cap.rate / 1e6, shift, cap.samples // per, cap.info["words"],
               cap.info["words"] * 4 / max(cap.info["read_seconds"], 1e-9) / 1e6,
               "WRONG: " + "; ".join(problems) if problems else "every word right" + note))
    for inputs in ("A0-A3", "A4-A7", "B0-B3", "B4-B7", "A", "B", "all"):       # the inputs themselves
        cap = board.capture_fast(inputs, seconds=0.05 * scale)
        problems = list(cap.problems) + (["it overflowed"] if cap.overflow and not usb2 else [])
        passed = passed and not problems
        log("%-5s the inputs  %6.5g MS/s  %11d samples  %10d words  %s"
            % (inputs, cap.rate / 1e6, cap.samples, cap.info["words"],
               "WRONG: " + "; ".join(problems) if problems else "arrived whole"))
    if edge:
        log("%d of the captures that make 93.75 million words a second overflowed: this computer or this link "
            "did not carry them without a pause.  What arrived of them was right." % edge)
    if usb2:
        log("The link is USB 2.0 (480 Mbit/s): the fast design makes more words a second than it carries.  "
            "A USB 3 port and cable carry them.")
    return passed
