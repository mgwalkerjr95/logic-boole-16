# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""The board on USB: loading it, its settings, and a capture.

The board keeps nothing when it loses power.  Plugged in, its FX3 runs the boot loader in its ROM and waits; the
computer sends the firmware (data/firmware.img) into the FX3's RAM, then has the firmware load the FPGA's design
(data/gateware.bin).  connect() does both when they are needed, which takes about three seconds.

There is a second design for the FPGA, the fast one (data/gateware-fast.bin, fast.py): fewer inputs, more samples
a second, and nothing but that.  capture_fast() loads it and capture() loads the standard one again, each when the
other is running; the thresholds are kept across the change.

docs/protocol.md describes every request and register used here.
"""
import hashlib
from array import array
import os
import shlex
import struct
import sys
import time
import urllib.request

from . import fast as F, trigger as T, usb, words as W
from .capture import CHANNELS, Capture

HERE = os.path.dirname(os.path.abspath(__file__))
FIRMWARE = os.path.join(HERE, "data", "firmware.img")      # an image put here is used as it is ("make install")
GATEWARE = os.path.join(HERE, "data", "gateware.bin")
GATEWARE_FAST = os.path.join(HERE, "data", "gateware-fast.bin")    # the fast design (fast.py)

# The firmware image this software was written for.  It holds code of Infineon's SDK, so where it is not beside
# this file it is downloaded once, checked against this SHA-256 and kept: firmware_image().
FIRMWARE_VERSION = 2
FIRMWARE_URL = ("https://github.com/mgwalkerjr95/logic-boole-16/releases/download/"
                "firmware-2/logic-boole-16-firmware-2.img")
FIRMWARE_SHA256 = "a54e11e68bed209400721c1de51100a6211b33714e28b8639ca0ef8b094a15d9"

VID, PID_FIRMWARE, PID_BOOT = 0x04B4, 0x00F1, 0x00F3    # the FX3's own boot loader, and the firmware
EP_IN = 0x81
MAGIC = b"LBC2"                             # how the firmware's status begins
DESIGN_ID = b"\x4C\x42\x16"                 # the FPGA's registers 00 ... 02
FASTEST = 200e6                             # samples a second with the divider at 0: a sample every 5 ns
BUFFER_KB, BUFFERS = 16, 8                  # the FX3's buffers for the stream
MAX_BYTES = 1 << 30                         # a capture is ended when it has grown to this
# A threshold is the duty cycle of a PWM: the 2.495 V reference divided to 0.2494 of itself, filtered, and
# compared with the probe's signal after the input divider (866 k over 215 k).
FULL_SCALE = 2.495 * 0.2494 * (866 + 215) / 215         # volts at the probe for a duty cycle of 4096 / 4096
SETTLE = 0.15                               # seconds: the filter behind the PWM settles in 120 ms
TICK = 32 / 32768                           # seconds: the firmware counts time in ticks of 0.977 ms
DONE, INIT_B = 27, 45                       # the FPGA's configuration pins, as FX3 GPIO numbers
CFG_READY, CFG_INIT_FELL, CFG_DONE = 4, 8, 16
LINK = {0: "not connected", 1: "12 Mbit/s", 2: "480 Mbit/s", 3: "5 Gbit/s"}
WRITER = ("off", "starting", "waiting for the FX3's flag", "writing", "holding off after a buffer", "stopping",
          "done, waiting for RUN to fall", "?")
PORT_ERRORS = {5: "a word arrived with no buffer to take it", 0x12: "the socket was not active",
               0x13: "the DMA controller was overrun"}
# the FX3's RAM, where an image's sections may go
RAM = range(0x40003000, 0x40080000)
TCM = range(0x00000100, 0x00004000)
IN_USE = "A capture is running: is another program using the board?"
RULE = os.path.join(HERE, "udev", "60-logic-boole.rules")     # in the package, so that pip and pipx install it too
NO_ACCESS = ("The board is on USB, but this user may not open it.  Install the udev rule once: "
             "sudo cp %s /etc/udev/rules.d/ && sudo udevadm control --reload "
             "&& sudo udevadm trigger" % shlex.quote(RULE))
HELD_BACK = ("The board is on USB, but macOS has not let it connect, so no program can use it.  macOS asks \"Allow "
             "accessory to connect?\" when a board it does not know comes to USB: choose Allow, or run the command "
             "again and choose Allow then.  If it does not ask, look at \"Allow accessories to connect\" under "
             "Privacy & Security in System Settings.")


WAITING = ("macOS has not let the board connect yet.  If it asks \"Allow accessory to connect?\", choose Allow: "
           "this waits 20 seconds more.")


def memory():
    """This computer's memory in bytes, or 8 GB if the system does not say."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return 8 << 30


def no_access():
    """Why a board that the system lists cannot be opened: what to do about it differs between the systems."""
    return HELD_BACK if sys.platform == "darwin" else NO_ACCESS


class BoardError(Exception):
    pass


def volts(duty):
    """A threshold's duty cycle (0 ... 4095) as volts at the probe."""
    return duty / 4096 * FULL_SCALE


def duty_for(v):
    d = round(v / FULL_SCALE * 4096)
    if not 0 <= d <= 4095:
        raise BoardError("A threshold is 0 ... %.2f V." % volts(4095))
    return d


def nearest_rate(rate, fastest=FASTEST):
    """The divider for the sample rate nearest to the one asked for, and that rate: fastest / (divider + 1).
    fastest is the design's rate with the divider at 0, which its register 2C gives."""
    if not rate > 0:
        raise BoardError("The sample rate is a positive number.")
    divider = round(fastest / rate) - 1
    if not 0 <= divider < 1 << 24:
        raise BoardError("The sample rate is %.10g MS/s at most and %.3g S/s at least."
                         % (fastest / 1e6, fastest / (1 << 24)))
    return divider, fastest / (divider + 1)


# ---- the firmware's image -------------------------------------------------------------------------------------

def kept_firmware():
    """Where a downloaded firmware image is kept on this computer."""
    folder = os.environ.get("LOGICBOOLE_CACHE")
    if not folder:
        base = (os.path.expanduser("~/Library/Caches") if sys.platform == "darwin"
                else os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"))
        folder = os.path.join(base, "logicboole")
    return os.path.join(folder, "firmware-%d-%s.img" % (FIRMWARE_VERSION, FIRMWARE_SHA256[:12]))


def _download(url):
    with urllib.request.urlopen(url, timeout=30) as answer:
        return answer.read()


def firmware_image(log=None, download=_download):
    """The file of the firmware image to load: the one beside this software if there is one, or the one kept from
    an earlier download, or it is downloaded now (once, about 110 kB), checked and kept."""
    if os.path.exists(FIRMWARE):
        return FIRMWARE
    kept = kept_firmware()
    by_hand = ("Download %s on a computer that has a connection and save it as %s, or build the image "
               "(firmware/README.md)." % (FIRMWARE_URL, kept))
    if os.path.exists(kept):
        with open(kept, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() == FIRMWARE_SHA256:
                return kept
        raise BoardError("%s is not firmware version %d as this software knows it.  Remove it, or: %s"
                         % (kept, FIRMWARE_VERSION, by_hand[0].lower() + by_hand[1:]))
    if log:
        log("firmware: downloading it, once, from %s" % FIRMWARE_URL)
    try:
        data = download(FIRMWARE_URL)
    except (OSError, ValueError) as e:
        raise BoardError("The board's firmware is not on this computer yet and could not be downloaded (%s).  %s"
                         % (e, by_hand)) from None
    if hashlib.sha256(data).hexdigest() != FIRMWARE_SHA256:
        raise BoardError("What %s gave is not firmware version %d as this software knows it: nothing was kept."
                         % (FIRMWARE_URL, FIRMWARE_VERSION))
    os.makedirs(os.path.dirname(kept), exist_ok=True)
    part = kept + ".part"
    with open(part, "wb") as f:
        f.write(data)
    os.replace(part, kept)
    if log:
        log("firmware: checked and kept as %s" % kept)
    return kept


def parse_image(path):
    """An FX3 firmware image (.img) as (sections, entry address); a section is (address, bytes).

    The file: "CY", two flag bytes, then sections of (length in words, address, data), a length of zero with the
    entry address, and the sum of all data words."""
    if isinstance(path, bytes):             # the image itself (the stand-in board's)
        raw, path = path, "The image"
    else:
        with open(path, "rb") as f:
            raw = f.read()
    if raw[:2] != b"CY":
        raise BoardError("%s is not an FX3 image: it does not start with CY." % path)
    if raw[2] & 1 or raw[3] != 0xB0:
        raise BoardError("%s is not a firmware image for the FX3's RAM." % path)
    at, sections, total = 4, [], 0
    while True:
        if at + 8 > len(raw):
            raise BoardError("%s ends inside a section's header." % path)
        count, address = struct.unpack_from("<II", raw, at)
        at += 8
        if count == 0:
            entry = address
            break
        data = raw[at:at + 4 * count]
        if len(data) != 4 * count:
            raise BoardError("%s ends inside the section at %08X." % (path, address))
        at += 4 * count
        last = address + len(data) - 1
        if not (address in RAM and last in RAM or address in TCM and last in TCM):
            raise BoardError("%s: the section at %08X is outside the FX3's memory." % (path, address))
        total = (total + sum(struct.unpack("<%dI" % count, data))) & 0xFFFFFFFF
        sections.append((address, data))
    if at + 4 > len(raw) or struct.unpack_from("<I", raw, at)[0] != total:
        raise BoardError("%s: the checksum is wrong." % path)
    if not any(a <= entry < a + len(d) for a, d in sections):
        raise BoardError("%s: the entry address is in none of its sections." % path)
    return sections, entry


def load_firmware(boot, path=FIRMWARE, log=None):
    """Writes the image into the FX3's RAM through its boot loader, reads it back, and starts it.

    The boot loader takes vendor request A0 with the address in wValue (low half) and wIndex (high half): with
    data going out it writes memory, coming in it reads memory, and with no data it jumps to the address."""
    sections, entry = parse_image(path)     # a bad file fails before the FX3 is touched
    for address, data in sections:
        for i in range(0, len(data), 4096):         # 4096 bytes is the most the boot loader takes at once
            part, at = data[i:i + 4096], address + i
            if boot.out(0xA0, at & 0xFFFF, at >> 16, part) != len(part):
                raise BoardError("The boot loader refused the firmware at %08X." % at)
            if boot.get(0xA0, at & 0xFFFF, at >> 16, len(part)) != part:
                raise BoardError("The firmware read back differently at %08X: nothing was started." % at)
    if log:
        log("firmware: %d bytes written to the FX3's RAM and read back" % sum(len(d) for _, d in sections))
    boot.out(0xA0, entry & 0xFFFF, entry >> 16)     # the FX3 leaves USB as it starts: no answer to wait for


# ---- the board ---------------------------------------------------------------------------------------------------

def look(backend=usb):
    """What is on USB, without changing anything: a dict with "state" and "text", and what else is known.

    state   none          no board
            noaccess      the system lists it but this program may not open it (Linux: the USB rule is not
                          installed; macOS: the board is not an accessory that macOS has let connect)
            boot          the board waits for its firmware
            other         a device with the board's IDs that does not answer as its firmware
            unconfigured  the firmware runs, the FPGA holds no design (or not this one)
            inuse         a capture is running (another program's)
            ready
    "serial" is the board's serial number on USB, if its firmware gives one.
    """
    dev = backend.open_device(VID, PID_FIRMWARE)
    if dev is None:
        boot = backend.open_device(VID, PID_BOOT)
        if boot is not None:
            boot.close()
            return {"state": "boot", "text": "The board is connected and waits for its firmware."}
        if backend.listed(((VID, PID_FIRMWARE), (VID, PID_BOOT))):
            return {"state": "noaccess", "text": no_access()}
        return {"state": "none", "text": "No board on USB."}
    board = Board(dev)
    try:
        try:
            s = board.status()
        except BoardError as e:
            return {"state": "other", "text": str(e)}
        out = {"firmware": s["version"], "link": dev.speed()}
        serial = dev.serial()
        if serial:
            out["serial"] = serial
        slow = ("  The link is USB 2.0 only (the cable, the port, or something between them): busy signals will "
                "overflow." if out["link"].endswith("Mbit/s") else "")
        if s["flags"] & 0x30:               # a stream is started or RUN is high: leave the FPGA alone
            out.update(state="inuse", text="Another program is capturing from the board.")
            return out
        r = board.registers() if s["pins"] >> DONE & 1 else None
        if r is None:
            out.update(state="unconfigured", text="The firmware runs (USB at %s); the FPGA is not loaded."
                       % out["link"])
            return out
        out.update(state="ready", design=r["version"], threshold_a=round(volts(r["duty_a"]), 3),
                   threshold_b=round(volts(r["duty_b"]), 3), inputs=r["inputs"],
                   text="Ready: USB at %s.%s" % (out["link"], slow))
        if r["design"] == "fast":           # the next capture() loads the standard design again
            out.update(fast=True, text="Ready, with the fast design loaded: USB at %s.%s" % (out["link"], slow))
        return out
    finally:
        board.close()


def connect(load=True, log=None, backend=usb, firmware=None, gateware=GATEWARE, reload=False,
            fast_gateware=None):
    """The board, ready for a capture, as a Board.  Close it when done (or use it in a "with").

    load     load the firmware and the FPGA's design if they are not running (about three seconds)
    reload   load both even if they are running: for trying a firmware or a design you have built
    log      a function that is given a line of text for each step, if you want to see them
    firmware an image of your own; otherwise the one this software was written for (firmware_image())
    gateware, fast_gateware     bitstreams of your own for the standard design and for the fast one

    A board that runs the fast design is left as it is: capture() loads the standard design when it needs it.
    """
    dev = backend.open_device(VID, PID_FIRMWARE)
    if dev is not None and reload:
        dev.out(0xB7)                       # back to the boot loader
        dev.close()
        dev = None
        boot = backend.open_device(VID, PID_BOOT, wait=10)
        if boot is None:
            raise BoardError("The board did not return to its boot loader: unplug it and plug it in again.")
        boot.close()
    if dev is None:
        boot = backend.open_device(VID, PID_BOOT)
        if boot is None:
            if backend.listed(((VID, PID_FIRMWARE), (VID, PID_BOOT))):
                raise BoardError(no_access())
            raise BoardError("No board on USB.")
        try:
            if not load:
                raise BoardError("The board waits for its firmware.")
            if firmware is None:            # the stand-in board takes an image of its own making
                firmware = getattr(backend, "image", None) or firmware_image(log)
            load_firmware(boot, firmware, log)
        finally:
            boot.close()
        dev = backend.open_device(VID, PID_FIRMWARE, wait=3)
        if dev is None and backend.listed(((VID, PID_FIRMWARE),)):
            # It runs, and the system keeps it from this program.  On a Mac laptop macOS asks its user about a
            # board it has not seen before, and lets it through when the answer is yes.  The firmware waits 30
            # seconds for the computer in all, so there is time for the answer.
            if sys.platform != "darwin":
                raise BoardError(no_access())
            if log:
                log(WAITING)
            dev = backend.open_device(VID, PID_FIRMWARE, wait=20)
            if dev is None:
                raise BoardError(no_access())
        elif dev is None:
            dev = backend.open_device(VID, PID_FIRMWARE, wait=7)
        if dev is None:
            raise BoardError("The firmware did not appear on USB: unplug the board and plug it in again.")
    # (the stand-in board has a "bitstream" of its own making for the fast design)
    board = Board(dev, {"standard": gateware,
                        "fast": fast_gateware or getattr(backend, "fast_gateware", None) or GATEWARE_FAST})
    try:
        s = board.status()
        if reload or not s["pins"] >> DONE & 1 or board.registers() is None:
            if not load:
                raise BoardError("The FPGA is not loaded.")
            board.load_fpga(gateware, log)
    except BaseException:
        board.close()
        raise
    return board


class Board:
    """The board with its firmware running.  connect() makes one."""

    KEYS = ("version speed flags matrix gpio uart pin_status last ep ms lo hi cfg_bytes cfg_flags cfg_wait "
            "cfg_clocks cfg_ms linkfail resets pib_errors pib_first pib_last kb count pin pin_count i2c "
            "fpga_version fpga_status").split()

    def __init__(self, device, designs=None):
        self.dev = device
        self.designs = {"standard": GATEWARE, "fast": GATEWARE_FAST}       # a file, or the bitstream's bytes
        self.fast_reversed = False          # a bench check of the fast design: the four samples that a pad hands
                                            # over at once are taken in the opposite order (its register 08, bit 4)
        self.designs.update(designs or {})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.dev.close()

    # ---- requests ----

    def status(self):
        """The firmware's status (request B0) as a dict."""
        raw = self.dev.get(0xB0, 0, 0, 64, 5000)
        if raw is None or raw[:4] != MAGIC:
            raise BoardError("A device with the board's USB IDs runs other firmware.  \"logicboole load\" replaces "
                             "it with this software's; or unplug the board and plug it in again.")
        s = dict(zip(self.KEYS, struct.unpack("<4x HBB HHHHHH I II IHHIH BB IHH BBBB HBB", raw)))
        s["pins"] = s["lo"] | s["hi"] << 32
        return s

    def _send(self, request, value=0, index=0, data=b"", *, what="a request"):
        r = self.dev.out(request, value, index, data, 5000)
        if r != len(data):
            raise BoardError("The firmware refused %s (request %02X, libusb %d)." % (what, request, r))

    def read(self, register, n):
        """n of the FPGA's registers from the given one on (1 ... 64), or None if they do not answer."""
        return self.dev.get(0xD1, register, 0, n, 5000)

    def write(self, register, data):
        """Writes the FPGA's registers from the given one on.  The firmware acknowledges before the write has
        happened: read back what must be sure."""
        self._send(0xD0, register, 0, bytes(data), what="writing the FPGA's register %02X" % register)

    def registers(self):
        """The FPGA's registers as a dict, or None if the FPGA does not answer as one of this project's designs.
        "design" is "standard" or "fast".  The fast design counts frames where the standard one counts samples
        ("limit", "samples"), and has "mode" (its register 08) in place of the divider and the trigger."""
        r = self.read(0, 0x40)
        if r is None or len(r) < 0x40 or r[0:3] not in (DESIGN_ID, F.ID):
            return None
        n = lambda a, b: int.from_bytes(r[a:b], "little")       # noqa: E731
        if r[0:3] == F.ID:
            return {"design": "fast", "version": r[3], "control": r[4], "status": r[5], "inputs": n(6, 8),
                    "mode": r[8], "duty_a": n(12, 14), "duty_b": n(14, 16), "limit": n(16, 21),
                    "samples": n(24, 29), "burst": n(32, 34), "fifo_max": n(34, 36), "words": n(36, 42),
                    "gave_up": r[42] & 1, "fifo_bits": r[43], "fastest": n(44, 46) * 1e6, "locked": r[5] & 1,
                    "run": r[5] >> 1 & 1, "sampling": r[5] >> 2 & 1, "done": r[5] >> 3 & 1,
                    "overflow": r[5] >> 4 & 1, "state": r[5] >> 5, "other_pins": n(0x2E, 0x30)}
        return {"design": "standard",
                "version": r[3], "control": r[4], "status": r[5], "inputs": n(6, 8), "divider": n(8, 11),
                "duty_a": n(12, 14), "duty_b": n(14, 16), "limit": n(16, 21), "samples": n(24, 29),
                "burst": n(32, 34), "fifo_max": n(34, 36), "words": n(36, 42), "gave_up": r[42] & 1,
                "fifo_bits": r[43], "fastest": (r[44] or 100) * 1e6, "locked": r[5] & 1, "run": r[5] >> 1 & 1,
                "sampling": r[5] >> 2 & 1, "done": r[5] >> 3 & 1, "overflow": r[5] >> 4 & 1, "state": r[5] >> 5,
                # the trigger and the capture clock: a design before version 4 has none, and reads 0 here
                # the Pi's lines in place of probes: a design before version 5 has none, and reads 0 here
                "pi": n(0x2E, 0x30),
                "stages": r[0x2D], "clock": r[0x30], "stages_used": r[0x31], "keep": n(0x32, 0x34),
                "triggered": r[0x34] & 1, "trigger_sent": r[0x34] >> 1 & 1, "trigger_upper": r[0x34] >> 2 & 1,
                "waiting": r[0x34] >> 3 & 1, "stages_met": r[0x34] >> 4 & 7, "words_before": n(0x36, 0x38)}

    def _need(self):
        r = self.registers()
        if r is None:
            raise BoardError("The FPGA is not loaded.")
        return r

    def link_errors(self):
        """What the FX3 counted on the 5 Gbit/s link since this was last asked: (PHY errors, link errors)."""
        raw = self.dev.get(0xB9, 0, 0, 16, 5000)
        if raw is None:
            return None
        return struct.unpack("<IHHHHBBH", raw)[1:3]

    # ---- loading ----

    def load_fpga(self, path=GATEWARE, log=None):
        """Has the FX3 load the FPGA with the bitstream at path (a .bin file), and checks that the design runs.
        The FPGA may be running a design already: it is cleared first."""
        if isinstance(path, bytes):         # the bitstream itself (the stand-in board's)
            data, path = path, "The bitstream"
        else:
            with open(path, "rb") as f:
                data = f.read()
        if b"\xAA\x99\x55\x66" not in data[:256]:
            raise BoardError("%s is not an FPGA bitstream (a .bin file is needed)." % path)
        self._send(0xB3, what="starting the FPGA's configuration")
        if not self.status()["cfg_flags"] & CFG_READY:
            raise BoardError("The FPGA did not get ready for its configuration (INIT_B stayed low).")
        for i in range(0, len(data), 4096):
            self._send(0xB4, 0, 0, data[i:i + 4096], what="the bitstream's bytes from %d" % i)
        self._send(0xB5, what="ending the FPGA's configuration")
        s = self.status()
        if s["cfg_flags"] & CFG_INIT_FELL:
            raise BoardError("The FPGA found a CRC error in the bitstream.")
        if s["cfg_bytes"] != len(data) or not s["cfg_flags"] & CFG_DONE or not s["pins"] >> DONE & 1:
            raise BoardError("The FPGA did not start after its configuration (DONE stayed low).")
        time.sleep(0.05)
        r = self.registers()
        if r is None:
            raise BoardError("The FPGA is configured, but its registers do not answer as this project's design.")
        if not r["locked"]:
            raise BoardError("The FPGA is configured, but its clock did not start (the PLL has not locked).")
        if log:
            log("FPGA: %d bytes loaded in %.2f s; design version %d runs"
                % (len(data), s["cfg_ms"] * TICK, r["version"]))

    def use(self, design, log=None):
        """Has the FPGA run the "standard" design or the "fast" one, and returns its registers.  If the other one
        runs, this one is loaded in its place (the log says how long that took) and the thresholds are set again
        to what they were."""
        r = self.registers()
        if r is not None and r["design"] == design:
            return r
        if self.status()["flags"] & 0x30:   # a stream is started or RUN is high: leave the FPGA alone
            raise BoardError(IN_USE)
        source = self.designs[design]
        if not isinstance(source, bytes) and not os.path.exists(source):
            raise BoardError("The %s design's file is not there (%s).  gateware/README.md says how it is built."
                             % (design, source))
        self.load_fpga(source, log)
        if r is not None and (r["duty_a"], r["duty_b"]) != (2048, 2048):       # a design starts with 2048 and 2048
            self.write(0x0C, struct.pack("<HH", r["duty_a"], r["duty_b"]))
        time.sleep(SETTLE)                  # the thresholds' filters lost their PWM while the FPGA was loaded
        r = self._need()
        if r["design"] != design:
            raise BoardError("%s is not the %s design." % (source if isinstance(source, str) else "What was loaded",
                                                           design))
        return r

    def unload(self):
        """Sends the FX3 back to its boot loader: the board is then as it is after being plugged in."""
        self._send(0xB7, what="the return to the boot loader")

    # ---- settings ----

    def thresholds(self):
        """(pod A, pod B) in volts at the probe."""
        r = self._need()
        return volts(r["duty_a"]), volts(r["duty_b"])

    def set_thresholds(self, a=None, b=None):
        """Sets the thresholds (volts at the probe; None leaves one as it is) and waits until they have settled.
        Returns the two that are now set: the nearest steps, which are 0.76 mV apart."""
        r = self._need()
        da = r["duty_a"] if a is None else duty_for(a)
        db = r["duty_b"] if b is None else duty_for(b)
        if (da, db) != (r["duty_a"], r["duty_b"]):
            self.write(0x0C, struct.pack("<HH", da, db))
            time.sleep(SETTLE)
            r = self._need()
            if (r["duty_a"], r["duty_b"]) != (da, db):
                raise BoardError("The FPGA's registers did not take the thresholds.")
        return volts(da), volts(db)

    def inputs(self):
        """The 16 inputs as they are now: bit 0 is A0 ... bit 15 is B7."""
        return self._need()["inputs"]

    # ---- a capture ----

    def capture(self, rate=1e6, seconds=1.0, samples=None, fmt="rle", test=None, a=None, b=None,
                max_bytes=MAX_BYTES, progress=None, cancel=None, trigger=None, before=None,
                samples_before=None, timeout=None, clock=None, glitch=None, pi=None):
        """One capture, as a Capture.

        rate        samples a second; the board takes the nearest of 200 MS/s / 1, / 2, / 3 ...
        seconds     how long.  The FPGA counts the samples, so the length is exact
        samples     how many samples, in place of seconds.  0: no limit; the capture then runs until cancel()
                    says so, until it has max_bytes of data, or until it overflows
        fmt         "rle" (run-length: a word for every change of the inputs) or "raw" (every sample is sent)
        test        None for the probes.  0 ... 15 for the FPGA's test pattern instead: a counter of the samples,
                    shifted right by that many bits.  Every word that arrives is then compared with it
        a, b        thresholds for pod A and pod B in volts, if they are to change
        progress    a function that is given the part done, 0 ... 1, as the capture runs, and None while the
                    capture waits for its trigger
        cancel      a function that is asked now and then; when it returns true the capture ends early

        trigger     what the capture waits for, as text: "B0=r" (B0 rises), "A0=1 A1=0", "A3=1 pulse 1us..5us",
                    "A0=0 for 2ms", "in=r" (the TRIG IN pin), "B7=f then B0=r" ... (trigger.py lists it all), or
                    as a list of stages.  The FPGA looks for it in the samples as it takes them.  The capture
                    is as long as asked for from the trigger sample on, and its `trigger` is that sample's
                    number
        before      with a trigger: how many seconds from before it are kept.  Not given: a tenth of the
                    capture's length.  While it waits the board keeps its last 8184 words (a run-length word
                    is a change of the inputs, a raw word two samples), so busy signals leave less than asked
                    for
        samples_before   the same as a number of samples, in place of before
        timeout     seconds after which a capture that still waits for its trigger is ended (None: it waits
                    until cancel() says so).  It then holds what the board saw last, its `trigger` is None and
                    its `stopped` is "timeout".  With a capture clock: seconds the capture may take in all
        clock       a capture clock, "A0=r", "B3=f" or "A0=e": a sample is kept at each such edge of that
                    input, the circuit's own clock, in place of every so many nanoseconds: what the inputs
                    were just before the edge.  The length is then a number of samples (samples=), and so are
                    samples_before and the times in a trigger; rate is only what the capture and its files
                    give as the clock's frequency
        glitch      a glitch filter: seconds (10 ns ... 1.25 us) for every input, or a dict such as
                    {"A0": 20e-9, "B": 1e-6} (a channel, a pod or "all").  A change of an input counts once the
                    input has held its new level for that long; shorter pulses are left out
        pi          lines of the Raspberry Pi the board sits on, in place of probes: "i2c" (GPIO2 SDA for A0,
                    GPIO3 SCL for A1), "uart" (GPIO14 TXD for A2, GPIO15 RXD for A3), "spi" (GPIO10 MOSI for A4,
                    GPIO9 MISO for A5, GPIO11 SCLK for A6, GPIO8 CE0 for A7, GPIO7 CE1 for B0), "all", single
                    lines such as "scl" or "gpio14", several with commas, or a list of them.  The line's solder
                    jumper on the board has to be closed: an open one reads low.  A Pi line is 3.3 V logic with
                    no threshold.  The trigger, the capture clock and the glitch filter work on it under the
                    probe's name ("A1=f": SCL falls), and the capture's `names` say which channels they are

        Look at the capture's `problems` (or `ok`) before trusting it, and at `overflow`: an overflowed capture is
        shorter than asked for, but whole up to its last sample.  The board passes on 100 million words a second
        at the most, and USB a little less: more than that overflows.  The raw format at 200 MS/s is that much,
        so it is for short captures."""
        if fmt not in ("rle", "raw"):
            raise BoardError('The format is "rle" or "raw".')
        if test is not None and not 0 <= test <= 15:
            raise BoardError("The test pattern's shift is 0 ... 15.")
        s = self.status()
        if not s["pins"] >> DONE & 1:
            raise BoardError("The FPGA is not loaded.")
        r = self.use("standard")            # (loads it if the fast design runs)
        divider, rate = nearest_rate(rate, r["fastest"])
        clock_reg, clock = T.clock_register(clock)
        stages = T.stages_of(trigger, None if clock_reg else rate)
        filters = T.filter_samples(glitch)
        lines = T.pi_lines(pi)
        if lines and r["version"] < T.PI_DESIGN:
            raise BoardError("The FPGA's design (version %d) does not take the Pi's lines.  "
                             "\"logicboole load\" loads the design that comes with this software." % r["version"])
        if (stages or clock_reg or any(filters)) and not r["stages"]:
            raise BoardError("The FPGA's design (version %d) has no trigger, capture clock or glitch filter.  "
                             "\"logicboole load\" loads the design that comes with this software." % r["version"])
        if len(stages) > r["stages"]:
            raise BoardError("The FPGA's design has %d trigger stages." % r["stages"])
        if clock_reg and (samples is None or before is not None):
            raise BoardError("With a capture clock there are no times: the capture's length is a number of "
                             "samples (samples), and so is what is kept before the trigger (samples_before).")
        if samples is None:
            samples = round(seconds * rate)
            if samples < 1:
                raise BoardError("The capture is shorter than one sample.")
        if not 0 <= samples < 1 << 40:
            raise BoardError("A capture has less than 2^40 samples.")
        if stages and samples_before is None:
            samples_before = samples // 10 if before is None else round(before * rate)
        if not stages:
            samples_before = 0
        if samples_before < 0:
            raise BoardError("What is kept before the trigger cannot be less than nothing.")
        keep = T.ring_words(samples_before, fmt, (1 << r["fifo_bits"]) - 8)
        if r["state"] or s["flags"] & 0x30:
            raise BoardError(IN_USE)
        if a is not None or b is not None:
            self.set_thresholds(a, b)
        control = (1 if fmt == "rle" else 0) | (2 | test << 4 if test is not None else 0)
        self.write(0x04, [control])
        self.write(0x08, divider.to_bytes(3, "little"))
        self.write(0x10, samples.to_bytes(5, "little"))
        self.write(0x20, (BUFFER_KB * 256).to_bytes(2, "little"))
        coded = [T.stage_registers(st) for st in stages]
        if r["stages"]:                     # version 4 on: the capture clock, the trigger, the glitch filters
            self.write(0x30, bytes([clock_reg, len(stages)]) + keep.to_bytes(2, "little"))
            for k, (mode, body) in enumerate(coded):
                self.write(0x38 + k, [mode])
                self.write(0x40 + 16 * k, body)
            self.write(0x80, bytes(filters))
        if r["version"] >= T.PI_DESIGN:     # version 5 on: the Pi's lines
            self.write(0x2E, lines.to_bytes(2, "little"))
        r = self._need()
        took = (r["control"], r["divider"], r["limit"], r["burst"]) == (control, divider, samples, BUFFER_KB * 256)
        if r["stages"]:
            took = took and (r["clock"], r["stages_used"], r["keep"]) == (clock_reg, len(stages), keep)
            for k, (mode, body) in enumerate(coded):
                took = took and self.read(0x38 + k, 1) == bytes([mode]) and self.read(0x40 + 16 * k, 16) == body
            took = took and self.read(0x80, 16) == bytes(filters)
        took = took and r["pi"] == lines
        if not took:
            raise BoardError("The FPGA's registers did not take the capture's settings.")
        self._send(0xC0, what="starting the FX3's parallel port")
        if s["speed"] == 3:
            self.link_errors()              # clears the counts
        if not self.dev.claim():
            raise BoardError("Another program has the board open.")
        parts = []                          # each read's data, copied as it arrives: nothing here grows or moves
        reader, finished = None, False
        waiting = bool(stages)              # for the trigger
        watched = waiting or bool(clock_reg)            # how far the capture is: the FPGA's counts say, not the time
        limit = samples / rate * 1.2 + 3 if samples and not watched else None      # seconds: later, something is wrong
        why = "time"
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            self._send(0xC3, what="stopping the old stream")
            self._send(0xC2, BUFFER_KB, BUFFERS, what="starting the stream")
            reader = self.dev.reader(EP_IN, lambda view: parts.append(bytes(view)))
            self._send(0xC4, 1, what="raising RUN")
            t0 = asked = time.monotonic()
            if progress is not None and waiting:
                progress(None)
            while not reader.failed and reader.sink_error is None:
                reader.pump(5)
                t = time.monotonic()
                if cancel is not None and cancel():
                    why = "stopped"
                    break
                if reader.bytes >= max_bytes:
                    why = "size"
                    break
                if limit is not None and t - t0 >= limit:
                    break
                if timeout is not None and (waiting or clock_reg) and t - t0 >= timeout:
                    why = "timeout"
                    break
                if t - asked >= 0.02:       # the FPGA's status comes with the firmware's: "done" is bit 3
                    asked = t
                    raw = self.dev.get(0xB0, 0, 0, 64, 1000)
                    if raw is not None and raw[7] & 64 and raw[63] & 8:
                        why = "done"
                        break
                    part = min((t - t0) * rate / samples, 1.0) if samples else None
                    if watched:             # registers 18 ... 37: the samples from the trigger on, and its status
                        now = self.dev.get(0xD1, 0x18, 0, 0x20, 1000)
                        if now is not None and len(now) == 0x20:
                            if waiting and now[0x1C] & 1:
                                waiting = False
                                if samples and not clock_reg:
                                    limit = t - t0 + samples / rate * 1.2 + 3
                            if samples:
                                part = min(int.from_bytes(now[:5], "little") / samples, 1.0)
                    if progress is not None and (waiting or part is not None):
                        progress(None if waiting else part)
            t1 = time.monotonic()
            reader.ending = True
            self._send(0xC4, 0, what="lowering RUN")        # the FPGA sends what it still holds
            self._send(0xC5, what="ending the stream")      # the FX3 passes on its last buffer, then an empty packet
            reader.last_data = time.monotonic()
            while (not reader.ended and not reader.failed and time.monotonic() - reader.last_data < 1.0
                   and time.monotonic() - t1 < 5.0):
                reader.pump(5)
            finished = True
        finally:
            if not finished:                # it failed part-way: RUN low, whatever happened
                self.dev.out(0xC4, 0, 0, b"", 1000)
            if reader is not None:
                reader.close()
            self.dev.out(0xC3, 0, 0, b"", 1000)             # the stream stopped
            self.dev.release()
        if reader.sink_error is not None:
            raise reader.sink_error
        if reader.failed:
            raise BoardError("Reading from the board failed after %d bytes (%s)." % (reader.bytes, reader.failure))
        s = self.status()
        link = self.link_errors() if s["speed"] == 3 else None
        r = self._need()
        data = b"".join(parts)
        del parts
        if len(data) % 4:
            data = data[:len(data) - len(data) % 4]
        count = len(data) // 4
        early = why in ("stopped", "size", "timeout")

        problems = []
        if not reader.ended:
            problems.append("the end of the data did not arrive")
        if why == "time":
            problems.append("the FPGA did not finish in the time the capture should take")
        if count != r["words"]:
            problems.append("%d words arrived, the FPGA sent %d" % (count, r["words"]))
        if s["pib_errors"]:
            code = s["pib_first"] & 0x3F
            problems.append("%d errors at the FX3's parallel port: the first, %s"
                            % (s["pib_errors"], PORT_ERRORS.get(code, "code %d" % code)))
        if r["gave_up"]:
            problems.append("the FPGA threw away data that the FX3 did not take")
        # How many samples the words hold: all that were taken, unless the capture overflowed.  With a trigger the
        # FPGA counts from the trigger sample on, and says which word holds that sample: the samples in the words
        # before it come on top.  A capture that ended while it waited holds only such samples
        held = W.samples_held(data, fmt)
        at = None                           # the trigger sample's number
        if stages and r["triggered"]:
            if r["trigger_sent"] and r["words_before"] < count:
                at = W.samples_held(data[:4 * r["words_before"]], fmt) + (fmt == "raw" and r["trigger_upper"])
            else:
                problems.append("the word with the trigger sample did not arrive")
        taken = held if stages and at is None else (at or 0) + r["samples"]
        if fmt == "raw" and held == taken + 1:
            held -= 1                       # an odd number of samples: the last word repeats the last one
        if r["overflow"] or s["pib_errors"]:
            if held > taken:
                problems.append("the words hold %d samples, more than the %d taken" % (held, taken))
        elif held != taken or (samples and not early and r["samples"] != samples):
            problems.append("%s samples asked for, %d taken, %d arrived"
                            % (samples or "no number of", r["samples"], held - (at or 0)))
        if test is not None and not problems:
            if not stages:
                wrong = W.check_pattern(data, fmt, test, held)
            elif at is not None:
                wrong = W.check_pattern_triggered(data, fmt, test, held, r["words_before"])
            else:                           # not triggered: the FPGA did not count these samples, and a raw
                wrong = W.check_pattern_triggered(data, fmt, test, held, count)     # word may repeat the last
                if wrong and fmt == "raw" and not W.check_pattern_triggered(data, fmt, test, held - 1, count):
                    wrong, held = None, held - 1
            if wrong:
                problems.append(wrong)
        if stages:                          # of the samples before the trigger, as many as were asked for
            data, gone = W.drop_samples(data, fmt, (held if at is None else at) - samples_before)
            held -= gone
            at = None if at is None else at - gone
        info = {"time": started, "format": fmt, "test": test, "words": len(data) // 4,
                "read_seconds": round(t1 - t0, 3),
                "usb": LINK.get(s["speed"], "?"), "fifo_max": r["fifo_max"], "fifo_size": 1 << r["fifo_bits"],
                "samples_asked": samples, "link_errors": link,
                "source": "Logic Boole 16 · %s" % ("test pattern" if test is not None else "probes")}
        if stages:
            info["trigger"] = T.describe(stages, None if clock_reg else rate)
        if clock_reg:
            info["clock"] = clock
        if any(filters):
            info["glitch_ns"] = [n * 5 for n in filters]
        if lines:
            info["pi"] = {CHANNELS[k]: "%s (%s)" % names for k, names in T.pi_names(lines).items()}
        cap = Capture.from_words(data, fmt, rate, held,
                                 thresholds=(round(volts(r["duty_a"]), 3), round(volts(r["duty_b"]), 3)),
                                 overflow=bool(r["overflow"]), stopped=why if early else None, problems=problems,
                                 info=info, trigger=at)
        for k, names in T.pi_names(lines).items():
            cap.names[k] = names[1]
        return cap

    # ---- a capture with the fast design ----

    def capture_fast(self, inputs="A0-A3", seconds=0.01, samples=None, fmt="rle", test=None, a=None, b=None,
                     max_bytes=MAX_BYTES, progress=None, cancel=None, log=None, keep=True):
        """One capture with the fast design (fast.py), as a Capture.  The fast design is loaded first if the
        standard one runs.

        inputs      which inputs, and with that how fast: "A0-A3", "A4-A7", "B0-B3" or "B4-B7" at 750 MS/s, "A" or
                    "B" (a pod) at 375 MS/s, "all" at 187.5 MS/s.  The other inputs read low in the capture
        seconds     how long.  The FPGA counts, so the length is exact
        samples     how many samples, in place of seconds.  0: no limit; the capture then runs until cancel()
                    says so, until it has max_bytes of data, or until it overflows
        fmt         "rle" (a word for each 5.33 ns in which an input changed) or "raw" (every sample is sent)
        test        None for the inputs.  0 ... 15 for the FPGA's test pattern instead: a counter of the frames
                    (fast.py), shifted right by that many bits.  Every word that arrives is then compared with it
        a, b        thresholds for pod A and pod B in volts, if they are to change
        progress, cancel    as in capture()
        keep        False, with test: the words are checked but not turned into samples, and the Capture holds one
                    run of the right length.  The test pattern changes in nearly every sample, so two seconds of
                    it at 750 MS/s are 1.5 billion runs (15 GB): the self-test passes False
        A busy signal at these rates makes data faster than the computer can hold it as samples, so a capture
        that keeps its samples takes words up to a 32nd of the computer's memory (256 MB on an 8 GB Raspberry Pi:
        0.7 s of the raw format), and changes up to an 8th of it.  A capture that reaches either ends early,
        whole up to its last sample, with `stopped` "size".

        There is no trigger, glitch filter, capture clock or Pi line here.  Look at the capture's `problems` (or
        `ok`) and at `overflow`: an overflowed capture is shorter than asked for, but whole up to its last sample.
        The raw format makes 93.75 million words a second, nearly all that the board and USB carry, so a long raw
        capture needs a computer that reads without a pause; the run-length format makes more than that only
        from signals that change in most 5.33 ns periods."""
        mode = F.mode_of(inputs)
        if fmt not in ("rle", "raw"):
            raise BoardError('The format is "rle" or "raw".')
        runs_limit = None
        if test is None or keep:            # the samples will be made: keep within the computer's memory
            ram = memory()
            max_bytes = min(max_bytes, max(ram // 32, 16 << 20))
            runs_limit = max(ram // 80, 1 << 20)
        if test is not None and not 0 <= test <= 15:
            raise BoardError("The test pattern's shift is 0 ... 15.")
        if not self.status()["pins"] >> DONE & 1:
            raise BoardError("The FPGA is not loaded.")
        r = self.use("fast", log)
        s = self.status()
        rate = mode.rate(r["fastest"])
        if samples is None:
            samples = round(seconds * rate)
            if samples < 1:
                raise BoardError("The capture is shorter than one sample.")
        frames = -(-samples // mode.per)    # the design takes whole frames: the samples of 5.33 ns
        if not 0 <= frames < 1 << 40:
            raise BoardError("A capture has less than 2^40 frames.")
        if r["state"] or s["flags"] & 0x30:
            raise BoardError(IN_USE)
        if a is not None or b is not None:
            self.set_thresholds(a, b)
        control = (1 if fmt == "rle" else 0) | (2 | test << 4 if test is not None else 0)
        self.write(0x04, [control])
        mode_byte = mode.register | (16 if self.fast_reversed else 0)
        self.write(0x08, [mode_byte])
        self.write(0x10, frames.to_bytes(5, "little"))
        self.write(0x20, (BUFFER_KB * 256).to_bytes(2, "little"))
        r = self._need()
        if (r["control"], r["mode"], r["limit"], r["burst"]) != (control, mode_byte, frames, BUFFER_KB * 256):
            raise BoardError("The FPGA's registers did not take the capture's settings.")
        self._send(0xC0, what="starting the FX3's parallel port")
        if s["speed"] == 3:
            self.link_errors()              # clears the counts
        if not self.dev.claim():
            raise BoardError("Another program has the board open.")
        parts = []                          # each read's data, copied as it arrives
        reader, finished = None, False
        limit = samples / rate * 1.2 + 3 if samples else None       # seconds: later, something is wrong
        why = "time"
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            self._send(0xC3, what="stopping the old stream")
            self._send(0xC2, BUFFER_KB, BUFFERS, what="starting the stream")
            reader = self.dev.reader(EP_IN, lambda view: parts.append(bytes(view)))
            self._send(0xC4, 1, what="raising RUN")
            t0 = asked = time.monotonic()
            while not reader.failed and reader.sink_error is None:
                reader.pump(5)
                t = time.monotonic()
                if cancel is not None and cancel():
                    why = "stopped"
                    break
                if reader.bytes >= max_bytes:
                    why = "size"
                    break
                if limit is not None and t - t0 >= limit:
                    break
                if t - asked >= 0.02:       # the FPGA's status comes with the firmware's: "done" is bit 3
                    asked = t
                    raw = self.dev.get(0xB0, 0, 0, 64, 1000)
                    if raw is not None and raw[7] & 64 and raw[63] & 8:
                        why = "done"
                        break
                    if progress is not None and samples:
                        progress(min((t - t0) * rate / samples, 1.0))
            t1 = time.monotonic()
            reader.ending = True
            self._send(0xC4, 0, what="lowering RUN")        # the FPGA sends what it still holds
            self._send(0xC5, what="ending the stream")      # the FX3 passes on its last buffer, then an empty packet
            reader.last_data = time.monotonic()
            while (not reader.ended and not reader.failed and time.monotonic() - reader.last_data < 1.0
                   and time.monotonic() - t1 < 5.0):
                reader.pump(5)
            finished = True
        finally:
            if not finished:                # it failed part-way: RUN low, whatever happened
                self.dev.out(0xC4, 0, 0, b"", 1000)
            if reader is not None:
                reader.close()
            self.dev.out(0xC3, 0, 0, b"", 1000)             # the stream stopped
            self.dev.release()
        if reader.sink_error is not None:
            raise reader.sink_error
        if reader.failed:
            raise BoardError("Reading from the board failed after %d bytes (%s)." % (reader.bytes, reader.failure))
        s = self.status()
        link = self.link_errors() if s["speed"] == 3 else None
        r = self._need()
        data = b"".join(parts)
        del parts
        if len(data) % 4:
            data = data[:len(data) - len(data) % 4]
        count = len(data) // 4
        early = why in ("stopped", "size")

        problems = []
        if not reader.ended:
            problems.append("the end of the data did not arrive")
        if why == "time":
            problems.append("the FPGA did not finish in the time the capture should take")
        if count != r["words"]:
            problems.append("%d words arrived, the FPGA sent %d" % (count, r["words"]))
        if s["pib_errors"]:
            code = s["pib_first"] & 0x3F
            problems.append("%d errors at the FX3's parallel port: the first, %s"
                            % (s["pib_errors"], PORT_ERRORS.get(code, "code %d" % code)))
        if r["gave_up"]:
            problems.append("the FPGA threw away data that the FX3 did not take")
        # How many frames the words hold: all that were taken, unless the capture overflowed.  The stream is the
        # standard design's with a frame for a sample, so words.py counts and checks it
        held, taken = W.samples_held(data, fmt), r["samples"]
        if fmt == "raw" and held == taken + 1:
            held -= 1                       # an odd number of frames: the last word repeats the last one
        if r["overflow"] or s["pib_errors"]:
            if held > taken:
                problems.append("the words hold %d frames, more than the %d taken" % (held, taken))
        elif held != taken or (frames and not early and taken != frames):
            problems.append("%s frames asked for, %d taken, %d arrived" % (frames or "no number of", taken, held))
        if test is not None and not problems:
            wrong = W.check_pattern(data, fmt, test, held)
            if wrong:
                problems.append(wrong.replace("word", "word (of frames)", 1))
        # the samples.  A capture that ran to its end has as many as were asked for: of its last frame, those
        # beyond that number are left out
        whole = bool(frames) and held == frames
        n = samples if whole else held * mode.per
        if test is not None and not keep:
            values, lengths = (array("H", [0]), array("Q", [n])) if n else (array("H"), array("Q"))
        else:
            values, lengths = F.sample_runs(*W.runs_of(data, fmt, held), mode, n, limit=runs_limit)
            if runs_limit is not None and len(values) >= runs_limit:
                why, early = "size", True   # cut where the changes reached the limit
        info = {"time": started, "format": fmt, "test": test, "words": count, "read_seconds": round(t1 - t0, 3),
                "usb": LINK.get(s["speed"], "?"), "fifo_max": r["fifo_max"], "fifo_size": 1 << r["fifo_bits"],
                "samples_asked": samples, "link_errors": link, "fast": mode.name, "inputs": mode.channels,
                "source": "Logic Boole 16 · fast design · %s" % ("test pattern" if test is not None
                                                                   else "inputs %s" % mode.name)}
        return Capture(rate, values, lengths,
                       thresholds=(round(volts(r["duty_a"]), 3), round(volts(r["duty_b"]), 3)),
                       overflow=bool(r["overflow"]), stopped=why if early else None, problems=problems, info=info)
