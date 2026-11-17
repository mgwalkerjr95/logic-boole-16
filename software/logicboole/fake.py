# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""A stand-in for the board, for trying the software with nothing plugged in and for the tests.

It takes the place of usb.py: the same requests get the answers the firmware and the FPGA's design would give
(docs/protocol.md), and a capture brings an I2C bus on A1 (SCL) and A0 (SDA), 100 kHz with a group of messages
every 20 ms, or the FPGA's test pattern.  The samples are made one run at a time and coded as the FPGA codes them;
the code that checks a capture shares nothing with this.  The trigger and the capture clock work as the FPGA's do;
the TRIG IN pin never changes, and the glitch filter's settings are kept but filter nothing (the stand-in's signals
have no pulse that short).

    logicboole --fake            the app with the stand-in
    logicboole --fake capture x.sr
"""
import math
import struct
import time
from array import array

from . import board as B
from . import trigger as T
from . import usb

FRAME, QUARTER = 0.02, 2.5e-6               # seconds: a group of I2C messages this often; a quarter of an SCL period
FIFO_BITS = 13                              # the FPGA's FIFO holds 2^13 words
WRITABLE = (set((4, 8, 9, 10, 12, 13, 14, 15, 16, 17, 18, 19, 20, 0x20, 0x21, 0x30, 0x31, 0x32, 0x33))
            | set(range(0x38, 0x3C)) | set(range(0x40, 0x90)))


def i2c_frame(n):
    """Group n of the I2C messages as (SDA, SCL) levels, one pair per quarter of a clock period."""
    out, sda = [], [1]

    def put(d, c):
        out.append((d, c))
        sda[0] = d

    def start():
        put(0, 1); put(0, 1)

    def stop():
        put(sda[0], 0); put(0, 0); put(0, 1); put(0, 1); put(1, 1)

    def bit(b):
        put(sda[0], 0); put(b, 0); put(b, 1); put(b, 1)

    def byte(v, ack):
        for i in range(7, -1, -1):
            bit(v >> i & 1)
        bit(0 if ack else 1)

    t = n // 8 % 32
    temp = 400 + (t if t < 16 else 32 - t)
    out += [(1, 1)] * 8
    start(); byte(0x16 << 1, 1)             # a write of text to address 16
    for ch in b"Logic Boole 16":
        byte(ch, 1)
    stop(); out.extend([(1, 1)] * 40)
    start(); byte(0x48 << 1, 1); byte(0x00, 1)          # a temperature sensor: its register 0 is read
    put(sda[0], 0); put(1, 0); put(1, 1); put(1, 1); start()
    byte(0x48 << 1 | 1, 1); byte(temp >> 4, 1); byte(temp << 4 & 0xF0, 0)
    stop(); out.extend([(1, 1)] * 40)
    start(); byte(0x50 << 1, 1); byte(0x00, 1); byte(0x10, 1); byte(n >> 8 & 0xFF, 1); byte(n & 0xFF, 1)
    stop(); out.extend([(1, 1)] * 40)       # a memory: two bytes written at 0010
    start(); byte(0x3C << 1, 0)             # an address nobody answers
    stop(); out.extend([(1, 1)] * 16)
    return out


def probe_runs(first, last, rate):
    """What the probes see from sample first up to sample last, as (value, how many samples)."""
    n, frames = first, {}
    while n < last:
        t = n / rate
        k = int(t // FRAME)
        if k not in frames:
            frames.clear()
            frames[k] = i2c_frame(k)
        i = int((t - k * FRAME) // QUARTER)
        if i < len(frames[k]):
            sda, scl = frames[k][i]
            until = k * FRAME + (i + 1) * QUARTER
        else:
            sda, scl, until = 1, 1, (k + 1) * FRAME
        end = max(n + 1, min(last, math.ceil(until * rate - 1e-6)))
        yield sda | scl << 1, end - n
        n = end


def pattern_runs(first, last, shift):
    """The test pattern likewise: sample t is (t >> shift) & 0xFFFF."""
    n = first
    while n < last:
        end = min(last, ((n >> shift) + 1) << shift)
        yield (n >> shift) & 0xFFFF, end - n
        n = end


class Coder:
    """Samples to the FPGA's words: add() takes runs, words() gives the bytes ready so far."""

    def __init__(self, fmt):
        self.rle = fmt == "rle"
        self.out = array("I")
        self.cur, self.count = None, 0      # run-length: the sample being counted.  Raw: a word's first half

    def add(self, value, n):
        if self.rle:
            while n > 0:
                if self.cur is None:
                    self.cur, self.count = value, 0
                if value != self.cur or self.count == 65536:
                    self.out.append((self.count - 1) << 16 | self.cur)
                    self.cur, self.count = value, 0
                k = min(n, 65536 - self.count)
                self.count += k
                n -= k
        else:
            if self.cur is not None and n:
                self.out.append(value << 16 | self.cur)
                self.cur = None
                n -= 1
            self.out.extend(array("I", [value << 16 | value]) * (n // 2))
            if n & 1:
                self.cur = value

    def end(self):
        if self.cur is not None:
            self.out.append((self.count - 1) << 16 | self.cur if self.rle else self.cur << 16 | self.cur)
            self.cur = None

    def cut(self):
        """The trigger sample comes next: in the run-length format a word begins with it."""
        if self.rle:
            self.end()

    def keep(self, n, slack=0):
        """While the capture waits for its trigger: only the newest n words stay.  slack: not looked at until
        there are that many more, which saves moving them after every word."""
        if len(self.out) > n + slack:
            del self.out[:len(self.out) - n]

    def words(self):
        out, self.out = self.out, array("I")
        if not B.W.LITTLE:
            out.byteswap()
        return out.tobytes()


def _image():
    """An image in the FX3's format for the stand-in to be loaded with: two sections of numbers, no code."""
    one = struct.pack("<256I", *range(1, 257))
    two = struct.pack("<64I", *range(1000, 1064))
    total = (sum(range(1, 257)) + sum(range(1000, 1064))) & 0xFFFFFFFF
    return (b"CY\x1C\xB0" + struct.pack("<II", 256, 0x40003000) + one + struct.pack("<II", 64, 0x00000100) + two
            + struct.pack("<II", 0, 0x40003000) + struct.pack("<I", total))


IMAGE = _image()


class FakeBus:
    image = IMAGE                           # what connect() loads into it in place of the board's firmware

    """Everything "on USB".  stage: "none" (no board), "boot" (plugged in, waiting) or "firmware".
    instant: a capture takes no time, for the tests.  overflow_at: the FPGA overflows at that sample (counted
    from the trigger sample in a capture with a trigger: while it waits, the FPGA cannot overflow).
    serial: the serial number the firmware gives on USB (None: a chip that gives none).
    link: 3 for a USB link of 5 Gbit/s, 2 for one of 480 Mbit/s (set it after making the bus).
    Faults, for the tests: drop_word and flip_word are the numbers of a word that is lost on the way, or arrives
    with a bit changed."""

    def __init__(self, stage="boot", instant=False, overflow_at=None, access=True, serial="5354414E442D494E"):
        self.stage, self.instant, self.overflow_at, self.access = stage, instant, overflow_at, access
        self.serial = serial
        self.link = 3
        self.drop_word = self.flip_word = None
        self.memory = {}
        self.fpga = False                   # configured with this project's design
        self.reg = bytearray(0x90)
        self.requests = []                  # every request made, for the tests
        self._start_firmware()

    # ---- what usb.py offers ----

    def open_device(self, vid, pid, wait=0.0):
        want = {B.PID_BOOT: "boot", B.PID_FIRMWARE: "firmware"}.get(pid)
        if vid != B.VID or want != self.stage or not self.access:
            return None
        return FakeDevice(self, want)

    def listed(self, ids):
        return self.stage != "none" and not self.access

    # ---- the firmware ----

    def _start_firmware(self):
        self.t_start = time.monotonic()
        self.cfg = {"bytes": 0, "flags": 0, "data": bytearray()}
        self.interface = self.stream = self.run = False
        self.port_errors = 0
        self.kb = self.count = 0
        self.claimed = False
        self.reader = None
        self._idle()

    def _idle(self):
        self.coder = None
        self.pos = self.taken = self.sent = 0       # samples kept; those that count (from the trigger on); words
        self.sampling = self.done = self.overflow = False
        self.stages, self.met, self.fired, self.last = [], 0, False, None
        self.words_before = self.trigger_upper = 0
        self.trigger_sent = False
        self.fine, self.fine_last = 0, None         # with a capture clock: the 5 ns samples gone by, and the last

    def status(self):
        flags = 1 | 4 | self.interface << 3 | self.stream << 4 | self.run << 5 | self.fpga << 6
        pins = self.fpga << B.DONE | 1 << B.INIT_B
        return struct.pack("<4s HBB HHHHHH I II IHHIH BB IHH BBBB HBB", B.MAGIC, 2, self.link, flags, 0, 0, 0, 0, 0, 0,
                           int((time.monotonic() - self.t_start) / B.TICK) & 0xFFFFFFFF, pins & 0xFFFFFFFF, pins >> 32,
                           self.cfg["bytes"], self.cfg["flags"], 12, 8, 290, 0, 0, self.port_errors, 0, 0, self.kb,
                           self.count, 0, 0, 0, 3 if self.fpga else 0, self._fpga_status() if self.fpga else 0)

    def _fpga_status(self):
        state = 0 if not self.run else 6 if self.done else 3
        return 1 | self.run << 1 | self.sampling << 2 | self.done << 3 | self.overflow << 4 | state << 5

    def registers(self):
        self.advance()
        r = self.reg
        r[0:4] = b"\x4C\x42\x16\x04"
        r[5] = self._fpga_status()
        r[6:8] = (3).to_bytes(2, "little")
        r[0x18:0x1D] = self.taken.to_bytes(5, "little")
        r[0x22:0x24] = (64).to_bytes(2, "little")
        r[0x24:0x2A] = self.sent.to_bytes(6, "little")
        r[0x2A], r[0x2B], r[0x2C], r[0x2D] = 0, FIFO_BITS, int(B.FASTEST / 1e6), T.STAGES
        waiting = bool(self.stages) and not self.fired and self.sampling
        r[0x34] = self.fired | self.trigger_sent << 1 | self.trigger_upper << 2 | waiting << 3 | self.met << 4
        r[0x36:0x38] = (self.words_before if self.trigger_sent else 0).to_bytes(2, "little")
        return bytes(r)

    def request_out(self, code, value, index, data):
        if code == 0xB3:
            self.fpga = False
            self.cfg = {"bytes": 0, "flags": 1 | 2 | B.CFG_READY, "data": bytearray()}
        elif code == 0xB4:
            self.cfg["bytes"] += len(data)
            self.cfg["data"] += data
        elif code == 0xB5:
            if b"\xAA\x99\x55\x66" in self.cfg["data"][:256]:
                self.fpga = True
                self.cfg["flags"] |= B.CFG_DONE | 32
                self.reg = bytearray(0x90)
                self.reg[0x0C:0x10] = struct.pack("<HH", 2048, 2048)
            self.cfg["data"] = bytearray()
        elif code == 0xB7:
            self.stage = "boot"
            self._start_firmware()
        elif code == 0xC0:
            self.interface = True
        elif code == 0xC1:
            self.interface = self.stream = self.run = False
        elif code == 0xC2:
            self.stream, self.kb, self.count, self.port_errors = True, value, index, 0
            self._set_run(False)
        elif code == 0xC3:
            self._set_run(False)
            self.stream = False
        elif code == 0xC4:
            if value & 1 and not self.stream:
                return -9                   # the firmware stalls it
            self._set_run(bool(value & 1))
        elif code == 0xC5:
            if self.reader is not None:
                self.reader.wrap_up = True
        elif code == 0xD0:
            if not self.fpga:
                return -9
            for i, v in enumerate(data):
                if value + i in WRITABLE:
                    self.reg[value + i] = v
        else:
            return -9
        return len(data)

    def request_in(self, code, value, n):
        if code == 0xB0:
            self.advance()
            return self.status()[:n]
        if code == 0xB9:
            return struct.pack("<IHHHHBBH", 0, 0, 0, 0, 0, 0, 3, 0)[:n]
        if code == 0xD1 and self.fpga:
            return self.registers()[value:value + n]
        return None

    # ---- the FPGA's sampler ----

    def _set_run(self, high):
        if high and not self.run:           # the settings are taken as RUN rises
            r = self.reg
            self._idle()
            self.control = r[4]
            self.rate = B.FASTEST / (int.from_bytes(r[8:11], "little") + 1)
            self.limit = int.from_bytes(r[0x10:0x15], "little")
            self.clock = r[0x30]
            self.ring = min(int.from_bytes(r[0x32:0x34], "little"), (1 << FIFO_BITS) - 8)
            for k in range(r[0x31] if r[0x31] <= T.STAGES else 0):
                mask, value, rise, fall, least, most = struct.unpack_from("<HHHHII", r, 0x40 + 16 * k)
                self.stages.append({"kind": r[0x38 + k] & 3, "mask": mask, "value": value, "rise": rise,
                                    "fall": fall, "min": least, "max": most, "len": 0, "fresh": False})
            self.coder = Coder("rle" if self.control & 1 else "raw")
            self.sampling, self.t_run = True, time.monotonic()
        elif not high and self.run:
            self.advance(final=True)
        self.run = high

    def _kept(self):
        """The samples that are due by now and kept, as runs.  With a capture clock (register 30) that is one
        sample at each of the clock input's edges: what the inputs were in the 5 ns sample before the edge."""
        test, shift = self.control & 2, self.control >> 4
        elapsed = time.monotonic() - self.t_run
        if not self.clock & 1:
            due = self.pos + (1 << 22) if self.instant else int(elapsed * self.rate)
            if test:                        # the test pattern is made a run at a time, which is slow: the stand-in
                due = min(due, self.pos + (200000 << shift))       # falls behind rather than stall
                return pattern_runs(self.pos, due, shift)
            return probe_runs(self.pos, due, self.rate)
        return self._clocked(self.fine + (1 << 22) if self.instant else int(elapsed * B.FASTEST), test, shift)

    def _clocked(self, due, test, shift):
        bit = 1 << (self.clock >> 4)
        for value, n in probe_runs(self.fine, due, B.FASTEST):
            before, self.fine_last = self.fine_last, value
            self.fine += n
            if before is not None and (before ^ value) & bit and self.clock & (2 if value & bit else 4):
                yield (self.pos >> shift) & 0xFFFF if test else before, 1
                if not self.sampling:
                    return

    def _trigger(self, v, n):
        """Looks for the stages still to be met in n samples alike.  Each stage is met by a later sample than the
        one before it; returns which of the n is the trigger sample, the one that meets the last stage, or None."""
        at, look = None, 0                  # look: the first of the n that can meet the next stage
        while self.met < len(self.stages) and look < n:
            s = self.stages[self.met]
            hit, here = (v & s["mask"]) == s["value"], None
            if s["kind"] == 0:              # the levels, and an edge if any is named
                if not s["rise"] | s["fall"]:
                    here = look if hit else None
                elif (hit and look == 0 and self.last is not None
                      and (~self.last & v & s["rise"] | self.last & ~v & s["fall"])):
                    here = 0
            elif s["kind"] == 1:            # a pulse has ended: it began in this capture, and was this long
                if look == 0 and not hit and s["fresh"] and s["len"] and s["min"] <= s["len"] <= s["max"]:
                    here = 0
            elif s["kind"] == 2 and hit:    # the levels have lasted
                here = max(look, max(s["min"], 1) - 1 - s["len"])
                here = here if here < n else None
            if here is None:                # (kind 3 is the TRIG IN pin, which never changes here)
                break
            self.met += 1
            at, look = here, here + 1
        for s in self.stages:               # how long each stage's levels have lasted
            if (v & s["mask"]) == s["value"]:
                s["len"] += n
            else:
                s["len"], s["fresh"] = 0, True
        self.last = v
        return at if self.met == len(self.stages) else None

    def _take(self, value, n):
        """n samples alike from the sampler: to the trigger while the capture waits for it, and into words."""
        if self.stages and not self.fired:
            at = self._trigger(value, n)
            before = n if at is None else at
            self.coder.add(value, before)
            self.pos += before
            n -= before
            if at is None:
                self.coder.keep(self.ring, 4096)
                return
            self.fired = True
            self.coder.cut()
            self.coder.keep(self.ring)
            self.words_before, self.trigger_upper = len(self.coder.out), int(self.coder.cur is not None)
        room = n
        if self.limit:
            room = min(room, self.limit - self.taken)
        if self.overflow_at is not None:
            room = min(room, max(self.overflow_at - self.taken, 0))
        self.coder.add(value, room)
        self.pos += room
        self.taken += room
        if self.overflow_at is not None and self.taken >= self.overflow_at:
            self.overflow = True
        if self.overflow or (self.limit and self.taken >= self.limit):
            self.sampling = False

    def advance(self, final=False):
        """Takes the samples that are due by now, and passes their words on if a read is waiting for them."""
        if self.coder is None:
            return
        if self.sampling:
            for value, n in self._kept():
                self._take(value, n)
                if not self.sampling:
                    break
            if final:
                self.sampling = False
            if not self.sampling:
                self.coder.end()
                if self.stages and not self.fired:
                    self.coder.keep(self.ring)
        if self.sampling and self.stages and not self.fired:
            return                          # the FIFO keeps the words until the trigger
        data = self.coder.words()
        first, self.sent = self.sent, self.sent + len(data) // 4
        if self.fired and self.sent > self.words_before:
            self.trigger_sent = True
        if self.flip_word is not None and first <= self.flip_word < self.sent:
            at = 4 * (self.flip_word - first)
            data = data[:at] + bytes([data[at] ^ 4]) + data[at + 1:]
        if self.drop_word is not None and first <= self.drop_word < self.sent:
            at = 4 * (self.drop_word - first)
            data = data[:at] + data[at + 4:]
        if data and self.reader is not None:
            self.reader.take(data)
        if not self.sampling and self.run:
            self.done = True


class FakeDevice:
    def __init__(self, bus, stage):
        self.bus, self.stage = bus, stage

    def close(self):
        pass

    def out(self, request, value=0, index=0, data=b"", timeout=1000):
        bus = self.bus
        bus.requests.append((request, value, index, len(data)))
        if bus.stage != self.stage:
            return -4                       # the device has gone
        if self.stage == "boot":
            if request != 0xA0:
                return -9
            address = value | index << 16
            if data:
                bus.memory[address] = bytes(data)
            else:                           # a jump: the firmware starts; the FPGA keeps what it holds
                bus.stage = "firmware"
                bus._start_firmware()
            return len(data)
        return bus.request_out(request, value, index, bytes(data))

    def get(self, request, value, index, n, timeout=1000):
        bus = self.bus
        if bus.stage != self.stage:
            return None
        if self.stage == "boot":
            got = bus.memory.get(value | index << 16, b"")
            return got if request == 0xA0 and len(got) == n else None
        return bus.request_in(request, value, n)

    def serial(self):
        return self.bus.serial if self.stage == "firmware" else None

    def speed(self):
        return "480 Mbit/s" if self.bus.link == 2 else "5 Gbit/s"

    def claim(self):
        if self.bus.claimed:
            return False
        self.bus.claimed = True
        return True

    def release(self):
        self.bus.claimed = False

    def packet_size(self, endpoint):
        return 1024

    def reader(self, endpoint, sink, size=1 << 20, queue=8):
        self.bus.reader = FakeReader(self.bus, sink, size, self.packet_size(endpoint))
        return self.bus.reader


class FakeReader:
    """The bulk endpoint as the computer sees it, with USB's rule for where a read ends: when it is full, or when
    a packet shorter than the endpoint's packet size arrives.  The FX3 sends whole buffers while a capture runs.
    At request C5 it sends the part-filled buffer, and then a buffer with nothing in it: an empty packet."""

    def __init__(self, bus, sink, size=1 << 20, packet=1024):
        self.bus, self.sink, self.size, self.packet = bus, sink, size, packet
        self.bytes = self.short_reads = self.failed = 0
        self.failure = self.sink_error = None
        self.ending = self.ended = self.wrap_up = False
        self.last_data = time.monotonic()
        self.held = bytearray()             # in the FX3: words that do not fill a buffer yet
        self.read = bytearray()             # the read that is filling

    def take(self, data):
        """Words from the FPGA.  Each buffer they fill goes to the computer."""
        self.held += data
        size = (self.bus.kb or B.BUFFER_KB) * 1024
        while len(self.held) >= size:
            self._buffer(bytes(self.held[:size]))
            del self.held[:size]

    def _buffer(self, data):
        """One of the FX3's buffers crosses USB as packets: full ones, then a shorter one if it does not end on a
        full one.  A buffer with nothing in it is one empty packet."""
        whole = len(data) - len(data) % self.packet
        at = 0
        while at < whole:
            n = min(self.size - len(self.read), whole - at)
            self.read += data[at:at + n]
            at += n
            if len(self.read) == self.size:
                self._read_done()
        if whole < len(data) or not data:   # a short packet ends the read it arrives in
            self.read += data[whole:]
            self._read_done()

    def _read_done(self):
        n = len(self.read)
        if n:
            self.last_data = time.monotonic()
            if self.sink_error is None:
                try:
                    self.sink(memoryview(bytes(self.read)))
                except Exception as e:
                    self.sink_error = e
        if self.ending and usb.ended_by_empty_packet(n, self.size, self.packet):
            self.ended = True
        elif not self.ending and n != self.size:
            self.short_reads += 1
        self.bytes += n
        del self.read[:]

    def pump(self, ms):
        if not self.bus.instant:
            time.sleep(ms / 1000)
        self.bus.advance()
        if self.wrap_up:
            self.wrap_up = False
            self._buffer(bytes(self.held))  # the part-filled buffer, as it is
            del self.held[:]
            self._buffer(b"")

    def close(self):
        self.bus.reader = None
