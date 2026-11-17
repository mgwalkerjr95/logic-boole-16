# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""The board's stream: 32-bit words in one of two formats, and the test pattern the FPGA can send in their place.

A sample is 16 bits: bit 0 is A0 ... bit 7 A7, bit 8 B0 ... bit 15 B7; 1 means above the pod's threshold.

    raw          bits 15:0 of a word are a sample, bits 31:16 the next one.  A capture of an odd number of samples
                 repeats the last sample in the last word's upper half
    run-length   bits 15:0 are a sample, bits 31:16 how many more sample periods the inputs stayed like that
                 (0 ... 65535).  The FPGA sends a word when the inputs change, and one every 65536 samples while
                 they do not

Everything here works on the bytes as they came from USB (low byte first), with Python's array module: no other
package is needed.  A "run" is a stretch of samples that are alike: values[i] held for lengths[i] samples.
"""
import struct
import sys
from array import array

LITTLE = sys.byteorder == "little"
PERIOD = 1 << 16                            # the test pattern's blocks: this many words, or samples


def halves(data):
    """The words' lower halves and their upper halves, as two arrays of 16-bit numbers."""
    h = array("H")
    h.frombytes(bytes(data[:len(data) - len(data) % 4]))
    if not LITTLE:
        h.byteswap()
    return h[0::2], h[1::2]


def samples_held(data, fmt):
    """How many samples the words hold (raw: counting a repeated last sample as one)."""
    if fmt == "raw":
        return len(data) // 4 * 2
    more = halves(data)[1]
    return sum(more) + len(more)


def runs_of(data, fmt, samples=None):
    """The capture as runs: (values, lengths).  samples cuts it to that many (raw: drops the repeated sample)."""
    if fmt == "rle":
        low, more = halves(data)
        lengths = array("Q", [n + 1 for n in more])
        return _cut(low, lengths, samples)
    h = array("H")
    h.frombytes(bytes(data[:len(data) - len(data) % 4]))
    if not LITTLE:
        h.byteswap()
    return runs_of_samples(h, len(h) if samples is None else min(samples, len(h)))


def _cut(values, lengths, samples):
    over = 0 if samples is None else sum(lengths) - samples
    while over > 0:                         # from the end: there is rarely anything to cut
        if lengths[-1] > over:
            lengths[-1] -= over
            break
        over -= lengths.pop()
        values.pop()
    return values, lengths


def runs_of_samples(h, n):
    """The first n of the 16-bit samples in the array h, as runs.  A long run is passed over a block at a time."""
    values, lengths = array("H"), array("Q")
    view = memoryview(h).cast("B")
    block = 4096
    i = 0
    while i < n:
        v = h[i]
        j, near = i + 1, min(n, i + 32)
        while j < near and h[j] == v:
            j += 1
        if j == near and j < n:
            same = array("H", [v]).tobytes() * block
            while j + block <= n and view[2 * j:2 * (j + block)] == same:
                j += block
            while j < n and h[j] == v:
                j += 1
        values.append(v)
        lengths.append(j - i)
        i = j
    return values, lengths


def words_of_runs(values, lengths, fmt):
    """Runs back to the words the FPGA would send for them (bytes).  For the stand-in board and the tests."""
    if fmt == "rle":
        out = array("I")
        for v, n in zip(values, lengths):
            while n > 0:
                k = min(n, 65536)
                out.append((k - 1) << 16 | v)
                n -= k
    else:
        h = array("H")
        for v, n in zip(values, lengths):
            h.extend(array("H", [v]) * n)
        if len(h) & 1:
            h.append(h[-1])
        out = h
    if not LITTLE:
        out.byteswap()
    return out.tobytes()


def drop_samples(data, fmt, n):
    """The words without their first n samples, and how many were dropped: (bytes, number).  A raw word holds two
    samples, so of an odd n one sample stays."""
    if n <= 0:
        return data, 0
    if fmt == "raw":
        k = min(n // 2, len(data) // 4)
        return data[4 * k:], 2 * k
    at, left = 0, n
    while left > 0 and at + 4 <= len(data):
        word = struct.unpack_from("<I", data, at)[0]
        length = (word >> 16) + 1
        if length > left:                   # the word loses its first samples
            return struct.pack("<I", (length - left - 1) << 16 | word & 0xFFFF) + data[at + 4:], n
        left -= length
        at += 4
    return data[at:], n - left


def sample_bytes(values, lengths):
    """Runs as 16-bit samples, low byte first, in pieces of a few megabytes: what a sigrok session file holds."""
    piece, size = [], 0
    for v, n in zip(values, lengths):
        piece.append(struct.pack("<H", v) * n)
        size += 2 * n
        if size >= 4 << 20:
            yield b"".join(piece)
            piece, size = [], 0
    if piece:
        yield b"".join(piece)


# ---- the test pattern: sample t is (t >> shift) & 0xFFFF ---------------------------------------------------------

def pattern_blocks(fmt, shift, samples, first=0):
    """The words of a test-pattern capture of that many samples, as pieces of bytes.  first: which sample of the
    pattern the capture begins with (a capture with a trigger does not begin with sample 0)."""
    step = 1 << shift
    if fmt == "rle":                        # every run is one value of the counter: 2^shift samples
        head = min(-first % step, samples)  # what is left of the value the capture begins in
        if head:
            yield struct.pack("<I", (head - 1) << 16 | (first >> shift) & 0xFFFF)
        base = (first + head) >> shift
        whole, rest = divmod(samples - head, step)
        if whole:
            period = array("I", [(step - 1) << 16 | (base + i) & 0xFFFF for i in range(PERIOD)])
            if not LITTLE:
                period.byteswap()
            period = period.tobytes()
            for at in range(0, whole, PERIOD):
                yield period if whole - at >= PERIOD else period[:4 * (whole - at)]
        if rest:
            yield struct.pack("<I", (rest - 1) << 16 | (base + whole) & 0xFFFF)
        return
    cache = {}
    for at in range(0, samples, PERIOD):
        start = (first + at) % (PERIOD << shift)        # the pattern comes round after this many samples
        block = cache.get(start)
        if block is None:
            pieces, t = [], start
            while t < start + PERIOD:
                until = min(start + PERIOD, ((t >> shift) + 1) << shift)
                pieces.append(struct.pack("<H", (t >> shift) & 0xFFFF) * (until - t))
                t = until
            block = b"".join(pieces)
            if len(cache) < 64:
                cache[start] = block
        n = min(PERIOD, samples - at)
        if n < PERIOD:
            block = block[:2 * n]
            if n & 1:
                block += block[-2:]         # the last sample again, to fill the word
        yield block


def pattern_origin(data, fmt, shift):
    """Which sample of the test pattern the first sample in the words is, found from where the pattern first
    steps (it does within 2^shift samples).  For a capture with a trigger, of which the FPGA keeps only the last
    words from before the trigger sample."""
    around = PERIOD << shift
    if fmt == "raw":
        h = array("H")
        h.frombytes(bytes(data[:min(len(data) - len(data) % 4, 4 * (1 << shift) + 8)]))
        if not LITTLE:
            h.byteswap()
        for i, v in enumerate(h):
            if v != h[0]:
                return ((v << shift) - i) % around
        return (h[0] << shift) % around if h else 0
    low, more = halves(data[:64])           # the word before the trigger sample's may hold part of a value
    at = 0
    for v, m in zip(low, more):
        if v != low[0]:
            return ((v << shift) - at) % around
        at += m + 1
    return (low[0] << shift) % around if low else 0


def check_pattern_triggered(data, fmt, shift, samples, before):
    """check_pattern() for a capture with a trigger.  before: how many words there are before the word with the
    trigger sample.  In the run-length format that word begins with the trigger sample, wherever that falls in
    the pattern, so the word before it may hold less than a whole value."""
    first = pattern_origin(data, fmt, shift)
    if fmt == "raw":
        return check_pattern(data, fmt, shift, samples, first)
    step, t = 1 << shift, first
    low, more = halves(data[:4 * before])
    for i, (v, m) in enumerate(zip(low, more)):
        n = m + 1
        whole = (t + n) % step == 0         # the word ends where the pattern steps
        if v != (t >> shift) & 0xFFFF or t % step + n > step or not (whole or i == before - 1):
            return "word %d is %08X, which the test pattern does not have there" % (i, m << 16 | v)
        t += n
    wrong = check_pattern(memoryview(data)[4 * before:], fmt, shift, samples - (t - first), t)
    return wrong and "from the trigger sample's word on, %s" % wrong


def check_pattern(data, fmt, shift, samples, first=0):
    """Compares a test-pattern capture of that many samples with what the FPGA should have sent, word for word.
    Returns None when every word is right, or what the first difference is.  first: which sample of the pattern
    the capture begins with."""
    view = memoryview(data)
    at = 0
    for block in pattern_blocks(fmt, shift, samples, first):
        got = view[at:at + len(block)]
        if got != block:
            n = min(len(got), len(block)) // 4
            for i in range(n):
                g, w = got[4 * i:4 * i + 4], block[4 * i:4 * i + 4]
                if g != w:
                    return ("word %d is %08X where the test pattern has %08X"
                            % (at // 4 + i, struct.unpack("<I", g)[0], struct.unpack("<I", w)[0]))
            return "the data ends after %d words, the test pattern has more" % (len(view) // 4)
        at += len(block)
    if len(view) != at:
        return "%d words arrived, the test pattern has %d" % (len(view) // 4, at // 4)
    return None
