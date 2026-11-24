# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""The skill file (skills/logic-boole-16/SKILL.md): how an AI agent uses the board.  "logicboole skill" prints or
installs it, and the MCP server's "guide" tool gives it to an agent that has no shell, in parts if asked."""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = "logic-boole-16"

# For an agent that reads the guide through MCP: the skill is written for the command line
MCP_PREFACE = """\
This guide is the skill file written for the `logicboole` command line.  Through MCP each command is a tool of
the same name and purpose, and its options are the tool's arguments:

| Command line | MCP |
|---|---|
| `logicboole info --json` | `board_info` |
| `logicboole threshold A B` | `set_thresholds` (a, b) |
| `logicboole inputs` | `read_inputs` |
| `logicboole capture FILE --rate R --time T -a A -b B --trigger W --before T --timeout T --name N` | `capture` (rate, seconds, a, b, trigger, before, timeout, names; clock, glitch, pi, fast, raw as their options): it picks the file and names it in its answer. More files: `save_as`, which refuses one that is there already unless `overwrite` is true |
| `logicboole measure FILE CH ... --from --to --list N` | `measure` (file, channels, start, end, list) |
| `logicboole find FILE "TEXT" --from --to --limit N` | `find` (file, text, start, end, limit) |
| `logicboole detect FILE` | `detect` (file) |
| `logicboole plot FILE CH ... --from --to --mark T --decode W` | `plot` (file, channels, start, end, marks, decoders): the picture comes back in the answer |
| `logicboole decode FILE WORDS --only --from --to --limit` | `decode` (file, decoders, only, start, end, limit) |
| `logicboole decoders [NAME]` | `list_decoders` (name) |
| `logicboole selftest [--fast]` | `self_test` (fast) |

Times are given to the tools in seconds, as numbers (0.002, not "2ms").  `capture`'s `timeout` is 10 seconds
if not given, both for a trigger and for a capture clock (with a clock it is the whole capture's time): give a
longer one when the event or the clock may take longer.  Where the text below says to run a command, call the
tool.  `guide` with a `topic` gives one section: %s.

"""


def skill_file():
    """The skill's SKILL.md: in the installed package, or in a copy of the repository beside the package."""
    for folder in (os.path.join(HERE, "skills"), os.path.join(HERE, os.pardir, "skills")):
        path = os.path.join(folder, SKILL, "SKILL.md")
        if os.path.isfile(path):
            return os.path.normpath(path)
    raise OSError("The skill file is not with this software (looked for skills/%s/SKILL.md)." % SKILL)


def skill_text():
    with open(skill_file(), encoding="utf-8") as f:
        return f.read()


def sections(text=None):
    """The skill's sections after its front matter, as [(heading, text)]; the part before the first heading is
    under the heading ""."""
    text = skill_text() if text is None else text
    body = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    parts = re.split(r"^## ", body, flags=re.M)
    out = [("", parts[0])]
    for p in parts[1:]:
        head, _, rest = p.partition("\n")
        out.append((head.strip(), "## " + p))
    return out


def guide(topic=None):
    """The guide for an agent on MCP: all of it, or the sections whose headings hold topic."""
    parts = sections()
    names = ", ".join("\"%s\"" % h for h, _ in parts if h)
    if not topic:
        return MCP_PREFACE % names + "".join(t for _, t in parts)
    wanted = [t for h, t in parts if h and topic.strip().lower() in h.lower()]
    if not wanted:
        return "No section's heading has \"%s\" in it.  The sections: %s." % (topic, names)
    return "".join(wanted)
