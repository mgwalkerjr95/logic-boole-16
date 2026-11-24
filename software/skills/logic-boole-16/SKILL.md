---
name: logic-boole-16
description: Capture, measure and decode digital signals with a Logic Boole 16 USB logic analyzer through its `logicboole` command line. Use this whenever the user wants to see what real hardware does on its pins and a Logic Boole 16 is connected or mentioned - debugging an I2C, SPI, UART or other bus, checking whether a line toggles, measuring a frequency, a pulse width or the time between two edges, catching a glitch or a reset pulse with a trigger, or reading an .lb16 capture file - even if they only say "the logic analyzer", "probe the bus" or "why doesn't the sensor answer".
license: GPL-3.0-or-later
---

# Logic Boole 16

A 16-channel logic analyzer on USB. You drive it with the `logicboole` command: take a capture into a file, then
read times and protocol messages off that file, and look at it as a picture. `info`, `inputs`, `threshold`,
`capture`, `show`, `measure`, `find`, `detect`, `plot`, `decode` and `decoders` print JSON with `--json`.

This file goes with the software it came with (`logicboole --version`). If a command or option written here is
refused as unknown, the software is older than this file: `pipx upgrade logicboole`. If the software has a
command this file does not describe, this file is the older one: `logicboole skill --install` puts the one that
matches in place (from a copy of the repository, `cp -r software/skills/logic-boole-16 ~/.claude/skills/` does
the same). After an upgrade, the next command loads the new firmware and FPGA design into a board that stayed
plugged in, and says so in a line.

## The board, in the terms the commands use

- 16 inputs in two pods: `A0` ... `A7` and `B0` ... `B7`.
- Each pod has one threshold, within the range the board takes: 0.3 V to 3 V on a V1.0.0 board
  (`logicboole info --json` gives the board's `revision` and `threshold_range`); one outside it is refused. An
  input reads 1 above its pod's threshold. Set it to about half the logic voltage of the signals on that pod:
  1.65 for 3.3 V logic, 0.9 for 1.8 V. Two logic voltages go on two pods.
- It samples at 200 MS/s divided by a whole number: 200M, 100M, 50M, 10M, 1M ... The board takes the nearest
  rate to the one asked for, and the answer says which.
- With `--fast` it samples fewer inputs faster: four at 750 MS/s, a pod of eight at 375 MS/s (see "Faster than
  200 MS/s").
- It sends only the changes of the inputs, so a high rate costs almost nothing on signals that are mostly
  idle, and a capture can be long.
- An input with nothing connected reads 0. So "always low" has three meanings that a capture cannot tell
  apart: the line is low, the probe is not on it, or it is on another input.
- You cannot see the wiring. Which signal is on which input, and whether a ground wire joins the board to the
  circuit, is something the user knows: ask, or read it from their notes, before you interpret a capture.

## Running the command

Use `logicboole` if it is on the PATH (`pipx install logicboole` puts it there). Otherwise the repository's top
folder has a launcher that needs nothing installed: `./logicboole` from there (it needs Python 3.9 or later, and libusb).

`--fake` before the command uses a stand-in for the board that makes a 100 kHz I2C bus on A0 (SDA) and A1
(SCL). Use it to try a command when no board is connected, and never to answer a question about real hardware.
Each `--fake` command starts from a stand-in that was just plugged in: `info` always says `boot`, and
thresholds set by one command are gone in the next (a real board keeps them).

    logicboole --fake capture --json --rate 2M --time 50ms /tmp/try.lb16

With `--json` the standard output is one JSON object and nothing else; lines about loading the board and about
waiting for a trigger go to the standard error. A board that was just plugged in is loaded by the first command that needs it, in about three
seconds. A command that fails prints the reason on the standard error and exits with status 1: read the reason,
it says what to change. (Status 2 is a command line that was not understood, an option the command does not
have: `logicboole COMMAND -h` lists them.) A capture that was saved but is not what was asked for exits with
status 3: it overflowed, or `--timeout` passed before its trigger came (with `--clock`, before it had its
samples).

Run one command on the board at a time. Do not start plain `logicboole` (no command): that is the app in a
browser, and it does not return. "Another program is capturing from the board" means the app, an MCP server or
another command has it: tell the user, and leave it to them to end that program (`logicboole load` takes the board
back, and ends that program's capture). A capture left running by a program that has ended is stopped by the next
command, which says so in a line.

## The steps

1. **Look.** `logicboole info --json` changes nothing. `state` is `ready` or `boot` (connected, will be loaded
   when needed) when there is a board; `none` means no board on USB; `noaccess` means the computer lists the
   board and does not let this program open it (on Linux the USB rule is not installed; on a Mac, macOS has not
   let the board connect), and `text` says what to do: pass that on to the user, who has to do it. When ready it
   also gives the board's serial number (`serial`), its `revision` and `threshold_range`, the thresholds and the
   inputs' levels now.
2. **Set the thresholds** for the logic voltages in use: `-a 1.65 -b 0.9` on the capture, or
   `logicboole threshold 1.65 0.9`. They stay set until the board is unplugged; a capture's answer says which
   were in force.
3. **Capture** into an `.lb16` file (see below).
4. **Check the capture** before using it (see "Reading the capture's answer").
5. **Read the file**: `detect` what is on it if nobody has said, then `measure`, `find`, `decode` and `plot`.
   They read the file only, so they can be repeated with other settings as often as needed without the board.
6. **Report** what the numbers show, with their resolution.

## Capturing

    logicboole capture --json --rate 10M --time 0.5 -a 1.65 run.lb16

| Option | |
|---|---|
| `--rate` | `200M`, `10M`, `1M` (the default), `250k` |
| `--time` | `2`, `0.5`, `20ms`. 1 second if not given. A number with no unit is seconds |
| `-n` | A number of samples, in place of `--time`. Never `-n 0`: that runs until Ctrl-C |
| `-a`, `-b` | The thresholds of pod A and pod B |
| `--trigger "..."` | Wait for this and count the length from it |
| `--before` | With a trigger: how much from before it to keep, as a time with its unit (`5ms`). A number with no unit is a number of samples. A tenth of the length if not given |
| `--timeout` | With a trigger: stop waiting after this long (`10` is 10 seconds). Always give it. With `--clock`: how long the whole capture may take |
| `--glitch 50ns` | A change counts once the input held its new level this long (10 ns to 1.25 us) |
| `--clock A0=r` | One sample at each rising edge of A0, in place of the sample rate (with `-n`) |
| `--pi i2c` | Only with the board on a Raspberry Pi and its solder jumpers closed: the Pi's own lines in place of probes. `i2c` (SDA on A0, SCL on A1), `uart` (TXD A2, RXD A3), `spi` (MOSI A4, MISO A5, SCLK A6, CE0 A7, CE1 B0), `all`. The channels are named after the lines. An open jumper reads low |
| `--raw` | Every sample, not only the changes: for signals that change at nearly every sample |
| `--fast A0-A3` | Four inputs at 750 MS/s, or a pod (`A`, `B`) at 375 MS/s, in place of `--rate`. No trigger (see below) |
| `--name A0=SDA,A1=SCL` | Names for channels, saved in the file (see below) |

**Name the channels** once you know the wiring: `--name A0=SDA,A1=SCL,B0=RESET`. The names are saved in the
`.lb16` and `.sr` files, the app shows them, and `measure`, `find`, `plot` and `decode` then take `SDA` where
they take `A0` (`find run.lb16 "SDA=f SCL=1"`, `decode run.lb16 i2c scl=SCL sda=SDA`), and their answers carry
`name` beside `channel`. A name is up to 24 letters, digits and `_ . + # / -`; not another channel's id, not
`in`, `then`, `for` or `pulse`, and not two channels with one name. The capture's own `--trigger` takes the
names given with it (`--name A0=SDA,A1=SCL --trigger "SDA=f SCL=1"`); `--clock` and `--glitch` take ids.

**Which rate.** At least 4 times the fastest signal's bit rate to decode it, 10 times or more to measure its
timing. A time read off a capture can be off by one sample at each end, so the rate sets the resolution: 5 ns
at 200M, 1 us at 1M. When unsure go higher; idle time costs nothing.

**How long.** The computer keeps a capture's data in its memory, up to a 32nd of it: a capture that grows past
that ends early, whole up to its last sample, with `stopped` `"size"`. On a computer with 8 GB that is 64 million
changes of the inputs, 0.7 s of a 48 MHz clock at 200M. Quiet buses are far from it at any rate.

**Faster than 200 MS/s.** `--fast` loads the board's second FPGA design, which trades inputs for speed:

| `--fast` | Inputs | Rate | A sample every |
|---|---|---|---|
| `A0-A3`, `A4-A7`, `B0-B3`, `B4-B7` | 4 | 750 MS/s | 1.33 ns |
| `A`, `B` | a pod of 8 | 375 MS/s | 2.67 ns |
| `all` | 16 | 187.5 MS/s | 5.33 ns |

    logicboole capture --json --fast A0-A3 --raw -t 2ms -a 1.65 clk.lb16

Use it for a clock above 85 MHz (at 200 MS/s some inputs miss periods above that; at 750 MS/s clocks up to
200 MHz are seen whole), or for timing finer than 5 ns between signals in one group of four. It takes samples
and sends them, nothing more: it goes without `--rate`, `--trigger`, `--before`, `--timeout`, `--glitch`,
`--clock` and `--pi`, so the capture starts at once. The length is 10 ms if `--time` is not given. Add `--raw`
for signals faster than about 15 MHz: the run-length format makes more data than raw on those and overflows.
At 200 MHz the threshold has to sit at half the logic voltage, or the board loses periods. The next capture
without `--fast` loads the standard design again, by itself, and the thresholds stay as set.
Keep fast captures to milliseconds: a long one of a busy signal ends early at the software's memory limit
("stopped": "size", a 32nd of the computer's memory in data, an 8th in changes), and on a Raspberry Pi turning
it into samples takes minutes.

In the answer `info.fast` names the choice and `info.inputs` the inputs taken. The other inputs still appear in
`channels`, reading 0 with no changes: they were not sampled, so say nothing about them.

**Save as `.lb16`.** It is small, and the commands that read captures read it. Name `.sr`, `.vcd` or `.csv` files as
well only when the user wants to open the capture in PulseView, GTKWave or a spreadsheet; an `.sr` file holds
every sample and gets large at high rates. `measure`, `find`, `detect`, `plot`, `decode` and `show` read `.sr`
files too, so a capture the user made in PulseView can be read the same way; one written by `logicboole` also
keeps the trigger's place and the thresholds. An `.lb16` file holds 2^32 samples (21 s at 200 MS/s): a longer
capture into one is refused before it starts, and the error says what to do.

**Triggers.** Without a trigger the capture starts at once, at whatever moment that is: its first message may
be cut off, and "the first transaction" is only the first one captured. To start at a known point, or to catch
something that happens once or rarely, describe it:

| | |
|---|---|
| `B0=r` | B0 rises (`f`: falls, `e`: either) |
| `A0=1 A1=0` | A0 is high and A1 is low |
| `A2=f A0=1` | A2 falls while A0 is high |
| `A0=1 for 10us` | A0 has been high for 10 us, and still is |
| `A0=0 pulse 100ns..1us` | A low pulse of 100 ns to 1 us has ended (`..50ns`: at most, `1us..`: at least) |
| `in=r` | The TRIG IN pin rises |
| `B7=f then B0=r` | Up to four of these, one after the other |

    logicboole capture --json --rate 200M --time 1ms --trigger "B0=f" --before 100us --timeout 10 reset.lb16

While the board waits, the command prints `Waiting for the trigger (B0=f), 10 s at the most.  Ctrl-C stops.` on
the standard error, and nothing else until the trigger comes or the time is up. Ctrl-C ends the wait early.

An I2C start is SDA falling while SCL is high: `--trigger "A0=f A1=1"` with SDA on A0 and SCL on A1. For what a
circuit sends first after a reset, trigger on the reset line, or on the bus's first edge while the user resets.

The board looks for the trigger in the samples it keeps, so a trigger sees what the rate shows: to catch a
short pulse, pick a rate whose sample is shorter than the pulse, and times in a trigger are rounded to whole
samples. A time that rounds to no sample at all is refused, and the error gives the sample's length.

Always give `--timeout` with a trigger. Without it the command waits until the trigger comes, and if it never
does you wait for ever. If the user has to do something to make the event happen (press reset, plug a cable),
tell them before you start the capture and give a timeout long enough for them to do it.

## Reading the capture's answer

```json
{"ok": true, "problems": [], "overflow": false, "stopped": null,
 "sample_rate": 10000000.0, "samples": 5000000, "seconds": 0.5,
 "trigger_sample": null, "trigger_seconds": null, "thresholds": [1.65, 0.9],
 "channels": [{"channel": "A0", "changes": 2440, "first": 2e-05, "starts": 1}, ...],
 "files": ["run.lb16"]}
```

- `ok` false: the capture failed its checks and `problems` says why. Do not use it. Take it again.
- `overflow` true (exit status 3): the signals changed faster than the board and USB carry. The capture ended
  early and is whole up to its end (`samples` says how far it got). A lower rate avoids it, or `--raw` for a
  short capture of a signal that changes at nearly every sample. If `info.usb` (or `logicboole info`) says the
  link is USB 2.0 (480 Mbit/s), it carries about 11 million words a second in place of nearly 100 million: tell
  the user that a USB 3 port and cable carry more.
- `stopped` `"size"`: the capture reached the memory limit above and ended there, whole. Take a shorter or slower
  one.
- With a trigger: `trigger_sample` and `trigger_seconds` say where in the capture the trigger is. `null` with
  `stopped` `"timeout"` means the trigger did not come, and the capture holds only the last samples the board
  saw. `ok` is still true, because nothing went wrong in the board, and the exit status is 3. Say that the event
  did not happen in that time; do not present that capture as the event.
- `channels`: for each input how often it changed, when first, and its level at the start. This is the
  quickest check that you are looking at the right wires.
- `info` holds details of the transfer (words sent, how long the read took, the USB link). Nothing in it is
  needed to judge a capture: `ok` covers it.

**A channel that should be busy shows no changes.** The usual causes, in the order worth checking: the signal
is on another input, or the probe is not making contact (an open input reads 0); no ground wire joins the
board and the circuit; the threshold is outside the signal's swing (a 1.8 V signal never crosses 2.5 V); the
circuit was idle during the capture (use a trigger with a timeout, or a longer time). `logicboole inputs
--json` shows the 16 levels now, for a quick look at which lines are high.

**"Is this line dead?"** A line that stays low for a whole capture may be held low, may never have been made
to fire, or may not be probed at all. Say which of these the capture cannot rule out, and what would: a
trigger on either edge (`B3=e`) with a timeout while the user makes the event happen, a capture with that
pod's threshold at 0.3 V (a line that still reads 0 is below 0.3 V, not at a middle level), and a multimeter
on the pin, which is the only one of the three that shows a voltage.

## What is on each channel

    logicboole detect run.lb16 --json

`detect` looks at every channel and says what it carries, as the app's auto-detect does. `buses` lists the
I2C, SPI and serial lines it finds, each with its wiring (`sda`, `scl`; `clk`, `cs`, `mosi`, `miso`; `rx`), its
`clock_hz`, or `baudrate` with `data_bits` and `parity`, SPI's `mode`, how many `bytes` decoded cleanly, `decode`
(the words for `decode` and `plot --decode`) and `names` (for `--name`: `SDA`, `SCL`, `SPI_CLK`, `SPI_CS`,
`SPI_MOSI`, `SPI_MISO`, `UART`, with a number from 2 on when a name is taken). `channels` gives each input's
`kind`: the bus it is part of,
`clock` or `pwm` (with `frequency` and `duty`), `data` (it changes, and no bus was found on it), or `steady`
(with its `level`).

Use it first when the user has not said what is on which input, then tell them what you take each input to be:
the wiring is a guess from the signals. It knows the common buses at their usual settings (serial at the
standard baud rates as 8N1, 8E1, 8O1 or 7E1, idling high; SPI with one chip select); `data` on a line that
should be a bus means a setting it does not try, or a capture too short or too slow to tell, and `decode` with
the right words still works on it. SPI with two chip selects on one clock (two devices) is not found, because each select sees
the other device's clock: `decode` each with its own `cs=`. A chip select that reads low from the start of the
capture can be taken for a data line: check the roles against the wiring.

## Measuring

    logicboole measure run.lb16 A0 A3 --json
    logicboole measure run.lb16 SCL --from 10ms --to 12ms --list 50 --json

For each channel named, by id or by its name in the capture (or, with none named, each that changes): `changes`, `rising`, `falling`, its level at
the start, and `period` (rising edge to rising edge), `high` and `low` (how long it stays there), each as
`min`, `median` and `max` in seconds; `frequency` is 1 / the median period and `duty` the share of the time
it is high. `--list N` adds the first N changes themselves as `{"time", "sample", "channel", "level"}`, across
the channels in the order they happened: the way to get the time from an edge on one channel to an edge on
another. `--from` and `--to` look at a part of the capture.

`average_frequency` is all the rising edges over the time they span. For a steady clock it is the precise
figure, and the one to report: a time is a whole number of samples, so a clock fast against the sample rate
has periods of two lengths and its median is one of them (a 200 MHz clock at 750 MS/s has periods of 3 and 4
samples: `frequency` says 187.5 MHz, `average_frequency` 199.92 MHz). On a bus that sends in bursts the gaps
lower the average: report `frequency` there.

Read `min` and `max` together with `median`: on a bus that sends in bursts the largest period spans an idle
gap and is not a slow clock, `frequency` (from the median) is the clock's rate inside the bursts, and `duty`
over the whole capture counts the idle time, so measure it inside one burst with `--from` and `--to`.
`sample_seconds` in the answer is one sample: the resolution of every time in it.

**Gaps between bursts.** `measure` does not list them: `find` does (below). On a decoded bus the messages' own
start and end give them too: `decode ... --only start,stop --json` on I2C gives every start and stop, and the
idle time is the next start minus the stop before it. Say which gap you report: from the end of one message to
the start of the next, or from start to start (how often it repeats).

## Finding moments

    logicboole find run.lb16 "A0=0 pulse ..500ns" --json      # glitches: low pulses of 500 ns or less
    logicboole find run.lb16 "SCL=1 for 50us" --json          # gaps in a bus, with how long each lasted
    logicboole find run.lb16 "CS=f then SCLK=e" --json        # CS falling, then the next clock edge

`find` takes the text of a trigger (the table under "Triggers"; channels by id or name) and lists every moment
in the capture that meets it, by the trigger's own rules: what it finds is what that trigger would have caught.
TRIG IN is not in a capture. Each moment in `found` has its `time`, in seconds from the capture's start (the
sample that meets the last stage).

- When the last stage is a pulse or levels, `held` gives how long they lasted: for a pulse, the pulse; for
  levels, the whole stretch in which they held, from `start` to `end`. `to_end` means they still held where
  the search ended, and `before` that they held already where it began, so each lasted longer than it shows.
  `held` at the top gives the `min`, `median` and `max` of them all.
- With stages (`then`), `first` is where the first stage was met and `span` the time from there to `time`.
- A first stage of levels is met once in each stretch of them, not at every sample: `"SCL=1 for 50us"` finds
  each gap once.
- `total` counts them all; `--limit N` lists N (20 if not given, 2000 at the most). `--from` and `--to` look
  in a part.

A pulse shorter than one sample is not in the capture at all, so `total` 0 means nothing longer than one sample
at that rate: say so. The times in the text are counted in samples too: `run.lb16` above is at 10M, a sample
every 100 ns, and a time that rounds to no sample (`..50ns` there) is refused with the sample's length.

## Decoding a protocol

`decode` runs sigrok's protocol decoders (I2C, SPI, UART, CAN, 1-Wire, JTAG, SWD and about a hundred more) on a
saved capture. It needs the `sigrok-cli` program; if it is missing the command says how to install it
(`brew install sigrok-cli`, `sudo apt install sigrok-cli`), which is the user's decision.

    logicboole decode run.lb16 i2c scl=A1 sda=A0 --json --limit 200
    logicboole decode run.lb16 uart rx=B0 baudrate=115200 --json --limit 200
    logicboole decode run.lb16 spi clk=A0 mosi=A1 miso=A2 cs=A3 --json --limit 200
    logicboole decode run.lb16 i2c scl=A1 sda=A0 eeprom24xx --json

The words are the decoder's name, then its channels and options as `name=value`. A second decoder's name
stacks it on the first (the last example reads the I2C bytes as an EEPROM's commands).

    logicboole decoders --json          # every decoder: id and name
    logicboole decoders spi --json      # one decoder's channels, options with defaults, and classes

Look a decoder up before using one you have not used here: the channel and option names differ from decoder
to decoder, and a wrong one is refused with the list of right ones.

The answer has `total` (how many were found) and `annotations`, each with `start` and `end` in seconds from
the capture's start, `start_sample`, `end_sample`, `decoder`, `class` and `text`:

```json
{"start": 3.05e-05, "end": 0.0001005, "class": "address-write", "text": "Address write: 16", ...}
```

- A capture can hold thousands of these. Give `--limit`, and narrow with `--from`/`--to` (around
  `trigger_seconds`, for example) or with `--only CLASS,CLASS`.
- Single bits are left out unless asked for by class (`--only bit`).
- The classes and texts are sigrok's, and each decoder has its own habits. Before you count or conclude from
  a filtered list, look at one whole message unfiltered (`--to` a time just after it ends).
- `total` 0, or only warnings, usually means the wrong channels, the wrong baud rate or a sample rate too low,
  not a silent bus: check `channels` in the capture's answer, and `measure` the clock or the shortest pulse (a
  UART's shortest low pulse is one bit: 1 / that time is the baud rate).

**I2C, as an example of those habits.** Addresses are shown in hex as 7 bits (`Address write: 16` is address
0x16; the byte on the wire is 0x2C). Each address gives two rows of the same class, the address and then
`Write` or `Read` for its last bit, so a count of `address-write` rows is twice the number of writes. To see
who answers, list `--only address-write,address-read,ack,nack,data-read,stop`: a `nack` straight after an
address means nobody answered at that address, but a `nack` after the last `data-read` of a read is the
master ending the read, which is normal. With the acks and the data left out the two look the same.

## Looking at a capture

    logicboole plot run.lb16 SDA SCL --from 1ms --to 1.4ms --mark 1.2ms --decode "i2c scl=SCL sda=SDA" -o look.png

`plot` draws the capture as a PNG: the channels as waveforms over a time axis, the trigger as a dashed red line,
numbered orange lines at the `--mark` times (`--mark 1.2ms,1.35ms`), and a row for each `--decode` with what
that decoder found (it needs sigrok-cli). With no channels named it shows the ones that change in that part.
It writes FILE.png beside the capture if `-o` is not given; `--width` is 1200 pixels, 4000 at the most.

If you can read images, read it: it is a check on what the numbers told you (a decode that looks wrong, a pulse
that should not be there, which edge a time runs from), and a picture to show the user, with marks at the times
you report. Where a channel changes more than once within a pixel it is drawn as a pale band, and the answer
lists that channel in `busy`: show a shorter part to see its edges. A picture of a whole busy capture is mostly
bands; one message, or a few hundred microseconds around a moment `find` gave, is what shows the bits.

## Recipes

**"What is on this bus?"** Capture with no trigger at a rate fast enough for anything likely (`--rate 10M
--time 0.5` covers buses up to about 2 MHz), run `detect`, and tell the user what you take each input to be.
Then capture again with `--name` from its `names` and a trigger at the start of a message, and `decode` with its
`decode` words.

**"Find the glitch."** Capture at a rate whose sample is shorter than the glitch could be (`200M`: 5 ns), as
long as the fault takes to show, then `find run.lb16 "RESET=0 pulse ..1us"` (and `=1` for high pulses). Each
one found has its time and length; `plot` around it with `--mark` at its time to show it. To catch one that is
rare, give the same text to `--trigger`, with a `--timeout`.

**"How long from CS low to the first clock edge?"** `find run.lb16 "CS=f then SCLK=e" --json`: each `span` is
that time, for every transfer in the capture. Report the min, median and max, with the resolution: one sample at
each end.

**"How often does it send, and how long are the gaps?"** `find run.lb16 "SCL=1 for 50us"` (longer than any
pause inside a message): each moment is a gap, `held.seconds` its length and `held.end` where the next message
begins, so the steps between successive `held.end` are how often it sends.

**"Did the device answer?"** On I2C, `decode ... --only address-write,address-read,ack,nack` (see the I2C
paragraph above for reading it). On serial, `decode` the device's TX line: `total` 0 with a busy line means the
wrong baud rate; `detect` gives the right one.

## Reporting

- Give numbers with their resolution: "SCL runs at 100 kHz (period 10 us, measured to within 0.5 us at
  2 MS/s)", not a bare figure with digits the capture cannot support. Averaging over many periods narrows the
  resolution, but every time is counted by the board's own clock, and this file does not give that clock's
  accuracy: do not offer a long average as an absolute frequency to many digits.
- Say what was captured: the rate, the length, the thresholds, the trigger, and which input you took for which
  signal. If the wiring was assumed, say that it was.
- A capture shows logic levels against a threshold, not voltages or edge shapes. Slow edges, ringing and
  levels near the threshold need an oscilloscope: say so when a result points that way (changes that come and
  go with the threshold, pulses a few samples long on a line that should be clean).
- Keep the `.lb16` file and name it in the report, so the user can open it in the app (`logicboole`, then
  Open) and see the same thing.

## Without a shell

`logicboole mcp` is a Model Context Protocol server with the same abilities as tools (`board_info`,
`set_thresholds`, `read_inputs`, `capture`, `measure`, `find`, `detect`, `plot`, `decode`, `list_decoders`,
`self_test`), for an agent that cannot run commands. Its `guide` tool gives this file, with a table of which
tool and argument stands for each command and option. `capture` takes `fast` as `--fast` above, and lists the
inputs it did not take in `not_taken`; its `timeout` is 10 seconds if not given, with a trigger or a capture
clock, and its `save_as` refuses a file that is there already unless `overwrite` is true. `self_test` takes
`fast: true`; `plot` returns the picture itself. With a shell, the commands above are enough.

## If the board itself is in doubt

`logicboole selftest` has the FPGA send a known pattern in place of the inputs, from 1 MS/s to 200 MS/s, and
compares every word that arrives. It needs nothing connected to the probes; its captures run for a little over
20 seconds in all, so allow a minute. A board that passes carries data correctly from the FPGA to the computer;
what it does not check is the inputs themselves. On a USB 2.0 link it still passes, and its last lines say
how many of its captures overflowed because that link carries less. `logicboole selftest --fast` does the same
for the fast design, with each choice of inputs.

For help with the board or the software, the user can write to support@nearfieldsupply.com, or open an issue at
https://github.com/mgwalkerjr95/logic-boole-16/issues, with what `logicboole --version` and
`logicboole info --json` print.
