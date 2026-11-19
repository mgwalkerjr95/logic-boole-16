# The board on USB: requests, registers and the stream

What a program on the computer sees of the Logic Boole 16. The firmware in the FX3 (`firmware/firmware.c`) and
the design in the FPGA (`gateware/capture2_top.v`) are the two ends of it. `software/logicboole/board.py` does
everything described here and can be read as an example; `software/logicboole/fake.py` answers the same requests
with no board, for tests.

## From plugging in to ready

The board keeps nothing when it loses power. Three steps bring it up, and `logicboole` does them by itself:

1. **The boot loader.** Plugged in, the FX3 runs the boot loader in its ROM and shows up as USB device
   `04B4:00F3`. Vendor request `A0` reads and writes its memory: the address is in wValue (low half) and wIndex
   (high half), at most 4096 bytes at a time. With no data, `A0` jumps to the address.
2. **The firmware.** The computer writes the sections of `firmware.img` into the FX3's RAM and jumps to the
   image's entry address. The FX3 leaves USB and comes back as `04B4:00F1`, running the firmware. If no request
   arrives in the firmware's first 30 seconds, it returns to the boot loader.
3. **The FPGA.** The computer sends `gateware.bin` with requests `B3`, `B4` and `B5`, and the firmware shifts it
   into the FPGA. The FPGA's registers then answer, and bit 0 of register 05 says that its clock runs.

An `.img` file is: `CY`, two flag bytes, then sections of (length in 32-bit words, address, data), a length of
zero followed by the entry address, and the sum of all data words.

## USB

Vendor ID 04B4, product ID 00F1: the IDs that Infineon's SDK examples use. The device names itself "Logic Boole
16", made by "Logic Boole", and its release number is 03 followed by the firmware's version. Its serial number
is 16 hexadecimal digits: the FX3's own ID, which the firmware reads from the chip's registers at E0055014 (the
first eight digits) and E0055010. If the chip gives no ID, the device has no serial number. One vendor interface
with one bulk endpoint, 0x81 (IN). Vendor requests go to the device on endpoint 0. A request that fails is
stalled, and bytes 16 and 17 of the status then hold the SDK's error code.

| Request | Direction | wValue, wIndex, data | What it does |
|---|---|---|---|
| B0 | in | 64 bytes | The status, see below |
| B3 | out | | Load the FPGA: begin. Stops the stream, pulls PROGRAM_B low for 2 ms, waits for INIT_B |
| B4 | out | 1 to 4096 bytes | Load the FPGA: data, bit 7 of each byte first |
| B5 | out | | Load the FPGA: end. Clocks until DONE is high |
| B6 | out | wValue 0 or 1 | Reconnect as a USB 2.0 device, or allowing 5 Gbit/s |
| B7 | out | | Back to the boot loader |
| B8 | in | 258 bytes | The USB driver's event log: where it writes next (16 bits), then its 256 bytes |
| B9 | in | 16 bytes | The 5 Gbit/s link's error counts, see below. Reading clears them |
| BA | in or out | wValue = seed, 1 to 4096 bytes | Test data on endpoint 0, for checking the link. What is sent out is compared, and B9 counts the bytes that differ |
| C0 | out | | Start the slave FIFO interface |
| C1 | out | | Stop it, and the stream |
| C2 | out | wValue = buffer size in KB (1 to 63), wIndex = number of buffers | Start the stream: restarts the interface, makes the DMA channel, clears the error counts. RUN stays low |
| C3 | out | | Stop the stream: RUN low, waits up to 0.1 s for the FPGA, throws away what the FX3 holds |
| C4 | out | wValue bit 0 = RUN | RUN high starts the capture, and is only accepted while the stream is started. RUN low ends it |
| C5 | out | | The end of the data, with RUN low: waits up to 0.1 s until the FPGA has sent everything, then passes on the part-filled buffer, then an empty packet |
| D0 | out | wValue = first register, 1 to 64 bytes | Write the FPGA's registers |
| D1 | in | wValue = first register, 1 to 64 bytes | Read the FPGA's registers |

`D0` is acknowledged before the I2C transfer to the FPGA has run. Read the registers back with `D1` to be sure
they took.

### The status (B0)

64 bytes. Numbers have their low byte first.

| Offset | Size | |
|---|---|---|
| 0 | 4 | `LBC2` |
| 4 | 2 | The firmware's version, 2 |
| 6 | 1 | USB speed: 2 is 480 Mbit/s, 3 is 5 Gbit/s |
| 7 | 1 | Bit 0: pins set up. 2: configured by the host. 3: interface started. 4: stream started. 5: RUN. 6: the FPGA's registers answer |
| 8 to 19 | 6 × 2 | SDK error codes: IO matrix, GPIO, UART, the first pin that failed, the last request that failed, the endpoint |
| 20 | 4 | Ticks of 0.977 ms since the firmware started |
| 24, 28 | 4, 4 | The levels on GPIO 31 to 0 and 60 to 32. DONE is GPIO 27, INIT_B is GPIO 45 |
| 32 | 4 | The last FPGA load: bytes shifted in |
| 36 | 2 | Its flags. Bit 0: INIT_B went low. 1: DONE went low. 2: INIT_B rose, the FPGA was ready. 3: INIT_B fell during the data, a CRC error. 4: DONE rose. 5: ended |
| 38, 40, 44 | 2, 4, 2 | How long INIT_B took (units of 0.1 ms), clocks after the last byte until DONE, ticks spent shifting |
| 46, 47 | 1, 1 | Failures of the 5 Gbit/s link, USB resets |
| 48 | 4 | Errors at the parallel port since C2: a word arrived with no buffer for it. Must be 0 |
| 52, 54 | 2, 2 | The first and the last such error's code |
| 56, 57 | 1, 1 | The stream's buffer size in KB, and how many buffers |
| 58, 59 | 1, 1 | The first pin that could not be set up, and how many could not |
| 60 | 2 | SDK error code of the I2C block, or of the last I2C transfer that failed |
| 62 | 1 | The FPGA's register 03, its design version; 0 if the registers do not answer |
| 63 | 1 | The FPGA's register 05, its status |

### The link's error counts (B9)

16 bytes: the SDK's status of the query (32 bits), PHY errors (16), link errors (16), recoveries (16), link
failures (16), the link's state (8), the USB speed (8), and the bytes of `BA` test data that differed (16).

## The FPGA's registers

Read and written with `D1` and `D0`. Numbers of more than one byte have their low byte first. One read of
several bytes is consistent: the status and the counts are copied when the read begins.

| Register | | |
|---|---|---|
| 00 to 03 | R | `4C 42 16`, and the design's version, 05 |
| 04 | RW | Bit 0: the format, 0 raw, 1 run-length. Bit 1: the test pattern in place of the probes. Bits 7:4: how far the test pattern is shifted |
| 05 | R | The status. Bit 0: the PLL has locked. 1: RUN. 2: sampling. 3: done. 4: overflow. Bits 7:5: the writer's state, 0 when idle |
| 06, 07 | R | The inputs now: pod A (bit 0 is A0), pod B |
| 08 to 0A | RW | The divider, 24 bits: a sample every (divider + 1) × 5 ns |
| 0C, 0D | RW | Pod A's threshold as a duty cycle in 1/4096, 12 bits. It takes effect when 0D is written |
| 0E, 0F | RW | Pod B's. It takes effect when 0F is written. Both start at 2048, 1.56 V |
| 10 to 14 | RW | The sample limit, 40 bits. 0: none. With a trigger it counts from the trigger sample |
| 18 to 1C | R | Samples taken in this capture, 40 bits. With a trigger: from the trigger sample on |
| 20, 21 | RW | Words in one of the FX3's buffers: its size in KB × 256. Must agree with C2's wValue |
| 22, 23 | R | The most words the FPGA's FIFO held in this capture, of 8192 |
| 24 to 29 | R | Words written to the FX3 in this capture, 48 bits |
| 2A | R | Bit 0: the FPGA threw away what it still held, because the FX3 took nothing for 40 ms after RUN fell |
| 2B | R | 13: the FIFO holds 2^13 words |
| 2C | R | 200: the sample rate with the divider at 0, in millions of samples a second |
| 2D | R | 4: how many trigger stages the design has. 0 in design version 03, which has none of the registers below |
| 2E, 2F | RW | The Raspberry Pi's lines in place of probes, 9 bits: see below. Design version 05 on; earlier designs read 0 |
| 30 | RW | The capture clock. Bit 0: a sample at each edge of one input, in place of the divider. Bit 1: at its rising edges. Bit 2: at its falling edges. Bits 7:4: which input, 0 (A0) to 15 (B7) |
| 31 | RW | How many trigger stages are in use, 0 to 4. 0: no trigger, the capture begins at once |
| 32, 33 | RW | How many words the FPGA's FIFO keeps while it waits for the trigger, 0 to 8184 |
| 34 | R | The trigger's status. Bit 0: triggered. 1: the word with the trigger sample has gone to the FX3, and 36 is valid. 2: raw format, the trigger sample is that word's upper half. 3: waiting for the trigger. Bits 6:4: how many stages have been met |
| 36, 37 | R | How many words went to the FX3 before the word with the trigger sample |
| 38 to 3B | RW | The mode of stage 0 to 3. Bits 1:0: its kind. Bit 2: TRIG IN rising. Bit 3: TRIG IN falling |
| 40 to 4F | RW | Stage 0: mask, value, rise, fall (16 bits each), min, max (32 bits each) |
| 50 to 7F | RW | Stages 1, 2 and 3 likewise |
| 80 to 8F | RW | The glitch filter of A0 to A7 and B0 to B7: N samples of 5 ns, 0 to 250. 0: off |

Registers 04, 08 to 0A, 10 to 14, 20, 21 and 30 to 33 are taken as RUN rises. The thresholds act at once. The
stages (38 to 7F), the glitch filters (80 to 8F) and the Pi's lines (2E, 2F) act as they are written: write
them while RUN is low.

**Sample rate** = 200 MS/s / (divider + 1): 200 MS/s, 100, 66.7, 50, 40 and so on, down to 11.9 samples a second.
The FPGA's clock is 100 MHz, and each input is taken at both of its edges: with the divider at 0 every one of
those samples is kept, and with an even divider (66.7 MS/s, 40 MS/s) the samples kept are taken at the two edges
in turn. Software that reads register 2C for the fastest rate also works with a design that has a different one.

**Threshold** in volts at the probe = duty / 4096 × 3.1287. That is the board's 2.495 V reference, times the
0.2494 of the divider behind the PWM, times the 5.028 of the probe's input divider (866 kΩ over 215 kΩ). 1.65 V
is a duty cycle of 2160. The filter behind the PWM settles in 120 ms: wait 0.15 s after writing a threshold. A
sample bit is 1 when the input is above its pod's threshold.

**Done** (register 05, bit 3) means that sampling has ended, at the sample limit or by overflow, and that every
word has gone to the FX3.

### The trigger

With 1 to 4 stages in use (register 31), a capture waits. The FPGA samples and makes words as always, but its
FIFO keeps only the newest of them, as many as register 32 says, and the FX3 gets nothing. Each stage is a
condition on the samples that the capture keeps. The stages are met one after the other, each by a later sample
than the one before it, and the sample that meets the last stage is the **trigger sample**. From there on the
FIFO empties to the FX3 as in any capture. The sample limit and "samples taken" count from the trigger sample.

A stage has a kind (bits 1:0 of its mode) and 16 bytes: `mask`, `value`, `rise` and `fall`, each with one bit for
each input, then `min` and `max`, which are numbers of samples.

| Kind | The stage is met by a sample when |
|---|---|
| 0 | the inputs under `mask` equal `value`, and, if `rise` or `fall` name any input, one of those has just risen or fallen: it differs from the sample before |
| 1 | a pulse has ended: the inputs under `mask` equalled `value` for `min` to `max` samples in a row, and in this sample they do not. A pulse that had begun before the capture does not count. `max` = FFFFFFFF: no longest length |
| 2 | the inputs under `mask` have equalled `value` for `min` samples in a row or more, and still do |
| 3 | the TRIG IN pin rose or fell since the sample before (bits 2 and 3 of the mode say which counts), and the inputs under `mask` equal `value` |

The lengths are counted from the capture's first sample, whichever stage the trigger is at. A capture that is
triggered soon after it starts holds fewer samples from before the trigger than register 32 allows: the trigger
does not wait for them.

While it waits the FPGA cannot overflow. A wait is ended like any capture, with RUN low: the words the FIFO kept
then arrive, and they are the last samples before the end.

**The trigger pins** are on header J7: pin 1 TRIG IN, pin 2 TRIG OUT, pin 3 ground, 3.3 V logic. TRIG OUT is
high from the trigger sample (in a capture with no trigger: from the first sample) to the last sample. In the
design it rises 6 cycles of the 100 MHz clock after the inputs took the trigger sample, and with a glitch filter
later by the longest filter and two more cycles. TRIG IN is sampled every 5 ns like a probe and has no glitch
filter of its own; with filters on the inputs it is delayed like them. At a capture's first sample, an edge of
TRIG IN counts if it came in the 5 ns before that sample.

Measured on one board: an input wired to TRIG OUT shows its rise 75 to 80 ns after the trigger sample, which
includes that input's own delay. A signal on TRIG IN and on a probe, 100 kHz to 50 MHz: the trigger sample is
the sample in which the probe shows the edge, or the one after it.

### The capture clock

With bit 0 of register 30 set, the divider is not used. The FPGA keeps one sample at each rising edge, falling
edge, or both, of one of the 16 inputs: the circuit's own clock, up to 50 MHz. It still looks at the inputs every
5 ns, and the sample it keeps is the one taken just before the sample that first shows the edge, so what the
inputs were 0 to 5 ns before the edge reached the FPGA. Everything else is as in any capture: the formats, the
sample limit, and the trigger, whose stages and lengths then count in these samples.

### The glitch filter

Each input has a number N in registers 80 to 8F. With N at 2 or more, a change of that input counts once N
samples in a row, 5 ns apart, show the new level, and a pulse shorter than N × 5 ns is removed. N = 250 is
1.25 µs. The filter works on every 5 ns sample, before the divider and before the trigger. When any input has a
filter, all 16 are delayed alike, by the largest N plus two clock cycles, so edges that pass keep their places
against each other.

### The Raspberry Pi's lines

Nine pins of the Pi's 40-pin header reach the FPGA, each through 1 kΩ and a solder jumper that is open as the
board is shipped. With its bit set in registers 2E and 2F, a line takes the place of one probe: the FPGA
samples it every 5 ns as it does a probe, and the sample's bit for that channel is then the Pi's line. The glitch
filter, the capture clock, the trigger and register 06 see it under the channel's number. Which channel a line
takes is fixed:

| Bit | Line | In place of | Jumper |
|---|---|---|---|
| 2E bit 0 | GPIO2, I2C1 SDA (pin 3) | A0 | JP2 |
| 2E bit 1 | GPIO3, I2C1 SCL (pin 5) | A1 | JP3 |
| 2E bit 2 | GPIO14, UART TXD (pin 8) | A2 | JP9 |
| 2E bit 3 | GPIO15, UART RXD (pin 10) | A3 | JP10 |
| 2E bit 4 | GPIO10, SPI0 MOSI (pin 19) | A4 | JP7 |
| 2E bit 5 | GPIO9, SPI0 MISO (pin 21) | A5 | JP6 |
| 2E bit 6 | GPIO11, SPI0 SCLK (pin 23) | A6 | JP8 |
| 2E bit 7 | GPIO8, SPI0 CE0 (pin 24) | A7 | JP5 |
| 2F bit 0 | GPIO7, SPI0 CE1 (pin 26) | B0 | JP4 |

A Pi line is 3.3 V logic and 1 is high: it does not pass a comparator, so the pod's threshold does not act on
it. The FPGA's nine pins are inputs with a pull-down at all times, so a line whose jumper is open reads 0. The
test pattern (register 04 bit 1) replaces the Pi's lines as it does the probes.

## A capture

1. Write registers 04, 08 to 0A, 10 to 14 and 20, 21 with `D0`. The writer's state (register 05, bits 7:5) must
   be 0. Read them back. If register 2D is not 0, also write 30 to 33, the modes and the 16 bytes of the stages
   in use, and the 16 glitch filters: they keep what the last capture set, so write zeros for a plain capture.
   If the design's version is 05 or more, write 2E and 2F likewise.
2. `C0`, `C3`, then `C2` with the buffer size and count. The software uses 8 buffers of 16 KB, with 4096 in
   register 20.
3. Queue reads on endpoint 0x81, and keep several queued for as long as the capture runs. The software keeps 8
   reads of 1 MB. The FPGA samples whether the computer reads or not.
4. `C4` with wValue 1. The first sample is taken 4096 cycles of the FPGA's 100 MHz clock later, about 41 µs.
5. Ask for the status (`B0`) every 20 ms or so. Bit 3 of byte 63 is "done". To end a capture early, go on to
   step 6 at any time. The registers can be read with `D1` while the capture runs: register 34 says whether it
   still waits for its trigger, and 18 to 1C how many samples it has taken since.
6. `C4` with wValue 0, then `C5`. Keep reading until an empty packet has ended a read: that is the end. The
   empty packet does not always come as a read of 0 bytes. When the data before it ended on a full packet (1024
   bytes at 5 Gbit/s, 512 at 480 Mbit/s), the empty packet ends the read that holds that data. So the end is a
   read that is not full and whose length is a whole number of packets, 0 included. On macOS, libusb reports a
   read that is called off as empty even if part of it had arrived, so let the reads end by themselves.
7. `C3`. Read the registers: samples taken (18 to 1C), words written (24 to 29), overflow (05) and the trigger's
   status (34, 36) stay until the next capture starts.

A capture can be trusted when the empty packet arrived, the number of words read equals the FPGA's count of
words written, the status counts no errors at the parallel port, and the samples the words hold equal the
samples taken.

**With a trigger**, the words before word number (register 36) hold the samples from before the trigger: count
the samples in them, and the trigger sample is the next one. In the raw format add one if bit 2 of register 34
is set. The samples the words hold are then that number plus "samples taken". A capture that ended while it
waited (bit 0 of register 34 is 0) holds only samples from before, and "samples taken" is 0.

## The stream

32-bit words, low byte first, and nothing else: no header. A sample is 16 bits: bit 0 is A0, bit 7 is A7, bit 8
is B0, bit 15 is B7. The first sample is taken as the capture starts, and sample n is at n / sample rate. With a
trigger, the first sample that arrives is the oldest one the FIFO still held. With a capture clock, sample n is
the nth edge of that clock.

- **Raw** (register 04 bit 0 = 0). Bits 15:0 of a word are a sample, bits 31:16 the next one. If the capture
  has an odd number of samples, the last word's upper half repeats the last sample: use "samples taken" to cut
  it off.
- **Run-length** (bit 0 = 1). Bits 15:0 are a sample, bits 31:16 how many more sample periods the inputs stayed
  like that (0 to 65535), so a word covers 1 to 65536 samples. A word is sent when the inputs change, and one
  every 65536 samples while they do not. The lengths of all words add up to "samples taken". With a trigger, a
  word begins with the trigger sample even if the inputs did not change there.

**The test pattern** (register 04 bit 1). Sample t is (t >> shift) & 0xFFFF in place of the probes, where shift
is bits 7:4 of register 04. With a shift of 0 every sample differs from the one before; with 8, every 256th.
`logicboole selftest` captures it in both formats and compares every word.

**Overflow.** The FPGA can hand the FX3 one word in each cycle of its 100 MHz clock, and the FX3 takes none for
about 0.75 µs each time one of its buffers is full. So a little under 100 million words a second can leave the
board. In the raw format two samples are a word: 100 MS/s is 50 million words a second, and 200 MS/s is 100
million, more than the board passes on. In the run-length format every change of the inputs is a word. When the
words come faster than the FX3, USB and the computer take them, for longer than the 8192-word FIFO can cover, a
word finds the FIFO full. Sampling then stops, the words already taken still arrive, and the status says
overflow. The capture is shorter than asked for, but whole from its first sample to its last: there are no gaps
inside a capture.

Measured on one board with a Mac: 96 million words a second, a 48 MHz clock on one input at 200 MS/s, ran for
1 second without an overflow. 100 million words a second overflow after 4.4 ms in either format, so a raw
capture at 200 MS/s holds up to 892,928 samples. A capture clock of 50 MHz with a sample at both of its edges is
100 million samples a second: in the raw format that is 50 million words a second and runs on, in the run-length
format every sample is a word and it overflows like the others.

## The fast design

A second design for the FPGA, `gateware/fast_top.v`: 4 inputs at 750 MS/s, 8 at 375 MS/s or all 16 at
187.5 MS/s, with no trigger, glitch filter, capture clock or Pi line. The firmware and the USB requests are the
same; only the bitstream sent with `B4` differs (`software/logicboole/data/gateware-fast.bin`), and the FPGA may
be loaded with one design while it runs the other. A program tells the two apart by register 02.
`software/logicboole/board.py` (`use()`, `capture_fast()`) and `fast.py` do what is described here.

**Registers.** Those that both designs have are at the same addresses.

| Register | | |
|---|---|---|
| 00 to 03 | R | `4C 42 46`, and the design's version, 01 |
| 04 | RW | Bit 0: the format, 0 raw, 1 run-length. Bit 1: the test pattern in place of the inputs. Bits 7:4: how far the test pattern is shifted |
| 05 | R | The status, as the standard design's. Bit 0: both PLLs have locked |
| 06, 07 | R | The inputs now: pod A (bit 0 is A0), pod B |
| 08 | RW | The mode. Bits 1:0: 0 all 16 inputs, 1 eight, 2 four. Bits 3:2: which. Eight: 0 pod A, 1 pod B. Four: 0 A0 to A3, 1 A4 to A7, 2 B0 to B3, 3 B4 to B7. Bit 4: the four samples of a frame in the opposite order (a check, leave it 0) |
| 0C to 0F | RW | The thresholds, as the standard design's |
| 10 to 14 | RW | The frame limit, 40 bits. 0: none |
| 18 to 1C | R | Frames taken in this capture, 40 bits |
| 20, 21 | RW | Words in one FX3 buffer |
| 22, 23 | R | The most words the FIFO has held in this capture |
| 24 to 29 | R | Words written to the FX3 in this capture, 48 bits |
| 2A | R | Bit 0: the rest was thrown away after RUN fell. Bit 1: FLAGA |
| 2B | R | 13: the FIFO holds 2^13 words |
| 2C, 2D | R | 750: the samples a second with four inputs, in millions, 16 bits |
| 2E, 2F | R | The pins this design does not use, as they are now. Bits 8:0: the Pi's lines in the order of the standard design's register 2E. Bit 12: INIT_B. 13: CTL5. 14: CTL8. 15: TRIG IN |

Registers 04, 08, 10 to 14 and 20, 21 are taken as RUN rises. A capture goes as the standard design's: write
them and read them back, `C0`, `C3`, `C2`, RUN high with `C4`, read the endpoint until the status says done,
RUN low, `C5`.

**Frames.** The design takes the inputs at every rising edge of a 750 MHz clock and works in a clock of a
quarter of that, 187.5 MHz. In each cycle of the slower clock it keeps 16 bits, a frame, 187.5 million a second:

| Mode | A frame's 16 bits | Samples a second |
|---|---|---|
| 4 inputs | Four samples of 4 bits: bits 3:0 the earliest, then 7:4, 11:8, and 15:12 the latest | 750 million |
| 8 inputs | Two samples of 8 bits, every second of the four: bits 7:0 the earlier, 15:8 the later | 375 million |
| 16 inputs | One sample, every fourth: bits 7:0 pod A, 15:8 pod B | 187.5 million |

Within a sample the inputs are in the order of their numbers, the lowest in the lowest bit, and 1 means above
the pod's threshold. The first frame is taken as the capture starts, and sample n is at n / sample rate.

**The stream** is the standard design's with a frame in place of a sample.

- **Raw.** Bits 15:0 of a word are a frame, bits 31:16 the next one. Read as bytes, low byte first, the stream
  is then the samples in order: a sample is the next 4 bits (the low half of a byte first), the next byte, or
  the next two bytes. If the capture has an odd number of frames, the last word's upper half repeats the last
  frame: use "frames taken" to cut it off. 93.75 million words a second, whatever the inputs do.
- **Run-length.** Bits 15:0 are a frame, bits 31:16 how many more frames were equal to it (0 to 65535). A word
  is sent when a frame differs from the one before, and one every 65536 frames while none does. So an input
  that changes makes one or two words per change, and inputs that do not change make almost none; a signal
  that changes in every 5.33 ns makes 187.5 million words a second, which overflows.

The test pattern (register 04 bit 1) is a counter of the frames: frame t is (t >> shift) & 0xFFFF.
`logicboole selftest --fast` captures it with each choice of inputs and compares every word. Overflow is as in
the standard design: sampling stops, the words already taken arrive, and the capture is whole up to its last
frame.
