# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""Checks of the app's server (logicboole/server.py) with no board:  python3 software/tests/test_server.py

The stand-in board (logicboole/fake.py) takes the captures, and a stand-in for sigrok-cli does the decoding, as in
test_host.py.  What is checked here: a capture that waits for its trigger outlives a reload of the page and is
ended when no page watches it any more; this program ends a running capture before it stops; requests that are
wrong, or that come from another web site, get an answer that says so.
"""
import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from logicboole import decode as D, fake, server   # noqa: E402
from logicboole.capture import Capture              # noqa: E402
from test_host import SHOWN, STAND_IN, FOUND        # noqa: E402  the stand-in for sigrok-cli

LAUNCHER = os.path.join(HERE, "..", "..", "logicboole")
NEVER = "trigger=B3%3Dr"                # the stand-in board's B3 never rises: a trigger that does not come


class Served:
    """An App on the stand-in board, served on a port of its own, and ways to ask it."""

    def start_app(self, **kw):
        self.bus = fake.FakeBus(instant=True)
        self.app = server.App(self.bus, fake=True, **kw)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.handler(self.app))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.app.close, 5)

    def ask(self, path, data=None, headers=None, method=None):
        """(status code, the answer as JSON or bytes)."""
        req = urllib.request.Request(self.url + path, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body, code, kind = r.read(), r.status, r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            with e:
                body, code, kind = e.read(), e.code, e.headers.get("Content-Type", "")
        return code, json.loads(body) if kind.startswith("application/json") else body

    def post(self, path, data=b"", headers=None):
        return self.ask(path, data, dict({"X-LB16": "1"}, **(headers or {})))

    def status(self, **q):
        return self.ask("/api/status" + ("?" + urllib.parse.urlencode(q) if q else ""))[1]

    def until(self, what, seconds=10.0, poll=True):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            s = self.status() if poll else {"capture": self.app.job}
            if what(s):
                return s
            time.sleep(0.02)
        self.fail("it did not get there: %s" % (s,))

    def raw(self, text):
        """Sends text as it is, and returns what comes back (the first line and the JSON)."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(text.encode())
            s.shutdown(socket.SHUT_WR)
            got = b""
            while True:
                part = s.recv(65536)
                if not part:
                    break
                got += part
        head, _, body = got.partition(b"\r\n\r\n")
        return head.split(b"\r\n")[0].decode(), json.loads(body) if body else None


class AWaitThatOutlivesThePage(Served, unittest.TestCase):
    def setUp(self):
        self.start_app(unwatched=0.6)
        self.until(lambda s: s["board"]["state"] == "ready")

    def test_a_reloaded_page_finds_the_wait_and_can_end_it(self):
        code, said = self.post("/api/capture?rate=1000000&seconds=0.02&clock=&" + NEVER)
        self.assertEqual(code, 202, said)
        first = self.until(lambda s: s["capture"].get("waiting"))["capture"]
        # the page is loaded again: all it has is the status, which says what runs and how it was asked for
        c = self.status()["capture"]
        self.assertEqual((c["state"], c["waiting"], c["id"]), ("running", True, first["id"]))
        self.assertEqual(c["settings"], {"rate": 1e6, "seconds": "0.02", "trigger": "B3=r"})
        self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.02")[0], 409)
        self.assertEqual(self.post("/api/stop")[0], 200)
        c = self.until(lambda s: s["capture"]["state"] != "running")["capture"]
        self.assertEqual((c["state"], c["stopped"], c["trigger_sample"], c["id"], c["fetched"]),
                         ("done", "stopped", None, first["id"], False))
        self.assertNotIn("ended_by", c)
        self.assertEqual(self.ask("/api/data")[0], 200)
        self.assertTrue(self.status()["capture"]["fetched"])

    def test_a_wait_nobody_watches_is_ended(self):
        self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.02&" + NEVER)[0], 202)
        self.until(lambda s: s["capture"].get("waiting"))
        for _ in range(12):                 # a page that asks keeps it waiting
            time.sleep(0.1)
            self.assertEqual(self.status()["capture"]["state"], "running")
        c = self.until(lambda s: s["capture"]["state"] != "running", poll=False)["capture"]     # nobody asks
        self.assertEqual((c["state"], c["stopped"], c["ended_by"], c["trigger_sample"]),
                         ("done", "stopped", "unwatched", None))
        self.assertFalse(self.bus.run or self.bus.stream)        # RUN is low and the stream is ended

    def test_a_capture_with_a_clock_that_stops_is_ended_too(self):
        self.app.unwatched = 0.3
        q = {"rate": 1e6, "clock": "B3=r", "samples": 1000}      # B3 never changes: no samples come
        self.assertEqual(self.post("/api/capture?" + urllib.parse.urlencode(q))[0], 202)
        c = self.until(lambda s: s["capture"]["state"] != "running", poll=False)["capture"]
        self.assertEqual((c["state"], c["ended_by"], c["settings"]["clock"]), ("done", "unwatched", "B3=r"))

    def test_a_capture_that_does_not_wait_is_not_ended(self):
        self.app.unwatched = 0.0
        self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.05")[0], 202)
        c = self.until(lambda s: s["capture"]["state"] != "running", poll=False)["capture"]
        self.assertEqual((c["state"], c["stopped"], c["samples"]), ("done", None, 50000))
        self.assertNotIn("ended_by", c)

    def test_the_status_is_held_until_the_wait_changes(self):
        self.app.unwatched = 30
        self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.02&" + NEVER)[0], 202)
        self.until(lambda s: s["capture"].get("waiting"))
        t = time.monotonic()
        self.assertEqual(self.status(wait=0.4)["capture"]["state"], "running")
        self.assertGreater(time.monotonic() - t, 0.35)
        threading.Timer(0.2, lambda: self.post("/api/stop")).start()
        t = time.monotonic()
        c = self.status(wait=5)["capture"]  # answered as soon as the capture ends, not after 5 s
        self.assertLess(time.monotonic() - t, 3)
        self.assertEqual(c["state"], "done")
        t = time.monotonic()
        self.status(wait=5)                 # nothing runs: answered at once
        self.assertLess(time.monotonic() - t, 1)
        for wait in ("x", "nan", "-3", "1e9"):
            self.assertIn("capture", self.status(wait=wait))

    def test_what_the_page_saves_has_the_command_lines_layout(self):
        self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.02&a=1.65&b=0.9&trigger=A0%3Df")[0], 202)
        c = self.until(lambda s: s["capture"]["state"] != "running")["capture"]
        info = c["info"]
        self.assertEqual(([round(v, 2) for v in info["thresholds"]], info["overflow"], info["stopped"],
                          info["problems"], info["trigger_sample"], info["trigger"]),
                         ([1.65, 0.9], False, None, [], c["trigger_sample"], "A0=f"))
        for key in ("time", "format", "usb", "words", "samples_asked", "source"):
            self.assertIn(key, info)


class Ending(Served, unittest.TestCase):
    def test_closing_ends_the_capture_and_leaves_the_board_free(self):
        self.start_app()
        self.until(lambda s: s["board"]["state"] == "ready")
        self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.02&" + NEVER)[0], 202)
        self.until(lambda s: s["capture"].get("waiting"))
        self.assertTrue(self.bus.run and self.bus.stream)
        self.assertEqual(self.app.close(), (True, True))
        self.assertFalse(self.bus.run or self.bus.stream)
        self.assertEqual((self.app.job["state"], self.app.job["ended_by"]), ("done", "closing"))
        self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.02")[0], 409)    # and nothing new starts
        self.assertEqual(self.app.close(), (False, True))

    def test_the_program_ends_a_capture_when_it_is_stopped(self):
        for kind in ("SIGTERM", "SIGHUP", "SIGINT"):
            if not hasattr(signal, kind):
                continue
            with self.subTest(kind):
                with socket.socket() as s:
                    s.bind(("127.0.0.1", 0))
                    port = s.getsockname()[1]
                p = subprocess.Popen([sys.executable, LAUNCHER, "--fake", "--no-browser", "--port", str(port)],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                try:
                    first = p.stdout.readline()
                    self.assertIn("Logic Boole 16 at http://localhost:%d" % port, first)
                    self.url = "http://127.0.0.1:%d" % port
                    self.until(lambda s: s["board"]["state"] == "ready", 60)
                    self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.02&" + NEVER)[0], 202)
                    self.until(lambda s: s["capture"].get("waiting"))
                    p.send_signal(getattr(signal, kind))
                    out = p.communicate(timeout=30)[0]
                finally:
                    if p.poll() is None:
                        p.kill()
                        p.wait()
                self.assertEqual(p.returncode, 0, out)
                self.assertIn("Ending the capture first", out)
                self.assertIn("The capture was ended and the board is free.", out)

    def test_served_to_other_computers(self):
        made = []

        class Here(ThreadingHTTPServer):    # takes "0.0.0.0" but listens on this computer only
            def __init__(self, address, handler):
                super().__init__(("127.0.0.1", 0), handler)
                made.append(self)
        out = io.StringIO()
        with unittest.mock.patch.object(server, "ThreadingHTTPServer", Here), \
                unittest.mock.patch.object(server, "has_display", return_value=False), \
                unittest.mock.patch.object(server.webbrowser, "open") as opened, contextlib.redirect_stdout(out):
            t = threading.Thread(target=server.serve, args=(fake.FakeBus(instant=True), True, "0.0.0.0", 8016))
            t.start()
            for _ in range(200):
                if made:
                    break
                time.sleep(0.01)
            made[0].shutdown()
            t.join(10)
        text = out.getvalue()
        base = socket.gethostname().split(".")[0]
        self.assertIn("Logic Boole 16 at http://localhost:8016 on this computer, and from other computers at "
                      "http://%s.local:8016" % base, text)
        self.assertIn("There is no display here to open a browser on", text)
        opened.assert_not_called()
        self.assertTrue(any(a.startswith("http://") and not a.startswith("http://127.") for a in server.addresses(8016)))

    def test_which_computers_have_a_display(self):
        for platform, name, env, has in (("linux", "posix", {}, False), ("linux", "posix", {"DISPLAY": ":0"}, True),
                                         ("linux", "posix", {"WAYLAND_DISPLAY": "wayland-0"}, True),
                                         ("darwin", "posix", {}, True), ("win32", "nt", {}, True)):
            with unittest.mock.patch.object(server.sys, "platform", platform), \
                    unittest.mock.patch.object(server.os, "name", name), \
                    unittest.mock.patch.dict(server.os.environ, env, clear=True):
                self.assertEqual(server.has_display(), has, (platform, env))


class BadRequests(Served, unittest.TestCase):
    def setUp(self):
        self.start_app()

    def test_numbers_that_are_not(self):
        for q in ("rate=1000000&seconds=inf", "rate=1000000&seconds=nan", "rate=inf&seconds=1", "rate=-5&seconds=1",
                  "rate=1000000&seconds=1&a=inf", "rate=1000000&seconds=0.01&trigger=A0%3Dr&before=nan",
                  "rate=1e999&seconds=1", "rate=1000000&seconds=1e999"):
            code, said = self.post("/api/capture?" + q)
            self.assertEqual(code, 400, q)
            self.assertIn("is a number", said["error"], q)

    def test_a_length_that_is_not_one(self):
        for length in ("abc", "-1", "1e3", "99999999999999999999"):
            first, said = self.raw("POST /api/stop HTTP/1.1\r\nHost: localhost\r\nX-LB16: 1\r\nContent-Length: %s\r\n"
                                   "\r\n" % length)
            self.assertIn(" 400 ", first, length)
            self.assertIn("Content-Length", said["error"], length)
        first, said = self.raw("POST /api/stop HTTP/1.1\r\nHost: localhost\r\nX-LB16: 1\r\nContent-Length: 10\r\n\r\nabc")
        self.assertIn(" 400 ", first)
        self.assertIn("ended before", said["error"])
        first, said = self.raw("POST /api/stop HTTP/1.1\r\nHost: localhost\r\nX-LB16: 1\r\n"
                               "Transfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n")
        self.assertIn(" 411 ", first)
        first, said = self.raw("POST /api/stop HTTP/1.1\r\nHost: localhost\r\nX-LB16: 1\r\nContent-Length: %d\r\n\r\n"
                               % (1 << 20))
        self.assertIn(" 413 ", first)
        self.assertEqual(self.status()["capture"]["state"], "idle")      # and the app goes on

    def test_whatever_goes_wrong_is_answered(self):
        with unittest.mock.patch.object(self.app, "status", side_effect=RuntimeError("something inside broke")):
            code, said = self.ask("/api/status")
        self.assertEqual(code, 500)
        self.assertEqual(said["error"], "The app's server failed on this request: RuntimeError: something inside broke")
        with unittest.mock.patch.object(self.app, "prepare", side_effect=KeyError("x")):
            code, said = self.post("/api/prepare")
        self.assertEqual((code, "KeyError" in said["error"]), (500, True))
        self.assertIn("board", self.status())


class OtherSites(Served, unittest.TestCase):
    """A page of another web site can make the browser send a GET to the app, though it cannot read the answer:
    /api/status would open the board and load it.  The browser says where a request comes from."""

    def setUp(self):
        self.start_app()
        self.own = "http://127.0.0.1:%d" % self.port

    def code(self, path, **headers):
        return self.ask(path, headers={k.replace("_", "-"): v for k, v in headers.items()})[0]

    def test_a_get_from_another_site_is_refused(self):
        for path in ("/api/status", "/api/decoders", "/api/data", "/app.js"):
            self.assertEqual(self.code(path, Sec_Fetch_Site="cross-site", Sec_Fetch_Mode="no-cors"), 403, path)
            self.assertEqual(self.code(path, Sec_Fetch_Site="same-site", Sec_Fetch_Mode="cors"), 403, path)
            self.assertEqual(self.code(path, Origin="http://evil.example"), 403, path)
            self.assertEqual(self.code(path, Origin="null"), 403, path)
        code, said = self.ask("/api/status", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertIn("another web site", said["error"])
        # a browser too old to say Sec-Fetch-Site still names the page it was on
        self.assertEqual(self.code("/api/status", Referer="http://evil.example/page"), 403)
        self.assertEqual(self.code("/api/status", Referer=self.own + "/"), 200)

    def test_curl_the_page_itself_and_a_link_to_it_are_not(self):
        self.assertEqual(self.code("/api/status"), 200)                      # curl, Python: no such headers
        for path in ("/", "/app.js", "/api/status"):                         # the app's own page and its requests
            self.assertEqual(self.code(path, Sec_Fetch_Site="same-origin", Sec_Fetch_Mode="cors"), 200, path)
        self.assertEqual(self.code("/", Sec_Fetch_Site="none", Sec_Fetch_Mode="navigate"), 200)       # typed in
        self.assertEqual(self.code("/", Sec_Fetch_Site="cross-site", Sec_Fetch_Mode="navigate"), 200)  # a link
        self.assertEqual(self.code("/api/status", Sec_Fetch_Site="cross-site", Sec_Fetch_Mode="navigate"), 403)
        self.assertEqual(self.code("/api/status", Origin=self.own), 200)

    def test_a_post_from_another_site_is_refused(self):
        code, said = self.post("/api/stop", headers={"Origin": "http://evil.example"})
        self.assertEqual(code, 403)
        self.assertEqual(self.post("/api/stop", headers={"Origin": self.own, "Sec-Fetch-Site": "same-origin"})[0], 200)


class SigroksDecoders(Served, unittest.TestCase):
    """One run of the decoders for each analyzer, a time limit, a way to stop them, and the capture kept between
    runs.  The stand-in for sigrok-cli runs for ever when it is given baudrate=7 (as a decoder that is slow on a
    large capture would)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.log = os.path.join(self.dir.name, "asked.txt")
        slow = STAND_IN.replace('    only = a[a.index("-A") + 1] if "-A" in a else None\n', '''\
    import os
    note({"pid": os.getpid()})
    if "baudrate=7" in a[a.index("-P") + 1]:
        while True:
            sys.stdout.write(FOUND[0] + "\\n")
    only = a[a.index("-A") + 1] if "-A" in a else None
''')
        self.assertNotEqual(slow, STAND_IN)
        path = os.path.join(self.dir.name, "sigrok-cli")
        with open(path, "w", encoding="utf-8") as f:
            f.write(slow % (sys.executable, self.log, SHOWN, FOUND))
        os.chmod(path, 0o755)
        patch = unittest.mock.patch.dict(os.environ, {D.ENV: path})
        patch.start()
        self.addCleanup(patch.stop)
        from array import array
        cap = Capture(1e6, array("H", [0, 3, 1, 2, 0]), array("Q", [10, 20, 30, 40, 50]))
        cap.save(os.path.join(self.dir.name, "a.lb16"))
        with open(os.path.join(self.dir.name, "a.lb16"), "rb") as f:
            self.file = f.read()
        self.start_app(decode_seconds=30)

    def asked(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f if line.endswith("\n")]

    def decode(self, **q):
        return self.post("/api/decode?" + urllib.parse.urlencode(q))

    def gone(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    def test_a_capture_is_sent_once_and_decoded_again(self):
        code, said = self.post("/api/upload", self.file)
        self.assertEqual(code, 200)
        key = said["capture"]
        for _ in range(3):
            code, found = self.decode(capture=key, words="i2c scl=A1 sda=A0", job="an1")
            self.assertEqual((code, found["total"]), (200, 6))
        self.assertEqual(len([a for a in self.asked() if "metadata" in a]), 3)
        code, said = self.decode(capture="not-one", words="i2c scl=A1 sda=A0")
        self.assertEqual((code, said["gone"]), (404, True))
        for _ in range(server.UPLOADS_KEPT):         # newer captures push the oldest out
            self.post("/api/upload", self.file)
        self.assertEqual(self.decode(capture=key, words="i2c scl=A1 sda=A0")[0], 404)
        self.assertEqual(len(os.listdir(self.app.folder)), server.UPLOADS_KEPT)
        folder = self.app.folder
        self.app.close()
        self.assertFalse(os.path.exists(folder))     # and nothing is left behind
        self.assertEqual(self.post("/api/upload", self.file)[0], 503)

    def test_a_new_run_for_an_analyzer_stops_the_one_before(self):
        key = self.post("/api/upload", self.file)[1]["capture"]
        got = {}
        t = threading.Thread(target=lambda: got.update(r=self.decode(capture=key, job="an1",
                                                                      words="uart rx=A0 baudrate=7")))
        t.start()
        self.until(lambda s: any("pid" in a for a in self.asked()), poll=False)
        pid = [a["pid"] for a in self.asked() if "pid" in a][-1]
        code, found = self.decode(capture=key, job="an1", words="i2c scl=A1 sda=A0")
        self.assertEqual((code, found["total"]), (200, 6))
        t.join(10)
        self.assertEqual((got["r"][0], got["r"][1]["cancelled"]), (409, True))
        self.until(lambda s: self.gone(pid), poll=False)

    def test_the_page_can_cancel_and_there_is_a_time_limit(self):
        key = self.post("/api/upload", self.file)[1]["capture"]
        got = {}
        t = threading.Thread(target=lambda: got.update(r=self.decode(capture=key, job="an2",
                                                                      words="uart rx=A0 baudrate=7")))
        t.start()
        self.until(lambda s: "an2" in self.app.decoding, poll=False)
        self.assertEqual(self.post("/api/cancel?job=an2"), (200, {"cancelled": True}))
        t.join(10)
        self.assertEqual(got["r"][0], 409)
        self.assertEqual(self.post("/api/cancel?job=an2"), (200, {"cancelled": False}))
        self.app.decode_seconds = 0.5
        t = time.monotonic()
        code, said = self.decode(capture=key, job="an3", words="uart rx=A0 baudrate=7")
        self.assertEqual(code, 400)
        self.assertIn("were stopped after 0.5 seconds", said["error"])
        self.assertLess(time.monotonic() - t, 10)
        self.assertEqual(self.app.decoding, {})

    def test_no_more_than_a_few_at_once(self):
        key = self.post("/api/upload", self.file)[1]["capture"]
        threads = [threading.Thread(target=self.decode, kwargs={"capture": key, "job": "j%d" % i,
                                                                 "words": "uart rx=A0 baudrate=7"})
                   for i in range(server.DECODE_JOBS)]
        for t in threads:
            t.start()
        self.until(lambda s: len(self.app.decoding) == server.DECODE_JOBS, poll=False)
        code, said = self.decode(capture=key, job="one-more", words="i2c scl=A1 sda=A0")
        self.assertEqual(code, 429)
        self.assertIn("wait for one to end", said["error"])
        self.app.close()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive())


if __name__ == "__main__":
    unittest.main()
