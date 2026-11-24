# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""A Model Context Protocol server: the board as tools for an AI agent.

    logicboole mcp

is started by the agent's program, which then talks to it on the standard input and output, one JSON-RPC message
on a line.  The agent is given these tools:

    board_info       what is on USB and the board's settings, with nothing changed
    set_thresholds   the two pods' thresholds
    read_inputs      the 16 inputs as they are now
    capture          one capture, with a trigger if wanted, saved to a file; or with the fast design (fast.py),
                     4 inputs at 750 MS/s, a pod at 375 or all 16 at 187.5, with no trigger
    measure          periods, frequencies, pulse lengths and changes read off a saved capture
    find             every moment in a saved capture that a trigger's text describes: glitches, gaps, sequences
    detect           what is on each channel of a saved capture: I2C, SPI, serial, a clock, PWM, steady
    plot             a picture of a saved capture (PNG), returned as an image and saved to a file
    decode           what a protocol decoder finds in a saved capture (sigrok's decoders, through sigrok-cli)
    list_decoders    the decoders there are, or what one of them takes
    self_test        the path from the FPGA to the computer, checked with the FPGA's test pattern
    guide            the agent skill's text (how to use the board well), for an agent that has no shell

The protocol comes in two forms and this speaks both.  Up to its revision 2025-11-25 a program begins with
"initialize"; from 2026-07-28 on there is no beginning, and every request says in its "_meta" which revision it
is in.  A capture is saved as an .lb16 file in the server's folder (--dir, or a folder for temporary files), and
the file's name is what "measure", "find", "detect", "plot" and "decode" are given: the server keeps nothing else
from one request to the next.

A tool that could not do what was asked answers with the reason as text and "isError", so that the agent can put
it right; only a request that is not the protocol's gets a JSON-RPC error.
"""
import base64
import json
import os
import queue
import sys
import tempfile
import threading
import time

from . import __version__, board as B, decode as D, fast as F, guide as G, trigger as T, usb
from .capture import CHANNELS, Capture, parse_names, writable
from .selftest import selftest, selftest_fast

MODERN = ("2026-07-28",)                    # revisions with no "initialize": each request names its own
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
META = "io.modelcontextprotocol/"
SERVER = {"name": "logicboole", "title": "Logic Boole 16", "version": __version__}

MOST = (1 << 32) - 1                        # samples in an .lb16 file
INSTRUCTIONS = (
    "Logic Boole 16 is a 16-channel logic analyzer on USB.  Its inputs are A0 ... A7 (pod A) and B0 ... B7 (pod "
    "B); it samples all 16 at up to 200 MS/s (a sample every 5 ns), and with `fast` four of them at 750 MS/s "
    "(1.33 ns), a pod at 375 MS/s or all 16 at 187.5 MS/s, with no trigger: for clocks above 85 MHz, up to 200 "
    "MHz, or timing finer than 5 ns.  Each pod has one threshold: set it to about half "
    "the logic voltage of the signals on that pod (1.65 V for 3.3 V logic, 0.9 V for 1.8 V).  Call `guide` "
    "once before the first capture: it is the full guide to using the board well.  `capture` takes a "
    "capture and saves it; its answer names the file, and `measure`, `find`, `detect`, `plot` and `decode` read "
    "that file (`detect` first, on a bus nobody has described).  Before using "
    "a capture look at `ok`, and at `overflow`, `stopped` and `triggered` if they are there.  An input with "
    "nothing connected reads 0, and a channel that never changes usually means the wrong channel, a probe that "
    "is not on the pin, a missing ground wire or a threshold outside the signal's swing.  Sample at four times "
    "the fastest signal's bit rate or more, ten times for timing; the board sends only the changes, so a high "
    "rate costs little on signals that are mostly idle.  A time read off a capture is right to within one "
    "sample at each of its ends: say so when reporting one.")


def text(name, about):
    return name, {"type": "string", "description": about}


def number(name, about):
    return name, {"type": "number", "description": about}


def whole(name, about):
    return name, {"type": "integer", "description": about}


def texts(name, about):
    return name, {"type": "array", "items": {"type": "string"}, "description": about}


def numbers(name, about):
    return name, {"type": "array", "items": {"type": "number"}, "description": about}


def tool(name, title, about, *properties, required=(), reads=False):
    schema = {"type": "object", "properties": dict(properties), "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return {"name": name, "title": title, "description": about, "inputSchema": schema,
            "annotations": {"readOnlyHint": reads, "destructiveHint": False, "openWorldHint": False}}


FILE = text("file", "The capture's file, as `capture` named it (.lb16), or a sigrok session (.sr)")
TOOLS = [
    tool("board_info", "Board status",
         "What is on USB and the board's settings, with nothing changed: whether a board is connected, its "
         "revision and the range of thresholds it takes (`threshold_range`, volts), its thresholds and its 16 "
         "inputs as they are now if it is loaded, and whether sigrok-cli (which `decode` "
         "needs) is installed.  A board that has just been plugged in waits for its firmware: any tool that "
         "needs the board loads it, in about three seconds.", reads=True),
    tool("set_thresholds", "Set thresholds",
         "Sets the thresholds, in volts at the probe, within the board's `threshold_range` (`board_info` gives "
         "it: 0.3 V to 3 V on a V1.0.0 board).  An input reads 1 above its pod's "
         "threshold.  About half the signals' logic voltage is right: 1.65 for 3.3 V logic, 0.9 for 1.8 V.  "
         "Answers with the two thresholds now set.",
         number("a", "Pod A's threshold (inputs A0 ... A7), in volts"),
         number("b", "Pod B's threshold (inputs B0 ... B7), in volts.  Not given: the same as pod A's"),
         required=("a",)),
    tool("read_inputs", "Read inputs now",
         "The 16 inputs as they are at this moment, and the thresholds they are read against: a quick look at "
         "which lines are high or low, with no capture."),
    tool("capture", "Capture",
         "Takes one capture of the 16 inputs and saves it.  The answer names the file (give it to `measure` and "
         "`decode`), says whether the capture passed its checks (`ok`), and lists for each channel that changed "
         "how often and when first.  With a trigger the board waits for it and the length counts from it.\n"
         "Trigger text: \"B0=r\" B0 rises (f: falls, e: either); \"A0=1 A1=0\" levels; \"A2=f A0=1\" an edge "
         "while a level holds; \"A0=1 for 10us\" a level has lasted that long; \"A0=0 pulse 100ns..1us\" a low "
         "pulse of that length has ended (\"..50ns\": at most, \"1us..\": at least); \"in=r\" the TRIG IN pin; "
         "up to four in turn: \"B7=f then B0=r\".",
         number("rate", "Samples a second: 1e6 if not given, 200e6 at the most (for more, `fast`).  The board "
                        "takes the nearest of 200e6 / 1, / 2, / 3 ... and the answer says which"),
         number("seconds", "How long: 1 if not given (0.01 with `fast`).  With a trigger, how long from the "
                           "trigger on"),
         whole("samples", "How many samples, in place of seconds"),
         number("a", "Pod A's threshold in volts, if it is to change"),
         number("b", "Pod B's threshold in volts, if it is to change"),
         text("trigger", "What to wait for, as the trigger text above"),
         number("before", "With a trigger: seconds from before it to keep.  A tenth of the length if not given; "
                          "the board keeps its last 8184 changes of the inputs while it waits"),
         whole("samples_before", "With a trigger: how many samples from before it to keep, in place of `before`.  "
                                 "With a capture clock this is the one to give"),
         number("timeout", "With a trigger: seconds to wait for it.  With a capture clock: seconds the whole "
                           "capture may take.  10 if not given, for either.  When the trigger does not come the "
                           "capture holds what the board saw last, and `triggered` is false; when a capture "
                           "clock's time runs out the capture ends with what it has (`stopped`)"),
         text("clock", "A capture clock in place of the sample rate: \"A0=r\" keeps one sample at each rising "
                       "edge of A0 (f: falling, e: both), for a bus with a clock of its own up to 50 MHz.  The "
                       "length is then `samples`, and times in the answers are sample numbers / rate.  The "
                       "capture ends after `timeout` (10 seconds if not given) even if it has fewer samples: give "
                       "a longer timeout for a clock that is slow or comes in bursts"),
         text("glitch", "A glitch filter: \"50ns\" for every input, or \"A0=20ns,B=1us\" for a channel and a "
                        "pod (10 ns to 1.25 us).  A change counts once the input held its new level that long"),
         text("pi", "Lines of the Raspberry Pi the board sits on, in place of probes, with no leads: \"i2c\" (SDA "
                    "on A0, SCL on A1), \"uart\" (TXD on A2, RXD on A3), \"spi\" (MOSI A4, MISO A5, SCLK A6, CE0 "
                    "A7, CE1 B0), \"all\", or single lines such as \"scl,gpio14\".  Each line's solder jumper on "
                    "the board has to be closed: an open one reads low.  3.3 V logic, no threshold.  The channels "
                    "are named after the lines (SDA, SCL ...), and the trigger takes those names"),
         text("fast", "Fewer inputs, faster, with the board's second FPGA design, in place of `rate`: \"A0-A3\", "
                      "\"A4-A7\", \"B0-B3\" or \"B4-B7\" at 750 MS/s (a sample every 1.33 ns), \"A\" or \"B\" (a "
                      "pod) at 375 MS/s, \"all\" at 187.5 MS/s.  For clocks above 85 MHz (up to 200 MHz, with the "
                      "threshold at half the logic voltage) and timing finer than 5 ns.  It takes samples and sends "
                      "them: no trigger, glitch filter, capture clock or Pi lines, and the capture starts at once.  "
                      "Give raw=true for signals faster than about 15 MHz.  The inputs not taken read low and are "
                      "listed in `not_taken`.  The next capture without it loads the standard design again"),
         ("raw", {"type": "boolean", "description": "Have every sample sent, not only the changes: for signals "
                                                     "that change at nearly every sample"}),
         texts("save_as", "Full paths of more files to write the capture to, for other programs: .sr (PulseView, "
                          "sigrok-cli), .vcd (GTKWave), .csv, .lb16.  A file that is there already is refused "
                          "before the capture, unless `overwrite` is true"),
         ("overwrite", {"type": "boolean", "description": "Write over files in `save_as` that are there already"}),
         text("names", "Names for channels, saved with the capture: \"A0=SDA,A1=SCL\".  The trigger, `measure`, "
                       "`find`, `plot` and `decode` then take SDA in place of A0, and the answers carry the names")),
    tool("measure", "Measure a capture",
         "Reads times off a saved capture.  For each channel asked for: its level at the start, how many "
         "changes, and the period between rising edges, the time it stays high and the time it stays low, each "
         "as min, median and max in seconds, with the frequency (1 / the median period), the average_frequency "
         "(all the rising edges over the time they span) and the duty.  For a steady clock report "
         "average_frequency: it is precise, where frequency is held to whole samples (a 200 MHz clock at 750 MS/s "
         "has periods of 3 and 4 samples, so its median gives 187.5 MHz); for a bus that sends in bursts report "
         "frequency, as the gaps lower the average.  `list` "
         "adds the changes themselves, one after the other.  All times are in seconds from the capture's start "
         "and are right to within one sample (`sample_seconds`).", FILE,
         texts("channels", "Channels, such as [\"A0\", \"B3\"], or the names the capture gives them.  Not given: "
                           "those that change"),
         number("start", "Look at the capture from this time on, in seconds from its start"),
         number("end", "And up to this time"),
         whole("list", "List this many of the changes too, 500 at the most, as [time, channel, new level]"),
         required=("file",), reads=True),
    tool("find", "Find in a capture",
         "Finds every moment in a saved capture that a trigger's text describes, by the trigger's own rules, so "
         "it finds what the board's trigger would catch: \"A0=0 pulse ..50ns\" every low pulse of 50 ns or less "
         "(glitches), \"A1=1 for 100us\" every time A1 stayed high 100 us (gaps in a bus), \"A2=0 A3=1\" each "
         "stretch of those levels, \"A2=f then A3=r\" A2 falling and A3 rising after it, \"B0=r\" each rising "
         "edge.  Channels by id or by the capture's names.  Each moment has its time (seconds from the capture's "
         "start); with a pulse or levels last, `held` says how long they lasted (and `held` at the top gives "
         "min, median and max over all); with stages, `span` is the time from the first stage.  `total` counts "
         "them all.  Right to within one sample (`sample_seconds`) at each end.", FILE,
         text("text", "What to find, as trigger text: \"A0=0 pulse ..50ns\""),
         number("start", "Look from this time on, in seconds from the capture's start"),
         number("end", "And up to this time"),
         whole("limit", "List this many at the most: 100 if not given, 2000 at the most"),
         required=("file", "text"), reads=True),
    tool("detect", "Detect what is on each channel",
         "Looks at every channel of a saved capture and says what it carries: the I2C, SPI and serial buses it "
         "finds (with their wiring, clock or baud rate, SPI mode, how many bytes, the words for `decode` and "
         "names for the channels), and for each other channel a clock or PWM (frequency, duty), data, or a "
         "steady level.  The wiring is a guess from the signals: check it before reporting it.  Use it first on "
         "a bus nobody has described.", FILE, required=("file",), reads=True),
    tool("plot", "Plot a capture",
         "Draws a saved capture as a PNG picture and returns it: the channels as waveforms over a time axis, "
         "the trigger (a dashed red line), numbered marks at times given, and rows of what decoders found.  For "
         "checking what has been worked out from the numbers, and for showing a person.  A column of pixels "
         "with more than one change in it is drawn as a band: a shorter part (start, end) shows each edge.  "
         "The picture is saved too, and the answer names the file.", FILE,
         texts("channels", "Channels by id or name.  Not given: those that change in the part shown"),
         number("start", "Show from this time on, in seconds from the capture's start"),
         number("end", "And up to this time"),
         numbers("marks", "Times to mark with numbered lines, in seconds from the capture's start"),
         texts("decoders", "A row for each: what a decoder finds, as `decode` takes it: [\"i2c scl=A1 sda=A0\"] "
                           "(needs sigrok-cli)"),
         whole("width", "Pixels across: 1200 if not given, 400 to 4000"),
         text("save_as", "Full path of the PNG to write, a new file.  Not given: a file in the server's folder"),
         required=("file",), reads=True),
    tool("decode", "Decode a protocol",
         "Runs protocol decoders on a saved capture and lists what they found, each with its start and end in "
         "seconds from the capture's start.  The decoders are sigrok's (I2C, SPI, UART, CAN, 1-Wire, JTAG, "
         "SWD and about a hundred more), so sigrok-cli must be installed.  `decoders` is the decoder's name, "
         "then its channels and options as name=value: \"i2c scl=A1 sda=A0\", \"uart rx=B0 baudrate=115200\", "
         "\"spi clk=A0 mosi=A1 miso=A2 cs=A3\".  A second name puts that decoder on top of the first: \"i2c "
         "scl=A1 sda=A0 eeprom24xx\".  `list_decoders` with a name tells a decoder's channels and options.",
         text("file", "The capture's file, as `capture` named it (.lb16), or a sigrok session (.sr)"),
         text("decoders", "The decoder, its channels and its options: \"i2c scl=A1 sda=A0\""),
         texts("only", "List only these classes: [\"address-write\", \"data-write\"], [\"rx-data\"], [\"bit\"].  "
                       "Each row of the answer names its class.  Not given: all that the last decoder found, "
                       "except single bits"),
         number("start", "List what begins at this time or later, in seconds from the capture's start"),
         number("end", "And before this time"),
         whole("limit", "List this many rows at the most: 200 if not given, 2000 at the most.  `total` counts "
                        "them all"),
         required=("file", "decoders"), reads=True),
    tool("list_decoders", "List decoders",
         "The protocol decoders sigrok-cli has, as id and name.  With `name`: that decoder's channels, its "
         "options with their defaults, and the classes of what it reports.",
         text("name", "A decoder's id, such as \"spi\""), reads=True),
    tool("self_test", "Self-test",
         "Checks the path from the FPGA to the computer with nothing connected to the probes: the FPGA sends a "
         "known pattern in place of the inputs, from 1 MS/s to 200 MS/s, and every word that arrives is "
         "compared.  Its captures run for a little over 20 seconds in all: allow a minute.  The answer says what "
         "passed.",
         ("fast", {"type": "boolean", "description": "Check the fast design instead (`capture` with `fast`): its "
                                                      "test pattern with each choice of inputs, in both formats"})),
    tool("guide", "Guide to the board",
         "The full guide to using Logic Boole 16 well: setting thresholds, choosing a rate, triggers, checking a "
         "capture, measuring, finding, decoding, and recipes for common questions.  It is the agent skill, "
         "written for the command line, with a table of the tools that stand for its commands.  Read it once "
         "before the first capture.  With `topic`: only the sections whose headings have that word.",
         text("topic", "A word from a section's heading, such as \"trigger\" or \"recipes\""), reads=True),
]
BY_NAME = {t["name"]: t for t in TOOLS}
KINDS = {"string": (lambda v: isinstance(v, str), "text"),
         "number": (lambda v: isinstance(v, (int, float)) and not isinstance(v, bool), "a number"),
         "integer": (lambda v: isinstance(v, int) and not isinstance(v, bool)
                     or isinstance(v, float) and v.is_integer(), "a whole number"),
         "boolean": (lambda v: isinstance(v, bool), "true or false"),
         "array": (lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v), "a list of texts"),
         "array of numbers": (lambda v: isinstance(v, list) and all(KINDS["number"][0](x) for x in v),
                              "a list of numbers")}


def checked(name, args):
    """A tool's arguments, checked against its schema; what is null counts as not given."""
    schema = BY_NAME[name]["inputSchema"]
    props = schema["properties"]
    if not isinstance(args, dict):
        raise ValueError("The arguments are an object.")
    out = {}
    for key, value in args.items():
        if key not in props:
            raise ValueError("%s takes no argument named %s.  It takes: %s." % (name, key,
                                                                                 ", ".join(props) or "none"))
        if value is None:
            continue
        kind = props[key]["type"]
        if kind == "array" and props[key]["items"]["type"] == "number":
            kind = "array of numbers"
        right, what = KINDS[kind]
        if not right(value):
            raise ValueError("%s is %s." % (key, what))
        out[key] = int(value) if props[key]["type"] == "integer" else value
    for key in schema.get("required", ()):
        if key not in out:
            raise ValueError("%s is not given." % key)
    return out


class Asked:
    """What a tool is told about the request it serves: whether it was cancelled, and where its progress goes."""

    def __init__(self, flag, token, send):
        self.flag, self.token, self.send = flag, token, send
        self.last, self.when = -1.0, 0.0

    def cancelled(self):
        return self.flag.is_set()

    def progress(self, part, message):
        now = time.monotonic()
        if self.token is None or part <= self.last or now - self.when < 0.5 and part < 1:
            return
        self.last, self.when = part, now
        self.send({"jsonrpc": "2.0", "method": "notifications/progress",
                   "params": {"progressToken": self.token, "progress": part, "total": 1, "message": message}})


class Tools:
    def __init__(self, backend=usb, fake=False, folder=None):
        self.backend, self.fake = backend, fake
        self.folder = folder or os.path.join(tempfile.gettempdir(), "logicboole-captures")

    def board(self):
        return B.connect(backend=self.backend, log=lambda line: print(line, file=sys.stderr))

    def call(self, name, args, asked):
        """A tool's answer as the protocol's result: its JSON as text, or the reason it could not be done."""
        try:
            found = getattr(self, name)(asked, **checked(name, args))
            if isinstance(found, str):      # plain text
                content = [{"type": "text", "text": found}]
            elif isinstance(found, list):   # content blocks as they are: text and an image
                content = found
            else:
                content = [{"type": "text", "text": json.dumps(found)}]
            return {"content": content, "isError": False}
        except FileNotFoundError as e:
            said = "%s: there is no such file." % e.filename
        except (B.BoardError, usb.UsbError, ValueError, OSError) as e:
            said = str(e)
        except Exception as e:              # the agent is told; the server goes on
            said = "%s: %s" % (type(e).__name__, e)
        return {"content": [{"type": "text", "text": said}], "isError": True}

    # ---- the tools ----

    def board_info(self, asked):
        found = B.look(self.backend)
        if "inputs" in found:
            found["inputs"] = {c: found["inputs"] >> i & 1 for i, c in enumerate(CHANNELS)}
        if self.fake:
            found["stand_in"] = "This is the software's stand-in for a board: it makes an I2C bus on A0 and A1."
        try:
            found["sigrok_cli"] = D.program()
        except D.DecodeError as e:
            found["sigrok_cli"] = None
            found["decoders"] = str(e)
        found["captures_folder"] = self.folder
        return found

    def set_thresholds(self, asked, a, b=None):
        with self.board() as board:
            a, b = board.set_thresholds(a, a if b is None else b)
        return {"threshold_a": round(a, 3), "threshold_b": round(b, 3)}

    def read_inputs(self, asked):
        with self.board() as board:
            v, (a, b) = board.inputs(), board.thresholds()
        return {"inputs": {c: v >> i & 1 for i, c in enumerate(CHANNELS)},
                "threshold_a": round(a, 3), "threshold_b": round(b, 3)}

    def capture(self, asked, rate=None, seconds=None, samples=None, a=None, b=None, trigger=None, before=None,
                samples_before=None, timeout=None, clock=None, glitch=None, pi=None, fast=None, raw=False,
                save_as=(), names=None, overwrite=False):
        named = parse_names(names) if names else {}
        empty = Capture(1, (), ())
        for k, (_, line) in T.pi_names(T.pi_lines(pi)).items():    # the Pi's lines are named after them
            empty.names[k] = line
        empty.set_names(named)              # a name that cannot be given fails before the capture
        if trigger and empty.names != CHANNELS:     # the trigger may use the names: the board is given ids
            from .search import with_ids
            trigger = with_ids(empty, trigger)
        if fast is not None:                # the fast design: the inputs, the length and the format only
            given = [name for name, value in (("rate", rate), ("trigger", trigger), ("before", before),
                                              ("samples_before", samples_before), ("timeout", timeout),
                                              ("clock", clock), ("glitch", glitch), ("pi", pi)) if value is not None]
            if given:
                raise ValueError("fast goes without %s: the fast design takes samples and sends them, and its rate "
                                 "is set by how many inputs it takes." % " and ".join(given))
            mode = F.mode_of(fast)
        if seconds is not None and samples is not None:
            raise ValueError("Give seconds or samples, not both.")
        if clock and samples is None:
            raise ValueError("With a capture clock the length is a number of samples: give samples.")
        if samples is not None and samples < 1:
            raise ValueError("samples is 1 or more.")
        if seconds is not None and seconds <= 0:
            raise ValueError("seconds is more than 0.")
        if before is not None and samples_before is not None:
            raise ValueError("Give before or samples_before, not both.")
        if (before or 0) < 0 or (samples_before or 0) < 0:
            raise ValueError("before is 0 or more.")
        for path in save_as:                # a file that cannot be written fails before the capture, not after
            writable(path)
            if os.path.exists(path) and not overwrite:
                raise ValueError("%s is there already: give overwrite=true to write over it." % path)
        rate = mode.rate(F.FASTEST) if fast is not None else B.nearest_rate(1e6 if rate is None else rate)[1]
        if samples is None:
            seconds = (0.01 if fast is not None else 1.0) if seconds is None else seconds
        kept = (samples or round(seconds * rate)) + (round(before * rate) if before else samples_before or 0)
        if kept > MOST:
            raise ValueError("A capture here has %d samples at the most: this one would have %d.  A lower rate "
                             "or a shorter time." % (MOST, kept))
        if (trigger or clock) and timeout is None:
            timeout = 10.0

        def progress(part):
            if part is None:
                asked.progress(0.0, "waiting for the trigger")
            else:
                asked.progress(min(part, 1.0), "capturing")

        with self.board() as board:
            if fast is not None:
                cap = board.capture_fast(mode, seconds=seconds or 0.01, samples=samples, fmt="raw" if raw else "rle",
                                         a=a, b=b, progress=progress, cancel=asked.cancelled)
            else:
                cap = board.capture(rate=rate, seconds=seconds or 1.0, samples=samples, fmt="raw" if raw else "rle",
                                    a=a, b=b, trigger=trigger, before=before, samples_before=samples_before,
                                    timeout=timeout, clock=clock,
                                    glitch=T.parse_filter(glitch) if glitch else None, pi=pi,
                                    progress=progress, cancel=asked.cancelled)
        if asked.cancelled():               # nobody waits for this capture: no file is left behind
            raise ValueError("The request was cancelled.")
        if named:
            cap.set_names(named)
        path = self.free_name("capture-" + time.strftime("%Y%m%d-%H%M%S"), ".lb16")
        cap.save(path)
        done = []
        for other in save_as:
            try:
                cap.save(other)
            except (ValueError, OSError) as e:      # the agent is told where the capture is all the same
                raise ValueError("The capture is saved as %s%s, but %s could not be written: %s"
                                 % (path, "".join(" and " + d for d in done), other,
                                    "there is no such folder" if isinstance(e, FileNotFoundError) else e)) from None
            done.append(other)
        out = {"ok": cap.ok, "file": path, "sample_rate": cap.rate, "sample_seconds": 1 / cap.rate,
               "samples": cap.samples, "seconds": cap.seconds, "thresholds": cap.thresholds}
        if save_as:
            out["also_saved"] = list(save_as)
        if cap.problems:
            out["problems"] = cap.problems
        if cap.overflow:
            out["overflow"] = ("The signals made more data than the board and USB carry: the capture ended early "
                               "and is whole up to its end.  A lower rate avoids it, or raw=true for signals "
                               "that change at nearly every sample." + B.slow_link(cap))
        if cap.stopped:
            out["stopped"] = {"size": "The capture had grown too large.",
                              "timeout": "The time given as timeout had passed."}.get(cap.stopped, cap.stopped)
        if cap.info.get("trigger"):
            out["trigger"] = cap.info["trigger"]
            out["triggered"] = cap.trigger is not None
            if cap.trigger is None:
                out["not_triggered"] = "The trigger did not come: these are the last samples the board saw."
            else:
                out["trigger_sample"], out["trigger_seconds"] = cap.trigger, cap.trigger / cap.rate
        if cap.info.get("clock"):
            out["clock"] = cap.info["clock"]
        if cap.info.get("glitch_ns"):
            out["glitch_ns"] = {c: ns for c, ns in zip(CHANNELS, cap.info["glitch_ns"]) if ns}
        if cap.info.get("pi"):
            out["pi"] = cap.info["pi"]
        taken = CHANNELS
        if cap.info.get("fast"):
            taken = cap.info["inputs"]
            out["fast"] = cap.info["fast"]
            out["not_taken"] = [c for c in CHANNELS if c not in taken]
        out["usb"] = cap.info.get("usb")
        summary = [c for c in cap.summary() if c["channel"] in taken]
        out["changing"] = {c["channel"]: {"changes": c["changes"], "first": c["first"], "starts": c["starts"]}
                           for c in summary if c["changes"]}
        out["steady_high"] = [c["channel"] for c in summary if not c["changes"] and c["starts"]]
        out["steady_low"] = [c["channel"] for c in summary if not c["changes"] and not c["starts"]]
        if named:
            out["names"] = {CHANNELS[k]: name for k, name in sorted(named.items())}
        return out

    def free_name(self, stem, ending):
        """A path in the server's folder that no file has yet: stem + ending, or stem-2 + ending ..."""
        os.makedirs(self.folder, exist_ok=True)
        stem = os.path.join(self.folder, stem)
        path, n = stem + ending, 1
        while os.path.exists(path):
            n += 1
            path = "%s-%d%s" % (stem, n, ending)
        return path

    def measure(self, asked, file, channels=None, start=None, end=None, list=0):
        cap = Capture.load(file)
        found = cap.measure(channels, start=start, end=end, listed=max(0, min(list, 500)))
        found["changes"] = [[c["time"], c["channel"], c["level"]] for c in found["changes"]]
        if cap.info.get("clock"):
            found["clock"] = ("This capture has a sample at each edge %s, not at a steady rate: the times are "
                              "sample numbers / sample_rate." % cap.info["clock"])
        return found

    def find(self, asked, file, text, start=None, end=None, limit=100):
        from . import search
        return search.find(Capture.load(file), text, start=start, end=end, limit=limit)

    def detect(self, asked, file):
        from . import detect
        found = detect.detect(Capture.load(file))
        found["note"] = "The wiring is a guess from the signals: check it before reporting it."
        return found

    def plot(self, asked, file, channels=None, start=None, end=None, marks=(), decoders=(), width=1200,
             save_as=None):
        from . import plot as P
        cap = Capture.load(file)
        notes = P.decoded(file, decoders, start, end, cap.seconds)
        png, info = P.plot(cap, channels, start=start, end=end, width=width, marks=marks, annotations=notes,
                           title=os.path.basename(file))
        if save_as is None:
            save_as = self.free_name(os.path.splitext(os.path.basename(file))[0], ".png")
        elif not save_as.lower().endswith(".png"):
            raise ValueError("%s: the picture is saved as .png." % save_as)
        elif os.path.exists(save_as):       # a tool that only reads writes nothing over
            raise ValueError("%s is there already: the picture is not written over a file." % save_as)
        with open(save_as, "wb") as f:
            f.write(png)
        info["file"] = save_as
        if info["busy"]:
            info["bands"] = ("%s change more than once in a pixel in places, drawn as bands: a shorter part "
                             "(start, end) shows each edge." % " ".join(info["busy"]))
        return [{"type": "text", "text": json.dumps(info)},
                {"type": "image", "data": base64.b64encode(png).decode("ascii"), "mimeType": "image/png"}]

    def guide(self, asked, topic=None):
        return G.guide(topic)

    def decode(self, asked, file, decoders, only=None, start=None, end=None, limit=200):
        found = D.decode(file, decoders, only=only, start=start, end=end, limit=max(1, min(limit, 2000)),
                         cancel=asked.cancelled)
        rows = found.pop("annotations")
        several = len({r["decoder"] for r in rows}) > 1
        found["listed"] = len(rows)
        found["columns"] = ["start", "end", "class", "text"]
        found["rows"] = [[r["start"], r["end"], r["decoder"] + "." + r["class"] if several else r["class"],
                          r["text"]] for r in rows]
        return found

    def list_decoders(self, asked, name=None):
        if name is None:
            return {"decoders": {d["id"]: d["name"] for d in D.decoders()}}
        found = D.about(name)
        if len(found["documentation"]) > 1500:
            found["documentation"] = found["documentation"][:1500] + " ..."
        return found

    def self_test(self, asked, fast=False):
        lines = []
        with self.board() as board:
            passed = (selftest_fast if fast else selftest)(board, log=lines.append, scale=0.02 if self.fake else 1.0)
        return {"passed": passed, "captures": lines}


def serve(backend=usb, fake=False, folder=None, stdin=None, stdout=None):
    """Serves until the standard input ends.  stdin and stdout: binary streams, if not the process's own."""
    tools = Tools(backend, fake, folder)
    source = sys.stdin.buffer if stdin is None else stdin
    sink = sys.stdout.buffer if stdout is None else stdout
    printed = sys.stdout
    if stdout is None:
        sys.stdout = sys.stderr             # the standard output carries the protocol and nothing else
    writing = threading.Lock()

    def send(message):
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        with writing:
            try:
                sink.write(data)
                sink.flush()
            except (OSError, ValueError):   # the other end is gone
                pass

    def result(ident, body, modern):
        if modern:
            body = dict(body, resultType="complete", _meta={META + "serverInfo": SERVER})
        send({"jsonrpc": "2.0", "id": ident, "result": body})

    def error(ident, code, said, data=None):
        e = {"code": code, "message": said}
        if data is not None:
            e["data"] = data
        send({"jsonrpc": "2.0", "id": ident, "error": e})

    calls, pending = queue.Queue(), {}      # the tools run one at a time: there is one board

    def work():
        while True:
            item = calls.get()
            if item is None:
                return
            ident, modern, name, args, token, flag = item
            if not flag.is_set():
                body = tools.call(name, args, Asked(flag, token, send))
                if not flag.is_set():       # a cancelled request gets no answer
                    result(ident, body, modern)
            pending.pop(ident, None)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    try:
        for raw in source:
            if not raw.strip():
                continue
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                error(None, -32700, "Parse error")
                continue
            if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                error(None, -32600, "Invalid Request")
                continue
            method, params = msg.get("method"), msg.get("params")
            if not isinstance(method, str):
                continue                    # an answer: this server asks nothing
            if not isinstance(params, dict):
                params = {}
            if "id" not in msg:
                if method == "notifications/cancelled":
                    try:
                        flag = pending.get(params.get("requestId"))
                    except TypeError:
                        flag = None
                    if flag is not None:
                        flag.set()
                continue
            ident = msg["id"]
            if isinstance(ident, bool) or not isinstance(ident, (str, int)):
                error(None, -32600, "Invalid Request: the id is a string or a whole number")
                continue
            meta = params.get("_meta")
            meta = meta if isinstance(meta, dict) else {}
            version = meta.get(META + "protocolVersion")
            modern = version in MODERN
            if version is not None and not modern and version not in LEGACY:
                error(ident, -32022, "Unsupported protocol version",
                      {"supported": list(MODERN + LEGACY), "requested": version})
                continue
            if modern and not isinstance(meta.get(META + "clientCapabilities"), dict):
                error(ident, -32602, "Invalid params: _meta has no %sclientCapabilities" % META)
                continue
            if method == "initialize":
                wanted = params.get("protocolVersion")
                result(ident, {"protocolVersion": wanted if wanted in LEGACY else LEGACY[0],
                               "capabilities": {"tools": {}}, "serverInfo": SERVER,
                               "instructions": INSTRUCTIONS}, False)
            elif method == "server/discover":
                result(ident, {"supportedVersions": list(MODERN + LEGACY), "capabilities": {"tools": {}},
                               "instructions": INSTRUCTIONS}, True)
            elif method == "ping":
                result(ident, {}, modern)
            elif method == "tools/list":
                result(ident, {"tools": TOOLS}, modern)
            elif method == "tools/call":
                name = params.get("name")
                if name not in BY_NAME:
                    error(ident, -32602, "Unknown tool: %s" % name)
                elif ident in pending:
                    error(ident, -32600, "Invalid Request: a request with this id is still running")
                else:
                    pending[ident] = flag = threading.Event()
                    calls.put((ident, modern, name, params.get("arguments") or {}, meta.get("progressToken"),
                               flag))
            else:
                error(ident, -32601, "Method not found: %s" % method)
    except KeyboardInterrupt:
        pass
    finally:
        for flag in list(pending.values()):     # the program that asked is gone: end what is running
            flag.set()
        calls.put(None)
        worker.join(15)
        sys.stdout = printed
    return 0
