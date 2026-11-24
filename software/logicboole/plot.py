# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""A picture of a saved capture, as a PNG file: the channels as waveforms over a time axis, with the trigger,
marks at given times, and rows of what protocol decoders found.  For a person to look at, and for an AI model that
reads images to check what it has worked out from the numbers.

Each column of pixels shows a channel's level, or an edge, or, where several changes fall in one column, a block
that says the signal is busy there: a picture of a long part of a busy bus is mostly blocks, and a shorter part
(start and end) shows its bits.  It needs nothing besides Python: the PNG is written here, and the letters are a
5 x 7 font of its own.
"""
import math
import struct
import zlib
from bisect import bisect_left, bisect_right

from .capture import CHANNELS

WIDTH = 1200                                # pixels, if not given
WIDTHS = (400, 4000)
S = 2                                       # the font's scale: 10 x 14 pixels a letter
ROW, LANE, LANES = 34, 26, 3                # a channel's height; a row of decoded results; rows a decoder may have

# The 5 x 7 letters of printable ASCII, from the space: six blocks of seven lines, sixteen letters to a line
FONT_TEXT = """
..... ..#.. .#.#. .#.#. ..#.. ##... .##.. ..#.. ...#. .#... ..... ..... ..... ..... ..... .....
..... ..#.. .#.#. .#.#. .#### ##..# #..#. ..#.. ..#.. ..#.. ..#.. ..#.. ..... ..... ..... ....#
..... ..#.. ..... ##### #.#.. ...#. #.#.. ..... .#... ...#. #.#.# ..#.. ..... ..... ..... ...#.
..... ..#.. ..... .#.#. .###. ..#.. .#... ..... .#... ...#. .###. ##### ..... ##### ..... ..#..
..... ..#.. ..... ##### ..#.# .#... #.#.# ..... .#... ...#. #.#.# ..#.. .##.. ..... ..... .#...
..... ..... ..... .#.#. ####. #..## #..#. ..... ..#.. ..#.. ..#.. ..#.. ..#.. ..... .##.. #....
..... ..#.. ..... .#.#. ..#.. ...## .##.# ..... ...#. .#... ..... ..... .#... ..... .##.. .....
.###. ..#.. .###. ##### ...#. ##### ..##. ##### .###. .###. ..... ..... ...#. ..... .#... .###.
#...# .##.. #...# ...#. ..##. #.... .#... ....# #...# #...# .##.. .##.. ..#.. ..... ..#.. #...#
#..## ..#.. ....# ..#.. .#.#. ####. #.... ...#. #...# #...# .##.. .##.. .#... ##### ...#. ....#
#.#.# ..#.. ...#. ...#. #..#. ....# ####. ..#.. .###. .#### ..... ..... #.... ..... ....# ...#.
##..# ..#.. ..#.. ....# ##### ....# #...# .#... #...# ....# .##.. .##.. .#... ##### ...#. ..#..
#...# ..#.. .#... #...# ...#. #...# #...# .#... #...# ...#. .##.. ..#.. ..#.. ..... ..#.. .....
.###. .###. ##### .###. ...#. .###. .###. .#... .###. .##.. ..... .#... ...#. ..... .#... ..#..
.###. .###. ####. .###. ###.. ##### ##### .###. #...# .###. ..### #...# #.... #...# #...# .###.
#...# #...# #...# #...# #..#. #.... #.... #...# #...# ..#.. ...#. #..#. #.... ##.## #...# #...#
....# #...# #...# #.... #...# #.... #.... #.... #...# ..#.. ...#. #.#.. #.... #.#.# ##..# #...#
.##.# ##### ####. #.... #...# ####. ####. #.### ##### ..#.. ...#. ##... #.... #.#.# #.#.# #...#
#.#.# #...# #...# #.... #...# #.... #.... #...# #...# ..#.. ...#. #.#.. #.... #...# #..## #...#
#.#.# #...# #...# #...# #..#. #.... #.... #...# #...# ..#.. #..#. #..#. #.... #...# #...# #...#
.###. #...# ####. .###. ###.. ##### #.... .#### #...# .###. .##.. #...# ##### #...# #...# .###.
####. .###. ####. .#### ##### #...# #...# #...# #...# #...# ##### .###. ..... .###. ..#.. .....
#...# #...# #...# #.... ..#.. #...# #...# #...# #...# #...# ....# .#... #.... ...#. .#.#. .....
#...# #...# #...# #.... ..#.. #...# #...# #...# .#.#. #...# ...#. .#... .#... ...#. #...# .....
####. #...# ####. .###. ..#.. #...# #...# #.#.# ..#.. .#.#. ..#.. .#... ..#.. ...#. ..... .....
#.... #.#.# #.#.. ....# ..#.. #...# #...# #.#.# .#.#. ..#.. .#... .#... ...#. ...#. ..... .....
#.... #..#. #..#. ....# ..#.. #...# .#.#. #.#.# #...# ..#.. #.... .#... ....# ...#. ..... .....
#.... .##.# #...# ####. ..#.. .###. ..#.. .#.#. #...# ..#.. ##### .###. ..... .###. ..... #####
.#... ..... #.... ..... ....# ..... ..##. ..... #.... ..#.. ...#. #.... .##.. ..... ..... .....
..#.. ..... #.... ..... ....# ..... .#..# .#### #.... ..... ..... #.... ..#.. ..... ..... .....
..... .###. #.##. .###. .##.# .###. .#... #...# #.##. .##.. ..##. #..#. ..#.. ##.#. #.##. .###.
..... ....# ##..# #.... #..## #...# ###.. #...# ##..# ..#.. ...#. #.#.. ..#.. #.#.# ##..# #...#
..... .#### #...# #.... #...# ##### .#... .#### #...# ..#.. ...#. ##... ..#.. #.#.# #...# #...#
..... #...# #...# #...# #...# #.... .#... ....# #...# ..#.. #..#. #.#.. ..#.. #...# #...# #...#
..... .#### ####. .###. .#### .###. .#... .###. #...# .###. .##.. #..#. .###. #...# #...# .###.
..... ..... ..... ..... .#... ..... ..... ..... ..... ..... ..... ...#. ..#.. .#... ..... .....
..... ..... ..... ..... .#... ..... ..... ..... ..... ..... ..... ..#.. ..#.. ..#.. ..... .....
####. .##.# #.##. .###. ###.. #...# #...# #...# #...# #...# ##### ..#.. ..#.. ..#.. .#... .....
#...# #..## ##..# #.... .#... #...# #...# #...# .#.#. #...# ...#. .#... ..#.. ...#. #.#.# .....
####. .#### #.... .###. .#... #...# #...# #.#.# ..#.. .#### ..#.. ..#.. ..#.. ..#.. ...#. .....
#.... ....# #.... ....# .#..# #..## .#.#. #.#.# .#.#. ....# .#... ..#.. ..#.. ..#.. ..... .....
#.... ....# #.... ####. ..##. .##.# ..#.. .#.#. #...# .###. ##### ...#. ..#.. .#... ..... .....
"""

WHITE, INK, GRID, BAND, TRACE, BUSY, MARK, TRIGGER, GREY, BLUE, YELLOW, GREEN, RED, EDGE = range(14)
PALETTE = ((255, 255, 255), (34, 34, 34), (222, 222, 222), (246, 246, 244), (32, 88, 156), (168, 194, 226),
           (226, 106, 18), (200, 36, 36), (118, 118, 118), (205, 224, 250), (255, 239, 186), (214, 239, 208),
           (250, 208, 208), (110, 110, 110))


def _font():
    lines = FONT_TEXT.strip("\n").splitlines()
    glyphs = {}
    for b in range(6):
        rows = [line.split() for line in lines[7 * b:7 * b + 7]]
        for k in range(16):
            glyphs[chr(32 + 16 * b + k)] = [int(rows[y][k].replace(".", "0").replace("#", "1"), 2) for y in range(7)]
    return glyphs


FONT = _font()


class Canvas:
    """Pixels as palette indices, and the few things a plot draws with."""

    def __init__(self, width, height):
        self.w, self.h = width, height
        self.px = bytearray(width * height)

    def rect(self, x0, y0, x1, y1, color):
        """Fills x0 <= x < x1, y0 <= y < y1."""
        x0, x1, y0, y1 = max(0, int(x0)), min(self.w, int(x1)), max(0, int(y0)), min(self.h, int(y1))
        if x0 < x1:
            row = bytes([color]) * (x1 - x0)
            for y in range(y0, y1):
                self.px[y * self.w + x0:y * self.w + x1] = row

    def vdash(self, x, y0, y1, color, on=4, off=3):
        for y in range(int(y0), int(y1), on + off):
            self.rect(x, y, x + 1, min(y + on, y1), color)

    def text(self, x, y, s, color, scale=S, right=None):
        """Writes s with its top left at (x, y), and stops before x reaches right."""
        for ch in s:
            if right is not None and x + 5 * scale > right:
                break
            for r, bits in enumerate(FONT.get(ch, FONT["?"])):
                for c in range(5):
                    if bits >> (4 - c) & 1:
                        self.rect(x + c * scale, y + r * scale, x + (c + 1) * scale, y + (r + 1) * scale, color)
            x += 6 * scale
        return x

    def png(self):
        raw = b"".join(b"\0" + bytes(self.px[y * self.w:(y + 1) * self.w]) for y in range(self.h))

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 3, 0, 0, 0))
                + chunk(b"PLTE", bytes(v for rgb in PALETTE for v in rgb)) + chunk(b"IDAT", zlib.compress(raw, 9))
                + chunk(b"IEND", b""))


def width_of(s, scale=S):
    return max(0, 6 * scale * len(s) - scale)


def time_text(t, unit=None, digits=None):
    """A time for an axis: 10 us, 2.5 ms."""
    if unit is None:
        if t == 0:
            return "0 s"
        unit = next((u for u in (1.0, 1e-3, 1e-6) if abs(t) >= u), 1e-9)
    name = {1.0: "s", 1e-3: "ms", 1e-6: "us", 1e-9: "ns"}[unit]
    v = t / unit
    return ("%.*f %s" % (digits, v, name)) if digits is not None else "%.6g %s" % (v, name)


def ticks(t0, t1, count):
    """The spacing of about count times on an axis from t0 to t1 (seconds), a round number: (step, unit, digits)."""
    raw = (t1 - t0) / max(1, count)
    k = math.floor(math.log10(raw)) if raw > 0 else -9
    step = next(m * 10 ** k for m in (1, 2, 5, 10) if m * 10 ** k >= raw)
    unit = next((u for u in (1.0, 1e-3, 1e-6) if step >= u * (1 - 1e-9)), 1e-9)
    digits = max(0, -math.floor(math.log10(step / unit) + 1e-9))
    return step, unit, digits


def _kind_color(cls, text=""):
    c = cls.lower() + " " + text.lower()
    if any(w in c for w in ("warning", "nack", "err", "framing")):
        return RED
    if any(w in c for w in ("start", "stop", "restart", "break")):
        return GREEN
    if "address" in c or "addr" in c:
        return BLUE
    return YELLOW


def plot(cap, channels=None, start=None, end=None, width=WIDTH, marks=(), annotations=(), title=""):
    """The picture as PNG bytes, and what it shows as a dict.

    channels     ids or names; not given: those that change in the part shown (all 16 if none does)
    start, end   seconds from the capture's start; not given: all of it
    marks        times to mark, in seconds from the capture's start, numbered 1, 2 ... on the picture
    annotations  what decoders found (decode.decode()'s annotations, with "texts" if they have them), drawn in
                 rows under the channels, a decoder's rows under its name

    The dict: start, end, seconds_per_pixel, channels, marks, annotations (how many were drawn), busy (the
    channels with columns too busy to show their edges: a shorter part shows them)."""
    rate = cap.rate
    width = max(WIDTHS[0], min(WIDTHS[1], int(width)))
    first = 0 if start is None else max(0, min(cap.samples, round(start * rate)))
    last = cap.samples if end is None else max(0, min(cap.samples, round(end * rate)))
    if last <= first:
        raise ValueError("The part to show ends before it starts, or is outside the capture (%.9g s long)."
                         % cap.seconds)
    edges = cap.edges()
    if channels:
        picked = []
        for name in channels:
            k = cap.channel(name)
            if k not in picked:
                picked.append(k)
    else:
        picked = [k for k, (_, e) in enumerate(edges) if bisect_left(e, last) > bisect_right(e, first)]
        picked = picked or list(range(16))
    labels = [CHANNELS[k] if cap.names[k] == CHANNELS[k] else "%s %s" % (CHANNELS[k], cap.names[k]) for k in picked]
    lanes = {}                              # the decoders, in the order of their first results
    for a in annotations:
        lanes.setdefault(a.get("lane") or a["decoder"], None)
    left = 16 + max([width_of(s) for s in labels + list(lanes)] + [width_of("B7")]) + 14
    right = width - 24
    plot_w = right - left
    top = 56
    height = top + ROW * len(picked) + LANE * LANES * len(lanes) + 46
    cv = Canvas(width, height)
    spp = (last - first) / plot_w           # samples a pixel

    def x_of(sample):
        return left + (sample - first) / spp

    # the title line, and the bands behind the channels
    head = "%s   %s   %s to %s" % (title, _rate_text(rate), time_text(first / rate), time_text(last / rate))
    cv.text(16, 10, head.strip(), INK, right=width - 8)
    for i in range(len(picked)):
        if i % 2:
            cv.rect(left, top + i * ROW, right, top + (i + 1) * ROW, BAND)
    bottom = top + ROW * len(picked) + LANE * LANES * len(lanes)

    # the axis: grid lines through everything, times under it
    step, unit, digits = ticks(first / rate, last / rate, plot_w / 130)
    t = math.ceil(first / rate / step - 1e-9) * step
    while t <= last / rate + 1e-15:
        x = int(round(x_of(t * rate)))
        if left <= x < right:
            cv.rect(x, top, x + 1, bottom, GRID)
            label = time_text(t, unit, digits)
            cv.text(min(max(left, x - width_of(label) // 2), right - width_of(label)), bottom + 10, label, GREY)
        t += step
    cv.rect(left, bottom, right, bottom + 1, GREY)

    # the channels
    busy = []
    for i, k in enumerate(picked):
        initial, e = edges[k]
        y = top + i * ROW
        hi, lo = y + 7, y + ROW - 9
        cv.text(16, y + (ROW - 14) // 2, labels[i], INK)
        dense = 0
        for col in range(plot_w):
            a, b = first + col * spp, first + (col + 1) * spp
            i0, i1 = bisect_left(e, a), bisect_left(e, b)
            x = left + col
            if i1 - i0 >= 2:                # more than one change in this column: busy, drawn as a band
                cv.rect(x, hi, x + 1, lo + 2, BUSY)
                cv.rect(x, hi, x + 1, hi + 2, TRACE)
                cv.rect(x, lo, x + 1, lo + 2, TRACE)
                dense += 1
                continue
            if i1 > i0:                     # an edge
                cv.rect(x, hi, x + 1, lo + 2, TRACE)
            level = initial ^ (i1 & 1)      # the level at the end of the column
            cv.rect(x, hi if level else lo, x + 1, (hi if level else lo) + 2, TRACE)
        if dense > plot_w // 20:
            busy.append(CHANNELS[k])

    # what the decoders found
    drawn, row0 = 0, top + ROW * len(picked)
    for d, name in enumerate(lanes):
        y0 = row0 + d * LANE * LANES
        cv.text(16, y0 + (LANE - 14) // 2, name, GREY)
        free = [left] * LANES
        for a in (a for a in annotations if (a.get("lane") or a["decoder"]) == name):
            x0, x1 = x_of(a["start_sample"]), x_of(a["end_sample"])
            if x1 < left or x0 >= right:
                continue
            x0, x1 = max(left, int(x0)), min(right, max(int(x1), int(x0) + 2))
            lane = next((n for n in range(LANES) if x0 >= free[n]), None)
            if lane is None:
                continue
            free[lane] = x1
            y = y0 + lane * LANE + 3
            cv.rect(x0, y, x1, y + LANE - 6, EDGE)
            if x1 - x0 > 2:
                cv.rect(x0 + 1, y + 1, x1 - 1, y + LANE - 7, _kind_color(a.get("class", ""), a.get("text", "")))
            for scale, pad in ((S, 3), (1, 1)):
                room = x1 - x0 - 2 * pad
                say = next((s for s in a.get("texts") or [a.get("text", "")] if width_of(s, scale) <= room), None)
                if say:
                    cv.text(x0 + pad + (room - width_of(say, scale)) // 2, y + (LANE - 6 - 7 * scale) // 2, say,
                            INK, scale)
                    break
            drawn += 1

    # the trigger and the marks, over everything
    if cap.trigger is not None and first <= cap.trigger <= last:
        x = int(round(x_of(cap.trigger)))
        cv.vdash(x, top - 18, bottom, TRIGGER)
        cv.text(x + 4, top - 17, "T", TRIGGER)
    shown = []
    for n, m in enumerate(marks, 1):
        s = m * rate
        if first <= s <= last:
            x = int(round(x_of(s)))
            cv.rect(x, top - 18, x + 2, bottom, MARK)
            cv.text(x + 4, top - 17, str(n), MARK)
            shown.append({"mark": n, "time": m})
    return cv.png(), {"start": first / rate, "end": last / rate, "seconds_per_pixel": spp / rate,
                      "channels": [CHANNELS[k] for k in picked], "names": [cap.names[k] for k in picked],
                      "marks": shown, "annotations": drawn, "busy": busy, "width": width, "height": height}


def decoded(path, decoders, start=None, end=None, seconds=None):
    """What decoders find in the part of a saved capture to be drawn, for plot()'s annotations.  decoders: a
    list of decoder words, each as decode takes them.  The decoders read the whole capture; what they found
    is kept from a while before start, so that a long result that began earlier (a slow serial byte) shows too."""
    from . import decode as D
    out, kinds = [], {}
    for words in decoders:
        length = (end if end is not None else seconds or 0) - (start or 0)
        found = D.decode(path, words, start=None if start is None else max(0.0, start - max(length, 1e-3)),
                         end=end, shorter=True)
        top = D.stack(words)[-1][0]         # a row's name: the decoder, numbered if it is there twice
        kinds[top] = kinds.get(top, 0) + 1
        lane = top if kinds[top] == 1 else "%s %d" % (top, kinds[top])
        out += [dict(a, lane=lane) for a in found["annotations"] if start is None or a["end"] >= start]
    return out


def _rate_text(rate):
    return "%.10g MS/s" % (rate / 1e6) if rate >= 1e6 else "%.10g kS/s" % (rate / 1e3)
