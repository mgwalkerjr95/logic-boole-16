# Host software

Everything the computer needs for the Logic Boole 16: the app, a command line, and a Python package. It is plain
Python with no packages to install. The only other thing it needs is libusb.

- **The app** shows the waveforms in a browser, decodes I²C, SPI and serial (and other protocols with sigrok's
  decoders), measures, and saves captures.
- **The command line** takes captures into files for PulseView, GTKWave or a spreadsheet, reads times off
  them, finds moments in them, says what is on each channel, draws them as pictures, runs sigrok's protocol
  decoders on them, and prints JSON for scripts.
- **The Python package** does the same from your own program.
- **For an AI agent** there are a skill file and a Model Context Protocol server, so that an agent can take
  captures and read them by itself.

The board has no memory of its own for firmware. Each time it is plugged in, the software loads the firmware and
the FPGA's design over USB, which takes about three seconds. Any command does this by itself when it is needed.
A board that stayed powered while the software was updated (on a Raspberry Pi, say) still runs the old ones: the
next command loads the new ones and says so in a line:
`The board runs firmware version 1; this software comes with version 2: loading it.`

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

Windows is not supported yet: the board needs a driver there that it does not have. Then install the package:

    pipx install logicboole

(pipx: `brew install pipx` on a Mac, `sudo apt install pipx` on Debian, Ubuntu and Raspberry Pi OS, then
`pipx ensurepath` once and a new terminal window.) `pip install logicboole` inside a virtual environment does the
same, and gives your own programs `import logicboole`. `pipx upgrade logicboole` updates it. Then

    logicboole check

checks this computer and the board and says what to do about anything that is missing, and

    logicboole

opens the app.

From a copy of this repository nothing needs installing: `./logicboole` in its top folder does what `logicboole`
does in this README. `pipx install ./software` installs that copy as the package.

When something does not work, [`../docs/troubleshooting.md`](https://github.com/mgwalkerjr95/logic-boole-16/blob/main/docs/troubleshooting.md)
goes through what to check. For help, write to support@nearfieldsupply.com, or open an issue at
https://github.com/mgwalkerjr95/logic-boole-16/issues.

### Linux: the USB rule

Linux gives a USB device to root alone until a rule says otherwise. The rule comes with the package, and
`logicboole check` prints these three commands, joined into one line, with the rule's place on your computer, when
the board cannot be opened. From a copy of this repository, install it once with:

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

The app also makes simulated signals of its own. It shows them when no board is on USB, and with a board they are
under Source, as "Simulated signals".

## The app

    logicboole

This starts the app and opens `http://localhost:8016` in your browser. With a board on USB, the source is the
board and the scope is empty ("Press Start to capture"): pick a rate and a length, set the two thresholds, and
press Start (or the space bar). With no board, the app shows its simulated signals under a strip marked
SIMULATED, and the board's card says "No board on USB.  Plug one in: the app finds it by itself." A board plugged
in then becomes the source.

- **Thresholds.** Each pod has its own, from 0.3 V to 3 V on a V1.0.0 board; one outside that is refused, with
  the reason. An input reads 1 above its pod's threshold. About half the signal's voltage is a good choice:
  1.65 V for 3.3 V logic, 0.9 V for 1.8 V. After a board has been loaded both are at 1.56 V.
- **Trigger.** Pick a channel and an edge, and the board waits for it: Start becomes Stop until it comes. The
  length then counts from the trigger, and time zero is at the trigger. Stop ends the wait and shows what the
  board saw last. A page reloaded during the wait takes it over, with Stop; a wait that no page has watched for
  10 seconds is ended. For levels, pulses by their length and several stages in turn, pick "other…" and type the
  trigger as the command line takes it: `A0=1 for 10us`, or `B7=f then B0=r` (the forms are under "Triggers"
  below). "Before the trigger", under Board, sets how much from before it is kept: a tenth of the length, half,
  as much again, or nothing.
- **Glitch filter.** Under Board. `50ns` leaves out pulses shorter than that on every input; `A0=20ns,B=1us`
  sets one channel and pod B. It works before the samples are kept and before the trigger sees them.
- **Capture clock.** Under Board. Pick an input and an edge, and the board takes a sample at each such edge in
  place of one every so many nanoseconds. The length is then a number of samples, and times in a trigger are
  numbers of samples too.
- **Analyzers.** Auto-detect looks for I²C, SPI and serial (8N1, 8E1, 8O1 and 7E1) in a capture and names the
  channels: `SDA`, `SCL`, `SPI_CLK`, `SPI_CS`, `SPI_MOSI`, `SPI_MISO`, `UART`, with a number from 2 on when a
  name is taken (`UART2`). Add one by hand with the buttons under Analyzers. A serial line's speed is typed, or
  picked from the list; Measure reads it off the capture. Decoded messages are listed beside the waveforms and
  can be searched.
- **Other protocols.** "More…" under Analyzers adds one of [sigrok](https://sigrok.org)'s decoders: pick it,
  give its channels inputs, and set its options. "On top" puts another decoder on what the first finds
  (`eeprom24xx` on `i2c`), and "Only" keeps some classes of what is found. These are run by `sigrok-cli` on this
  computer, so it has to be installed ("Protocol decoders" below says how); the three buses above need nothing.
  Cancel stops one, and one that runs for 120 seconds is stopped.
- **Measuring.** Point at a pulse for its width, period, frequency and duty cycle. For the time between two
  points, Shift-drag across the waveforms, or click the time scale: a click sets marker A, the next B, and
  Shift-click moves B. B − A is shown once both are set.
- **Files.** Save writes an `.lb16` file with the capture, the channel names and the analyzers; Open reads it
  back, and refuses a file that is not a capture. Export writes the decoded messages or the signal changes as
  CSV. "Previous capture" (or Backspace) brings back the capture that a new one replaced, one step back.
- **Built-in test.** Under Board, the input "Built-in test" captures the FPGA's test pattern in place of the
  probes and checks every word of it: a check of the path from the FPGA to the screen with nothing connected.

Options: `--port 8020` for another port, `--no-browser`, and `--host 0.0.0.0` to reach the app from another
computer, for a Raspberry Pi with no screen: it prints the addresses to open there, the computer's `.local` name
and its IP address. The app has no password: use `--host 0.0.0.0` only on a network you trust. Ctrl-C, or
closing the terminal, stops the app; a capture it is running is ended first, so the board is left free.

## The command line

    logicboole capture bus.lb16 bus.sr --rate 10M --time 2 -a 1.65 -b 0.9

takes 2 seconds at 10 MS/s with pod A's threshold at 1.65 V and pod B's at 0.9 V, and writes `bus.lb16` and
`bus.sr`.

| Command | What it does |
|---|---|
| `logicboole` | The app |
| `logicboole check` | The first thing to run: checks this computer and the board, and says what to do about anything missing |
| `logicboole info` | What is on USB, the board's serial number and revision, the range of thresholds it takes, the thresholds, and the inputs' levels now |
| `logicboole capture FILE ...` | One capture, written to each file named |
| `logicboole threshold A [B]` | Sets the thresholds, in volts |
| `logicboole inputs` | The 16 inputs as they are now |
| `logicboole selftest` | Captures the FPGA's test pattern in both formats from 1 MS/s to 200 MS/s, with and without a trigger, and compares every word. `selftest --fast` does it with the fast design |
| `logicboole show FILE` | What a saved capture (`.lb16` or `.sr`) holds |
| `logicboole measure FILE [CHANNEL ...]` | Periods, frequencies and pulse lengths read off a saved capture (`.lb16` or `.sr`) |
| `logicboole find FILE TEXT` | Every moment in a saved capture that a trigger's text describes: glitches, gaps, one edge after another |
| `logicboole detect FILE` | What is on each channel of a saved capture: I²C, SPI, serial, a clock, PWM |
| `logicboole plot FILE [CHANNEL ...]` | A picture of a saved capture, as a PNG file |
| `logicboole decode FILE DECODER ...` | What a protocol decoder finds in a saved capture: sigrok's decoders, through `sigrok-cli` |
| `logicboole decoders [NAME]` | The decoders there are, or what one of them takes |
| `logicboole convert IN OUT ...` | A saved capture (`.lb16` or `.sr`) in other formats |
| `logicboole mcp` | A Model Context Protocol server, for an AI agent. See [With an AI agent](#with-an-ai-agent) |
| `logicboole skill [--install]` | The skill file for an AI agent: printed, or installed where Claude Code finds it |
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
| `--test [SHIFT]` | The FPGA's test pattern in place of the probes, checked word for word. SHIFT is 0 to 15, 8 if not given |
| `--trigger WHAT` | Wait for this, and count the length from it. See below |
| `--before TIME` | With `--trigger`: how much from before the trigger is kept, as a time (`5ms`) or a whole number of samples. A tenth of the length if not given |
| `--timeout TIME` | With `--trigger`: stop waiting after this long. With `--clock`: how long the whole capture may take |
| `--clock INPUT=EDGE` | A sample at each edge of one input, the circuit's own clock, in place of the sample rate. See below |
| `--glitch TIME` | A glitch filter: `20ns` for every input, or `A0=20ns,B=1us` for a channel and a pod. 10 ns to 1.25 µs |
| `--pi LINES` | Lines of the Raspberry Pi the board sits on, in place of probes: `i2c`, `uart`, `spi`, `all`, or single lines. See below |
| `--fast INPUTS` | The fast design: four inputs at 750 MS/s (`A0-A3`, `A4-A7`, `B0-B3`, `B4-B7`), a pod at 375 MS/s (`A`, `B`) or all 16 at 187.5 MS/s (`all`). See below |
| `--name CH=NAME,...` | Names for channels, saved with the capture: `A0=SDA,A1=SCL`. See below |
| `--json` | The result as JSON |

Ctrl-C ends a capture early and keeps what it has. While the board waits for a trigger, or a capture of no set
length runs, the command says so on the standard error; on a terminal it also shows how far a capture has got.
A file that cannot be written is found before the capture starts, and with no file the capture is shown and not
saved.

**Triggers.** The FPGA looks for the trigger itself, in every sample it keeps, so nothing is missed between the
board and the computer.

    logicboole capture reset.sr --rate 200M --time 1ms --trigger "B0=f" --before 100us --timeout 10

While it waits it prints `Waiting for the trigger (B0=f), 10 s at the most.  Ctrl-C stops.`, and Ctrl-C ends the
wait.

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
longer one is one sample long: 5 ns at 200 MS/s. A pulse time that rounds to no sample at all is refused, and
the message gives the sample's length. A number with no unit is a number of samples.

The trigger pins are on header J7, 3.3 V logic: pin 1 TRIG IN, pin 2 TRIG OUT, pin 3 ground. TRIG OUT goes high
at the trigger and stays high to the end of the capture, to start a scope or another instrument: on one board it
came 75 to 80 ns after the trigger sample. `in=r` takes the trigger from another instrument, on signals up to
50 MHz.

The board keeps what it saw before the trigger in the FPGA's FIFO of 8192 words: with `--before`, up to its last
8184 changes of the inputs. The trigger does not wait for that time to pass first, so a trigger that comes soon
after the start has less before it, and the output says where in the capture the trigger is. If it never comes,
Ctrl-C or `--timeout` ends the wait, and the capture holds what the board saw last: the command says
`NOT TRIGGERED`, and after `--timeout` it exits with status 3.

**A capture clock.** `--clock A0=r` keeps one sample at each rising edge of A0 (`f`: falling, `e`: both), for a
bus that has a clock of its own, up to 50 MHz: every sample is then one bit time of the bus, whatever the
clock's frequency does. The length is a number of samples (`-n`), and so are `--before` and the times in a
trigger. The board still looks at the inputs every 5 ns, and a sample is what the inputs were just before the
clock's edge: on one board, inputs that changed 15 ns or more before the edge were read at their new level every
time, and inputs that changed about 5 ns before it were not.

    logicboole capture spi.sr -n 100000 --clock B0=r --timeout 10

With a capture clock, `--timeout` is how long the whole capture may take: a clock that stops, or never starts,
ends the capture there with the samples it has, and the command exits with status 3.

**The glitch filter.** With `--glitch 50ns`, a change of an input counts once the input has held its new level
for 50 ns, and shorter pulses are removed before the samples are kept and before the trigger sees them. It is
10 ns to 1.25 µs, in steps of 5 ns. Every input is delayed by the same time, so their edges keep their places
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
and the glitch filter work on it under the channel's id (`A0=f` here: SDA falls), and the trigger under the
line's name as well (`SDA=f`). The saved files name the channel after the line, SDA, SCL, TXD and so on, and
`decode` takes those names:

    logicboole capture i2c.sr --rate 2M --time 1 --pi i2c --trigger "SDA=f SCL=1" --timeout 30
    logicboole decode i2c.sr i2c scl=SCL sda=SDA

This is in the FPGA's design from version 5 on. On board 1 on a Raspberry Pi 5, an `i2cdetect` scan, SPI bytes
on both chip selects and UART bytes were captured this way and decoded as sent (`../gateware/README.md` has the
figures).

**The fast design** (`--fast`). The FPGA can be loaded with a second design that takes fewer inputs more often:
four inputs every 1.33 ns, a pod every 2.67 ns, or all 16 every 5.33 ns. It takes samples and sends them and has
nothing else, so `--fast` goes with `-t`, `-n`, `-a`, `-b`, `--raw` and `--test` only, and 10 ms is its default
length. The first capture with `--fast` loads the design, and the next capture without it loads the standard
design again; the thresholds stay as they were. The inputs that are not taken read low in the capture.

A busy signal at these rates makes data faster than a computer can hold it as samples, so a fast capture takes
data up to a 32nd of the computer's memory and changes up to an 8th of it, and ends early there, whole up to its
last sample, saying it had grown too large. On a Raspberry Pi with 8 GB that is 0.7 s of `--raw`, and about
0.15 s of a signal that changes in nearly every sample. Captures of milliseconds, as below, are far from it.

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

The computer holds a capture's words in its memory, up to a 32nd of it (256 MB, 64 million changes, on a
computer with 8 GB). A capture that grows past that ends there, whole up to its last sample, and says it had
grown too large: the 48 MHz clock above fills it in 0.7 s on such a computer. Quiet buses at any rate, and
busy signals for a short time, are far from it.

**On a USB 2.0 port or cable** the board works, at 480 Mbit/s, and `logicboole info` says that the link is USB
2.0. It then carries about a ninth as much: on one board 11.1 million words a second (44 MB/s) arrived whole
for 20 seconds, and 12.5 million overflowed after 26 ms. That is every sample with `--raw` up to 22 MS/s, or
any rate while the inputs together change less than about 11 million times a second. A capture that overflows
on such a link says so. `logicboole selftest` passes on such a link and says which of its captures overflowed
because of it.

**Files.** The ending of a file's name picks its format:

| | |
|---|---|
| `.sr` | A sigrok session: opens in PulseView and `sigrok-cli`, with their protocol decoders |
| `.vcd` | Value change dump: GTKWave and most waveform viewers |
| `.csv` | A line for every change: the time in seconds and the 16 levels |
| `.lb16` | The app's own file. Small, because it holds only the changes. `logicboole convert` turns it into the others |

An `.sr` file holds every sample, so a long capture at a high rate makes a large one that PulseView has to load
whole: 1 second at 100 MS/s is 200 MB in memory. Pick the lowest rate that shows what you need. `show`,
`measure`, `find`, `detect`, `plot`, `decode` and `convert` read `.sr` files as well as `.lb16`, PulseView's own
among them. An `.sr` file written by this software also keeps the trigger's place and the thresholds, which these
commands read back (`sigrok-cli` passes over them). Any other file is refused with a line that says what they
read.

An `.lb16` file holds 2^32 samples (21.47 s at 200 MS/s, 71.6 minutes at 1 MS/s). A longer capture into one is
refused before it starts; one of no set length (`-n 0`) that runs past it is written as an `.sr` file of the
same name in its place.

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

**Names.** `--name A0=SDA,A1=SCL` gives channels names, which are saved in the `.lb16` and `.sr` files and shown
by the app. The capture's `--trigger`, and `measure`, `find`, `plot` and `decode` on the file, then take a name
where they take a channel's id (`measure bus.lb16 SCL`, `decode bus.lb16 i2c scl=SCL sda=SDA`), and the output
gives both. A name is up to 24 letters, digits and `_ . + # / -`. Names given in the app are in its files too.

**Finding moments.** `find` takes the text of a trigger and lists every moment in a saved capture that meets it,
by the trigger's own rules, so it finds what that trigger would have caught. With a pulse or levels last it says
how long each lasted; with stages, the time from the first.

    logicboole capture bus.lb16 --rate 2M --time 1 -a 1.65 --name A0=SDA,A1=SCL
    logicboole find bus.lb16 "SCL=1 for 100us" --limit 3

    bus.lb16: "A1=1 for 100us" found 200 times from 0 s to 1 s (a sample is 500 ns):
        0.001480000 s   held 112.5 us, from 0.001380500 s
        0.002062500 s   held 112.5 us, from 0.001963000 s
        0.002630000 s   held 112.5 us, from 0.002530500 s
      ... and 197 more (--limit lists more).
      held: the shortest 112 us, the median 112.5 us, the longest 17.288 ms

That is every gap of 100 µs or more between the bus's messages, with how long each lasted. `"A0=0 pulse
..2us"` lists every low pulse on A0 of 2 µs or less (times are counted in whole samples, 500 ns each here, and a
time that rounds to none is refused), `"CS=f then SCLK=e"` the time from each fall of CS to the next clock edge. `--from` and `--to` look in a part of the capture, and `--limit` (20) is how many are listed;
the total is always counted. A line that stays at the levels described is found once, where it gets there.

**What is on each channel.** `detect` does what the app's auto-detect does:

    logicboole detect bus.lb16

    bus.lb16: 2 MS/s for 1 s
      I2C      SDA A0, SCL A1; 100 kHz; 1300 bytes
                 decode: i2c scl=A1 sda=A0
      steady: A2 A3 A4 A5 A6 A7 B0 B1 B2 B3 B4 B5 B6 B7 low
      The wiring is a guess from the signals: check it.  To name these channels in a capture: --name A0=SDA,A1=SCL

It finds I²C, SPI with one chip select (with its mode) and serial lines (at the standard baud rates, as 8N1, 8E1,
8O1 or 7E1: `serial   A6 at 115200 baud, 7E1`), and says of every other channel whether it is a clock or PWM (with
its frequency and duty), steady, or data with no bus found. The `decode` line is what to give `decode`, and the
names are `SDA`, `SCL`, `SPI_CLK`, `SPI_CS`, `SPI_MOSI`, `SPI_MISO` and `UART`, with a number from 2 on when a
name is taken (`UART2`).

**Pictures.** `plot` writes a PNG: the channels as waveforms over a time axis, the trigger as a dashed red line,
numbered lines at the `--mark` times, and a row for each `--decode` with what that decoder found (`--decode`
needs `sigrok-cli`).

    logicboole plot bus.lb16 --from 1.47ms --to 1.75ms --mark 1.488ms --decode "i2c scl=SCL sda=SDA"

With no channels named it shows those that change in that part, and with no `-o` it writes `bus.png`. Where a
channel changes more than once within a pixel it is drawn as a band: a shorter part shows every edge. It needs
nothing besides Python, so it works on a computer with no screen, such as a Raspberry Pi over ssh, and an AI
agent that reads images can look at it.

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

**For scripts.** `--json` on `info`, `inputs`, `threshold`, `capture`, `show`, `measure`, `find`, `detect`,
`plot`, `decode` and `decoders` prints JSON. Lines about loading the board go to the standard error. With a
trigger, `trigger_sample` and `trigger_seconds` say where in the capture it is; they are `null` with no trigger,
and when the trigger did not come.

| Exit status | |
|---|---|
| 0 | The command worked; a capture is what was asked for |
| 1 | Something went wrong, and what is printed says what: no board, a file that cannot be read, a capture that failed its checks. `info` with no board exits 1 too, and with `--json` still prints the JSON |
| 2 | The command line was not understood: an option the command does not have, or a value it cannot read |
| 3 | A capture that was saved but is not what was asked for: it overflowed, or `--timeout` passed before its trigger came (with `--clock`, before it had its samples) |
| 130 | Ctrl-C stopped a command. Ctrl-C ends a capture early instead, keeps what it has, and exits 0 or 3 |

    logicboole capture --json --rate 1M --time 0.5 -a 1.65 -b 0.9 run.lb16

```json
{
 "ok": true,
 "problems": [],
 "overflow": false,
 "stopped": null,
 "sample_rate": 1000000.0,
 "samples": 500000,
 "seconds": 0.5,
 "trigger_sample": null,
 "trigger_seconds": null,
 "thresholds": [1.65, 0.9],
 "channels": [{"channel": "A0", "changes": 2508, "first": 2e-05, "starts": 1}, ...],
 "files": ["run.lb16"],
 "info": {"usb": "5 Gbit/s", "words": 14459, ...}
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
from logicboole import Capture, decode, detect, plot, search

cap = Capture.load("bus.lb16")
scl = cap.measure(["A1"])["channels"][0]       # times in seconds
print(scl["frequency"], scl["period"], scl["low"])

print(detect.detect(cap)["buses"])             # what is on which channel
gaps = search.find(cap, "A1=1 for 100us")      # every gap in the bus, as find --json
png, info = plot.plot(cap, ["A0", "A1"], start=0.001, end=0.002, marks=[0.0015])
open("part.png", "wb").write(png)

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
| `Board.revision` | The board's revision: `.name` (`"V1.0.0"`), `.low` and `.high` (the thresholds it is made for), `.step`, `.volts(duty)` and `.duty_for(volts)` |
| `Board.inputs()` | The 16 inputs now, bit 0 is A0 and bit 15 is B7 |
| `Board.read(register, n)`, `Board.write(register, data)` | The FPGA's registers |
| `Capture.values`, `.lengths`, `.rate`, `.samples`, `.thresholds` | The capture as runs |
| `Capture.ok`, `.problems`, `.overflow`, `.stopped` | Whether it passed its checks, and how it ended |
| `Capture.trigger` | The number of the trigger sample, or `None` |
| `Capture.edges()`, `.summary()`, `.save(path)`, `Capture.load(path)` | `load` reads `.lb16` and `.sr` |
| `Capture.measure(channels, start, end, listed)` | Periods, pulse lengths and changes, as the command line's `measure --json` |
| `Capture.set_names("A0=SDA,A1=SCL")`, `.names`, `.channel(name)` | Channel names: saved with the capture, and taken in place of ids |
| `search.find(cap, text, start, end, limit)` | Every moment a trigger's text describes, as `find --json` |
| `detect.detect(cap)` | What is on each channel, as `detect --json` |
| `plot.plot(cap, channels, start, end, width, marks, annotations)` | A PNG picture (bytes) and what it shows; `plot.decoded(path, decoders, start, end)` gives the decoders' rows for it |
| `decode.decode(path, words, only, start, end, limit)` | sigrok's decoders on a saved capture, as `decode --json` |
| `decode.decoders()`, `decode.about(name)` | The decoders sigrok-cli has, and what one takes |

## With an AI agent

An AI agent that can run commands needs only the command line: with `--json` every answer is JSON, and
`--fake` lets it try a command with no board. Two things make it easier.

**The skill.** [`skills/logic-boole-16/SKILL.md`](https://github.com/mgwalkerjr95/logic-boole-16/blob/main/software/skills/logic-boole-16/SKILL.md) tells an agent how to use the
board: the steps from a first look to a report, how to pick a rate and a threshold, the triggers, what the
answers mean, and what to check when a channel shows nothing. It is an Agent Skill, a folder with one text
file, and it comes with the package. For Claude Code:

    logicboole skill --install                      # into ~/.claude/skills/, for every project
    logicboole skill --install .claude/skills       # or for this project only

Run it again after updating the software, to update the skill too. For another agent, `logicboole skill
--install FOLDER` puts the skill where that agent keeps its skills, and `logicboole skill` prints the text, to give
it as instructions. From a copy of this repository, copying the folder does the same:

    cp -r software/skills/logic-boole-16 ~/.claude/skills/

**The MCP server.** `logicboole mcp` is a Model Context Protocol server, for an agent that works with tools
and not with a shell. The agent's program starts it and talks to it on its standard input and output. In the
form most such programs take:

```json
{"mcpServers": {"logicboole": {"command": "logicboole", "args": ["mcp"]}}}
```

If the package is not installed, give the full path of the `logicboole` launcher in this repository as the
command. For Claude Code: `claude mcp add logicboole -- logicboole mcp`.

A desktop program started from the Dock or a menu may not search the folders a terminal does, and then cannot
find `logicboole`: give the full path that `which logicboole` prints as the command. `decode` looks for
`sigrok-cli` the same way; `LOGICBOOLE_SIGROK_CLI` names it, with the full path that `which sigrok-cli` prints:

```json
{"mcpServers": {"logicboole": {"command": "/Users/me/.local/bin/logicboole", "args": ["mcp"],
                               "env": {"LOGICBOOLE_SIGROK_CLI": "/opt/homebrew/bin/sigrok-cli"}}}}
```

| Tool | |
|---|---|
| `board_info` | What is on USB and the board's settings, with nothing changed |
| `set_thresholds` | The two pods' thresholds |
| `read_inputs` | The 16 inputs as they are now |
| `capture` | One capture, with a trigger, a capture clock or a glitch filter if wanted, saved to a file. With `fast`, the fast design: 4 inputs at 750 MS/s, a pod at 375 MS/s or all 16 at 187.5 MS/s |
| `measure` | Periods, frequencies, pulse lengths and changes read off a saved capture |
| `find` | Every moment in a saved capture that a trigger's text describes |
| `detect` | What is on each channel of a saved capture: I²C, SPI, serial, a clock, PWM |
| `plot` | A picture of a saved capture, returned as an image and saved as a PNG file |
| `decode` | What a protocol decoder finds in a saved capture (needs `sigrok-cli`) |
| `list_decoders` | The decoders there are, or what one of them takes |
| `self_test` | The path from the FPGA to the computer, checked with the FPGA's test pattern. With `fast`, the fast design's |
| `guide` | The skill's text, with a table of the tools that stand for its commands: for an agent that has no shell |

`capture` saves an `.lb16` file and answers with its name and a summary, and takes `names` as `--name`;
`measure`, `find`, `detect`, `plot` and `decode` are given that name. The files go into a folder for temporary files, or into the one named with `logicboole mcp --dir FOLDER`,
and stay there until you delete them. With a trigger, `capture` waits 10 seconds for it unless told otherwise
(`timeout`); with a capture clock, those 10 seconds are for the whole capture. Its `save_as` writes more files,
and refuses one that is there already unless `overwrite` is true.
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
| `logicboole/board.py` | Loading the board, its registers and thresholds, the board's revisions (`REVISIONS`: what differs between them), and the steps of a capture |
| `logicboole/words.py` | The two formats of the stream, and the test pattern |
| `logicboole/fast.py` | The fast design: its choices of inputs, and its frames as samples |
| `logicboole/trigger.py` | The trigger, the capture clock, the glitch filter and the Pi's lines: their text, and the FPGA's registers |
| `logicboole/capture.py` | A finished capture, the file formats, and the measuring |
| `logicboole/decode.py` | sigrok's decoders on a saved capture: what `sigrok-cli` is asked, and what it answers |
| `logicboole/search.py` | `find`: a trigger's text run over a saved capture |
| `logicboole/detect.py` | `detect`: the app's auto-detect (`web/detect.js`) in Python, checked against it by `test_web.mjs` |
| `logicboole/plot.py` | `plot`: the picture, the PNG writer and its 5 × 7 letters |
| `logicboole/guide.py` | The skill file, and the MCP server's `guide` |
| `logicboole/cli.py` | The command line |
| `logicboole/mcp.py` | The Model Context Protocol server: the protocol, and the tools an agent is given |
| `skills/logic-boole-16/SKILL.md` | The skill: how an agent uses the board through the command line. The package carries it as `logicboole/skills/` |
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
