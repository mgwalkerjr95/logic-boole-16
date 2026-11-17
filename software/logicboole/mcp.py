# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""A Model Context Protocol server: the board as tools for an AI agent.

    logicboole mcp

is started by the agent's program, which then talks to it on the standard input and output, one JSON-RPC message
on a line.  The agent is given these tools:

    board_info       what is on USB and the board's settings, with nothing changed
    set_thresholds   the two pods' thresholds
    read_inputs      the 16 inputs as they are now
    capture          one capture, with a trigger if wanted, saved to a file
    measure          periods, frequencies, pulse lengths and changes read off a saved capture
    decode           what a protocol decoder finds in a saved capture (sigrok's decoders, through sigrok-cli)
    list_decoders    the decoders there are, or what one of them takes
    self_test        the path from the FPGA to the computer, checked with the FPGA's test pattern

The protocol comes in two forms and this speaks both.  Up to its revision 2025-11-25 a program begins with
"initialize"; from 2026-07-28 on there is no beginning, and every request says in its "_meta" which revision it
is in.  A capture is saved as an .lb16 file in the server's folder (--dir, or a folder for temporary files), and
the file's name is what "measure" and "decode" are given: the server keeps nothing else from one request to the
next.

A tool that could not do what was asked answers with the reason as text and "isError", so that the agent can put
it right; only a request that is not the protocol's gets a JSON-RPC error.
"""
import json
import os
import queue
import sys
import tempfile
import threading
import time

from . import __version__, board as B, decode as D, trigger as T, usb
from .capture import CHANNELS, Capture
from .selftest import selftest

MODERN = ("2026-07-28",)                    # revisions with no "initialize": each request names its own
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
META = "io.modelcontextprotocol/"
SERVER = {"name": "logicboole", "title": "Logic Boole 16", "version": __version__}
ENDINGS = ("lb16", "sr", "vcd", "csv")
MOST = (1 << 32) - 1                        # samples in an .lb16 file
INSTRUCTIONS = (
    "Logic Boole 16 is a 16-channel logic analyzer on USB.  Its inputs are A0 ... A7 (pod A) and B0 ... B7 (pod "
    "B); it samples at up to 200 MS/s (a sample every 5 ns).  Each pod has one threshold: set it to about half "
    "the logic voltage of the signals on that pod (1.65 V for 3.3 V logic, 0.9 V for 1.8 V).  `capture` takes a "
    "capture and saves it; its answer names the file, and `measure` and `decode` read that file.  Before using "
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


def tool(name, title, about, *properties, required=(), reads=False):
    schema = {"type": "object", "properties": dict(properties), "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return {"name": name, "title": title, "description": about, "inputSchema": schema,
            "annotations": {"readOnlyHint": reads, "destructiveHint": False, "openWorldHint": False}}


FILE = text("file", "The capture's file, as `capture` named it (.lb16)")
TOOLS = [
    tool("board_info", "Board status",
         "What is on USB and the board's settings, with nothing changed: whether a board is connected, its "
         "thresholds and its 16 inputs as they are now if it is loaded, and whether sigrok-cli (which `decode` "
         "needs) is installed.  A board that has just been plugged in waits for its firmware: any tool that "
         "needs the board loads it, in about three seconds.", reads=True),
    tool("set_thresholds", "Set thresholds",
         "Sets the thresholds, in volts at the probe (0.3 V to 3 V).  An input reads 1 above its pod's "
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
         number("rate", "Samples a second: 1e6 if not given, 200e6 at the most.  The board takes the nearest "
                        "of 200e6 / 1, / 2, / 3 ... and the answer says which"),
         number("seconds", "How long: 1 if not given.  With a trigger, how long from the trigger on"),
         whole("samples", "How many samples, in place of seconds"),
         number("a", "Pod A's threshold in volts, if it is to change"),
         number("b", "Pod B's threshold in volts, if it is to change"),
         text("trigger", "What to wait for, as the trigger text above"),
         number("before", "With a trigger: seconds from before it to keep.  A tenth of the length if not given; "
                          "the board keeps its last 8184 changes of the inputs while it waits"),
         whole("samples_before", "With a trigger: how many samples from before it to keep, in place of `before`.  "
                                 "With a capture clock this is the one to give"),
         number("timeout", "With a trigger: seconds to wait for it, 10 if not given.  When it does not come the "
                           "capture holds what the board saw last, and `triggered` is false"),
         text("clock", "A capture clock in place of the sample rate: \"A0=r\" keeps one sample at each rising "
                       "edge of A0 (f: falling, e: both), for a bus with a clock of its own up to 50 MHz.  The "
                       "length is then `samples`, and times in the answers are sample numbers / rate"),
         text("glitch", "A glitch filter: \"50ns\" for every input, or \"A0=20ns,B=1us\" for a channel and a "
                        "pod (10 ns to 1.25 us).  A change counts once the input held its new level that long"),
         ("raw", {"type": "boolean", "description": "Have every sample sent, not only the changes: for signals "
                                                     "that change at nearly every sample"}),
         texts("save_as", "Full paths of more files to write the capture to, for other programs: .sr (PulseView, "
                          "sigrok-cli), .vcd (GTKWave), .csv, .lb16")),
    tool("measure", "Measure a capture",
         "Reads times off a saved capture.  For each channel asked for: its level at the start, how many "
         "changes, and the period between rising edges, the time it stays high and the time it stays low, each "
         "as min, median and max in seconds, with the frequency (1 / the median period) and the duty.  `list` "
         "adds the changes themselves, one after the other.  All times are in seconds from the capture's start "
         "and are right to within one sample (`sample_seconds`).", FILE,
         texts("channels", "Channels, such as [\"A0\", \"B3\"].  Not given: those that change"),
         number("start", "Look at the capture from this time on, in seconds from its start"),
         number("end", "And up to this time"),
         whole("list", "List this many of the changes too, 500 at the most, as [time, channel, new level]"),
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
         "passed."),
]
BY_NAME = {t["name"]: t for t in TOOLS}
KINDS = {"string": (lambda v: isinstance(v, str), "text"),
         "number": (lambda v: isinstance(v, (int, float)) and not isinstance(v, bool), "a number"),
         "integer": (lambda v: isinstance(v, int) and not isinstance(v, bool)
                     or isinstance(v, float) and v.is_integer(), "a whole number"),
         "boolean": (lambda v: isinstance(v, bool), "true or false"),
         "array": (lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v), "a list of texts")}


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
        right, what = KINDS[props[key]["type"]]
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
            return {"content": [{"type": "text", "text": json.dumps(found)}], "isError": False}
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

    def capture(self, asked, rate=1e6, seconds=None, samples=None, a=None, b=None, trigger=None, before=None,
                samples_before=None, timeout=None, clock=None, glitch=None, raw=False, save_as=()):
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
        for path in save_as:
            if path.rsplit(".", 1)[-1].lower() not in ENDINGS:
                raise ValueError("%s: a capture is saved as .lb16, .sr, .vcd or .csv." % path)
        rate = B.nearest_rate(rate)[1]
        if samples is None:
            seconds = 1.0 if seconds is None else seconds
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
            cap = board.capture(rate=rate, seconds=seconds or 1.0, samples=samples, fmt="raw" if raw else "rle",
                                a=a, b=b, trigger=trigger, before=before, samples_before=samples_before,
                                timeout=timeout, clock=clock,
                                glitch=T.parse_filter(glitch) if glitch else None, progress=progress,
                                cancel=asked.cancelled)
        if asked.cancelled():               # nobody waits for this capture: no file is left behind
            raise ValueError("The request was cancelled.")
        os.makedirs(self.folder, exist_ok=True)
        stem = os.path.join(self.folder, "capture-" + time.strftime("%Y%m%d-%H%M%S"))
        path, n = stem + ".lb16", 1
        while os.path.exists(path):
            n += 1
            path = "%s-%d.lb16" % (stem, n)
        cap.save(path)
        for other in save_as:
            cap.save(other)
        out = {"ok": cap.ok, "file": path, "sample_rate": cap.rate, "sample_seconds": 1 / cap.rate,
               "samples": cap.samples, "seconds": cap.seconds, "thresholds": cap.thresholds}
        if save_as:
            out["also_saved"] = list(save_as)
        if cap.problems:
            out["problems"] = cap.problems
        if cap.overflow:
            out["overflow"] = ("The signals made more data than the board and USB carry: the capture ended early "
                               "and is whole up to its end.  A lower rate avoids it, or raw=true for signals "
                               "that change at nearly every sample.")
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
        out["usb"] = cap.info.get("usb")
        summary = cap.summary()
        out["changing"] = {c["channel"]: {"changes": c["changes"], "first": c["first"], "starts": c["starts"]}
                           for c in summary if c["changes"]}
        out["steady_high"] = [c["channel"] for c in summary if not c["changes"] and c["starts"]]
        out["steady_low"] = [c["channel"] for c in summary if not c["changes"] and not c["starts"]]
        return out

    def measure(self, asked, file, channels=None, start=None, end=None, list=0):
        cap = Capture.load(file)
        found = cap.measure(channels, start=start, end=end, listed=max(0, min(list, 500)))
        found["changes"] = [[c["time"], c["channel"], c["level"]] for c in found["changes"]]
        if cap.info.get("clock"):
            found["clock"] = ("This capture has a sample at each edge %s, not at a steady rate: the times are "
                              "sample numbers / sample_rate." % cap.info["clock"])
        return found

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

    def self_test(self, asked):
        lines = []
        with self.board() as board:
            passed = selftest(board, log=lines.append, scale=0.02 if self.fake else 1.0)
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
