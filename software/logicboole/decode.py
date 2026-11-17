# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""Protocol decoders: sigrok's, run on a saved capture.

sigrok (sigrok.org) has decoders for more than a hundred protocols, and its command line program, sigrok-cli,
runs them on a file.  This hands sigrok-cli a capture and reads back what the decoders found, each thing with the
samples it spans:

    logicboole decode bus.lb16 i2c scl=A1 sda=A0
    logicboole decode bus.lb16 uart rx=B0 baudrate=115200 --only rx-data
    logicboole decode bus.lb16 i2c scl=A1 sda=A0 eeprom24xx      a decoder on top of another
    logicboole decoders                 the decoders sigrok-cli has
    logicboole decoders spi             one decoder's channels, its options and what it reports

A decoder is named, and the words with "=" after it are its channels and its options.  A second name puts that
decoder on top of the first.  What is listed is what the last decoder found, without the single bits; `only`
picks by class instead ("bit", "address-write", "rx-data" ...), from any decoder named.

sigrok-cli is a program of its own and has to be installed: "brew install sigrok-cli" on macOS,
"sudo apt install sigrok-cli" on Debian, Ubuntu and Raspberry Pi OS.  LOGICBOOLE_SIGROK_CLI names one that is
somewhere else.
"""
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

from .capture import CHANNELS, Capture

ENV = "LOGICBOOLE_SIGROK_CLI"
MISSING = ("sigrok-cli was not found.  The decoders are sigrok's: install it with \"brew install sigrok-cli\" "
           "(macOS) or \"sudo apt install sigrok-cli\" (Debian, Ubuntu, Raspberry Pi OS).")
SECTIONS = {"Possible decoder input IDs:": "inputs", "Possible decoder output IDs:": "outputs",
            "Decoder tags:": "tags", "Annotation classes:": "classes", "Annotation rows:": "rows",
            "Binary classes:": "binary", "Required channels:": "required", "Optional channels:": "optional",
            "Options:": "options"}
HERTZ = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9}
LINE = re.compile(r"(\d+)-(\d+) (\S+)-\d+: (.*)", re.S)


class DecodeError(ValueError):
    pass


def program():
    """Where sigrok-cli is."""
    path = os.environ.get(ENV) or shutil.which("sigrok-cli")
    if not path:
        raise DecodeError(MISSING)
    return path


def _run(args):
    try:
        return subprocess.run([program()] + args, capture_output=True, encoding="utf-8", errors="replace",
                              timeout=60)
    except OSError as e:
        raise DecodeError("sigrok-cli could not be run: %s" % e)
    except subprocess.TimeoutExpired:
        raise DecodeError("sigrok-cli did not answer.")


def decoders():
    """The decoders sigrok-cli has, as a list of dicts: id and name."""
    out, inside = [], False
    for line in _run(["-L"]).stdout.splitlines():
        if line.startswith("Supported protocol decoders"):
            inside = True
        elif inside:
            if not line.strip():
                break
            ident, _, name = line.strip().partition(" ")
            out.append({"id": ident, "name": name.strip()})
    if not out:
        raise DecodeError("This sigrok-cli lists no protocol decoders.")
    return out


def _option(ident, text):
    """An option from sigrok-cli's line about it: "Bit order ('lsb-first', 'msb-first', default 'lsb-first')"."""
    out = {"id": ident, "about": text}
    m = re.fullmatch(r"(.*) \((.*)default (.*)\)", text)
    if m:
        def plain(v):
            return v.strip().strip("'")
        out["about"], out["default"] = m.group(1), plain(m.group(3))
        values = [plain(v) for v in m.group(2).split(",") if v.strip()]
        if values:
            out["values"] = values
    return out


def about(name):
    """One decoder as a dict: id, name, description, inputs and outputs (what it reads and what a decoder on top
    of it is given), channels (id, name, about, required), options (id, about, default, values if it takes
    only some), classes (what it reports: id, about) and documentation."""
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise DecodeError("%s is not a decoder's name: \"logicboole decoders\" lists them." % name)
    p = _run(["-P", name, "--show"])
    if p.returncode or not p.stdout.startswith("ID: "):
        raise DecodeError("sigrok-cli has no decoder named %s: \"logicboole decoders\" lists them." % name)
    d = {"id": name, "name": "", "description": "", "inputs": [], "outputs": [], "channels": [], "options": [],
         "classes": [], "documentation": ""}
    section, lines = None, p.stdout.splitlines()
    for at, line in enumerate(lines):
        if line == "Documentation:":
            d["documentation"] = "\n".join(lines[at + 1:]).strip()
            break
        if line in SECTIONS:
            section = SECTIONS[line]
        elif line.startswith("- ") and section:
            ident, _, text = line[2:].partition(": ")
            if section in ("inputs", "outputs"):
                d[section].append(line[2:].strip())
            elif section in ("required", "optional"):
                short, _, long = ident.partition(" (")
                d["channels"].append({"id": short, "name": long.rstrip(")"), "about": text,
                                      "required": section == "required"})
            elif section == "options":
                d["options"].append(_option(ident, text))
            elif section == "classes":
                d["classes"].append({"id": ident, "about": text})
        elif section is None:
            key, sep, value = line.partition(": ")
            if sep and key in ("Long name", "Description"):
                d["name" if key == "Long name" else "description"] = value
    return d


def stack(words):
    """What was asked for as a list of (decoder, {name: value}): from "i2c scl=A1 sda=A0 eeprom24xx" or the same
    as a list of words."""
    if isinstance(words, str):
        words = words.split()
    parts = []
    for w in words:
        name, eq, value = w.partition("=")
        if not eq:
            parts.append((w, {}))
        elif not parts:
            raise DecodeError("A decoder's name comes first: \"i2c scl=A1 sda=A0\".")
        elif not name or not value or name in parts[-1][1]:
            raise DecodeError("\"%s\" is not understood: a channel or an option is given once, as name=value." % w)
        else:
            parts[-1][1][name] = value
    if not parts:
        raise DecodeError("No decoder was named: \"i2c scl=A1 sda=A0\", for example.")
    return parts


def _same(a, b):
    try:
        return float(a) == float(b)
    except ValueError:
        return a == b


def _check(parts, names):
    """Looks each decoder up and checks its channels and options against it.  names: the capture's channels, as
    {a name in lower case: the name the session file has}.  Returns what sigrok-cli's -P takes."""
    specs, below = [], None
    for name, given in parts:
        d = about(name)
        if below is None and "logic" not in d["inputs"]:
            raise DecodeError("%s does not read the inputs: it goes on top of a decoder that gives it %s, named "
                              "before it." % (name, " or ".join(d["inputs"]) or "its data"))
        if below is not None and not set(d["inputs"]) & set(below["outputs"]):
            raise DecodeError("%s does not go on top of %s: it takes %s." % (name, below["id"],
                                                                           " or ".join(d["inputs"])))
        channels = {c["id"]: c for c in d["channels"]}
        options = {o["id"]: o for o in d["options"]}
        items = []
        for key, value in given.items():
            if key in channels:
                real = names.get(value.lower())
                if real is None:
                    raise DecodeError("%s=%s: the capture has no channel %s.  Its channels are %s."
                                      % (key, value, value, " ".join(sorted(set(names.values()), key=_order))))
                items.append("%s=%s" % (key, real))
            elif key in options:
                o = options[key]
                if "values" in o and not any(_same(value, v) for v in o["values"]):
                    raise DecodeError("%s=%s: %s's %s is one of %s." % (key, value, name, key,
                                                                         ", ".join(o["values"])))
                if re.search(r"[:,\s]", value):
                    raise DecodeError("%s=%s: a value has no colon, comma or space." % (key, value))
                items.append("%s=%s" % (key, value))
            else:
                raise DecodeError("%s has no channel or option named %s.  Its channels: %s.  Its options: %s."
                                  % (name, key, ", ".join(channels) or "none", ", ".join(options) or "none"))
        lacking = [c for c in channels.values() if c["required"] and c["id"] not in given]
        if lacking or channels and not any(k in channels for k in given):
            want = lacking or list(channels.values())
            raise DecodeError("%s %s: %s." % (name, "needs" if lacking else "was given no channel.  It takes",
                                              ", ".join("%s=CHANNEL (%s)" % (c["id"], c["about"]) for c in want)))
        specs.append(":".join([name] + items))
        below = d
    return ",".join(specs)


def _order(name):
    return (CHANNELS.index(name), "") if name in CHANNELS else (len(CHANNELS), name)


def _session(path):
    """A sigrok session file's sample rate, its number of samples and its channels' names."""
    rate, unit, prefix, names = None, 1, "logic-1", []
    try:
        with zipfile.ZipFile(path) as z:
            for line in z.read("metadata").decode("utf-8", "replace").splitlines():
                key, _, value = (s.strip() for s in line.partition("="))
                if key == "samplerate":
                    m = re.fullmatch(r"([0-9.]+)\s*([kMG]?Hz)?", value)
                    rate = float(m.group(1)) * HERTZ[(m.group(2) or "Hz").lower()] if m else None
                elif key == "unitsize":
                    unit = int(value)
                elif key == "capturefile":
                    prefix = value
                elif key.startswith("probe") and key[5:].isdigit():
                    names.append(value)
            size = sum(i.file_size for i in z.infolist()
                       if i.filename == prefix or i.filename.startswith(prefix + "-"))
    except (zipfile.BadZipFile, KeyError, ValueError):
        raise DecodeError("%s is not a sigrok session file (.sr)." % path)
    if not rate or not names:
        raise DecodeError("%s names no sample rate or no logic channels." % path)
    return rate, size // max(unit, 1), names


def _texts(rest):
    """An annotation's class and texts from the end of sigrok-cli's line: class: "text" "shorter" "shortest".

    sigrok-cli puts a text between quotes as it is, so a text that holds a quote looks like the end of one: a
    text is taken to end where a quote, a space and a quote follow each other, or at the line's end."""
    cls = ""
    if not rest.startswith('"'):
        cls, _, rest = rest.partition(': "')
        rest = '"' + rest
    text = rest.rstrip("\n")[1:]
    return cls, (text[:-1] if text.endswith('"') else text).split('" "')


def decode(path, words, only=None, start=None, end=None, limit=None, cancel=None, shorter=False):
    """Runs decoders on a saved capture (.lb16 or .sr) and returns what they found, as a dict.

    words     the decoders with their channels and options: "i2c scl=A1 sda=A0", or that as a list of words.
              Channels are named as the capture names them; an .lb16 capture's are always A0 ... B7 too
    only      the classes to list, as a list of names ("address-write", "bit"), from any decoder named.  Not
              given: all that the last decoder found except its single bits
    start, end   seconds from the capture's start: only what begins between them is listed
    limit     list this many at the most; "total" still counts them all
    cancel    a function that is asked now and then; when it returns true the decoders are stopped
    shorter   also give each annotation's "texts": the text and the shorter forms of it the decoder has, the
              longest first ("Address write: 16", "AW: 16", "16"), for a place that has little room

    The dict: file, decoders (what sigrok-cli was given), sample_rate, samples, trigger_sample and
    trigger_seconds (an .lb16 capture that had a trigger; else None), total, and annotations: each a dict of
    start and end (seconds from the capture's start), start_sample, end_sample, decoder, class and text, in the
    order of their starts."""
    parts = stack(words)
    kind = path.rsplit(".", 1)[-1].lower() if "." in os.path.basename(path) else ""
    wanted = {w for w in ([only] if isinstance(only, str) else only or ()) if w}
    with tempfile.TemporaryDirectory(prefix="logicboole-") as tmp:
        trigger = None
        if kind == "lb16":
            cap = Capture.load(path)
            rate, samples, trigger = cap.rate, cap.samples, cap.trigger
            names = {n.lower(): c for n, c in zip(cap.names, CHANNELS)}
            names.update((c.lower(), c) for c in CHANNELS)
            session = os.path.join(tmp, "capture.sr")
        elif kind == "sr":
            if not os.path.isfile(path):
                raise DecodeError("%s: there is no such file." % path)
            rate, samples, listed = _session(path)
            names = {n.lower(): n for n in listed}
            session = path
        else:
            raise DecodeError("%s: decoders run on a capture saved as .lb16 or .sr." % path)
        spec = _check(parts, names)
        out = {"file": path, "decoders": spec, "sample_rate": rate, "samples": samples, "trigger_sample": trigger,
               "trigger_seconds": None if trigger is None else trigger / rate, "total": 0, "annotations": []}
        if not samples:
            return out
        if kind == "lb16":
            cap.save(session, CHANNELS)
        first = 0 if start is None else start * rate
        last = float("inf") if end is None else end * rate
        top = parts[-1][0]
        args = [program(), "-i", session, "-P", spec, "-l", "4"]    # at this log level each line has its class
        if not wanted:
            args += ["-A", top]
        rows, stopped = [], False
        errors = os.path.join(tmp, "errors.txt")
        with open(errors, "w") as err:
            try:
                p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=err, encoding="utf-8", errors="replace")
            except OSError as e:
                raise DecodeError("sigrok-cli could not be run: %s" % e)
            try:
                for count, line in enumerate(p.stdout):
                    if cancel is not None and count % 4096 == 0 and cancel():
                        stopped = True
                        break
                    m = LINE.match(line)
                    if m is None:           # a text with a line break in it: the rest of the row before
                        continue
                    a = int(m.group(1))
                    if not first <= a < last:
                        continue
                    cls, texts = _texts(m.group(4))
                    if cls in wanted if wanted else not re.search(r"(^|-)bits?$", cls):
                        rows.append((a, -int(m.group(2)), m.group(3), cls, texts))
            finally:
                if stopped:
                    p.kill()
                p.stdout.close()
                status = p.wait()
        if stopped:
            raise DecodeError("The decoders were stopped.")
        if status:
            with open(errors, encoding="utf-8", errors="replace") as f:
                said = [s.strip() for s in f if re.match(r"(sr|srd|cli): (?!\[)", s) or s.startswith("Error")]
            raise DecodeError("sigrok-cli failed on %s: %s" % (spec, " ".join(said[-3:]) or "it does not say why."))
    rows.sort()
    out["total"] = len(rows)
    out["annotations"] = [{"start": a / rate, "end": -b / rate, "start_sample": a, "end_sample": -b,
                           "decoder": dec, "class": cls, "text": texts[0]}
                          for a, b, dec, cls, texts in (rows if limit is None else rows[:limit])]
    if shorter:
        for found, row in zip(out["annotations"], rows):
            found["texts"] = row[4]
    return out
