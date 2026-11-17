# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""The command line.

    logicboole                          the app: waveforms and protocol decoders in the browser
    logicboole check                    the first thing to run: checks this computer and the board, and says what
                                        to do about anything that is missing
    logicboole info                     what is on USB, and the board's settings
    logicboole capture FILE ...         one capture, written to each FILE (.sr .vcd .csv .lb16)
    logicboole capture --trigger "B0=r" FILE ...    the same, waiting for B0 to rise ("capture -h" lists it all)
    logicboole threshold A [B]          sets the thresholds, in volts
    logicboole inputs                   the 16 inputs as they are now
    logicboole selftest                 checks the path from the FPGA to the computer with its test pattern
    logicboole show FILE.lb16           what a saved capture holds
    logicboole measure FILE.lb16 A0 ...  periods, frequencies and pulse lengths read off a saved capture
    logicboole decode FILE i2c scl=A1 sda=A0     what a protocol decoder finds in a saved capture (sigrok's
                                        decoders, through sigrok-cli; "logicboole decoders" lists them)
    logicboole convert IN.lb16 OUT ...  a saved capture in other formats
    logicboole mcp                      a Model Context Protocol server, for an AI agent to use the board
    logicboole load                     loads the firmware and the FPGA's design again (or ones you built)
    logicboole unload                   sends the board back to its boot loader

A board that has just been plugged in is loaded by whichever command needs it first.  "logicboole COMMAND -h"
lists a command's options; --fake before the command uses a stand-in for the board; --json makes info, inputs,
threshold, capture, show, measure, decode and decoders print JSON for a program to read.
"""
import argparse
import json
import os
import signal
import sys

from . import __version__, board as B, decode as D, trigger as T, usb
from .capture import CHANNELS, Capture
from .selftest import selftest


def number(text):
    """1M, 250k, 2.5G or a plain number."""
    scale = {"k": 1e3, "M": 1e6, "G": 1e9}.get(text[-1:])
    try:
        return float(text[:-1]) * scale if scale else float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("%s is not a number (1000, 250k, 1M and the like are)" % text)


def duration(text):
    """Seconds: 2, 0.5, 20ms, 100us, 50ns."""
    for unit, scale in (("ms", 1e-3), ("us", 1e-6), ("ns", 1e-9), ("s", 1.0)):
        if text.endswith(unit):
            text = text[:-len(unit)]
            break
    else:
        scale = 1.0
    try:
        return float(text) * scale
    except ValueError:
        raise argparse.ArgumentTypeError("a time is in seconds: 2, 0.5, 20ms, 100us")


def rate_text(rate):
    return "%.10g MS/s" % (rate / 1e6) if rate >= 1e6 else "%.10g kS/s" % (rate / 1e3)


def span(t):
    """A time for reading: 10 us, 2.5 ms."""
    for unit, scale in (("s", 1.0), ("ms", 1e-3), ("us", 1e-6)):
        if t >= scale:
            return "%.6g %s" % (t / scale, unit)
    return "%.6g ns" % (t * 1e9)


def hertz(f):
    for unit, scale in (("MHz", 1e6), ("kHz", 1e3)):
        if f >= scale:
            return "%.6g %s" % (f / scale, unit)
    return "%.6g Hz" % f


def levels(v):
    return "  ".join("%s %d" % (c, v >> i & 1) for i, c in enumerate(CHANNELS))


def describe(cap):
    """Lines about what each channel did."""
    out, quiet = [], []
    for ch in cap.summary():
        if ch["changes"]:
            out.append("  %s: %d changes, the first at %.9g s; it starts %s"
                       % (ch["channel"], ch["changes"], ch["first"], "high" if ch["starts"] else "low"))
        else:
            quiet.append(ch)
    if quiet:
        high = [ch["channel"] for ch in quiet if ch["starts"]]
        out.append("  no change on %s (%s)" % (" ".join(ch["channel"] for ch in quiet),
                                              "high: " + " ".join(high) if high else "all low"))
    return out


def as_json(cap, files=()):
    return {"ok": cap.ok, "problems": cap.problems, "overflow": cap.overflow, "stopped": cap.stopped,
            "sample_rate": cap.rate, "samples": cap.samples, "seconds": cap.seconds,
            "trigger_sample": cap.trigger, "trigger_seconds": None if cap.trigger is None else cap.trigger / cap.rate,
            "thresholds": cap.thresholds, "channels": cap.summary(), "files": list(files), "info": cap.info}


def trigger_lines(cap):
    """What became of a capture's trigger, if it had one."""
    what = cap.info.get("trigger")
    if cap.trigger is not None:
        where = ("sample %d" % cap.trigger if cap.info.get("clock")
                 else "%.9g s into the capture (sample %d)" % (cap.trigger / cap.rate, cap.trigger))
        return ["  trigger%s: %s" % (" (%s)" % what if what else "", where)]
    if what:
        return ["  NOT TRIGGERED (%s): these are the last %d samples the board saw." % (what, cap.samples)]
    return []


def parser():
    p = argparse.ArgumentParser(prog="logicboole", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="logicboole " + __version__)
    p.add_argument("--fake", action="store_true", help="use a stand-in for the board (nothing needs to be plugged in)")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def command(name, text):
        return sub.add_parser(name, help=text, description=text)

    c = command("app", "the app in the browser (also what plain \"logicboole\" starts)")
    for q in (p, c):
        q.add_argument("--port", type=int, default=8016, help="the port the app is served on (8016)")
        q.add_argument("--host", default="127.0.0.1", help="0.0.0.0 makes the app reachable from other computers, "
                                                           "for a Raspberry Pi with no screen")
        q.add_argument("--no-browser", action="store_true", help="do not open a browser window")

    c = command("info", "what is on USB, and the board's settings")
    c.add_argument("--json", action="store_true")

    c = command("capture", "one capture, written to each FILE (.sr .vcd .csv .lb16)")
    c.add_argument("files", nargs="*", metavar="FILE")
    c.add_argument("-r", "--rate", type=number, default=1e6, help="samples a second: 200M, 100M, 50M, 10M, 1M (the "
                   "default), 250k ...; the board takes the nearest of 200 MS/s / 1, / 2, / 3 ...")
    c.add_argument("-t", "--time", type=duration, default=1.0, help="how long: 2, 0.5, 20ms (1 s is the default)")
    c.add_argument("-n", "--samples", type=number, help="how many samples, in place of --time; 0: until Ctrl-C")
    c.add_argument("-a", type=float, metavar="VOLTS", help="pod A's threshold")
    c.add_argument("-b", type=float, metavar="VOLTS", help="pod B's threshold")
    c.add_argument("--raw", action="store_true", help="have every sample sent, not only the changes: for signals "
                                                       "that change at nearly every sample.  At 200 MS/s that is "
                                                       "all the board can pass on, and such a capture is short")
    c.add_argument("--test", type=int, nargs="?", const=8, metavar="SHIFT", help="the FPGA's test pattern in place "
                   "of the probes: a counter of the samples shifted right by SHIFT bits (8), checked word for word")
    c.add_argument("--trigger", metavar="WHAT", help="wait for this, and count the capture's length from it.  "
                   "\"B0=r\": B0 rises (f: falls, e: either).  \"A0=1 A1=0\": levels.  \"A2=f A0=1\": an edge "
                   "while a level holds.  \"A0=1 for 10us\": a level has lasted.  \"A0=1 pulse 100ns..1us\": a "
                   "pulse of that length has ended (\"..50ns\": at most, \"1us..\": at least).  \"in=r\": the "
                   "TRIG IN pin.  Up to four of these in turn: \"B7=f then B0=r\"")
    c.add_argument("--before", metavar="TIME", help="with --trigger: how much from before the trigger is kept, as "
                   "a time (5ms) or a number of samples.  A tenth of the length if not given; the board keeps "
                   "its last 8184 changes of the inputs while it waits")
    c.add_argument("--timeout", type=duration, metavar="TIME", help="with --trigger: stop waiting after this long "
                   "(Ctrl-C does it too).  The capture then holds what the board saw last")
    c.add_argument("--clock", metavar="INPUT=EDGE", help="a capture clock: \"A0=r\" keeps a sample at each rising "
                   "edge of A0 (f: falling, e: either), the circuit's own clock up to 50 MHz, in place of one "
                   "every so many nanoseconds.  The length is then a number of samples (-n), and --rate is only "
                   "what the files give as the clock's frequency")
    c.add_argument("--glitch", type=T.parse_filter, metavar="TIME", help="a glitch filter: \"20ns\" for every "
                   "input, or \"A0=20ns,B=1us\" for a channel and a pod (10 ns ... 1.25 us).  A change counts "
                   "once the input has held its new level for that long")
    c.add_argument("--json", action="store_true")

    c = command("threshold", "sets the thresholds, in volts at the probe")
    c.add_argument("a", type=float, metavar="A", help="pod A's, and pod B's too if B is not given")
    c.add_argument("b", type=float, nargs="?", metavar="B")
    c.add_argument("--json", action="store_true")

    c = command("inputs", "the 16 inputs as they are now")
    c.add_argument("--json", action="store_true")

    command("selftest", "checks the path from the FPGA to the computer with the FPGA's test pattern")
    command("check", "the first thing to run: checks this computer and the board, and says what is missing")

    c = command("show", "what a saved capture holds")
    c.add_argument("file", metavar="FILE.lb16")
    c.add_argument("--json", action="store_true")

    c = command("measure", "periods, frequencies and pulse lengths read off a saved capture")
    c.add_argument("file", metavar="FILE.lb16")
    c.add_argument("channels", nargs="*", metavar="CHANNEL", help="A0 ... B7; none: the channels that change")
    c.add_argument("--from", dest="start", type=duration, metavar="TIME", help="look at the capture from this "
                   "time on (seconds from its start: 0.5, 20ms, 100us)")
    c.add_argument("--to", dest="end", type=duration, metavar="TIME", help="and up to this time")
    c.add_argument("--list", type=int, default=0, metavar="N", help="also list the first N changes of these "
                   "channels, with their times")
    c.add_argument("--json", action="store_true")

    c = command("decode", "what a protocol decoder finds in a saved capture: sigrok's decoders, run by "
                          "sigrok-cli, which has to be installed")
    c.add_argument("file", metavar="FILE", help="a capture saved as .lb16 or .sr")
    c.add_argument("decoder", nargs="+", metavar="DECODER [NAME=VALUE ...]", help="a decoder, then its channels "
                   "and options: \"i2c scl=A1 sda=A0\", \"uart rx=B0 baudrate=9600\", \"spi clk=A0 mosi=A1 "
                   "miso=A2 cs=A3\".  A second decoder goes on top of the first: \"i2c scl=A1 sda=A0 "
                   "eeprom24xx\".  \"logicboole decoders NAME\" lists a decoder's channels and options")
    c.add_argument("--only", metavar="CLASS,...", help="list only these classes of what was found (the JSON "
                   "names each one's class): \"address-write,data-write\", \"rx-data\", \"bit\".  Not "
                   "given: all that the last decoder found, except single bits")
    c.add_argument("--from", dest="start", type=duration, metavar="TIME", help="list what begins at this time "
                   "or later (seconds from the capture's start: 0.5, 20ms, 100us)")
    c.add_argument("--to", dest="end", type=duration, metavar="TIME", help="and before this time")
    c.add_argument("--limit", type=int, metavar="N", help="list N at the most")
    c.add_argument("--json", action="store_true")

    c = command("decoders", "the protocol decoders sigrok-cli has, or what one of them takes")
    c.add_argument("name", nargs="?", metavar="NAME", help="a decoder: its channels, its options and what it "
                                                           "reports")
    c.add_argument("--json", action="store_true")

    c = command("mcp", "a Model Context Protocol server on the standard input and output: an AI agent's program "
                       "starts this, and the agent then has the board's captures, measurements and decoders as "
                       "tools")
    c.add_argument("--dir", metavar="FOLDER", help="where the server keeps the captures it takes (a folder for "
                                                    "temporary files if not given)")

    c = command("convert", "a saved capture in other formats")
    c.add_argument("file", metavar="IN.lb16")
    c.add_argument("files", nargs="+", metavar="OUT", help=".sr .vcd .csv .lb16")

    c = command("load", "loads the firmware and the FPGA's design, also when they are running already")
    c.add_argument("--firmware", metavar="FILE.img", help="a firmware image you have built")
    c.add_argument("--gateware", default=B.GATEWARE, metavar="FILE.bin", help="an FPGA bitstream you have built")

    command("unload", "sends the board back to its boot loader")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return run(args) or 0
    except FileNotFoundError as e:
        print("%s: there is no such file." % e.filename, file=sys.stderr)
        return 1
    except (B.BoardError, usb.UsbError, ValueError, OSError) as e:
        print(str(e), file=sys.stderr)
        return 1


def check(backend, stand_in):
    """The first thing to run with a board: this computer, then the board, each with what to do if it is missing.
    About five seconds.  Returns 0 if a capture can be taken."""
    print("Python %d.%d.%d: fine." % sys.version_info[:3])
    if backend is usb:
        usb.library()                       # says what to install if it is not there
        print("libusb: found.")
    found = B.look(backend)
    if found["state"] == "none":
        print("The board: not on USB.  Connect its USB-C socket to this computer and run this again.")
        return 1
    if found["state"] in ("noaccess", "other", "inuse"):
        print("The board: %s" % found["text"])
        return 1
    print("The board: on USB.")
    if backend is usb:
        had = os.path.exists(B.FIRMWARE) or os.path.exists(B.kept_firmware())
        B.firmware_image()                  # downloads it if it is not here
        print("The firmware: %s." % ("on this computer" if had else "downloaded, checked and kept on this computer"))
    with B.connect(backend=backend, log=None) as board:
        s, r = board.status(), board.registers()
        serial = board.dev.serial()
        print("Loaded: firmware version %d, FPGA design version %d%s."
              % (s["version"], r["version"], ", serial number %s" % serial if serial else ""))
        if s["speed"] == 3:
            print("USB: 5 Gbit/s.")
        else:
            print("USB: %s.  The board works on this link and carries about a ninth of what USB 3 carries: use "
                  "a USB 3 port and a USB 3 cable for signals that change more than about 10 million times a "
                  "second." % B.LINK.get(s["speed"], "?"))
        ok = True
        for fmt, rate, shift, seconds in (("rle", 200e6, 8, 0.5), ("raw", 10e6, 0, 0.2)):
            cap = board.capture(rate=rate, seconds=seconds * (0.02 if stand_in else 1), fmt=fmt, test=shift)
            ok = ok and cap.ok and not cap.overflow
            print("A capture of the FPGA's test pattern, %d samples at %s: %s."
                  % (cap.samples, rate_text(cap.rate), "every word right" if cap.ok and not cap.overflow
                     else "WRONG: " + "; ".join(cap.problems or ["it overflowed"])))
    if not ok:
        print("The board did not pass.  Try another USB port or cable, then \"logicboole selftest\".")
        return 1
    print("The board is ready.  \"logicboole\" opens the app.")
    return 0


def run(args):
    if args.fake:
        from . import fake
        backend = fake.FakeBus()
    else:
        backend = usb
    cmd = args.command or "app"
    say = (lambda line: print(line, file=sys.stderr))       # what is done to the board on the way: not the result

    if cmd == "app":
        from . import server
        return server.serve(backend, args.fake, args.host, args.port, not args.no_browser)

    if cmd == "mcp":
        from . import mcp
        return mcp.serve(backend, args.fake, args.dir)

    if cmd == "decoders":
        return decoders(args)

    if cmd == "decode":
        return decode(args)

    if cmd == "measure":
        return measure(args)

    if cmd in ("show", "convert"):
        cap = Capture.load(args.file)
        if cmd == "convert":
            for path in args.files:
                cap.save(path)
                print("wrote %s" % path)
        elif args.json:
            print(json.dumps(as_json(cap), indent=1))
        else:
            print("%s for %.9g s: %d samples%s" % (rate_text(cap.rate), cap.seconds, cap.samples,
                                                  "; thresholds %.2f V and %.2f V" % tuple(cap.thresholds)
                                                  if cap.thresholds else ""))
            print("\n".join(trigger_lines(cap) + describe(cap)))
        return 0

    if cmd == "check":
        return check(backend, args.fake)

    if cmd == "info":
        found = B.look(backend)
        if args.json:
            print(json.dumps(found, indent=1))
            return 0
        print(found["text"])
        if "firmware" in found:
            print("  firmware version %d%s" % (found["firmware"], ", FPGA design version %d" % found["design"]
                                               if "design" in found else ""))
        if "serial" in found:
            print("  serial number %s" % found["serial"])
        if found["state"] == "ready":
            print("  thresholds: pod A %.2f V, pod B %.2f V" % (found["threshold_a"], found["threshold_b"]))
            print("  inputs now: %s" % levels(found["inputs"]))
        elif found["state"] in ("boot", "unconfigured"):
            print("  Any command that needs the board loads it; \"logicboole load\" does only that.")
        return 0 if found["state"] in ("ready", "boot", "unconfigured") else 1

    if cmd == "load":
        B.connect(backend=backend, log=print, firmware=args.firmware, gateware=args.gateware, reload=True).close()
        return 0

    if cmd == "capture":
        for path in args.files:             # a file that cannot be written fails before the capture, not after
            if path.rsplit(".", 1)[-1].lower() not in ("lb16", "sr", "vcd", "csv"):
                raise ValueError("%s: a capture is saved as .lb16, .sr, .vcd or .csv" % path)

    with B.connect(backend=backend, log=say) as board:
        if cmd == "unload":
            board.unload()
            boot = backend.open_device(B.VID, B.PID_BOOT, wait=5)       # it is back on USB within about a second
            if boot is None:
                raise B.BoardError("The board did not return to its boot loader: unplug it and plug it in again.")
            boot.close()
            print("The board is back in its boot loader.")
        elif cmd == "threshold":
            a, b = board.set_thresholds(args.a, args.a if args.b is None else args.b)
            if args.json:
                print(json.dumps({"threshold_a": round(a, 3), "threshold_b": round(b, 3)}))
            else:
                print("pod A %.3f V, pod B %.3f V" % (a, b))
        elif cmd == "inputs":
            v = board.inputs()
            print(json.dumps({c: v >> i & 1 for i, c in enumerate(CHANNELS)}) if args.json else levels(v))
        elif cmd == "selftest":
            ok = selftest(board, scale=0.02 if args.fake else 1.0)
            print("The capture path passed." if ok else "The capture path did NOT pass.")
            return 0 if ok else 1
        elif cmd == "capture":
            return capture(board, args)
    return 0


def decoders(args):
    if args.name is None:
        found = D.decoders()
        if args.json:
            print(json.dumps(found, indent=1))
        else:
            for d in found:
                print("  %-20s %s" % (d["id"], d["name"]))
            print("%d decoders.  \"logicboole decoders NAME\" lists what one takes." % len(found))
        return 0
    d = D.about(args.name)
    if args.json:
        print(json.dumps(d, indent=1))
        return 0
    print("%s: %s.  %s" % (d["id"], d["name"], d["description"]))
    if "logic" in d["inputs"]:
        print("  reads the inputs")
    else:
        print("  goes on top of a decoder that gives it %s" % " or ".join(d["inputs"]))
    for c in d["channels"]:
        print("  channel  %s=CHANNEL   %s%s" % (c["id"], c["about"], "" if c["required"] else " (may be left out)"))
    for o in d["options"]:
        print("  option   %s=%s   %s%s" % (o["id"], o.get("default", "..."), o["about"],
                                         ": " + ", ".join(o["values"]) if "values" in o else ""))
    if d["classes"]:
        print("  reports  %s" % ", ".join("%s (%s)" % (c["id"], c["about"]) for c in d["classes"]))
    if d["outputs"]:
        print("  a decoder on top of it is given %s" % " or ".join(d["outputs"]))
    return 0


def decode(args):
    only = [w for w in args.only.replace(",", " ").split()] if args.only else None
    found = D.decode(args.file, args.decoder, only=only, start=args.start, end=args.end, limit=args.limit)
    if args.json:
        print(json.dumps(found, indent=1))
        return 0
    rows = found["annotations"]
    print("%s: %s, %d samples.  %s found %d%s" % (args.file, rate_text(found["sample_rate"]), found["samples"],
                                                 " ".join(args.decoder), found["total"],
                                                 ":" if rows else "."))
    if found["trigger_sample"] is not None:
        print("  the trigger is at %.9f s (sample %d)" % (found["trigger_seconds"], found["trigger_sample"]))
    several = len({r["decoder"] for r in rows}) > 1
    names = [r["decoder"] + "." + r["class"] if several else r["class"] for r in rows]
    width = min(max(map(len, names), default=0), 28)
    for r, name in zip(rows, names):
        print("  %13.9f s  %-*s  %s" % (r["start"], width, name, r["text"]))
    if found["total"] > len(rows):
        print("  ... and %d more." % (found["total"] - len(rows)))
    return 0


def measure(args):
    cap = Capture.load(args.file)
    m = cap.measure(args.channels or None, start=args.start, end=args.end, listed=max(args.list, 0))
    if args.json:
        print(json.dumps(m, indent=1))
        return 0
    print("%s: %s (a sample is %s), from %.9g s to %.9g s" % (args.file, rate_text(cap.rate),
                                                            span(m["sample_seconds"]), m["start"], m["end"]))
    if not m["channels"]:
        print("  no channel changes there")
    for c in m["channels"]:
        print("  %s: %d changes (%d rising, %d falling); %s at the start"
              % (c["channel"], c["changes"], c["rising"], c["falling"], "high" if c["starts"] else "low"))
        for key, name in (("period", "period"), ("high", "high for"), ("low", "low for")):
            v = c[key]
            if v is None:
                continue
            line = "      %-9s %s" % (name, span(v["median"]) if v["min"] == v["max"]
                                      else "%s ... %s, the median %s" % (span(v["min"]), span(v["max"]),
                                                                         span(v["median"])))
            if key == "period":
                line += "  (%s%s)" % (hertz(c["frequency"]), ", high %.4g %% of the time" % (100 * c["duty"]))
            print(line)
    for e in m["changes"]:
        print("  %13.9f s  %s %s" % (e["time"], e["channel"], "rises" if e["level"] else "falls"))
    if m["changes"] and m["changes_total"] > len(m["changes"]):
        print("  ... and %d more changes." % (m["changes_total"] - len(m["changes"])))
    return 0


def capture(board, args):
    if args.clock and args.samples is None:
        raise ValueError("With --clock the capture's length is a number of samples: -n 100000.")
    before = samples_before = None
    if args.before is not None:             # a time, or a number of samples
        before = T.seconds(args.before)
        if before is None:
            samples_before = round(number(args.before))
    asked = []
    old = signal.signal(signal.SIGINT, lambda *_: asked.append(1))     # Ctrl-C ends the capture; what it has is kept
    try:
        cap = board.capture(rate=args.rate, seconds=args.time,
                            samples=None if args.samples is None else round(args.samples),
                            fmt="raw" if args.raw else "rle", test=args.test, a=args.a, b=args.b,
                            cancel=lambda: bool(asked), trigger=args.trigger, before=before,
                            samples_before=samples_before, timeout=args.timeout, clock=args.clock,
                            glitch=args.glitch)
    finally:
        signal.signal(signal.SIGINT, old)
    for path in args.files:
        cap.save(path)
    if args.json:
        print(json.dumps(as_json(cap, args.files), indent=1))
        return 0 if cap.ok else 1
    print("%s: %d samples in %d words (%.1f MB), USB at %s; thresholds %.2f V and %.2f V"
          % ("a sample at each edge %s" % cap.info["clock"] if cap.info.get("clock")
             else "%s for %.9g s" % (rate_text(cap.rate), cap.seconds),
             cap.samples, cap.info["words"], cap.info["words"] * 4 / 1e6, cap.info["usb"], cap.thresholds[0],
             cap.thresholds[1]))
    if cap.info.get("glitch_ns"):
        print("  glitch filter: %s" % ", ".join("%s %d ns" % (c, ns) for c, ns in zip(CHANNELS, cap.info["glitch_ns"])
                                                if ns))
    for line in trigger_lines(cap):
        print(line)
    if cap.overflow:
        print("  OVERFLOW: the signals made more data than the board and USB carry, or the computer did not take "
              "it fast enough.  The capture ends after %.9g s and is whole up to there." % cap.seconds)
    if cap.stopped:
        print("  ended early: %s" % {"stopped": "Ctrl-C", "size": "it had grown too large",
                                     "timeout": "the time given with --timeout had passed"}[cap.stopped])
    for p in cap.problems:
        print("  WRONG: %s" % p)
    if args.test is not None:
        if cap.ok:
            print("  every word is the test pattern's")
    elif cap.samples <= 50e6 or cap.format == "rle":
        print("\n".join(describe(cap)))
    else:
        print("  The channels are not listed: going through more than 50 million raw samples takes a while.  "
              "--json lists them.")
    for path in args.files:
        print("wrote %s" % path)
    return 0 if cap.ok else 1


if __name__ == "__main__":
    sys.exit(main())
