# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""libusb, reached through ctypes: vendor requests on endpoint 0 and queued reads of a bulk endpoint.

Nothing in this file knows about the board; board.py does.  libusb is the one thing the host software needs
besides Python:

    macOS                               brew install libusb
    Debian, Ubuntu, Raspberry Pi OS     already installed (the package is libusb-1.0-0)
"""
import ctypes
import ctypes.util
import glob
import re
import subprocess
import sys
import time


class UsbError(Exception):
    pass


SPEEDS = {1: "1.5 Mbit/s", 2: "12 Mbit/s", 3: "480 Mbit/s", 4: "5 Gbit/s", 5: "10 Gbit/s"}
COMPLETED, CANCELLED = 0, 3                 # enum libusb_transfer_status
FAILURES = {1: "error", 2: "timed out", 4: "stall", 5: "the device is gone", 6: "overflow"}
BULK = 2                                    # enum libusb_transfer_type


class _Transfer(ctypes.Structure):          # struct libusb_transfer, without its array of isochronous packets
    pass


_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.POINTER(_Transfer))
_Transfer._fields_ = [("dev_handle", ctypes.c_void_p), ("flags", ctypes.c_uint8), ("endpoint", ctypes.c_ubyte),
                      ("type", ctypes.c_ubyte), ("timeout", ctypes.c_uint), ("status", ctypes.c_int),
                      ("length", ctypes.c_int), ("actual_length", ctypes.c_int), ("callback", _CALLBACK),
                      ("user_data", ctypes.c_void_p), ("buffer", ctypes.c_void_p), ("num_iso_packets", ctypes.c_int)]


class _Timeval(ctypes.Structure):           # struct timeval where long is 64 bits (see Reader.pump)
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


_lib = None
_keep = None                                # macOS: see library()


def library():
    """libusb, loaded once."""
    global _lib
    if _lib is not None:
        return _lib
    if sys.platform == "win32":
        raise UsbError("Windows is not supported yet: the board needs a driver there that it does not have.  Use "
                       "macOS or Linux (a Raspberry Pi too).")
    u = None
    for path in (ctypes.util.find_library("usb-1.0"), "libusb-1.0.so.0", "/opt/homebrew/lib/libusb-1.0.dylib",
                 "/usr/local/lib/libusb-1.0.dylib"):
        if path:
            try:
                u = ctypes.CDLL(path)
                break
            except OSError:
                pass
    if u is None:
        raise UsbError("libusb is not installed.  macOS: brew install libusb.  "
                       "Debian, Ubuntu, Raspberry Pi OS: sudo apt install libusb-1.0-0")
    p, i = ctypes.c_void_p, ctypes.c_int
    t = ctypes.POINTER(_Transfer)
    for name, result, arguments in (
            ("libusb_init", i, [ctypes.POINTER(p)]),
            ("libusb_exit", None, [p]),
            ("libusb_open_device_with_vid_pid", p, [p, ctypes.c_uint16, ctypes.c_uint16]),
            ("libusb_close", None, [p]),
            ("libusb_control_transfer", i, [p, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint16,
                                            ctypes.c_char_p, ctypes.c_uint16, ctypes.c_uint]),
            ("libusb_get_device", p, [p]),
            ("libusb_get_device_descriptor", i, [p, ctypes.c_char_p]),
            ("libusb_get_string_descriptor_ascii", i, [p, ctypes.c_uint8, ctypes.c_char_p, i]),
            ("libusb_get_device_speed", i, [p]),
            ("libusb_get_max_packet_size", i, [p, ctypes.c_ubyte]),
            ("libusb_claim_interface", i, [p, i]),
            ("libusb_release_interface", i, [p, i]),
            ("libusb_alloc_transfer", t, [i]),
            ("libusb_free_transfer", None, [t]),
            ("libusb_submit_transfer", i, [t]),
            ("libusb_cancel_transfer", i, [t]),
            ("libusb_handle_events_timeout_completed", i, [p, p, p])):
        f = getattr(u, name)
        f.restype, f.argtypes = result, arguments
    if sys.platform == "darwin":
        # One context that is never ended.  On macOS libusb stops its event thread when a program's last context
        # ends, and that can hang for ever if a device leaves USB at the same moment: libusb_exit() then waits
        # for the thread, and the thread, which has been told of the device, waits for a lock (seen with libusb
        # 1.0.30 on this board, which leaves USB every time its firmware starts).  With this context left, the
        # thread is not stopped.
        global _keep
        _keep = ctypes.c_void_p()
        if u.libusb_init(ctypes.byref(_keep)):
            _keep = None
    _lib = u
    return u


def open_device(vid, pid, wait=0.0):
    """The device with these IDs as a Device, or None if it is not on USB (or this user may not open it).
    wait: how many seconds to keep looking."""
    u = library()
    ctx = ctypes.c_void_p()
    if u.libusb_init(ctypes.byref(ctx)):
        raise UsbError("libusb did not start")
    end = time.monotonic() + wait
    while True:
        handle = u.libusb_open_device_with_vid_pid(ctx, vid, pid)
        if handle:
            return Device(u, ctx, handle)
        if time.monotonic() >= end:
            u.libusb_exit(ctx)
            return None
        time.sleep(0.1)


def listed(ids):
    """Whether the system lists a device with one of these (vendor, product) IDs, whether or not this program may
    open it.  On Linux this is asked of /sys; on macOS of ioreg, which also lists a device that macOS has not
    let connect (its accessory security), a device that libusb does not see at all.  False elsewhere."""
    if sys.platform == "darwin":
        try:
            text = subprocess.run(["/usr/sbin/ioreg", "-p", "IOUSB", "-l", "-w0"], capture_output=True, text=True,
                                  timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return in_ioreg(text, ids)
    for path in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        try:
            with open(path) as f:
                vid = int(f.read(), 16)
            with open(path[:-len("idVendor")] + "idProduct") as f:
                pid = int(f.read(), 16)
        except (OSError, ValueError):
            continue
        if (vid, pid) in ids:
            return True
    return False


def in_ioreg(text, ids):
    """Whether what "ioreg -p IOUSB -l" printed holds a device with one of these (vendor, product) IDs."""
    vid = pid = None
    for line in text.splitlines() + ["+-o"]:
        if "+-o" in line:                   # the next device begins: was the last one of these?
            if (vid, pid) in ids:
                return True
            vid = pid = None
            continue
        m = re.match(r'[\s|]*"(idVendor|idProduct)" = (\d+)\s*$', line)
        if m and m.group(1) == "idVendor":
            vid = int(m.group(2))
        elif m:
            pid = int(m.group(2))
    return False


class Device:
    """An open USB device."""

    def __init__(self, lib, ctx, handle):
        self.lib, self.ctx, self.handle = lib, ctx, handle
        self.error = None                   # libusb's error for the last get() that failed (-4: the device is gone)

    def close(self):
        if self.handle:
            self.lib.libusb_close(self.handle)
            self.lib.libusb_exit(self.ctx)
            self.handle = None

    def out(self, request, value=0, index=0, data=b"", timeout=1000):
        """A vendor request with data going to the device (or none): the bytes sent, or libusb's error (< 0)."""
        return self.lib.libusb_control_transfer(self.handle, 0x40, request, value, index, data or None, len(data),
                                                timeout)

    def get(self, request, value, index, n, timeout=1000):
        """A vendor request with data coming from the device: the bytes, or None if it failed or came short."""
        buf = ctypes.create_string_buffer(n)
        r = self.lib.libusb_control_transfer(self.handle, 0xC0, request, value, index, buf, n, timeout)
        self.error = None if r == n else r if r < 0 else 0
        return buf.raw if r == n else None

    def serial(self):
        """The device's serial number as it gives it on USB, or None if it has none."""
        d = ctypes.create_string_buffer(18)     # the device descriptor, as libusb read it when the device came
        if self.lib.libusb_get_device_descriptor(self.lib.libusb_get_device(self.handle), d):
            return None
        index = d.raw[16]                       # iSerialNumber: which string it is; 0 = none
        if not index:
            return None
        text = ctypes.create_string_buffer(128)
        n = self.lib.libusb_get_string_descriptor_ascii(self.handle, index, text, len(text))
        return text.raw[:n].decode("ascii", "replace") if n > 0 else None

    def speed(self):
        """The connection as the computer sees it: "480 Mbit/s", "5 Gbit/s" ..."""
        code = self.lib.libusb_get_device_speed(self.lib.libusb_get_device(self.handle))
        return SPEEDS.get(code, "unknown")

    def packet_size(self, endpoint):
        """The size of the endpoint's packets: 1024 bytes at 5 Gbit/s, 512 at 480 Mbit/s."""
        n = self.lib.libusb_get_max_packet_size(self.lib.libusb_get_device(self.handle), endpoint)
        return n if n > 0 else 512 if self.speed() == "480 Mbit/s" else 1024

    def claim(self):
        """Takes interface 0.  False if another program has it."""
        return self.lib.libusb_claim_interface(self.handle, 0) == 0

    def release(self):
        self.lib.libusb_release_interface(self.handle, 0)

    def reader(self, endpoint, sink, size=1 << 20, queue=8):
        return Reader(self, endpoint, sink, size, queue)


def ended_by_empty_packet(n, asked, packet):
    """Whether a read that returned n of the bytes it asked for was ended by an empty packet.

    A read ends when it is full, or when a packet shorter than the endpoint's packet size arrives.  A read that is
    not full and holds a whole number of packets was therefore ended by a packet with nothing in it.  That packet
    does not always arrive alone as a read of 0 bytes: when the data before it filled its last packet, the empty
    packet ends the read that holds that data."""
    return n < asked and n % packet == 0


class Reader:
    """Reads of a bulk endpoint, several kept waiting at the device at once.

    The FPGA samples whether the computer reads or not, so the FX3 must never find the computer without a read
    waiting: `queue` reads of `size` bytes are handed to libusb, and each is handed back to it as soon as its data
    has been passed on.  sink(view) is called with each read's data in the order it arrived; the view is only
    good during the call.  Nothing happens between calls of pump(), which lets libusb work for a few
    milliseconds.

    Once a read has failed, nothing more is passed on: what sink was given is whole up to there.

    The stream's end: set `ending`, have the device send what it holds and then an empty packet, and call pump()
    until `ended` is true: a read came back that an empty packet had ended (see ended_by_empty_packet).  Reads
    are not called off while data may still come, because libusb on macOS reports a read that was called off as
    empty even when part of it had arrived."""

    def __init__(self, device, endpoint, sink, size, queue):
        self.lib, self.ctx, self.sink = device.lib, device.ctx, sink
        self.packet = device.packet_size(endpoint)
        self.pending = self.bytes = self.short_reads = self.failed = 0
        self.interrupted = False            # Ctrl-C came while libusb was in _done(): pump() raises it
        self.failure = None                 # what the first read that failed said
        self.sink_error = None              # what sink raised, if it did: nothing is passed on after that
        self.ending = self.ended = self.stopping = False
        self.last_data = time.monotonic()
        self._callback = _CALLBACK(self._done)      # kept: libusb calls it for as long as a read is out
        self._buffers = [(ctypes.c_ubyte * size)() for _ in range(queue)]
        self._transfers = []
        if ctypes.sizeof(ctypes.c_long) == 8:
            self._tv = _Timeval()
        else:                               # 32-bit systems disagree about struct timeval: all zero ("do not
            self._tv = None                 # wait") reads the same in each, and pump() sleeps instead
            self._zero = (ctypes.c_ubyte * 16)()
        for buf in self._buffers:
            tp = self.lib.libusb_alloc_transfer(0)
            if not tp:
                raise UsbError("libusb could not make a transfer")
            self._transfers.append(tp)
            t = tp.contents
            t.dev_handle, t.endpoint, t.type, t.timeout = device.handle, endpoint, BULK, 0
            t.buffer, t.length, t.callback = ctypes.addressof(buf), size, self._callback
            r = self.lib.libusb_submit_transfer(tp)
            if r:
                self.close()                # the reads already queued are called off
                raise UsbError("a read could not be queued (libusb %d)" % r)
            self.pending += 1

    def _done(self, tp):                    # libusb calls this from inside pump()
        try:
            self._finished(tp)
        except KeyboardInterrupt:           # Python would print it and go on, and the Ctrl-C would be lost
            self.interrupted = True

    def _finished(self, tp):
        t = tp.contents
        self.pending -= 1
        if t.status == COMPLETED and self.failed:
            pass                            # after a read that failed: what follows it would leave a gap
        elif t.status == COMPLETED:
            n = t.actual_length
            if n > 0:
                self.last_data = time.monotonic()
                if self.sink_error is None:
                    try:
                        self.sink(memoryview((ctypes.c_ubyte * n).from_address(t.buffer)))
                    except Exception as e:      # an exception must not travel through libusb
                        self.sink_error = e
            if self.ending and ended_by_empty_packet(n, t.length, self.packet):
                self.ended = True           # the empty packet after the last data
            elif not self.ending and n != t.length:
                self.short_reads += 1
            self.bytes += n
            if not self.stopping and not self.ended and self.lib.libusb_submit_transfer(tp) == 0:
                self.pending += 1
        elif t.status != CANCELLED:
            if not self.failed:
                self.failure = FAILURES.get(t.status, "libusb transfer status %d" % t.status)
            self.failed += 1

    def pump(self, ms):
        """Lets libusb work for up to that many milliseconds; reads that finish in them reach sink."""
        if self._tv is not None:
            self._tv.tv_sec, self._tv.tv_usec = 0, ms * 1000
            self.lib.libusb_handle_events_timeout_completed(self.ctx, ctypes.byref(self._tv), None)
        else:
            self.lib.libusb_handle_events_timeout_completed(self.ctx, ctypes.byref(self._zero), None)
            time.sleep(0.001)
        if self.interrupted and not self.stopping:
            self.interrupted = False
            raise KeyboardInterrupt

    def close(self):
        """Calls off the reads still waiting and frees them."""
        self.stopping = True
        for tp in self._transfers:
            self.lib.libusb_cancel_transfer(tp)
        end = time.monotonic() + 3.0
        while self.pending > 0 and time.monotonic() < end:
            self.pump(20)
        if self.pending == 0:               # a read libusb still holds must not be freed under it
            for tp in self._transfers:
                self.lib.libusb_free_transfer(tp)
        self._transfers = []
        if self.interrupted:                # a Ctrl-C that came while the reads were called off
            self.interrupted = False
            raise KeyboardInterrupt

