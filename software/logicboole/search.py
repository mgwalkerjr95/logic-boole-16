# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""Finding moments in a saved capture, described as a trigger is (trigger.py):

    B0=r                    each time B0 rises
    A0=0 pulse ..50ns       each low pulse on A0 of 50 ns or less: glitches
    A1=1 for 100us          each time A1 has stayed high for 100 us: gaps in a bus, with how long each lasted
    A3=0 A2=1               each stretch in which A3 is low and A2 high
    A3=f then A6=r          each time A3 falls and A6 then rises: the time between them too

The rules are the trigger's, so a search finds what the board's trigger would catch: the stages are met one after
the other, each by a later sample than the one before, and the sample that meets the last stage is where the moment
is.  After a moment is found the search goes on from the next sample, and a first stage of levels is met again only
by a new stretch of those levels, so that a line that stays high is found once, not at every sample.

Channels can be given by the names the capture has for them ("SDA=f SCL=1").  TRIG IN is not in a capture.
"""
import statistics
from array import array
from itertools import islice

from . import trigger as T

LIMIT = 100                                 # moments listed if no number is given
MOST = 2000


def with_ids(cap, text):
    """Trigger text with the capture's channel names put back as A0 ... B7: "SDA=f SCL=1" -> "A0=f A1=1"."""
    out = []
    for w in text.replace(",", " ").split():
        name, eq, what = w.partition("=")
        out.append("%s=%s" % (T.CHANNELS[cap.channel(name)], what) if eq and name.lower() != "in" else w)
    return " ".join(out)


def _runs(cap, first, last):
    """The capture's runs from sample first to sample last, as (value, the sample it starts at, how many), one at
    a time as they are asked for, with the value of the sample before first (None at the capture's start)."""
    values, lengths = cap.values, cap.lengths
    i, pos, before = 0, 0, None
    while i < len(values) and pos + lengths[i] <= first:    # the runs that end before first
        pos += lengths[i]
        i += 1
    if first > 0 and i < len(values) and pos <= first - 1:
        before = values[i]
    elif first > 0 and i > 0 and pos == first:
        before = values[i - 1]

    def each(pos):
        for v, n in zip(islice(values, i, None), islice(lengths, i, None)):
            if pos >= last:
                return
            if first <= pos and pos + n <= last:
                yield v, pos, n
            else:                           # the run the part begins or ends in
                a, b = max(pos, first), min(pos + n, last)
                if a < b:
                    yield v, a, b - a
            pos += n
    return each(pos), before


def find(cap, text, start=None, end=None, limit=LIMIT):
    """Every moment in the capture that the trigger text describes, as a dict.

    start, end   seconds from the capture's start, to look in a part of it
    limit        how many moments to list (MOST at the most); total counts them all

    The dict: find (the text as the board reads it), sample_rate, sample_seconds, start, end, total, found and,
    when the last stage is of levels, held (the min, median and max of how long they held).  Each of found:
    time and sample (where the moment is: the sample that meets the last stage), first and span with more than
    one stage (where the first stage was met, and the time from there), and held when the last stage is of
    levels: for a pulse, the pulse (start, end, seconds); for levels, the whole stretch in which they held, which
    may go on past time (to_end: still holding where the search ends; before: holding where it began)."""
    rate = cap.rate
    stages = [dict(s, len=0, fresh=False) for s in T.parse(with_ids(cap, text), rate)]
    if any(s["kind"] == 3 for s in stages):
        raise T.TriggerError("TRIG IN is not in a saved capture: a search looks at the inputs.")
    first = 0 if start is None else max(0, round(start * rate))
    last = cap.samples if end is None else min(cap.samples, round(end * rate))
    if last < first:
        raise ValueError("The end of the part to look in is before its start.")
    limit = max(0, min(int(limit), MOST))
    runs, prev = _runs(cap, first, last)
    end_stage = stages[-1]
    levels = end_stage["kind"] in (1, 2) or end_stage["kind"] == 0 and not end_stage["rise"] | end_stage["fall"]
    found, held, pending = [], array("Q"), []       # pending: moments whose stretch of levels has not ended yet
    total, met, began, rearm = 0, 0, None, -1

    def moment(at, s, pos):
        nonlocal total
        total += 1
        entry = {"time": at / rate, "sample": at} if len(found) < limit else None
        if entry is not None:
            if len(stages) > 1:
                entry["first"], entry["span"] = began / rate, (at - began) / rate
            found.append(entry)
        if s["kind"] == 1:                  # the pulse ended at this sample
            a = at - s["len"]
            held.append(at - a)
            if entry is not None:
                entry["held"] = {"start": a / rate, "end": at / rate, "seconds": (at - a) / rate}
        elif levels:                        # the stretch began here, and its end is still to come
            pending.append((entry, pos - s["len"], not s["fresh"] and pos - s["len"] == first))

    def close(stop, to_end):
        for entry, a, before in pending:
            held.append(stop - a)
            if entry is not None:
                entry["held"] = {"start": a / rate, "end": stop / rate, "seconds": (stop - a) / rate}
                if to_end:
                    entry["held"]["to_end"] = True
                if before:
                    entry["held"]["before"] = True
        pending.clear()

    for v, pos, n in runs:
        look = 0
        while look < n:
            s = stages[met]
            hit, here = (v & s["mask"]) == s["value"], None
            if s["kind"] == 0:
                if not s["rise"] | s["fall"]:               # levels: met by any sample in which they hold
                    if hit and (met or pos - s["len"] > rearm):     # (a first stage: in a new stretch of them)
                        here = look
                elif (hit and look == 0 and prev is not None
                      and (~prev & v & s["rise"] | prev & ~v & s["fall"])):
                    here = 0                                # an edge, at the first sample of a run
            elif s["kind"] == 1:                            # a pulse has ended: it began in the part looked at
                if look == 0 and not hit and s["fresh"] and s["len"] and s["min"] <= s["len"] <= s["max"]:
                    here = 0
            elif s["kind"] == 2 and hit and (met or pos - s["len"] > rearm):
                here = max(look, s["min"] - 1 - s["len"])   # the levels have lasted
                here = here if here < n else None
            if here is None:
                break
            if met == 0:
                began = pos + here
            met += 1
            look = here + 1
            if met == len(stages):
                moment(pos + here, s, pos)
                met, rearm = 0, pos + here
        for s in stages:                    # how long each stage's levels have held, up to the next run
            if (v & s["mask"]) == s["value"]:
                s["len"] += n
            else:
                s["len"], s["fresh"] = 0, True
        if pending and (v & end_stage["mask"]) != end_stage["value"]:
            close(pos, False)
        prev = v
    close(last, True)
    out = {"find": T.describe(stages, rate), "sample_rate": rate, "sample_seconds": 1 / rate, "start": first / rate,
           "end": last / rate, "total": total, "found": found}
    if levels:
        out["held"] = ({"min": min(held) / rate, "median": statistics.median(held) / rate, "max": max(held) / rate}
                       if held else None)
    return out
