# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""The app: a waveform viewer with protocol decoders, in the browser.

This file is the board's side of it.  It serves the pages in web/ on this computer only and does what they ask of
the board: load it, set its thresholds, capture.  Everything shown (drawing, decoding, measuring) is done by the
pages themselves, which also make simulated signals, so the app is of use with no board connected.

    GET  /api/status     the board's state, and how the capture that was asked for is getting on
    POST /api/prepare    load the firmware and the FPGA's design
    POST /api/capture    ?rate=&seconds=&test=&a=&b=   start a capture.  More, each as the command line has it:
                         trigger= ("B7=f then B0=r": the capture waits for it and its length counts from it),
                         before= (seconds from before the trigger to keep) or samples_before=, glitch= ("50ns"),
                         pi= (the Raspberry Pi's lines in place of probes: "i2c", "uart", "spi", "all"),
                         samples= (the length as a number of samples, in place of seconds=),
                         clock= ("A0=r": a sample at each such edge; the length is then samples=)
    POST /api/stop       end the capture that is running: what it has is kept
    GET  /api/data       the last capture's words, as the board sent them (run-length format)
    GET  /api/decoders   sigrok's protocol decoders, if sigrok-cli is installed: their ids and names
    GET  /api/decoder    ?name=   one decoder's channels, options and classes
    POST /api/decode     ?words=&only=&limit=   runs decoders ("i2c scl=A1 sda=A0") on the capture the page
                         sends with the request, as an .lb16 file, and answers with what they found

The board is opened for each of these and closed again, so the command line can be used beside the app as long
as no capture is running.
"""
import json
import os
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import board as B, decode as D, trigger as T, usb

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_BYTES = 128 << 20                       # of one capture: what a browser holds without trouble
MAX_SAMPLES = (1 << 32) - 1                 # the pages count samples in 32 bits
TEST_SHIFT = 8                              # "Built-in test": the pattern steps every 256 samples
BODY_MAX = 1 << 30                          # of a capture the page sends to be decoded
DECODE_ROWS = 50000                         # of one run of the decoders: what the page lists without trouble
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


class App:
    def __init__(self, backend=usb, fake=False):
        self.backend, self.fake = backend, fake
        self.lock = threading.Lock()
        self.job = {"state": "idle"}
        self.data = b""
        self.loading = False
        self.stop = False                   # the capture that is running is to end
        self.tried = False                  # loading was tried since the board was plugged in
        self.load_note = ""

    # ---- loading ----

    def prepare(self):
        """Loads the board.  Returns (whether it worked, what happened as text)."""
        with self.lock:
            if self.loading or self.job["state"] == "running":
                return False, "The board is busy."
            self.loading = True
        log = []
        try:
            B.connect(backend=self.backend, log=log.append).close()
            ok = True
        except (B.BoardError, usb.UsbError, OSError) as e:
            log.append(str(e))
            ok = False
        finally:
            self.loading = False
        self.tried, self.load_note = True, "" if ok else log[-1]
        return ok, "\n".join(log)

    # ---- captures ----

    def start(self, **settings):
        with self.lock:
            if self.job["state"] == "running" or self.loading:
                return False
            self.job = {"state": "running", "got": 0, "want": 1000, "waiting": False}
            self.stop = False
        threading.Thread(target=self._run, kwargs=settings, daemon=True).start()
        return True

    def _run(self, rate, seconds, test, a, b, trigger=None, before=None, samples=None, samples_before=None,
             clock=None, glitch=None, pi=None):
        def progress(part):                 # None: the capture waits for its trigger
            self.job["waiting"] = part is None
            if part is not None:
                self.job["got"] = int(1000 * part)
        try:
            with B.connect(backend=self.backend) as board:
                cap = board.capture(rate=rate, seconds=seconds, samples=samples, fmt="rle",
                                    test=TEST_SHIFT if test else None, a=a, b=b, max_bytes=MAX_BYTES,
                                    progress=progress, trigger=trigger, before=before,
                                    samples_before=samples_before, clock=clock, glitch=glitch, pi=pi,
                                    cancel=lambda: self.stop)
            self.data = cap.words
            self.job = {"state": "done", "got": 1000, "want": 1000, "bytes": len(cap.words), "rate": cap.rate,
                        "samples": cap.samples, "asked": cap.info["samples_asked"], "test": bool(test),
                        "thresholds": cap.thresholds, "overflow": cap.overflow, "stopped": cap.stopped,
                        "problems": cap.problems, "usb": cap.info["usb"], "words": cap.info["words"],
                        "trigger": cap.info.get("trigger"), "trigger_sample": cap.trigger,
                        "clock": cap.info.get("clock"), "glitch_ns": cap.info.get("glitch_ns"),
                        "pi": cap.info.get("pi")}
        except (B.BoardError, usb.UsbError) as e:
            self.job = {"state": "error", "error": str(e)}
        except T.TriggerError as e:
            self.job = {"state": "error", "error": str(e)}
        except Exception as e:              # the page shows it; the server goes on
            self.job = {"state": "error", "error": "%s: %s" % (type(e).__name__, e)}

    # ---- what the page asks every few seconds ----

    def status(self):
        if self.job["state"] == "running":
            board = {"state": "busy", "text": "Capturing."}
        elif self.loading:
            board = {"state": "loading", "text": "Loading the board: its firmware, then the FPGA's design."}
        else:
            try:
                board = B.look(self.backend)
            except (B.BoardError, usb.UsbError) as e:
                board = {"state": "error", "text": str(e)}
            if board["state"] in ("boot", "unconfigured"):
                if not self.tried:          # a board that has just been plugged in is loaded without being asked
                    self.tried = True
                    threading.Thread(target=self.prepare, daemon=True).start()
                    board = {"state": "loading", "text": "Loading the board: its firmware, then the FPGA's design."}
                elif self.load_note:
                    board["text"] += "  Loading it failed: " + self.load_note
            elif board["state"] in ("none", "noaccess"):
                self.tried, self.load_note = False, ""
        return {"fake": self.fake, "board": board, "capture": self.job}

    # ---- sigrok's decoders ----

    @staticmethod
    def decoders():
        try:
            return {"available": True, "decoders": D.decoders()}
        except D.DecodeError as e:          # sigrok-cli is not installed: the page says how to get it
            return {"available": False, "why": str(e), "decoders": []}

    @staticmethod
    def decode(path, q):
        """What the decoders find in the capture at path, for the page: rows of start sample, end sample,
        decoder, class and texts (the longest first)."""
        try:
            limit = max(1, min(int(q.get("limit") or DECODE_ROWS), DECODE_ROWS))
        except ValueError:
            limit = DECODE_ROWS
        only = [w for w in (q.get("only") or "").replace(",", " ").split()]
        found = D.decode(path, q.get("words") or "", only=only, limit=limit, shorter=True)
        return {"decoders": found["decoders"], "total": found["total"],
                "rows": [[r["start_sample"], r["end_sample"], r["decoder"], r["class"], r["texts"]]
                         for r in found["annotations"]]}


def handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def reply(self, code, body, kind="application/json"):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            path = url.path
            if path == "/api/status":
                return self.reply(200, app.status())
            if path == "/api/data":
                return self.reply(200, app.data, "application/octet-stream")
            if path == "/api/decoders":
                return self.reply(200, app.decoders())
            if path == "/api/decoder":
                try:
                    return self.reply(200, D.about(parse_qs(url.query).get("name", [""])[0]))
                except D.DecodeError as e:
                    return self.reply(400, {"error": str(e)})
            name = "index.html" if path == "/" else path.lstrip("/")
            full = os.path.realpath(os.path.join(WEB, name))
            if not full.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(full):
                return self.reply(404, {"error": "not found"})
            with open(full, "rb") as f:
                self.reply(200, f.read(), TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

        def body(self, f=None):
            """Reads what was sent with the request, into the file f or into nothing.  False: too much."""
            left = int(self.headers.get("Content-Length") or 0)
            if left > BODY_MAX:
                self.close_connection = True
                return False
            while left:
                part = self.rfile.read(min(left, 1 << 20))
                if not part:
                    break
                left -= len(part)
                if f is not None:
                    f.write(part)
            return True

        def do_POST(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            mine = self.headers.get("X-LB16") == "1"    # only this app's own pages: another site cannot send it
            if mine and url.path == "/api/decode":
                with tempfile.TemporaryDirectory(prefix="logicboole-") as folder:
                    path = os.path.join(folder, "capture.lb16")
                    with open(path, "wb") as f:
                        if not self.body(f):
                            return self.reply(413, {"error": "the capture is too large to send to the decoders"})
                    try:
                        return self.reply(200, app.decode(path, q))
                    except (ValueError, OSError) as e:      # also what the decoders say of a wrong setting
                        return self.reply(400, {"error": str(e).replace(path, "What the page sent")})
            if not self.body():
                return self.reply(413, {"error": "too much was sent"})
            if not mine:
                return self.reply(403, {"error": "not from the app"})
            if url.path == "/api/capture":
                try:
                    rate = float(q["rate"])
                    clock = T.clock_register(q.get("clock"))[1]
                    if clock and not q.get("samples"):
                        raise B.BoardError("With a capture clock the length is a number of samples.")
                    samples = int(q["samples"]) if q.get("samples") else None
                    seconds = float(q["seconds"]) if samples is None else None
                    a = float(q["a"]) if q.get("a") else None
                    b = float(q["b"]) if q.get("b") else None
                    for v in (a, b):
                        if v is not None:
                            B.duty_for(v)
                    rate = B.nearest_rate(rate)[1]
                    glitch = T.parse_filter(q["glitch"]) if q.get("glitch") else None
                    T.filter_samples(glitch)
                    pi = q.get("pi") or None
                    T.pi_lines(pi)
                    trigger = q.get("trigger") or None
                    if trigger:
                        T.parse(trigger, None if clock else rate)
                    before = float(q["before"]) if trigger and q.get("before") else None
                    ahead = int(q["samples_before"]) if trigger and q.get("samples_before") else None
                    if (before or 0) < 0 or (ahead or 0) < 0 or before is not None and ahead is not None:
                        raise ValueError
                    if clock and before is not None:
                        raise B.BoardError("With a capture clock what is kept before the trigger is a number "
                                           "of samples.")
                    count = round(seconds * rate) if samples is None else samples
                    # with a trigger, what is kept from before it comes on top
                    kept = count + (ahead if ahead is not None else round(before * rate) if before is not None
                                    else count // 10 if trigger else 0)
                    if count < 1 or kept > MAX_SAMPLES:
                        raise B.BoardError("A capture in the app has 1 ... %d samples." % MAX_SAMPLES)
                except T.TriggerError as e:
                    return self.reply(400, {"error": str(e)})
                except (KeyError, ValueError):
                    return self.reply(400, {"error": "rate is a number, and so are seconds or samples, and a, b "
                                                     "and samples_before if given"})
                except B.BoardError as e:
                    return self.reply(400, {"error": str(e)})
                if not app.start(rate=rate, seconds=seconds, test=q.get("test") == "1", a=a, b=b, trigger=trigger,
                                 before=before, samples=samples, samples_before=ahead, clock=clock, glitch=glitch,
                                 pi=pi):
                    return self.reply(409, {"error": "the board is busy"})
                return self.reply(202, app.status())
            if url.path == "/api/stop":
                app.stop = True
                return self.reply(200, app.status())
            if url.path == "/api/prepare":
                ok, log = app.prepare()
                return self.reply(200, {"ok": ok, "log": log})
            self.reply(404, {"error": "not found"})

    return Handler


def serve(backend=usb, fake=False, host="127.0.0.1", port=8016, browser=True):
    app = App(backend, fake)
    try:
        server = ThreadingHTTPServer((host, port), handler(app))
    except OSError as e:
        raise B.BoardError("Port %d cannot be used (%s): is the app running already?  Or try --port."
                           % (port, e.strerror))
    server.daemon_threads = True
    url = "http://localhost:%d" % port
    print("Logic Boole 16 at %s%s  (Ctrl-C stops it)" % (url, ", with a stand-in for the board" if fake else ""))
    if browser:
        threading.Timer(0.3, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
