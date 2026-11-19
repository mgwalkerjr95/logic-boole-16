# Host software

Everything the computer needs for the Logic Boole 16: the app, a command line, and a Python package. It is plain
Python with no packages to install. The only other thing it needs is libusb.

- **The app** shows the waveforms in a browser, decodes I²C, SPI and serial (and other protocols with sigrok's
  decoders), measures, and saves captures.
- **The command line** takes captures into files for PulseView, GTKWave or a spreadsheet, reads times off
  them, runs sigrok's protocol decoders on them, and prints JSON for scripts.
- **The Python package** does the same from your own program.
- **For an AI agent** there are a skill file and a Model Context Protocol server, so that an agent can take
  captures and read them by itself.

The board has no memory of its own for firmware. Each time it is plugged in, the software loads the firmware and
the FPGA's design over USB, which takes about three seconds. Any command does this by itself when it is needed.

The firmware image is not in this repository, because it holds code of Infineon's SDK. The first time the
software loads a board it downloads the image (about 110 kB) from the repository's
[firmware-2 release](https://github.com/mgwalkerjr95/logic-boole-16/releases/tag/firmware-2), checks it against the SHA-256 in `logicboole/board.py` and keeps it:

| | Where it is kept |
|---|---|
| macOS | `~/Library/Caches/logicboole/` |
| Linux | `~/.cache/logicboole/` (or under `$XDG_CACHE_HOME`) |

After that no connection is needed. On a computer with no connection, download the file elsewhere and save it
there under the name the error message gives; it is checked the same way. `LOGICBOOLE_CACHE` names another folder.
An image of your own build, put into `logicboole/data/` by `make install` in `../firmware`, is used in its place.

## Setting up

You need Python 3.9 or later and libusb.

| | |
|---|---|
| macOS | `brew install libusb` |
| Debian, Ubuntu, Raspberry Pi OS | libusb is already installed. Install the USB rule below, once |

Windows is not supported. Then install the package:

    pipx install logicboole

(pipx: `brew install pipx` on a Mac, `sudo apt install pipx` on Debian, Ubuntu and Raspberry Pi OS, then
`pipx ensurepath` once.) `pip install logicboole` inside a virtual environment does the same, and gives your own
programs `import logicboole`. Then

    logicboole check

checks this computer and the board and says what to do about anything that is missing, and

    logicboole

opens the app.

From a copy of this repository nothing needs installing: `./logicboole` in its top folder does what `logicboole`
does in this README. `pipx install ./software` installs that copy as the package.

### Linux: the USB rule

Linux gives a USB device to root alone until a rule says otherwise. The rule comes with the package, and
`logicboole check` prints these commands with its place on your computer when the board cannot be opened. From a
copy of this repository, install it once with:

    sudo cp software/logicboole/udev/60-logic-boole.rules /etc/udev/rules.d/
    sudo udevadm control --reload
    sudo udevadm trigger

### macOS: allowing the board

A Mac laptop asks before a USB accessory it does not know may connect: "Allow accessory to connect?". Choose
Allow. The board comes to USB twice, as the FX3's boot loader when it is plugged in and under its own name when
its firmware starts, and macOS asked when a board's firmware first started here. `logicboole` waits about 20
seconds for the answer; after that it stops and says that macOS has not let the board connect, and the command
can be run again. Until macOS lets it connect, the board is on USB and no program can use it: `logicboole info`
says so. The setting is "Allow accessories to connect", under Privacy & Security in System Settings.

### With no board

`--fake` in front of any command uses a stand-in for the board that makes an I²C bus on A0 and A1:

    logicboole --fake
    logicboole --fake capture bus.sr

The app also makes simulated signals of its own: pick "Simulated signals" as the source.

## The app

    logicboole

This starts the app and opens `http://localhost:8016` in your browser. With a board on USB, the source is the
board: pick a rate and a length, set the two thresholds, and press Start (or the space bar).

- **Thresholds.** Each pod has its own. An input reads 1 above its pod's threshold. About half the signal's
  voltage is a good choice: 1.65 V for 3.3 V logic, 0.9 V for 1.8 V.
- **Trigger.** Pick a channel and an edge, and the board waits for it: Start becomes Stop until it comes. The
  length then counts from the trigger, and time zero is at the trigger. Stop ends the wait and shows what the
  board saw last. For levels, pulses by their length and several stages in turn, pick "other…" and type the
  trigger as the command line takes it: `A0=1 for 10us`, or `B7=f then B0=r` (the forms are under "Triggers"
  below). "Before the trigger", under Board, sets how much from before it is kept: a tenth of the length, half,
  as much again, or nothing.
- **Glitch filter.** Under Board. `50ns` leaves out pulses shorter than that on every input; `A0=20ns,B=1us`
  sets one channel and pod B. It works before the samples are kept and before the trigger sees them.
- **Capture clock.** Under Board. Pick an input and an edge, and the board takes a sample at each such edge in
  place of one every so many nanoseconds. The length is then a number of samples, and times in a trigger are
  numbers of samples too.
- **Analyzers.** Auto-detect looks for I²C, SPI and serial in a capture and names the channels. Add one by hand
  with the buttons under Analyzers. Decoded messages are listed beside the waveforms and can be searched.
- **Other protocols.** "More…" under Analyzers adds one of [sigrok](https://sigrok.org)'s decoders: pick it,
  give its channels inputs, and set its options. "On top" puts another decoder on what the first finds
  (`eeprom24xx` on `i2c`), and "Only" keeps some classes of what is found. These are run by `sigrok-cli` on this
  computer, so it has to be installed ("Protocol decoders" below says how); the three buses above need nothing.
- **Measuring.** Point at a pulse for its width, period, frequency and duty cycle. Shift-drag across the
  waveforms to set two markers and read the time between them.
- **Files.** Save writes an `.lb16` file with the capture, the channel names and the analyzers; Open reads it
  back. Export writes the decoded messages or the signal changes as CSV.
- **Built-in test.** Under Board, the input "Built-in test" captures the FPGA's test pattern in place of the
  probes and checks every word of it: a check of the path from the FPGA to the screen with nothing connected.

Options: `--port 8020` for another port, `--no-browser`, and `--host 0.0.0.0` to reach the app from another
computer, for a Raspberry Pi with no screen. The app has no password: use `--host 0.0.0.0` only on a network you
trust.

## The command line

    logicboole capture bus.sr --rate 10M --time 2 -a 1.65 -b 0.9

takes 2 seconds at 10 MS/s with pod A's threshold at 1.65 V and pod B's at 0.9 V, and writes `bus.sr`.

| Command | What it does |
|---|---|
| `logicboole` | The app |
| `logicboole check` | The first thing to run: checks this computer and the board, and says what to do about anything missing |
| `logicboole info` | What is on USB, the board's serial number, the thresholds, and the inputs' levels now |
| `logicboole capture FILE ...` | One capture, written to each file named |
| `logicboole threshold A [B]` | Sets the thresholds, in volts |
| `logicboole inputs` | The 16 inputs as they are now |
| `logicboole selftest` | Captures the FPGA's test pattern in both formats from 1 MS/s to 200 MS/s, with and without a trigger, and compares every word. `selftest --fast` does it with the fast design |
| `logicboole show FILE.lb16` | What a saved capture holds |
| `logicboole measure FILE.lb16 [CHANNEL ...]` | Periods, frequencies and pulse lengths read off a saved capture |
| `logicboole decode FILE DECODER ...` | What a protocol decoder finds in a saved capture: sigrok's decoders, through `sigrok-cli` |
| `logicboole decoders [NAME]` | The decoders there are, or what one of them takes |
| `logicboole convert IN.lb16 OUT ...` | A saved capture in other formats |
| `logicboole mcp` | A Model Context Protocol server, for an AI agent. See [With an AI agent](#with-an-ai-agent) |
| `logicboole load` | Loads the firmware and the FPGA's design again, or ones you built |
| `logicboole unload` | Sends the board back to its boot loader |

`logicboole COMMAND -h` lists a command's options.

**Options of `capture`**

| Option | |
|---|---|
| `-r`, `--rate` | Samples a second: `200M`, `100M`, `10M`, `1M` (the default), `250k`. The board samples at 200 MS/s divided by a whole number, and takes the nearest |
| `-t`, `--time` | How long: `2`, `0.5`, `20ms`. 1 second is the default. The FPGA counts the samples, so the length is exact |
| `-n`, `--samples` | How many samples, in place of `--time`. `0`: until Ctrl-C |
| `-a`, `-b` | The thresholds of pod A and pod B, in volts |
| `--raw` | Every sample is sent, not only the changes. For signals that change at nearly every sample. At 200 MS/s a raw capture holds up to 892,928 samples (4.46 ms) |
| `--test [SHIFT]` | The FPGA's test pattern in place of the probes, checked word for word |
| `--trigger WHAT` | Wait for this, and count the length from it. See below |
| `--before TIME` | With `--trigger`: how much from before the trigger is kept, as a time (`5ms`) or a number of samples. A tenth of the length if not given |
| `--timeout TIME` | With `--trigger`: stop waiting after this long |
| `--clock INPUT=EDGE` | A sample at each edge of one input, the circuit's own clock, in place of the sample rate. See below |
| `--glitch TIME` | A glitch filter: `20ns` for every input, or `A0=20ns,B=1us` for a channel and a pod |
| `--pi LINES` | Lines of the Raspberry Pi the board sits on, in place of probes: `i2c`, `uart`, `spi`, `all`, or single lines. See below |
| `--fast INPUTS` | The fast design: four inputs at 750 MS/s (`A0-A3`, `A4-A7`, `B0-B3`, `B4-B7`), a pod at 375 MS/s (`A`, `B`) or all 16 at 187.5 MS/s (`all`). See below |
| `--json` | The result as JSON |

Ctrl-C ends a capture early and keeps what it has.

**Triggers.** The FPGA looks for the trigger itself, in every sample it keeps, so nothing is missed between the
board and the computer.

    logicboole capture reset.sr --rate 200M --time 1ms --trigger "B0=f" --before 100us

| | |
|---|---|
| `B0=r` | B0 rises. `f`: falls. `e`: either |
| `A0=1 A1=0` | A0 is high and A1 is low |
| `A2=f A0=1` | A2 falls while A0 is high |
| `A0=1 for 10us` | A0 has been high for 10 µs, and still is |
| `A0=1 pulse 100ns..1us` | A pulse has ended: A0 was high for 100 ns to 1 µs, and is low now. `..50ns`: at most. `1us..`: at least |
| `in=r` | The TRIG IN pin rises (`in=f`, `in=e`) |
| `B7=f then B0=r` | Up to four of these in turn, each met by a later sample than the one before |

Times become numbers of samples at the capture's rate, so the shortest pulse a trigger can tell from the next
longer one is one sample long: 5 ns at 200 MS/s. A number with no unit is a number of samples.

The trigger pins are on header J7, 3.3 V logic: pin 1 TRIG IN, pin 2 TRIG OUT, pin 3 ground. TRIG OUT goes high
at the trigger and stays high to the end of the capture, to start a scope or another instrument: on one board it
came 75 to 80 ns after the trigger sample. `in=r` takes the trigger from another instrument, on signals up to
50 MHz.

The board keeps what it saw before the trigger in the FPGA's FIFO of 8192 words: with `--before`, up to its last
8184 changes of the inputs. The trigger does not wait for that time to pass first, so a trigger that comes soon
after the start has less before it, and the output says where in the capture the trigger is. If it never comes,
Ctrl-C or `--timeout` ends the wait, and the capture holds what the board saw last.

**A capture clock.** `--clock A0=r` keeps one sample at each rising edge of A0 (`f`: falling, `e`: both), for a
bus that has a clock of its own, up to 50 MHz: every sample is then one bit time of the bus, whatever the
clock's frequency does. The length is a number of samples (`-n`), and so are `--before` and the times in a
trigger. The board still looks at the inputs every 5 ns, and a sample is what the inputs were just before the
clock's edge: on one board, inputs that changed 15 ns or more before the edge were read at their new level every
time, and inputs that changed about 5 ns before it were not.

    logicboole capture spi.sr -n 100000 --clock B0=r

**The glitch filter.** With `--glitch 50ns`, a change of an input counts once the input has held its new level
for 50 ns, and shorter pulses are removed before the samples are kept and before the trigger sees them. The
longest is 1.25 µs, in steps of 5 ns. Every input is delayed by the same time, so their edges keep their places
against each other.

**The Raspberry Pi's lines.** On a Pi, nine pins of the Pi's header reach the board's FPGA: I2C1, the UART and
SPI0. With `--pi`, a capture takes them in place of probes, with no leads:

| `--pi` | Line | In place of | Solder jumper |
|---|---|---|---|
| `i2c` | GPIO2, SDA | A0 | JP2 |
| | GPIO3, SCL | A1 | JP3 |
| `uart` | GPIO14, TXD | A2 | JP9 |
| | GPIO15, RXD | A3 | JP10 |
| `spi` | GPIO10, MOSI | A4 | JP7 |
| | GPIO9, MISO | A5 | JP6 |
| | GPIO11, SCLK | A6 | JP8 |
| | GPIO8, CE0 | A7 | JP5 |
| | GPIO7, CE1 | B0 | JP4 |

`--pi all` takes all nine, and single lines go by either name: `--pi scl,gpio14`. The other channels stay probes.
Each line's solder jumper has to be closed, because the board is shipped with all of them open and an open one
reads low. A Pi line is 3.3 V logic and the pod's threshold does not act on it. The trigger, the capture clock
and the glitch filter work on it under the channel's name (`A0=f` here: SDA falls). The saved files name the
channel after the line, SDA, SCL, TXD and so on, and `decode` takes those names:

    logicboole capture i2c.sr --rate 2M --time 1 --pi i2c --trigger "A0=f A1=1" --timeout 30
    logicboole decode i2c.sr i2c scl=SCL sda=SDA

This is in the FPGA's design from version 5 on. On board 1 on a Raspberry Pi 5, an `i2cdetect` scan, SPI bytes
on both chip selects and UART bytes were captured this way and decoded as sent (`../gateware/README.md` has the
figures).

**The fast design** (`--fast`). The FPGA can be loaded with a second design that takes fewer inputs more often:
four inputs every 1.33 ns, a pod every 2.67 ns, or all 16 every 5.33 ns. It takes samples and sends them and has
nothing else, so `--fast` goes with `-t`, `-n`, `-a`, `-b`, `--raw` and `--test` only, and 10 ms is its default
length. The first capture with `--fast` loads the design, and the next capture without it loads the standard
design again; the thresholds stay as they were. The inputs that are not taken read low in the capture.

    logicboole capture --fast A0-A3 -t 10ms fast.lb16     # A0 to A3, 750 MS/s
    logicboole capture --fast B -t 50ms pod-b.sr          # pod B, 375 MS/s

In Python it is `board.capture_fast("A0-A3", seconds=0.01)`, and the agent's server takes it as `capture` with
`fast`. The app does not offer it yet.
The design's bitstream is `logicboole/data/gateware-fast.bin`; [`../gateware/README.md`](https://github.com/mgwalkerjr95/logic-boole-16/blob/main/gateware/README.md)
describes the design and its build. It has passed its self-test and captured clocks up to 85 MHz on one
board; the gateware README has what was measured.

**How much the board carries.** The board sends a word for every change of the inputs, or with `--raw` a word
for every two samples, and a little under 100 million words a second can leave it. On one board, a 48 MHz
clock on one input at 200 MS/s, 96 million words a second, was captured for 1 second; a 50 MHz clock, 100
million, overflowed after 4.4 ms. So a capture at 200 MS/s lasts as long as you ask while the 16 inputs
together change less often than that. A capture that overflows ends early, says so, and is whole up to its end.

**On a USB 2.0 port or cable** the board works, at 480 Mbit/s, and `logicboole info` says that the link is USB
2.0. It then carries about a ninth as much: on one board 11.1 million words a second (44 MB/s) arrived whole
for 20 seconds, and 12.5 million overflowed after 26 ms. That is every sample with `--raw` up to 22 MS/s, or
any rate while the inputs together change less than about 11 million times a second. `logicboole selftest`
passes on such a link and says which of its captures overflowed because of it.

**Files.** The ending of a file's name picks its format:

| | |
|---|---|
| `.sr` | A sigrok session: opens in PulseView and `sigrok-cli`, with their protocol decoders |
| `.vcd` | Value change dump: GTKWave and most waveform viewers |
| `.csv` | A line for every change: the time in seconds and the 16 levels |
| `.lb16` | The app's own file. Small, because it holds only the changes. `logicboole convert` turns it into the others |

An `.sr` file holds every sample, so a long capture at a high rate makes a large one that PulseView has to load
whole: 1 second at 100 MS/s is 200 MB in memory. Pick the lowest rate that shows what you need.

**Measuring.** `measure` reads times off a saved capture:

    logicboole capture bus.lb16 --rate 2M --time 1 -a 1.65
    logicboole measure bus.lb16 A1

    bus.lb16: 2 MS/s (a sample is 500 ns), from 0 s to 1 s
      A1: 23900 changes (11950 rising, 11950 falling); high at the start
          period    9.5 us ... 17.293 ms, the median 10 us  (100 kHz, high 93.91 % of the time)
          high for  4.5 us ... 17.288 ms, the median 5 us
          low for   4.5 us ... 5.5 us, the median 5 us

The period is from one rising edge to the next, and the frequency is 1 / the median period. A clock fast
against the sample rate has periods of two lengths (a 200 MHz clock at 750 MS/s: 3 and 4 samples), so its
median is held to one of them; for a steady clock the line also gives the average over all the periods, the
precise figure (in the JSON, `average_frequency`, always there). With no channel
named, `measure` takes the channels that change. `--from 10ms --to 12ms` looks at a part of the capture, and
`--list 20` adds the first 20 changes one by one, across the channels in the order they happened. A time read
off a capture is a whole number of samples, so it can be off by one sample at each of its ends.

**Protocol decoders.** `decode` runs [sigrok](https://sigrok.org)'s protocol decoders on a saved capture,
`.lb16` or `.sr`: I²C, SPI, serial, CAN, 1-Wire, JTAG, SWD and about a hundred more. It needs the `sigrok-cli`
program (`brew install sigrok-cli` on macOS; `sudo apt install sigrok-cli` on Debian, Ubuntu and Raspberry Pi
OS). Nothing else in the software needs it.

    logicboole decode bus.lb16 i2c scl=A1 sda=A0 --limit 6

    bus.lb16: 2 MS/s, 2000000 samples.  i2c scl=A1 sda=A0 found 3300:
        0.000020000 s  start          Start
        0.000030500 s  address-write  Address write: 16
        0.000100500 s  address-write  Write
        0.000110500 s  ack            ACK
        0.000120500 s  data-write     Data write: 4C
        0.000200500 s  ack            ACK
      ... and 3294 more.

After the decoder's name come its channels and its options as `name=value`: `uart rx=B0 baudrate=115200`,
`spi clk=A0 mosi=A1 miso=A2 cs=A3`. A second decoder's name puts that decoder on top of the first:
`i2c scl=A1 sda=A0 eeprom24xx`. `logicboole decoders` lists the decoders, and `logicboole decoders spi` the
channels and options of one.

| Option | |
|---|---|
| `--only CLASS,...` | List only these classes of what was found: `--only address-write,data-write`. The second column of the list is the class |
| `--from TIME`, `--to TIME` | List what begins in this part of the capture |
| `--limit N` | List N at the most |
| `--json` | The result as JSON: every item with its start and end, in seconds and in samples |

What a decoder says about each single bit is left out unless `--only` asks for it. The decoders, and the texts
they give, are sigrok's own; this was tested with sigrok-cli 0.7.2. A channel or an option the decoder does not
have is refused before sigrok-cli runs, with the names it does have.

PulseView and `sigrok-cli` itself open the `.sr` files:

    logicboole capture bus.sr --rate 2M --time 1 -a 1.65
    sigrok-cli -i bus.sr -P i2c:scl=A1:sda=A0 -A i2c

**For scripts.** `--json` on `info`, `inputs`, `threshold`, `capture`, `show`, `measure`, `decode` and
`decoders` prints JSON. The exit status is 0 when the command worked and the capture passed its checks. Lines about loading the board go to the standard error. With
a trigger, `trigger_sample` and `trigger_seconds` say where in the capture it is; they are `null` with no
trigger, and when the trigger did not come.

    logicboole capture --json --rate 1M --time 0.5 run.lb16

```json
{
 "ok": true,
 "problems": [],
 "overflow": false,
 "sample_rate": 1000000.0,
 "samples": 500000,
 "trigger_sample": null,
 "thresholds": [1.65, 0.9],
 "channels": [{"channel": "A0", "changes": 2440, "first": 2e-05, "starts": 1}, ...]
}
```

## From Python

```python
import logicboole

with logicboole.connect() as board:            # loads the board if it has just been plugged in
    board.set_thresholds(1.65, 0.9)            # volts: pod A, pod B
    cap = board.capture(rate=10e6, seconds=2)

print(cap.ok, cap.overflow, cap.samples, cap.rate)
for value, length in zip(cap.values, cap.lengths):      # runs: the 16 inputs, and for how many samples
    ...
initial, changes = cap.edges()[0]              # A0: its first level, and the samples at which it changes
cap.save("bus.sr")                             # or .vcd, .csv, .lb16

with logicboole.connect() as board:            # wait for B0 to fall, for 2 s at the most
    cap = board.capture(rate=200e6, seconds=1e-3, trigger="B0=f", before=100e-6, timeout=2)
if cap.trigger is None:
    print("no trigger:", cap.stopped)
else:
    print("triggered at sample", cap.trigger, "of", cap.samples)
```

Without installing the package, add the `software` folder to `sys.path` first.

```python
from logicboole import Capture, decode

cap = Capture.load("bus.lb16")
scl = cap.measure(["A1"])["channels"][0]       # times in seconds
print(scl["frequency"], scl["period"], scl["low"])

# sigrok's decoders, through sigrok-cli
found = decode.decode("bus.lb16", "i2c scl=A1 sda=A0", only=["address-write", "data-write"])
for a in found["annotations"]:
    print(a["start"], a["class"], a["text"])
```

| | |
|---|---|
| `logicboole.connect()` | The board, loaded and ready, as a `Board` |
| `logicboole.look()` | What is on USB, without changing anything |
| `Board.capture(rate, seconds, samples, fmt, test, a, b, ...)` | One capture, as a `Capture` |
| `Board.capture(..., trigger, before, samples_before, timeout, clock, glitch, pi)` | The same with a trigger, a capture clock, a glitch filter or the Pi's lines: the command line's options |
| `Board.set_thresholds(a, b)`, `Board.thresholds()` | Volts at the probe |
| `Board.inputs()` | The 16 inputs now, bit 0 is A0 and bit 15 is B7 |
| `Board.read(register, n)`, `Board.write(register, data)` | The FPGA's registers |
| `Capture.values`, `.lengths`, `.rate`, `.samples`, `.thresholds` | The capture as runs |
| `Capture.ok`, `.problems`, `.overflow`, `.stopped` | Whether it passed its checks, and how it ended |
| `Capture.trigger` | The number of the trigger sample, or `None` |
| `Capture.edges()`, `.summary()`, `.save(path)`, `Capture.load(path)` | |
| `Capture.measure(channels, start, end, listed)` | Periods, pulse lengths and changes, as the command line's `measure --json` |
| `decode.decode(path, words, only, start, end, limit)` | sigrok's decoders on a saved capture, as `decode --json` |
| `decode.decoders()`, `decode.about(name)` | The decoders sigrok-cli has, and what one takes |

## With an AI agent

An AI agent that can run commands needs only the command line: with `--json` every answer is JSON, and
`--fake` lets it try a command with no board. Two things make it easier.

**The skill.** [`skills/logic-boole-16/SKILL.md`](https://github.com/mgwalkerjr95/logic-boole-16/blob/main/software/skills/logic-boole-16/SKILL.md) tells an agent how to use the
board: the steps from a first look to a report, how to pick a rate and a threshold, the triggers, what the
answers mean, and what to check when a channel shows nothing. It is an Agent Skill, a folder with one text
file. For Claude Code, copy the folder to `~/.claude/skills/`, or to `.claude/skills/` in a project:

    cp -r software/skills/logic-boole-16 ~/.claude/skills/

For another agent, put the folder where that agent keeps its skills, or give it the file's text as
instructions.

**The MCP server.** `logicboole mcp` is a Model Context Protocol server, for an agent that works with tools
and not with a shell. The agent's program starts it and talks to it on its standard input and output. In the
form most such programs take:

```json
{"mcpServers": {"logicboole": {"command": "logicboole", "args": ["mcp"]}}}
```

If the package is not installed, give the full path of the `logicboole` launcher in this repository as the
command. For Claude Code: `claude mcp add logicboole -- logicboole mcp`.

| Tool | |
|---|---|
| `board_info` | What is on USB and the board's settings, with nothing changed |
| `set_thresholds` | The two pods' thresholds |
| `read_inputs` | The 16 inputs as they are now |
| `capture` | One capture, with a trigger, a capture clock or a glitch filter if wanted, saved to a file. With `fast`, the fast design: 4 inputs at 750 MS/s, a pod at 375 MS/s or all 16 at 187.5 MS/s |
| `measure` | Periods, frequencies, pulse lengths and changes read off a saved capture |
| `decode` | What a protocol decoder finds in a saved capture (needs `sigrok-cli`) |
| `list_decoders` | The decoders there are, or what one of them takes |
| `self_test` | The path from the FPGA to the computer, checked with the FPGA's test pattern. With `fast`, the fast design's |

`capture` saves an `.lb16` file and answers with its name and a summary; `measure` and `decode` are given that
name. The files go into a folder for temporary files, or into the one named with `logicboole mcp --dir FOLDER`,
and stay there until you delete them. With a trigger, `capture` waits 10 seconds for it unless told otherwise.
The server does one thing at a time, and opens the board only while a tool uses it, so the app and the command
line can be used between an agent's captures. `logicboole --fake mcp` serves the stand-in board.

The server speaks the protocol's revisions from 2024-11-05 to 2026-07-28: both the form that begins with
`initialize` and the newer one in which every request stands alone.

## What a capture checks

Every capture is checked before it is handed over: the end of the data arrived, the number of words read equals
the number the FPGA says it wrote, the FX3 counted no errors at its parallel port, and the samples in the words
equal the samples the FPGA says it took. Anything else is listed in `problems`, and the command line then prints
`WRONG` and exits with status 1.

**Overflow** is not a fault of the board. Run-length capture sends a word for every change of the inputs, and
when changes come faster than USB and the computer take them, the FPGA stops sampling. The capture is then
shorter than asked for and whole up to its last sample: there are no gaps inside a capture. Use a lower rate, or
`--raw` for signals that change at nearly every sample.

## Checking the software and a board

    python3 software/tests/test_host.py     # the Python side against the stand-in board; needs no sigrok-cli
    node software/tests/test_web.mjs        # the app's decoders and file formats (needs Node.js)
    logicboole selftest                   # a real board: the whole path, every word compared

## Changing it

| File | What is in it |
|---|---|
| `logicboole/usb.py` | libusb through ctypes: vendor requests and the queued reads of the bulk endpoint |
| `logicboole/board.py` | Loading the board, its registers and thresholds, and the steps of a capture |
| `logicboole/words.py` | The two formats of the stream, and the test pattern |
| `logicboole/fast.py` | The fast design: its choices of inputs, and its frames as samples |
| `logicboole/trigger.py` | The trigger, the capture clock, the glitch filter and the Pi's lines: their text, and the FPGA's registers |
| `logicboole/capture.py` | A finished capture, the file formats, and the measuring |
| `logicboole/decode.py` | sigrok's decoders on a saved capture: what `sigrok-cli` is asked, and what it answers |
| `logicboole/cli.py` | The command line |
| `logicboole/mcp.py` | The Model Context Protocol server: the protocol, and the tools an agent is given |
| `skills/logic-boole-16/SKILL.md` | The skill: how an agent uses the board through the command line |
| `logicboole/server.py` | The app's side on the computer: serves the pages and does what they ask of the board |
| `logicboole/web/` | The app's pages: `app.js` (the screen), `capture.js` (a capture as lists of edges), `decoders.js` (I²C, SPI, serial), `sigrok.js` (sigrok's decoders: what is asked and how the answer is laid out), `detect.js` (auto-detect), `demo.js` (simulated signals) |
| `logicboole/fake.py` | The stand-in board |
| `logicboole/selftest.py` | The captures of `logicboole selftest` |
| `logicboole/data/` | The FPGA bitstreams that are loaded into the board (`gateware.bin`, and `gateware-fast.bin` for the fast design), and the place for a firmware image of your own build |

[`../docs/protocol.md`](https://github.com/mgwalkerjr95/logic-boole-16/blob/main/docs/protocol.md) describes the USB requests, the FPGA's registers and the stream.
The firmware's source is in [`../firmware`](https://github.com/mgwalkerjr95/logic-boole-16/tree/main/firmware) and the FPGA's in [`../gateware`](https://github.com/mgwalkerjr95/logic-boole-16/tree/main/gateware); each
has a README on building it. `logicboole load --firmware FILE.img --gateware FILE.bin` runs a build of yours on
the board without installing it.

A new protocol decoder for the app is a function in `web/decoders.js` that takes a capture and returns rows; the
three that are there are examples. A new file format is a method of `Capture` in `capture.py`.

## License

Copyright (C) 2026 Michael Walker.

The host software is free software: you can redistribute it and/or modify it under the terms of the GNU General
Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option)
any later version. It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even
the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](https://github.com/mgwalkerjr95/logic-boole-16/blob/main/software/LICENSE) for the GNU
General Public License.

The files in `logicboole/data/` that are loaded into the board come under the terms of what they are built
from: see [logicboole/data/NOTICE.md](https://github.com/mgwalkerjr95/logic-boole-16/blob/main/software/logicboole/data/NOTICE.md).
