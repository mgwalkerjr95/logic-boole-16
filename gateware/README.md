# Gateware

The design in the board's FPGA, a Xilinx Spartan-6 XC6SLX9-2TQG144C, written in Verilog.

## What it does

The FPGA takes the outputs of the 16 comparators at both edges of its 100 MHz clock, which a PLL makes from the
board's 24 MHz oscillator: a sample every 5 ns, 200 million a second. It packs the samples into 32-bit words and
writes them through a FIFO of 8192 words into the FX3's synchronous slave FIFO. The FX3 passes them on to USB.

- **Sample rate.** Every (divider + 1)th of those samples is kept: 200 MS/s with a divider of 0, down to about
  12 samples a second with the largest, 2^24 - 1.
- **Two formats.** Raw: two 16-bit samples in a word. Run-length: one sample and the number of further sample
  periods the inputs stayed like that.
- **Two samples in a cycle.** At 200 MS/s two samples arrive in each cycle of the clock, while the FX3 takes one
  word in a cycle. The raw format then makes a word in every cycle. The run-length format makes two in a cycle
  when both samples differ from the one before, so the FIFO can take two words at once.
- **Thresholds.** One 12-bit PWM output for each pod, which the board filters into the threshold voltage of that
  pod's comparators.
- **Trigger.** Up to four stages, met one after the other: levels, edges, a pulse of a given length that has
  ended, levels that have lasted a given time, or an edge on the TRIG IN pin. While a capture waits, the FIFO
  keeps the newest 8184 words at the most, so the capture also holds what came before the trigger. TRIG OUT is
  high from the trigger sample to the capture's last sample.
- **Capture clock.** In place of the divider, a sample is kept at each rising edge, falling edge or both of one
  of the inputs: the circuit's own clock, up to 50 MHz. The inputs are still looked at every 5 ns, and the sample
  kept is the one from just before the edge.
- **Glitch filter.** For each input, a change counts once the input has held its new level for N samples of 5 ns,
  N up to 250 (1.25 µs). All inputs are then delayed alike, so edges keep their places against each other.
- **Registers.** The FX3 writes the settings (divider, sample limit, format, thresholds, trigger, capture clock,
  filters) and reads the status and the counts over I2C at address 0x2A. One line from the FX3, RUN, starts and
  ends a capture.
- **Overflow.** If the words come faster than the FX3, USB and the computer take them, and the FIFO fills,
  sampling stops and the status says so. The words taken until then still arrive, so a capture has no gaps.
- **Test pattern.** A counter of the samples can take the place of the probes, to check the path to the computer.
- **Idle.** While RUN is low and nothing is left to send, the FPGA drives none of the slave FIFO's lines.
- **Indicators.** LED ARM blinks at 1 Hz when the design runs, and while RUN is high it is on for as long as the
  capture waits for its trigger. LED TRIG is on from the trigger (or the first sample) to the last sample and
  while words go to the FX3. A read-only 256-bit status register is on the JTAG instruction USER1.

The register map and the format of the stream are in [`../docs/protocol.md`](../docs/protocol.md). The comment at
the top of `capture2_top.v` describes the design itself.

## Files

| Path | What it is |
|---|---|
| `capture2_top.v` | The top module: clock, registers, glitch filter, sampler, trigger, FIFO, the writer to the FX3, the JTAG register |
| `i2c_slave.v` | The I2C slave in front of the registers |
| `pins.ucf` | The board's constraints: pin numbers and I/O standards for revision C, and the clock periods |
| `capture2.ucf` | What this design adds to them: the INIT_B pin, and the drive strength of the lines to the FX3 |
| `sim/capture2_tb.v` | A test bench that checks itself, with a model of the FX3's slave FIFO and an I2C master |
| `sim/xilinx_stubs.v` | Stand-ins for the Xilinx primitives, for the simulation only |
| `sim.sh` | Runs the simulation |
| `build.sh` | Builds the bitstream with ISE 14.7 |

`build.sh` writes to `build/` and `sim.sh` to `sim/build/`. Git ignores both.

## Simulating

The simulation needs [Icarus Verilog](https://steveicarus.github.io/iverilog/): `iverilog` and `vvp` on the
`PATH`. It passes with version 13.0.

    sh sim.sh

It prints one line for each of its 41 runs and ends with `PASS`. The runs go side by side, as many at once as
the computer has processor cores: about eight minutes on a laptop with eight, and 50 minutes of processor time.
`sh sim.sh 1` runs them one at a time. The output of every run is in `sim/build/capture2_tb.log`.

The test bench is in ten parts, one run each. The first does 37 captures and checks:

- the registers: the identity, the values they start with, writing and reading back, several bytes in one read,
  no answer to another I2C address, and the duty cycles of the two threshold outputs
- every word that reaches the FX3 against what the design sampled, sample by sample, in both formats, with the
  test pattern and with inputs that change at random times, at 200 MS/s, at 100 MS/s and at lower rates
- that the samples are the inputs every 5 ns, in order: a count on the inputs that goes up every 5 ns must come
  out as consecutive numbers at 200 MS/s, and as every 2nd, 3rd, 4th, 5th, 8th and 11th number at lower rates
- the counts of samples and words, captures of 1 to 6 samples in each format, and an odd number of samples in
  the raw format
- quiet inputs in the run-length format: one word every 65536 samples, also when a run becomes full between the
  two samples of a cycle
- inputs that change every 12 ns or so: thousands of cycles that make two words
- a computer that does not read: overflow, sampling stops, and the words taken before it arrive whole
- words that come twice as fast as the FX3 takes them, and the raw format at 200 MS/s into an FX3 that changes
  buffers slowly: overflow both times, and what arrived is the capture's beginning with nothing missing
- RUN falling while the FX3 takes nothing: the design gives up after its time limit and releases the lines
- the lines to the FX3: released while RUN is low and driven during a capture, with PCLK running from RUN's
  rise until the design has stopped after RUN's fall, and not otherwise
- the timing the FX3's datasheet asks for: 2 ns of setup and 0.5 ns of hold around PCLK's rising edge, and PCLK
  high and low for at least 4 ns each
- that no word is written while the FX3 has no buffer for it

The other nine parts check the trigger, the capture clock and the glitch filter. In each capture the test bench
works out for itself, sample by sample, which sample must be the trigger sample or which samples must be kept,
and compares the words that reach the FX3 with that:

- triggers on levels in both formats, at the capture's first sample, after the FIFO has gone round many times
  while waiting, and with sample limits of 1 to 5 from the trigger on
- stages in turn, edges, levels that have lasted, and pulses by their length, at 200 MS/s and at lower rates
- the status while a capture waits, TRIG OUT 60 to 70 ns after the trigger sample reached the inputs, and the
  two LEDs
- pulses of every length around the limits of a stage, one sample more and one less
- TRIG IN, also with the glitch filter on
- a wait that is ended before the trigger comes: the newest words arrive
- overflow after the trigger, and the largest number of words kept from before it
- the capture clock at 13, 48.5 and 50 MHz, at rising, falling and both edges, with a trigger, and with no
  clock at all
- the glitch filter against a model that filters the same inputs one sample at a time, with different lengths
  on different inputs, at 250 samples, and switched off again

The next 30 runs repeat the first two captures with the FX3's flag arriving 0 to 8 clock cycles late and its
edge 0.6 to 13 ns after the clock's. The design must not depend on either. The last run shortens the design's
hold-off after a buffer to less than the flag's delay. The FX3 model must then report words it had no buffer
for, so that run is expected to fail, and `sim.sh` reports it as `FAIL as expected`.

The test bench does not exercise the JTAG register.

## Building

Spartan-6 FPGAs are supported only by ISE 14.7 from AMD (Xilinx); Vivado does not support them. ISE 14.7 runs on
x86 Linux and on Windows, and its free WebPACK license covers the XC6SLX9. `build.sh` is for Linux (x86-64).

Install ISE 14.7 and put the license file, `Xilinx.lic`, in `~/.Xilinx/`. Then:

    ./build.sh

`build.sh` takes ISE's environment from `/opt/Xilinx/14.7/ISE_DS/settings64.sh`. If ISE is installed somewhere
else, give the path of that file:

    ISE_SETTINGS=/path/to/14.7/ISE_DS/settings64.sh ./build.sh

The script runs xst, ngdbuild, map, par, trce and bitgen for the part `xc6slx9-2-tqg144`. It stops if a tool
fails, and it makes no bitstream if the timing report shows a timing error. It writes to `build/`:

| File | What it is |
|---|---|
| `capture2.bin` | The bitstream that the FX3 loads into the FPGA |
| `capture2.bit` | The same with ISE's header in front |
| `capture2_jtag.bit` | The same routed design for loading over JTAG (J4), with the start-up sequence clocked by TCK |
| `capture2.syr`, `.bld`, `_map.mrp`, `.par`, `.twr`, `_pad.txt` | The reports of synthesis, translation, mapping, place and route, timing, and the pins |
| `build.log` | Everything the tools printed |

## Installing a build

The host software loads the file `software/logicboole/data/gateware.bin`. To use your build, copy it there:

    cp build/capture2.bin ../software/logicboole/data/gateware.bin

## Trying a build on a board

The board keeps nothing when it loses power. After it is plugged in, the host software loads the firmware into
the FX3 and then `gateware.bin` into the FPGA, both over USB. Nothing is written to flash, so a build that does
not work cannot brick the board: unplug it and plug it in again.

To run a build on the board without installing it:

    ../logicboole load --gateware build/capture2.bin

LED ARM blinks at 1 Hz once the design runs. Then `../logicboole selftest` checks the whole path from the FPGA to
the computer with the design's test pattern, every word compared. `../logicboole load` with no file goes back to
the published bitstream.

## The published bitstream

`../software/logicboole/data/gateware.bin` is this design, built with ISE 14.7 by the commands in `build.sh`.

| | |
|---|---|
| Size | 340,604 bytes |
| SHA-256 | `e802dd41145d9e164c4b4f03206dffc4e1068fd2b06f39a8c7ac746782911757` |
| Timing errors | 0 |
| 100 MHz clock (10 ns period) | Minimum period 9.425 ns: 0.575 ns of setup slack. Hold slack 0.405 ns |
| Slice registers | 3,370 of 11,440 (29%) |
| Slice LUTs | 5,315 of 5,720 (92%), 159 of them as the glitch filter's shift registers |
| Occupied slices | 1,422 of 1,430 (99%) |
| Block RAM | 16 of 32 RAMB16 (50%): the FIFO, 8192 words of 32 bits |
| Input flip-flops | Both flip-flops of each of the 16 inputs are in its pad (IDDR2) |
| Clock buffers | 2 of 16 |
| PLLs | 1 of 2 |
| Pins | 83 of 102 |

The timing is ISE's analysis for the -2 speed grade at its default conditions, 85 °C and 1.14 V. The trigger,
the capture clock and the glitch filter take two thirds of the logic: the FPGA is nearly full, and a change that
adds much logic may no longer fit or meet the 10 ns.

To check the file (on macOS the command is `shasum -a 256`):

    sha256sum ../software/logicboole/data/gateware.bin

**On a board.** With this bitstream on one board, connected to a Mac:

- `logicboole selftest` passes: 34 captures of the test pattern with every word compared, among them 600 million
  samples at 200 MS/s, and 200 million samples at 200 MS/s that make 50 million words a second. Eight of them wait
  for a trigger on the pattern, where the sample that must be the trigger sample is known.
- Two clocks from a generator on the probes, 1 MHz and 100 kHz, captured at 200 MS/s for a second: every half
  period of the 1 MHz clock is 99 to 101 samples long. The clocks' edges fall into the two halves of the FPGA's
  clock cycle in shares of 50.3% to 50.5%, which puts the two samples of a cycle 4.95 to 5.05 ns apart.
- Faster clocks from the same generator on one input of each pod, captured at 200 MS/s: from 10 MHz to 85 MHz
  the frequency read is within 105 parts in a million of the one set, and every half period is seen on both
  inputs. At 90 MHz the input on pod B still shows every one, and the input on pod A misses 337 periods of
  396,040. A half period at 85 MHz is 5.9 ns.
- The same clock on each of the 16 inputs in turn, one input at a time, with firmware version 2: at 50, 70, 80
  and 85 MHz every input shows every period, 374,000 of them in a capture at 85 MHz, and the inputs with no
  clock stay at 0. At 85 MHz the inputs of pod A read the clock high for 51 to 53% of the time and those of pod
  B for 45 to 48%; B7 reads it lowest.
- Through a USB 2.0 cable (480 Mbit/s): the test pattern in the raw format at 22.2 MS/s, 11.1 million words a
  second, arrives whole for 20 seconds, 444 million samples; at 25 MS/s it overflows after 26 ms. The 1 MHz and
  100 kHz clocks captured at 200 MS/s for a second read as they do on USB 3, and `logicboole selftest` passes,
  13 of its captures ending in an overflow because they make more words than that link carries.
- A 48 MHz clock on one input makes 96 million words a second in the run-length format. A capture of it ran for
  1 second, 200 million samples, with every half period 2 or 3 samples long, and the FIFO never held more than 78
  words.
- A 50 MHz clock makes 100 million words a second, and the FIFO is then full after 4.4 ms, because the FX3 takes
  no words for about 0.75 µs each time one of its buffers is full. The raw format at 200 MS/s is that rate too:
  such a capture holds up to 892,928 samples, and that many arrived whole every time.

- Triggers on those clocks (1 MHz and 100 kHz, then 40 MHz and 20 MHz) at 200 MS/s and lower rates, in both
  formats: edges, levels, levels with an edge, levels that have lasted, pulses by their length, and up to four
  stages in turn, also with a capture clock and with a glitch filter: 50 captures. Each was compared with a
  model that goes through the capture's samples one at a time, and the trigger sample was the model's every
  time. Ten triggers that those signals cannot meet did not come.
- The capture clock: a 10, 25 and 50 MHz clock on one input and a clock of half that on another. One million
  samples at the rising edges, at the falling edges and at both, in both formats, each with exactly one sample
  for each edge. Where the second input changed 15 ns or more before the clock's edge, every sample had its new
  level. Where it changed about 5 ns before the edge, some samples had the old level and some the new.
- The glitch filter: a 25 MHz clock (20 ns high, 20 ns low) passes whole with the filter at 10 and at 15 ns, in
  part at 20 ns, and is gone at 25 ns and more, while a 1 MHz clock on another input keeps every edge. A 1 MHz
  clock passes at 400 ns and is gone at 1.25 µs. With a filter on one of two inputs that carry the same clock,
  their edges stay in the same samples against each other as with no filter.
- TRIG OUT, wired to input B7: in 600 captures at 200 MS/s with ten different triggers, B7 shows TRIG OUT's
  rise 15 or 16 samples after the trigger sample, 75 to 80 ns. That time includes the trigger header's buffer,
  the wire and B7's own input. With a glitch filter it is later by the longest filter and 20 ns: 120 ns later
  with 100 ns on every input, 1.27 µs later with 1.25 µs on one. In a capture with no trigger it rises 50 ns
  after the first sample.
- TRIG IN, wired to a clock that is also on input B1, from 100 kHz to 50 MHz: in 2,000 captures at 200 MS/s,
  rising and falling, the trigger sample is the sample in which B1 shows the same edge, or the one after it. It
  also holds with a level beside the edge, in stages in turn, at sample rates down to 1 MS/s, in the raw format
  and with glitch filters on other inputs. With the clock switched off, no trigger came.

These are three or four inputs of one board, with 3.3 V clocks and the thresholds at 1.56 V or 1.65 V.

## License

Copyright (C) 2026 Michael Walker.

This source describes Open Hardware and is licensed under the CERN-OHL-S v2. You may redistribute and modify this
source and make products using it under the terms of the CERN-OHL-S v2, in [LICENSE](LICENSE) and at
https://ohwr.org/cern_ohl_s_v2.txt.

This source is distributed WITHOUT ANY EXPRESS OR IMPLIED WARRANTY, INCLUDING OF MERCHANTABILITY, SATISFACTORY
QUALITY AND FITNESS FOR A PARTICULAR PURPOSE. Please see the CERN-OHL-S v2 for applicable conditions.
