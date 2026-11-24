# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""The app: a waveform viewer with protocol decoders, in the browser.

This file is the board's side of it.  It serves the pages in web/ on this computer only and does what they ask of
the board: load it, set its thresholds, capture.  Everything shown (drawing, decoding, measuring) is done by the
pages themselves, which also make simulated signals, so the app is of use with no board connected.

    GET  /api/status     ?wait=   the board's state, and how the capture that was asked for is getting on, with
                         the settings it was started with.  wait= (seconds, up to 5) holds the answer while a
                         capture runs, until it starts or stops waiting for its trigger, or ends, or that time
                         has passed: the page asks again at once, and no timer of the browser's is needed
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
    POST /api/upload     a capture (an .lb16 file) to be decoded, kept for the requests that follow: answers its id
    POST /api/decode     ?capture=&job=&words=&only=&limit=   runs decoders ("i2c scl=A1 sda=A0") on the capture
                         with that id, or on one sent with the request, and answers with what they found.  job
                         names what asks (an analyzer): a new request with the same job stops the one before
    POST /api/cancel     ?job=   stops that job's decoders

The board is opened for each of these and closed again, so the command line can be used beside the app as long
as no capture is running.  A capture that waits (for its trigger, or for the edges of its capture clock) is
ended when no page has asked for its state for UNWATCHED seconds: the page that started it was closed, and
nothing else could end it.  What it saw is kept, and a page that opens later shows it.  When this program is
stopped (Ctrl-C, or the system ends it) a capture that runs is ended first, so that the board is left free.

Every request must name this computer in its Host header as an address (127.0.0.1, 192.168.1.20) or as
localhost, or by the computer's own name when the app is served to other computers (--host 0.0.0.0).  A web page
elsewhere could otherwise point a name of its own at this computer (DNS rebinding) and reach the app through the
user's browser.  A request that a browser says comes from another site (Sec-Fetch-Site, Origin, Referer) is
refused too, except to open the app's page itself; and requests that change anything must carry the header
X-LB16, which only the app's own pages send.
"""
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import board as B, decode as D, trigger as T, usb

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_BYTES = 128 << 20                       # of one capture's words (the page also limits the changes it holds)
MAX_SAMPLES = (1 << 32) - 1                 # the pages count samples in 32 bits
TEST_SHIFT = 8                              # "Built-in test": the pattern steps every 256 samples
BODY_MAX = 1 << 30                          # of a capture the page sends to be decoded
SMALL_BODY = 1 << 16                        # of what comes with any other request (nothing, as a rule)
DECODE_ROWS = 50000                         # of one run of the decoders: what the page lists without trouble
DECODE_SECONDS = 120                        # sigrok's decoders are stopped after this long
DECODE_JOBS = 4                             # of sigrok's decoders running at once
UPLOADS_KEPT = 4                            # captures kept for sigrok's decoders, the newest
UNWATCHED = 10.0                            # seconds: a capture that waits is ended when no page has asked longer
WAIT_MAX = 5.0                              # seconds a request for the status may be held
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}
SHOWN = ("rate", "seconds", "samples", "test", "a", "b", "trigger", "before", "samples_before", "glitch", "pi",
         "clock")                           # the settings of a capture that the status gives back


def names_for(host):
    """The names a request may give in its Host header, besides an address, for an app served on host."""
    names = {"localhost"}
    if not _loopback(host):                 # served to other computers: they may use this one's name
        own = socket.gethostname().lower().rstrip(".")
        base = own.split(".")[0]
        names |= {own, base, base + ".local"}
    return names


def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def host_ok(header, names):
    """Whether a request's Host header ("localhost:8016", "[::1]:8016") names this computer in a way that no
    other site can borrow: an address, or one of names."""
    host = (header or "").strip().lower()
    if host.startswith("["):                # an IPv6 address, with its port after the bracket
        host = host[1:host.find("]")] if "]" in host else ""
    elif host.count(":") == 1:
        host = host.split(":")[0]
    if host in names:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _origin(url):
    """"http://localhost:8016/x" as "http://localhost:8016", or None."""
    try:
        u = urlparse((url or "").strip())
    except ValueError:
        return None
    return "%s://%s" % (u.scheme.lower(), u.netloc.lower()) if u.scheme and u.netloc else None


class Answer(Exception):
    """Ends a request with this code and error text."""

    def __init__(self, code, text, **more):
        super().__init__(text)
        self.code, self.body = code, dict({"error": text}, **more)


class App:
    def __init__(self, backend=usb, fake=False, unwatched=UNWATCHED, decode_seconds=DECODE_SECONDS):
        self.backend, self.fake = backend, fake
        self.unwatched, self.decode_seconds = unwatched, decode_seconds
        self.lock = threading.Lock()
        self.changed = threading.Condition()    # the capture started or stopped waiting, or ended
        self.job = {"state": "idle"}
        self.jobs = 0                       # captures started, to number them
        self.data = b""
        self.loading = False
        self.stop = False                   # the capture that is running is to end
        self.ended_by = None                # why the server ended it: "unwatched", "closing"
        self.closing = False
        self.thread = None                  # the capture's
        self.seen = time.monotonic()        # when a page last asked for the status
        self.tried = False                  # loading was tried since the board was plugged in
        self.load_note = ""
        self.decode_lock = threading.Lock()
        self.decoding = {}                  # job: the Event that stops its decoders
        self.uploads = OrderedDict()        # id: the path of a capture kept for the decoders
        self.folder = None

    # ---- loading ----

    def prepare(self):
        """Loads the board.  Returns (whether it worked, what happened as text)."""
        with self.lock:
            if self.loading or self.job["state"] == "running" or self.closing:
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

    def start(self, settings=None, **kw):
        """Starts a capture with board.capture()'s settings kw, in a thread of its own.  settings: what the page
        asked for, as it gave it, for the status to give back.  False: the board is busy."""
        with self.lock:
            if self.job["state"] == "running" or self.loading or self.closing:
                return False
            self.jobs += 1
            self.job = {"state": "running", "id": self.jobs, "got": 0, "want": 1000, "waiting": False,
                        "settings": dict(settings or {})}
            self.stop, self.ended_by = False, None
            self.seen = time.monotonic()
            self.thread = threading.Thread(target=self._run, args=(self.job,), kwargs=kw, daemon=True)
            self.thread.start()
        return True

    def _notify(self):
        with self.changed:
            self.changed.notify_all()

    def _cancelled(self):
        """Asked now and then by the capture: whether to end it."""
        if self.stop:
            return True
        job = self.job
        waits = job.get("waiting") or job.get("settings", {}).get("clock")
        if waits and time.monotonic() - self.seen > self.unwatched:
            self.ended_by = "unwatched"     # nobody would see it, and nothing else could end it
            return True
        return False

    def _run(self, job, rate, seconds, test, a, b, trigger=None, before=None, samples=None, samples_before=None,
             clock=None, glitch=None, pi=None):
        def progress(part):                 # None: the capture waits for its trigger
            if job.get("waiting") != (part is None):
                job["waiting"] = part is None
                self._notify()
            if part is not None:
                job["got"] = int(1000 * part)
        try:
            with B.connect(backend=self.backend) as board:
                cap = board.capture(rate=rate, seconds=seconds, samples=samples, fmt="rle",
                                    test=TEST_SHIFT if test else None, a=a, b=b, max_bytes=MAX_BYTES,
                                    progress=progress, trigger=trigger, before=before,
                                    samples_before=samples_before, clock=clock, glitch=glitch, pi=pi,
                                    cancel=self._cancelled)
            self.data = cap.words
            # what is known of it, as the command line saves it in an .lb16 file (Capture._file_info): the page
            # saves it so, and the command line reads its trigger, thresholds, overflow and problems back
            info = json.loads(json.dumps(cap._file_info(), default=str))
            done = {"state": "done", "got": 1000, "want": 1000, "bytes": len(cap.words), "rate": cap.rate,
                    "samples": cap.samples, "asked": cap.info["samples_asked"], "test": bool(test),
                    "thresholds": cap.thresholds, "overflow": cap.overflow, "stopped": cap.stopped,
                    "problems": cap.problems, "usb": cap.info["usb"], "words": cap.info["words"],
                    "trigger": cap.info.get("trigger"), "trigger_sample": cap.trigger,
                    "clock": cap.info.get("clock"), "glitch_ns": cap.info.get("glitch_ns"),
                    "pi": cap.info.get("pi"), "info": info, "fetched": False}
        except (B.BoardError, usb.UsbError) as e:
            done = {"state": "error", "error": str(e)}
        except T.TriggerError as e:
            done = {"state": "error", "error": str(e)}
        except Exception as e:              # the page shows it; the server goes on
            done = {"state": "error", "error": "%s: %s" % (type(e).__name__, e)}
        done.update(id=job["id"], settings=job["settings"])
        if self.ended_by:
            done["ended_by"] = self.ended_by
        self.job = done
        self._notify()

    def close(self, timeout=20.0):
        """Ends what runs, before this program ends: a capture (RUN is lowered and the stream ended before this
        returns, unless the board does not answer for timeout seconds) and sigrok's decoders.  Returns whether a
        capture was running, and whether its thread has ended."""
        self.closing = True
        running = self.job["state"] == "running"
        if running:
            self.ended_by = "closing"
        self.stop = True
        with self.decode_lock:
            for stop in self.decoding.values():
                stop.set()
        self._notify()
        thread = self.thread
        if thread is not None:
            thread.join(timeout)
        ended = thread is None or not thread.is_alive()
        with self.decode_lock:
            self.uploads.clear()
            if self.folder:
                shutil.rmtree(self.folder, ignore_errors=True)
                self.folder = None
        return running, ended

    # ---- what the page asks every few seconds ----

    def status(self, wait=0.0):
        self.seen = time.monotonic()
        if wait > 0 and self.job["state"] == "running":
            job = self.job

            waiting, end = job.get("waiting"), time.monotonic() + min(wait, WAIT_MAX)
            with self.changed:
                while self.job is job and job.get("waiting") == waiting and not self.closing:
                    left = end - time.monotonic()
                    if left <= 0:
                        break
                    self.changed.wait(left)
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
                if not self.tried and not self.closing:     # a board that has just been plugged in is loaded
                    self.tried = True
                    threading.Thread(target=self.prepare, daemon=True).start()
                    board = {"state": "loading", "text": "Loading the board: its firmware, then the FPGA's design."}
                elif self.load_note:
                    board["text"] += "  Loading it failed: " + self.load_note
            elif board["state"] in ("none", "noaccess"):
                self.tried, self.load_note = False, ""
        return {"fake": self.fake, "board": board, "capture": self.job}

    def check_thresholds(self, *wanted):
        """Refuses thresholds the board cannot take (BoardError), before a capture is started for them.  The
        scale is the connected board's revision's.  While the board is busy nothing is checked: the capture will
        be refused anyway, and the board is left alone."""
        if all(v is None for v in wanted) or self.loading or self.job["state"] == "running":
            return
        try:
            found = B.look(self.backend)
        except (B.BoardError, usb.UsbError):
            found = {}
        revision = B.revision_of(found.get("revision"))
        for v in wanted:
            if v is not None:
                revision.check(v)           # within what the board is made for, with the reason if not

    # ---- sigrok's decoders ----

    @staticmethod
    def decoders():
        try:
            return {"available": True, "decoders": D.decoders()}
        except D.DecodeError as e:          # sigrok-cli is not installed: the page says how to get it
            return {"available": False, "why": str(e), "decoders": []}

    def keep(self, write):
        """Keeps a capture for the decoders: write(f) writes it into the file f.  Returns its id.  Only the
        newest UPLOADS_KEPT are kept."""
        with self.decode_lock:
            if self.closing:
                raise Answer(503, "The app is stopping.")
            if self.folder is None:
                self.folder = tempfile.mkdtemp(prefix="logicboole-app-")
            key = secrets.token_hex(8)
            path = os.path.join(self.folder, key + ".lb16")
        with open(path, "wb") as f:
            write(f)
        with self.decode_lock:
            self.uploads[key] = path
            while len(self.uploads) > UPLOADS_KEPT:
                _, old = self.uploads.popitem(last=False)
                try:
                    os.remove(old)
                except OSError:
                    pass
        return key

    def kept(self, key):
        with self.decode_lock:
            path = self.uploads.get(key)
            if path is not None:
                self.uploads.move_to_end(key)
        return path

    def cancel(self, job):
        with self.decode_lock:
            stop = self.decoding.get(job)
        if stop is not None:
            stop.set()
        return stop is not None

    def decode(self, path, q, job=None):
        """What the decoders find in the capture at path, for the page: rows of start sample, end sample,
        decoder, class and texts (the longest first).  job: a name for what asks; a new request with the same
        name stops this one."""
        try:
            limit = max(1, min(int(q.get("limit") or DECODE_ROWS), DECODE_ROWS))
        except ValueError:
            limit = DECODE_ROWS
        only = [w for w in (q.get("only") or "").replace(",", " ").split()]
        stop = threading.Event()
        key = job if job else object()
        with self.decode_lock:
            if key in self.decoding:        # the same analyzer, with new settings: the old run is of no use
                self.decoding[key].set()
            elif len(self.decoding) >= DECODE_JOBS:
                raise Answer(429, "%d of sigrok's decoders are running already: wait for one to end." % DECODE_JOBS)
            self.decoding[key] = stop
        deadline = time.monotonic() + self.decode_seconds
        try:
            found = D.decode(path, q.get("words") or "", only=only, limit=limit, shorter=True,
                             cancel=lambda: stop.is_set() or self.closing or time.monotonic() > deadline)
        except D.DecodeError:
            if stop.is_set() or self.closing:
                raise Answer(409, "Stopped: the decoders were cancelled.", cancelled=True)
            if time.monotonic() > deadline:
                raise Answer(400, "The decoders were stopped after %g seconds: this capture has too much in it "
                                  "for them.  A shorter capture, or a decoder on top that finds less, may do."
                             % self.decode_seconds)
            raise
        finally:
            with self.decode_lock:
                if self.decoding.get(key) is stop:
                    del self.decoding[key]
        return {"decoders": found["decoders"], "total": found["total"],
                "rows": [[r["start_sample"], r["end_sample"], r["decoder"], r["class"], r["texts"]]
                         for r in found["annotations"]]}


def _finite(q, key, kind=float):
    """A number the page sent, or None if it sent none.  ValueError: it is not a finite number."""
    if not q.get(key):
        return None
    v = kind(q[key])
    if not math.isfinite(v):
        raise ValueError(key)
    return v


def handler(app, host="127.0.0.1"):
    names = names_for(host)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        # ---- what every request goes through ----

        def guarded(self, answer):
            """Runs answer(); whatever goes wrong in it is answered as JSON, never with silence."""
            self.answered = False
            try:
                answer()
            except Answer as e:
                self.reply(e.code, e.body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
                self.close_connection = True        # the page has gone: nobody to answer
            except Exception as e:
                self.close_connection = True
                if not self.answered:
                    try:
                        self.reply(500, {"error": "The app's server failed on this request: %s: %s"
                                                  % (type(e).__name__, e)})
                    except OSError:
                        pass

        def do_GET(self):
            self.guarded(self.get)

        def do_POST(self):
            self.guarded(self.post)

        def refused(self, path):
            """True, after answering, if the request does not name this computer as it should (host_ok), or a
            browser says it comes from a page of another site."""
            port = self.server.server_address[1]
            if not host_ok(self.headers.get("Host"), names):
                self.close_connection = True
                self.reply(403, {"error": "Open the app as http://localhost:%d, or by this computer's address."
                                          % port})
                return True
            if self.foreign(path):
                self.close_connection = True
                self.reply(403, {"error": "Refused: this request came from a page of another web site.  Open the "
                                          "app itself, as http://localhost:%d." % port})
                return True
            return False

        def foreign(self, path):
            """Whether the browser that sent the request says it comes from a page of another site.  A program
            such as curl says nothing, and is let through as before.  Opening the app's page from a link
            elsewhere is let through; nothing else is."""
            own = "http://" + (self.headers.get("Host") or "").strip().lower()
            origin = self.headers.get("Origin")
            if origin is not None and _origin(origin) != own:
                return True
            site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
            if site:
                if site in ("same-origin", "none"):
                    return False
                return not (self.command == "GET" and not path.startswith("/api/")
                            and (self.headers.get("Sec-Fetch-Mode") or "").lower() == "navigate")
            referer = self.headers.get("Referer")   # older browsers send no Sec-Fetch-Site
            return bool(referer) and path.startswith("/api/") and _origin(referer) != own

        def reply(self, code, body, kind="application/json"):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body).encode()
            self.answered = True
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def body(self, f=None, most=SMALL_BODY, too_much="too much was sent"):
            """Reads what was sent with the request, into the file f or into nothing."""
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                self.close_connection = True
                raise Answer(411, "Send what goes with the request with its length (Content-Length).")
            raw = (self.headers.get("Content-Length") or "0").strip()
            if not re.fullmatch(r"[0-9]{1,15}", raw):
                self.close_connection = True
                raise Answer(400, "Content-Length is not a number of bytes: %s" % raw[:40])
            left = int(raw)
            if left > most:
                self.close_connection = True
                raise Answer(413, too_much)
            while left:
                part = self.rfile.read(min(left, 1 << 20))
                if not part:
                    self.close_connection = True
                    raise Answer(400, "The request ended before what it said it would send had come.")
                left -= len(part)
                if f is not None:
                    f.write(part)

        # ---- GET ----

        def get(self):
            url = urlparse(self.path)
            path = url.path
            if self.refused(path):
                return
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if path == "/api/status":
                try:
                    wait = max(0.0, min(float(q.get("wait") or 0), WAIT_MAX))
                except ValueError:
                    wait = 0.0
                if wait != wait:            # NaN
                    wait = 0.0
                return self.reply(200, app.status(wait))
            if path == "/api/data":
                if app.job["state"] == "done":
                    app.job["fetched"] = True
                return self.reply(200, app.data, "application/octet-stream")
            if path == "/api/decoders":
                return self.reply(200, app.decoders())
            if path == "/api/decoder":
                try:
                    return self.reply(200, D.about(q.get("name", "")))
                except D.DecodeError as e:
                    return self.reply(400, {"error": str(e)})
            name = "index.html" if path == "/" else path.lstrip("/")
            full = os.path.realpath(os.path.join(WEB, name))
            if not full.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(full):
                return self.reply(404, {"error": "not found"})
            with open(full, "rb") as f:
                self.reply(200, f.read(), TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

        # ---- POST ----

        def post(self):
            url = urlparse(self.path)
            if self.refused(url.path):
                return
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            mine = self.headers.get("X-LB16") == "1"    # only this app's own pages: another site cannot send it
            if mine and url.path == "/api/upload":
                key = app.keep(lambda f: self.body(f, BODY_MAX, "the capture is too large to send to the decoders"))
                return self.reply(200, {"capture": key})
            if mine and url.path == "/api/decode":
                return self.decode(q)
            if not mine:
                self.body(most=BODY_MAX)
                return self.reply(403, {"error": "not from the app"})
            self.body()
            if url.path == "/api/capture":
                return self.capture(q)
            if url.path == "/api/stop":
                app.stop = True
                return self.reply(200, app.status())
            if url.path == "/api/cancel":
                return self.reply(200, {"cancelled": app.cancel(q.get("job") or "")})
            if url.path == "/api/prepare":
                ok, log = app.prepare()
                return self.reply(200, {"ok": ok, "log": log})
            self.reply(404, {"error": "not found"})

        def decode(self, q):
            key = q.get("capture")
            sent = (self.headers.get("Content-Length") or "0").strip()
            if key and sent in ("", "0"):  # one kept from before: nothing is sent with the request
                self.body()
                path = app.kept(key)
                if path is None:            # this program was started again, or newer captures came since
                    raise Answer(404, "The app's server no longer has this capture.", gone=True)
                return self.decoded(path, q)
            with tempfile.TemporaryDirectory(prefix="logicboole-") as folder:
                path = os.path.join(folder, "capture.lb16")
                with open(path, "wb") as f:
                    self.body(f, BODY_MAX, "the capture is too large to send to the decoders")
                return self.decoded(path, q)

        def decoded(self, path, q):
            try:
                return self.reply(200, app.decode(path, q, q.get("job")))
            except Answer:
                raise
            except (ValueError, OSError, KeyError, IndexError, TypeError, struct.error) as e:
                # also what the decoders say of a wrong setting, and of a file that is not a capture
                return self.reply(400, {"error": str(e).replace(path, "What the page sent")})

        def capture(self, q):
            try:
                rate = _finite(q, "rate")
                if rate is None or rate <= 0:
                    raise ValueError("rate")
                clock = T.clock_register(q.get("clock"))[1]
                if clock and not q.get("samples"):
                    raise B.BoardError("With a capture clock the length is a number of samples.")
                samples = int(q["samples"]) if q.get("samples") else None
                seconds = None
                if samples is None:
                    seconds = _finite(q, "seconds")
                    if seconds is None:
                        raise KeyError("seconds")
                a, b = _finite(q, "a"), _finite(q, "b")
                app.check_thresholds(a, b)
                rate = B.nearest_rate(rate)[1]
                glitch = T.parse_filter(q["glitch"]) if q.get("glitch") else None
                T.filter_samples(glitch)
                pi = q.get("pi") or None
                T.pi_lines(pi)
                trigger = q.get("trigger") or None
                if trigger:
                    T.parse(trigger, None if clock else rate)
                before = _finite(q, "before") if trigger else None
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
            except (KeyError, ValueError, OverflowError):
                return self.reply(400, {"error": "rate is a number, and so are seconds or samples, and a, b "
                                                 "and samples_before if given"})
            except B.BoardError as e:
                return self.reply(400, {"error": str(e)})
            shown = {k: q[k] for k in SHOWN if q.get(k)}
            shown["rate"] = rate
            if not app.start(shown, rate=rate, seconds=seconds, test=q.get("test") == "1", a=a, b=b,
                             trigger=trigger, before=before, samples=samples, samples_before=ahead, clock=clock,
                             glitch=glitch, pi=pi):
                return self.reply(409, {"error": "the board is busy"})
            return self.reply(202, app.status())

    return Handler


# ---- the program ----

def _say(text=""):
    """Prints, and goes on if there is nowhere to print to (the terminal was closed: SIGHUP)."""
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        pass


def has_display():
    """Whether a browser can be opened here.  A Raspberry Pi with no screen (or a computer reached over ssh) has
    none: a browser opened there would be a text one, in the terminal."""
    if sys.platform == "darwin" or os.name == "nt":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def addresses(port):
    """How other computers reach this one: by its name on the local network and by its address."""
    out = []
    base = socket.gethostname().split(".")[0]
    if base and base.lower() != "localhost":
        out.append("http://%s.local:%d" % (base, port))
    address = None
    try:                                    # the address of the way out: nothing is sent
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            address = s.getsockname()[0]
    except OSError:
        try:
            address = socket.gethostbyname(socket.gethostname())
        except OSError:
            pass
    if address and not address.startswith("127."):
        out.append("http://%s:%d" % (address, port))
    return out


def serve(backend=usb, fake=False, host="127.0.0.1", port=8016, browser=True):
    app = App(backend, fake)
    try:
        server = ThreadingHTTPServer((host, port), handler(app, host))
    except OSError as e:
        raise B.BoardError("Port %d cannot be used (%s): is the app running already?  Or try --port."
                           % (port, e.strerror))
    server.daemon_threads = True
    everywhere = host in ("", "0.0.0.0", "::")
    url = "http://localhost:%d" % port if everywhere or _loopback(host) else "http://%s:%d" % (
        "[%s]" % host if ":" in host else host, port)
    stand_in = ", with a stand-in for the board" if fake else ""
    others = addresses(port) if everywhere else []
    if others:
        _say("Logic Boole 16 at %s on this computer, and from other computers at %s%s  (Ctrl-C stops it)"
             % (url, " or ".join(others), stand_in))
    else:
        _say("Logic Boole 16 at %s%s  (Ctrl-C stops it)" % (url, stand_in))
    if browser:
        if has_display():
            threading.Timer(0.3, webbrowser.open, [url]).start()
        else:
            _say("There is no display here to open a browser on: open the address above in a browser on "
                 "another computer.")

    def ending(signum, frame):
        raise KeyboardInterrupt
    kinds = [getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, name)]
    before = {}
    for kind in kinds[1:]:                  # SIGINT is a KeyboardInterrupt already
        try:
            before[kind] = signal.signal(kind, ending)
        except (ValueError, OSError):       # not the main thread: whoever started this ends it
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _say()
    finally:
        for kind in kinds:                  # a second Ctrl-C waits for the board to be left free
            try:
                before.setdefault(kind, signal.signal(kind, lambda *_: None))
            except (ValueError, OSError):
                pass
        if app.job["state"] == "running":
            _say("Ending the capture first, so that the board is left free ...")
        running, ended = app.close()
        if running:
            _say("The capture was ended and the board is free." if ended else
                 "The board did not answer: unplug it and plug it in again before the next capture.")
        server.server_close()
        for kind, old in before.items():
            try:
                signal.signal(kind, old)
            except (ValueError, OSError, TypeError):
                pass
