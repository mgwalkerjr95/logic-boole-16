// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: CERN-OHL-S-2.0
//
// The FPGA design of the Logic Boole 16, board revision C: what the probes see goes to the computer, and the
// settings (sample rate, thresholds, how many samples, the format) are registers that the FX3 writes.  The whole
// path is probes -> comparators -> FPGA -> FX3 -> USB, with the settings reached over USB too.  Built from this
// file, i2c_slave.v, pins.ucf and capture2.ucf by build.sh, and simulated by sim.sh.  What the computer sees of
// the registers and of the stream is in ../docs/protocol.md.
//
// A capture.  The FX3 writes the settings into the registers below over I2C, then raises RUN (CTL6, an FX3 GPIO).
// The FPGA takes the settings as RUN rises, drives the lines of the FX3's synchronous slave FIFO, starts PCLK and,
// START_WAIT cycles later, samples the 16 inputs.  Each pad takes its input at both edges of the 100 MHz clock, so
// there is a sample every 5 ns, 200 million a second, and the design keeps every (divider + 1)th of them.  At the
// full rate two samples arrive in each cycle of the clock, and everything from here to the FIFO handles two.  The
// samples go through a FIFO of 8192 words to the FX3, 32 bits a word, in one of two formats:
//   raw          bits 15:0 a sample, bits 31:16 the next one.  If the capture ends on an odd sample the last
//                word's upper half repeats it
//   run-length   bits 15:0 a sample, bits 31:16 how many more sample periods the inputs stayed like that
//                (0 ... 65535): a word when the inputs change and one every 65536 samples while they do not
// A sample is bits 7:0 pod A's inputs 7 ... 0 and bits 15:8 pod B's; 1 = above the pod's threshold.
// The FX3 takes one word in a cycle, 100 million a second, and USB carries a little less.  At 200 MS/s the raw
// format makes that many, and the run-length format up to twice that many when every sample differs, so the FIFO
// takes two words in a cycle and such a capture lasts as long as the FIFO has room: 16,384 samples at the least.
// Sampling ends when RUN falls, when the sample limit is reached, or when a word finds the FIFO full (the
// overflow: the FX3, or the computer behind it, did not take the data fast enough).  What is in the FIFO then
// still goes to the FX3, so the words that arrive are the capture from its first sample on with nothing missing in
// between.  After that the status says "done", and when RUN is low PCLK stops and the lines are released.  If RUN
// is low and the FX3 takes nothing for DRAIN_MAX cycles (40 ms) the rest is thrown away.
//
// The way of a sample, a clock cycle for each step:
//   pads         two flip-flops in each pad, then one more: in_e (the earlier half) and in_l
//   the filter   only if a glitch filter is set (registers 80 ... 8F): see below
//   g            the inputs as the sampler sees them, the one before them, and the edges of the capture clock
//   s            the samples kept: none, one or two in a cycle (s_n, s_x, s_y)
//   d1, d2, d3   the same three cycles later, while the trigger looks at them
//   a            the samples that are part of the capture: all until the trigger, the sample limit from it on
//   b            which of them equal the one before
//   words        none, one or two words, into the FIFO
//
// The glitch filter.  Each input has a number N (0: off).  With N set, a change of that input counts once N
// samples in a row (5 ns apart) show the new level, and a shorter pulse is removed.  Every input is then delayed
// alike, by the largest N and two cycles, so edges that pass keep their places against each other: an input with a
// smaller N waits the difference in a shift register.  The filter works on every 5 ns sample, before the divider.
//
// State capture (register 30).  In place of every (divider + 1)th sample, a sample is kept at each edge of one of
// the inputs, the capture clock: the sample taken just before the one that first shows the edge, so what the
// inputs were 0 ... 5 ns before the edge.  The clock may be any input and as fast as 50 MHz.
//
// The trigger (registers 31 ... 7F).  With 1 ... 4 stages set, the capture waits: samples are taken and made into
// words as always, but the FIFO keeps only the newest of them (register 32 says how many words) and the FX3 gets
// nothing.  Each stage is a condition on the samples kept; the stages are met one after the other, each by a later
// sample than the one before, and the sample that meets the last one is the trigger sample.  From there on the
// FIFO empties to the FX3 as in any capture, and the sample limit counts from the trigger sample.  The word that
// holds the trigger sample is found by its number (register 36): in the run-length format a word begins with the
// trigger sample.  TRIG OUT rises 6 cycles after the pads took the trigger sample.  A stage is one of:
//   kind 0   the inputs under "mask" equal "value", and, if any edges are named, one of those inputs has just
//            risen ("rise") or fallen ("fall")
//   kind 1   a pulse has ended: the inputs under the mask equalled the value for "min" ... "max" samples and now
//            do not (a pulse that had begun before the capture does not count)
//   kind 2   they have equalled it for "min" samples in a row or more, and still do
//   kind 3   TRIG IN rose or fell (bits 2 and 3 of the stage's mode) since the sample before, and the inputs
//            under the mask equal the value
//
// The lines to the FX3: PCLK out, the inverse of the FPGA's clock, so that the FX3 takes the other lines half a
// cycle (5 ns) after the FPGA changes them; DQ31 ... 0 out; CTL0 SLCS# low, CTL1 SLWR# low in each cycle that
// carries a word, CTL2 SLOE#, CTL3 SLRD# and CTL7 PKTEND# high, CTL11 / CTL12 A1 / A0 low; CTL4 FLAGA in, high
// while the FX3 has a buffer to write into; CTL6 RUN in.  CTL5 and CTL8 are not used.  While RUN is low and
// nothing is left to send, every one of them is an input with a pull-up or pull-down.  The FX3 takes data one
// buffer at a time, and its flag says "full" a few cycles late, so the FPGA does not write until the flag falls:
// it counts.  A burst is the words of one FX3 buffer (register 20); after its last word the flag is ignored for
// HOLD cycles, and the next burst starts when the flag is high.
//
// Registers, on the FX3's I2C bus at address 0x2A (i2c_slave.v).  Numbers of more than one byte have their low
// byte first.  What a read returns of the status and the counts is copied as the read is addressed, so one read
// of several bytes fits together.
//   00 ... 03  R   4C 42 16 and the design's version, 04
//   04         RW  bit 0: the format, 0 raw, 1 run-length.  Bit 1: a test pattern in place of the probes, a
//                  counter of the samples taken, of which bits 7:4 say how far it is shifted right (0: every
//                  sample differs from the one before; 8: every 256th)
//   05         R   status.  Bit 0 the PLL has locked, 1 RUN, 2 sampling, 3 done (sampling has ended and every
//                  word has gone to the FX3), 4 overflow, 7:5 the writer's state (0 off, 1 starting, 2 waiting for
//                  the FX3's flag, 3 writing, 4 holding off after a buffer, 5 stopping, 6 done and waiting for RUN
//                  to fall)
//   06, 07     R   the inputs now: pod A, pod B
//   08 ... 0A  RW  the divider (24 bits): a sample every (divider + 1) x 5 ns
//   0C, 0D     RW  pod A's threshold: the PWM's duty cycle in 1/4096 (12 bits).  It changes when 0D is written
//   0E, 0F     RW  pod B's, likewise with 0F.  Both start at 2048: 0.311 V at TP34 / TP35, 1.56 V at the probe
//   10 ... 14  RW  the sample limit (40 bits); 0: none.  With a trigger it counts from the trigger sample
//   18 ... 1C  R   samples taken in this capture (40 bits); with a trigger, from the trigger sample on
//   20, 21     RW  words in one FX3 buffer (its size in bytes / 4; 4096; 0 counts as 4096)
//   22, 23     R   the most words the FIFO has held in this capture
//   24 ... 29  R   words written to the FX3 in this capture (48 bits)
//   2A         R   bit 0: the rest was thrown away after RUN fell (see above); bit 1 FLAGA
//   2B         R   13: the FIFO holds 2^13 words
//   2C         R   200: the samples a second with the divider at 0, in millions
//   2D         R   4: how many trigger stages there are
//   30         RW  bit 0: state capture.  Bit 1: at the capture clock's rising edges, bit 2: at its falling
//                  edges.  Bits 7:4: which input is the capture clock (0 A0 ... 15 B7)
//   31         RW  how many trigger stages are in use (0 ... 4); 0: no trigger, the capture begins at once
//   32, 33     RW  how many words the FIFO keeps while it waits for the trigger (at most its size less 8)
//   34         R   bit 0 triggered, 1 the word with the trigger sample has gone to the FX3 (36 is then valid),
//                  2 raw format: the trigger sample is that word's upper half, 3 waiting for the trigger,
//                  6:4 how many stages have been met
//   36, 37     R   how many words went to the FX3 before the word with the trigger sample
//   38 ... 3B  RW  the mode of stage 0 ... 3: bits 1:0 its kind, bit 2 TRIG IN rising, bit 3 TRIG IN falling
//   40 ... 4F  RW  stage 0: mask, value, rise, fall (16 bits each), min, max (32 bits each)
//   50 ... 7F  RW  stages 1, 2 and 3 likewise
//   80 ... 8F  RW  the glitch filter of A0 ... A7, B0 ... B7: N samples, 0 ... 250 (0: off)
// The settings at 04, 08, 10, 20, 30, 31 and 32 are taken as RUN rises; the thresholds at once (the filter behind
// the PWM needs 120 ms to settle); the stages and the glitch filters act as they are written, so they are written
// while RUN is low.  Unused registers read 0.
//
// The other outputs: LED ARM blinks at 1 Hz, and while RUN is high it is on as long as the capture waits for its
// trigger; LED TRIG is on from the trigger (or the first sample of a capture without one) to the last sample and
// while words go to the FX3; DBG0 1 kHz, DBG1 sampling, DBG2 RUN, DBG3 FLAGA; TRIG OUT is high from the trigger
// (or the first sample) to the last sample.
//
// JTAG instruction USER1 selects a 256-bit register, bit 0 first, for looking in when I2C does not answer; it is
// read only.  The counts are copied in the 100 MHz clock when a read begins and shown at the next one: read twice.
//   word 0   0x044C42B3: version 4 and a marker
//   word 1   bit 0 the PLL has locked (as it is now), 1 RUN, 2 sampling, 3 done, 4 overflow, 7:5 the state,
//            8 INIT_B, 9 SCL, 10 SDA, 11 FLAGA, 15:12 a count of the copies made, 31:16 the inputs
//   word 2   words written, bits 31:0
//   word 3   15:0 bits 47:32 of the same, 31:16 how often the I2C address has matched
//   word 4   samples taken, bits 31:0
//   word 5   7:0 bits 39:32 of the same, 15:8 register 04, 31:16 the FIFO's highest level
//   word 6   11:0 pod A's duty cycle, 27:16 pod B's, 31 the parity of the lines this design does not use
//   word 7   100 MHz clock cycles since configuration, divided by 256
module capture2_top #(
    parameter HALF_SECOND = 50000000,   // cycles of the 100 MHz clock; the test bench shortens these
    parameter HALF_MS = 50000,
    parameter START_WAIT = 4096,        // cycles from RUN to the first sample (PCLK starts at the 17th)
    parameter HOLD = 20,                // cycles after a buffer's last word in which the flag is ignored
    parameter LED_HOLD = 1000000,       // cycles LED TRIG stays on after a word
    parameter FIFO_BITS = 13,           // the FIFO holds 2^FIFO_BITS words
    parameter DRAIN_MAX = 4000000,      // cycles after RUN fell in which the FX3 must take what is left
    parameter I2C_FILTER = 16
) (
    input  wire clk24,
    output wire fx3_pclk,
    input  wire [7:0] cha,
    input  wire [7:0] chb,
    input  wire trig_in,
    output wire pwm_a,
    output wire pwm_b,
    output wire trig_out,
    output wire led_arm,
    output wire led_trig,
    output wire [3:0] dbg,
    output wire fx3_ctl0,
    output wire fx3_ctl1,
    output wire fx3_ctl2,
    output wire fx3_ctl3,
    input  wire fx3_ctl4,
    input  wire fx3_ctl5,
    input  wire fx3_ctl6,
    output wire fx3_ctl7,
    input  wire fx3_ctl8,
    output wire fx3_ctl11,
    output wire fx3_ctl12,
    output wire [31:0] fx3_dq,
    input  wire fx3_scl,
    inout  wire fx3_sda,
    input  wire init_b,
    input  wire pif_gpio2,
    input  wire pif_gpio3,
    input  wire pif_gpio7,
    input  wire pif_gpio8,
    input  wire pif_gpio9,
    input  wire pif_gpio10,
    input  wire pif_gpio11,
    input  wire pif_gpio14,
    input  wire pif_gpio15
);
    localparam N = 256;
    localparam [31:0] MARKER = 32'h044C42B3;
    localparam [7:0] VERSION = 8'd4;
    localparam [7:0] SAMPLE_MHZ = 8'd200;
    localparam STAGES = 4;
    localparam [FIFO_BITS:0] RING_MAX = (1 << FIFO_BITS) - 8;
    localparam [2:0] S_OFF = 3'd0, S_START = 3'd1, S_WAIT = 3'd2, S_BURST = 3'd3, S_HOLD = 3'd4, S_STOP = 3'd5,
                     S_DONE = 3'd6;

    // What the lines read while nothing drives them.  INIT_B has the board's 4.7k pull-up, which an unused pin's
    // pull-down would work against.  SCL and SDA have the board's pull-ups.
    PULLUP pu_ctl0 (.O(fx3_ctl0));
    PULLUP pu_ctl1 (.O(fx3_ctl1));
    PULLUP pu_ctl2 (.O(fx3_ctl2));
    PULLUP pu_ctl3 (.O(fx3_ctl3));
    PULLUP pu_ctl7 (.O(fx3_ctl7));
    PULLUP pu_init (.O(init_b));
    PULLDOWN pd_pclk (.O(fx3_pclk));
    PULLDOWN pd_ctl4 (.O(fx3_ctl4));
    PULLDOWN pd_ctl5 (.O(fx3_ctl5));
    PULLDOWN pd_ctl6 (.O(fx3_ctl6));
    PULLDOWN pd_ctl8 (.O(fx3_ctl8));
    PULLDOWN pd_ctl11 (.O(fx3_ctl11));
    PULLDOWN pd_ctl12 (.O(fx3_ctl12));
    PULLDOWN pd_pif2 (.O(pif_gpio2));
    PULLDOWN pd_pif3 (.O(pif_gpio3));
    PULLDOWN pd_pif7 (.O(pif_gpio7));
    PULLDOWN pd_pif8 (.O(pif_gpio8));
    PULLDOWN pd_pif9 (.O(pif_gpio9));
    PULLDOWN pd_pif10 (.O(pif_gpio10));
    PULLDOWN pd_pif11 (.O(pif_gpio11));
    PULLDOWN pd_pif14 (.O(pif_gpio14));
    PULLDOWN pd_pif15 (.O(pif_gpio15));
    genvar g;
    generate
        for (g = 0; g < 32; g = g + 1) begin : pd_dq
            PULLDOWN pd (.O(fx3_dq[g]));
        end
    endgenerate

    // ---- the clock: 24 MHz x 25 / 6 = 100 MHz (the PLL's oscillator runs at 600 MHz) ----
    wire clk24_pad, pll_fb, clk_pll, clk, locked;
    IBUFG ibufg_clk24 (.I(clk24), .O(clk24_pad));
    PLL_BASE #(
        .BANDWIDTH("OPTIMIZED"), .CLK_FEEDBACK("CLKFBOUT"), .COMPENSATION("INTERNAL"), .CLKIN_PERIOD(41.667),
        .REF_JITTER(0.010), .DIVCLK_DIVIDE(1), .CLKFBOUT_MULT(25), .CLKFBOUT_PHASE(0.0), .CLKOUT0_DIVIDE(6),
        .CLKOUT0_PHASE(0.0), .CLKOUT0_DUTY_CYCLE(0.5)
    ) pll (
        .CLKIN(clk24_pad), .CLKFBIN(pll_fb), .CLKFBOUT(pll_fb), .RST(1'b0), .CLKOUT0(clk_pll), .CLKOUT1(),
        .CLKOUT2(), .CLKOUT3(), .CLKOUT4(), .CLKOUT5(), .LOCKED(locked));
    BUFG bufg_clk (.I(clk_pll), .O(clk));

    // ---- the registers ----
    reg [7:0] r_control = 8'd0;
    reg [23:0] r_div = 24'd0;
    reg [11:0] r_duty_a = 12'd2048, r_duty_b = 12'd2048;
    reg [7:0] duty_a_low = 8'd0, duty_b_low = 8'd0;
    reg [39:0] r_limit = 40'd0;
    reg [15:0] r_burst = 16'd4096;
    reg [7:0] r_clock = 8'd0;                   // 30: state capture and its clock
    reg [2:0] r_nstage = 3'd0;                  // 31: trigger stages in use
    reg [15:0] r_ring = 16'd0;                  // 32: words kept while waiting
    reg [8 * STAGES - 1:0] r_tmode = 0;         // 38 ... 3B: a byte for each stage
    reg [128 * STAGES - 1:0] r_trig = 0;        // 40 ... 7F: 16 bytes for each stage
    reg [127:0] r_glitch = 128'd0;              // 80 ... 8F: a byte for each input

    wire i2c_sda_low, i2c_wr, i2c_rd_begin;
    wire [7:0] i2c_wr_addr, i2c_wr_data, i2c_rd_addr;
    wire [15:0] i2c_hits;
    reg [7:0] i2c_rd_data = 8'd0;
    i2c_slave #(.ADDRESS(7'h2A), .FILTER(I2C_FILTER)) i2c (
        .clk(clk), .scl(fx3_scl), .sda(fx3_sda), .sda_low(i2c_sda_low), .wr(i2c_wr), .wr_addr(i2c_wr_addr),
        .wr_data(i2c_wr_data), .rd_begin(i2c_rd_begin), .rd_addr(i2c_rd_addr), .rd_data(i2c_rd_data),
        .hits(i2c_hits));
    assign fx3_sda = i2c_sda_low ? 1'b0 : 1'bz;

    always @(posedge clk) begin
        if (i2c_wr)
            case (i2c_wr_addr)
            8'h04: r_control <= i2c_wr_data;
            8'h08: r_div[7:0] <= i2c_wr_data;
            8'h09: r_div[15:8] <= i2c_wr_data;
            8'h0A: r_div[23:16] <= i2c_wr_data;
            8'h0C: duty_a_low <= i2c_wr_data;
            8'h0D: r_duty_a <= {i2c_wr_data[3:0], duty_a_low};
            8'h0E: duty_b_low <= i2c_wr_data;
            8'h0F: r_duty_b <= {i2c_wr_data[3:0], duty_b_low};
            8'h10: r_limit[7:0] <= i2c_wr_data;
            8'h11: r_limit[15:8] <= i2c_wr_data;
            8'h12: r_limit[23:16] <= i2c_wr_data;
            8'h13: r_limit[31:24] <= i2c_wr_data;
            8'h14: r_limit[39:32] <= i2c_wr_data;
            8'h20: r_burst[7:0] <= i2c_wr_data;
            8'h21: r_burst[15:8] <= i2c_wr_data;
            8'h30: r_clock <= i2c_wr_data;
            8'h31: r_nstage <= i2c_wr_data[2:0];
            8'h32: r_ring[7:0] <= i2c_wr_data;
            8'h33: r_ring[15:8] <= i2c_wr_data;
            default: ;
            endcase
        if (i2c_wr && i2c_wr_addr[7:2] == 6'b001110)                // 38 ... 3B
            r_tmode[{i2c_wr_addr[1:0], 3'd0} +: 8] <= i2c_wr_data;
        if (i2c_wr && i2c_wr_addr[7:6] == 2'b01)                    // 40 ... 7F
            r_trig[{i2c_wr_addr[5:0], 3'd0} +: 8] <= i2c_wr_data;
        if (i2c_wr && i2c_wr_addr[7:4] == 4'h8)                     // 80 ... 8F
            r_glitch[{i2c_wr_addr[3:0], 3'd0} +: 8] <= i2c_wr_data;
    end

    // ---- 1 Hz, 1 kHz, the two thresholds ----
    reg [25:0] div = 0;
    reg hz = 0;
    reg [15:0] kdiv = 0;
    reg khz = 0;
    reg [11:0] pwm_count = 0;
    (* IOB = "TRUE" *) reg pwm_a_r = 0;
    (* IOB = "TRUE" *) reg pwm_b_r = 0;
    always @(posedge clk) begin
        if (div == HALF_SECOND - 1) begin
            div <= 0;
            hz <= ~hz;
        end else
            div <= div + 1'b1;
        if (kdiv == HALF_MS - 1) begin
            kdiv <= 0;
            khz <= ~khz;
        end else
            kdiv <= kdiv + 1'b1;
        pwm_count <= pwm_count + 1'b1;
        pwm_a_r <= (pwm_count < r_duty_a);
        pwm_b_r <= (pwm_count < r_duty_b);
    end
    assign pwm_a = pwm_a_r;
    assign pwm_b = pwm_b_r;

    // ---- inputs: two flip-flops each, the first in the pad.  A probe's pad has two first flip-flops (IDDR2): one
    // takes the input at the clock's falling edge and one at its rising edge, and both show what they took at the
    // rising edge, the falling edge's half being the earlier by 5 ns ----
    (* IOB = "TRUE" *) reg flaga_m = 0;
    (* IOB = "TRUE" *) reg run_m = 0;
    reg flaga = 0, run = 0;
    wire [15:0] pad = {chb, cha};
    wire [15:0] q_rise, q_fall;
    generate
        for (g = 0; g < 16; g = g + 1) begin : in_ddr
            IDDR2 #(.DDR_ALIGNMENT("C0"), .INIT_Q0(1'b1), .INIT_Q1(1'b1), .SRTYPE("SYNC")) iddr (
                .Q0(q_rise[g]), .Q1(q_fall[g]), .C0(clk), .C1(~clk), .CE(1'b1), .D(pad[g]), .R(1'b0), .S(1'b0));
        end
    endgenerate
    wire tin_rise, tin_fall;            // TRIG IN, taken like a probe
    IDDR2 #(.DDR_ALIGNMENT("C0"), .INIT_Q0(1'b0), .INIT_Q1(1'b0), .SRTYPE("SYNC")) iddr_trig (
        .Q0(tin_rise), .Q1(tin_fall), .C0(clk), .C1(~clk), .CE(1'b1), .D(trig_in), .R(1'b0), .S(1'b0));
    reg [16:0] in_e = 0, in_l = 0;      // the earlier half and the later one, 1 = above the threshold; 16: TRIG IN
    reg [1:0] locked_s = 0;
    reg [1:0] init_s = 0, others_s = 0;
    // the lines this design does not use (the Pi's nine, CTL5, CTL8) stay inputs: JTAG shows their parity
    wire others = ^{pif_gpio15, pif_gpio14, pif_gpio11, pif_gpio10, pif_gpio9, pif_gpio8, pif_gpio7, pif_gpio3,
                    pif_gpio2, fx3_ctl8, fx3_ctl5};
    always @(posedge clk) begin
        flaga_m <= fx3_ctl4;
        run_m <= fx3_ctl6;
        flaga <= flaga_m;
        run <= run_m;
        in_e <= {tin_fall, ~q_fall};    // the comparators invert
        in_l <= {tin_rise, ~q_rise};
        locked_s <= {locked_s[0], locked};
        init_s <= {init_s[0], init_b};
        others_s <= {others_s[0], others};
    end
    wire go = run & locked_s[1];

    // ---- the writer's state, and what the other parts need of it ----
    reg [2:0] state = S_OFF;
    reg [15:0] cnt = 0;
    wire clear = (state == S_OFF) && go;                                    // a capture begins: everything to 0
    wire begin_sampling = (state == S_START) && (cnt == START_WAIT - 1) && go;
    reg c_format = 0, c_test = 0;
    reg [3:0] c_shift = 0;
    reg [23:0] c_div = 0;
    reg [15:0] c_burst = 16'd4096;
    reg c_state = 0, c_rise = 0, c_fall = 0, c_filt = 0;
    reg [3:0] c_clk = 0;
    reg [2:0] c_nstage = 0;
    reg [FIFO_BITS:0] c_ring = 0;
    wire [2:0] nstage_in = (r_nstage > STAGES) ? 3'd0 : r_nstage;

    // ---- the glitch filter.  The largest N is looked for all the time, 16 cycles for the 16 inputs, and in the next
    // 16 each input's wait is set to the largest N less its own: so every input is as late as the slowest ----
    reg [4:0] gscan = 0;
    reg [7:0] gmax = 0, nmax = 0;
    reg [127:0] gwait = 128'd0;
    wire [7:0] gthis = r_glitch[{gscan[3:0], 3'd0} +: 8];
    always @(posedge clk) begin
        gscan <= gscan + 1'b1;
        if (!gscan[4]) begin
            if (gscan[3:0] == 4'd0 || gthis > gmax)
                gmax <= gthis;
        end else begin
            gwait[{gscan[3:0], 3'd0} +: 8] <= gmax - gthis;
            nmax <= gmax;
        end
    end
    wire [16:0] f_e, f_l;               // the inputs behind the filter
    generate
        for (g = 0; g < 17; g = g + 1) begin : filt
            // TRIG IN (16) has no filter of its own and waits like an input whose filter is off
            wire [7:0] n = (g == 16) ? 8'd0 : r_glitch[8 * (g % 16) +: 8];
            wire [7:0] w = (g == 16) ? nmax : gwait[8 * (g % 16) +: 8];
            wire tap_e, tap_l;
            tap128 de (.clk(clk), .d(in_e[g]), .tap(w[7:1]), .q(tap_e));
            tap128 dl (.clk(clk), .d(in_l[g]), .tap(w[7:1] + w[0]), .q(tap_l));
            wire x1 = w[0] ? tap_l : tap_e;             // an odd wait: the later half of the cycle before comes first
            wire x2 = w[0] ? tap_e : tap_l;
            reg y = 0, o_e = 0, o_l = 0;                // y: the level that counts; o: it a sample later
            reg [7:0] c = 0;                            // for how many samples the input has differed from y
            wire [7:0] c1p = c + 1'b1;
            wire acc1 = (x1 != y) && (c1p >= n);
            wire y1 = acc1 ? x1 : y;
            wire [7:0] c1 = (x1 == y || acc1) ? 8'd0 : c1p;
            wire [7:0] c2p = c1 + 1'b1;
            wire acc2 = (x2 != y1) && (c2p >= n);
            wire y2 = acc2 ? x2 : y1;
            wire [7:0] c2 = (x2 == y1 || acc2) ? 8'd0 : c2p;
            always @(posedge clk) begin
                y <= y2;
                c <= c2;
                o_e <= (n == 8'd0) ? x1 : y;
                o_l <= (n == 8'd0) ? x2 : y1;
            end
            assign f_e[g] = o_e;
            assign f_l[g] = o_l;
        end
    endgenerate

    // ---- what the sampler sees: the two halves of this cycle (g_e, g_l), the later half of the cycle before
    // (g_p), and where the capture clock and TRIG IN changed: "a" between g_p and g_e, "b" between g_e and g_l ----
    wire [16:0] m_e = c_filt ? f_e : in_e;
    wire [16:0] m_l = c_filt ? f_l : in_l;
    wire ck_e = m_e[c_clk], ck_l = m_l[c_clk];
    reg [15:0] g_e = 0, g_l = 0, g_p = 0;
    reg ck_last = 0, tin_last = 0;
    reg ed_a = 0, ed_b = 0, tr_a = 0, tr_b = 0, tf_a = 0, tf_b = 0;
    always @(posedge clk) begin
        g_e <= m_e[15:0];
        g_l <= m_l[15:0];
        g_p <= g_l;
        ck_last <= ck_l;
        tin_last <= m_l[16];
        ed_a <= (c_rise && !ck_last && ck_e) || (c_fall && ck_last && !ck_e);
        ed_b <= (c_rise && !ck_e && ck_l) || (c_fall && ck_e && !ck_l);
        tr_a <= !tin_last && m_e[16];
        tr_b <= !m_e[16] && m_l[16];
        tf_a <= tin_last && !m_e[16];
        tf_b <= m_e[16] && !m_l[16];
    end

    // ---- the sampler.  Each cycle brings two samples, "a" and then "b": g_e and g_l, or in a state capture g_p
    // and g_e, each the sample before an edge.  With the divider, "skip" says how many go by before the next one
    // that is kept, counted from this cycle's first; with the divider at 0 both are kept.  The test pattern counts
    // the samples kept: tcount is the count for a cycle's first sample, tcount1 that plus one for its second (31
    // bits each: the pattern is 16 bits of them, shifted by up to 15).  s_xr ... s_yf: TRIG IN rose or fell
    // between the sample kept before and this one ----
    reg sampling = 0, overflow = 0;
    reg [23:0] skip = 0;
    reg [30:0] tcount = 0, tcount1 = 31'd1;
    reg [1:0] s_n = 0;                  // how many samples this cycle kept: s_x, and s_y after it
    reg [15:0] s_x = 0, s_y = 0;
    reg s_xr = 0, s_xf = 0, s_yr = 0, s_yf = 0, pend_r = 0, pend_f = 0;
    wire gate_stop;                     // the sample limit has been reached (below)
    wire fast = (c_div == 24'd0);
    wire take_a = sampling && (c_state ? ed_a : (fast || skip == 24'd0));
    wire take_b = sampling && (c_state ? ed_b : (fast || skip == 24'd1));
    wire [15:0] cand_a = c_state ? g_p : g_e;
    wire [15:0] cand_b = c_state ? g_e : g_l;
    wire both = take_a && take_b;
    wire any = take_a || take_b;
    wire [30:0] tshift = tcount >> c_shift, tshift1 = tcount1 >> c_shift;
    // TRIG IN's edges up to each of the two samples.  In a state capture "a" is taken before the edge at a
    wire ar = c_state ? pend_r : (pend_r | tr_a);
    wire af = c_state ? pend_f : (pend_f | tf_a);
    wire br = (take_a ? 1'b0 : ar) | (c_state ? tr_a : tr_b);
    wire bf = (take_a ? 1'b0 : af) | (c_state ? tf_a : tf_b);
    always @(posedge clk) begin
        s_n <= both ? 2'd2 : any ? 2'd1 : 2'd0;
        s_x <= c_test ? tshift[15:0] : take_a ? cand_a : cand_b;
        s_y <= c_test ? tshift1[15:0] : cand_b;
        s_xr <= take_a ? ar : br;
        s_xf <= take_a ? af : bf;
        s_yr <= br;
        s_yf <= bf;
        if (!sampling) begin
            pend_r <= 1'b0;
            pend_f <= 1'b0;
        end else if (take_b) begin
            pend_r <= c_state & tr_b;
            pend_f <= c_state & tf_b;
        end else begin
            pend_r <= (take_a ? 1'b0 : ar) | (c_state & tr_a) | tr_b;
            pend_f <= (take_a ? 1'b0 : af) | (c_state & tf_a) | tf_b;
        end
        if (clear) begin
            sampling <= 1'b0;
            tcount <= 31'd0;
            tcount1 <= 31'd1;
            skip <= 24'd0;
        end else begin
            if (begin_sampling)
                sampling <= 1'b1;
            if (both) begin
                tcount <= tcount + 2'd2;
                tcount1 <= tcount1 + 2'd2;
            end else if (any) begin
                tcount <= tcount + 1'b1;
                tcount1 <= tcount1 + 1'b1;
            end
            if (sampling && !fast && !c_state) begin
                if (skip == 24'd0)
                    skip <= c_div - 1'b1;
                else if (skip == 24'd1)
                    skip <= c_div;
                else
                    skip <= skip - 2'd2;
            end
            if (!go || overflow || gate_stop)
                sampling <= 1'b0;
        end
    end

    // ---- the trigger, three cycles.  First: for each stage, do the two samples match, and has an input it names
    // just risen or fallen (t_last is the sample kept before s_x) ----
    reg [1:0] d1_n = 0, d2_n = 0, d3_n = 0;
    reg [15:0] d1_x = 0, d1_y = 0, d2_x = 0, d2_y = 0, d3_x = 0, d3_y = 0;
    reg [15:0] t_last = 0;
    reg t_any = 0;                      // a sample has been kept in this capture: t_last is one
    always @(posedge clk) begin
        d1_n <= s_n;
        d1_x <= s_x;
        d1_y <= s_y;
        d2_n <= d1_n;
        d2_x <= d1_x;
        d2_y <= d1_y;
        d3_n <= d2_n;
        d3_x <= d2_x;
        d3_y <= d2_y;
        if (clear)
            t_any <= 1'b0;
        else if (s_n != 2'd0)
            t_any <= 1'b1;
        if (s_n == 2'd2)
            t_last <= s_y;
        else if (s_n == 2'd1)
            t_last <= s_x;
    end
    // Second: each stage's condition for each of the two samples (ev_x, ev_y).  A stage counts how long its
    // inputs have matched without a gap, downwards: to_min from "min" to 0, to_max from "max" to 0 and then "over"
    wire [STAGES - 1:0] ev_x, ev_y;
    generate
        for (g = 0; g < STAGES; g = g + 1) begin : stage
            wire [15:0] mask = r_trig[128 * g +: 16], value = r_trig[128 * g + 16 +: 16];
            wire [15:0] rise = r_trig[128 * g + 32 +: 16], fall = r_trig[128 * g + 48 +: 16];
            wire [31:0] tmin = r_trig[128 * g + 64 +: 32], tmax = r_trig[128 * g + 96 +: 32];
            wire [1:0] kind = r_tmode[8 * g +: 2];
            wire in_r = r_tmode[8 * g + 2], in_f = r_tmode[8 * g + 3];
            reg mx = 0, my = 0, ex = 0, ey = 0, ix = 0, iy = 0, level_only = 0;
            always @(posedge clk) begin
                mx <= ((s_x & mask) == value);
                my <= ((s_y & mask) == value);
                ex <= t_any && |((~t_last & s_x & rise) | (t_last & ~s_x & fall));
                ey <= |((~s_x & s_y & rise) | (s_x & ~s_y & fall));
                ix <= (in_r & s_xr) | (in_f & s_xf);
                iy <= (in_r & s_yr) | (in_f & s_yf);
                level_only <= ~|{rise, fall};
            end
            reg [31:0] to_min = 0, to_max = 0, min_m1 = 0, max_m1 = 0;
            reg over = 0, was = 0, fresh = 0, ex_r = 0, ey_r = 0;
            wire z0 = (to_min == 32'd0), z1 = (to_min == 32'd1), z2 = (to_min == 32'd2);
            wire o0 = (to_max == 32'd0), o1 = (to_max == 32'd1);
            wire min0 = (tmin == 32'd0), min1 = (tmin == 32'd1), max0 = (tmax == 32'd0);
            wire over_x = mx & (over | o0);             // after the first sample: longer than max
            wire evx = (kind == 2'd0) ? (mx & (level_only | ex)) :
                       (kind == 2'd1) ? (~mx & was & fresh & z0 & ~over) :
                       (kind == 2'd2) ? (mx & (z0 | z1)) : (mx & ix);
            wire evy = (kind == 2'd0) ? (my & (level_only | ey)) :
                       (kind == 2'd1) ? (~my & mx & fresh & (z0 | z1) & ~over_x) :
                       (kind == 2'd2) ? (my & (mx ? (z0 | z1 | z2) : (min0 | min1))) : (my & iy);
            always @(posedge clk) begin
                min_m1 <= tmin - 1'b1;
                max_m1 <= tmax - 1'b1;
                ex_r <= evx;
                ey_r <= evy;
                if (clear) begin
                    to_min <= tmin;
                    to_max <= tmax;
                    over <= 1'b0;
                    was <= 1'b0;
                    fresh <= 1'b0;
                end else if (d1_n == 2'd1) begin
                    was <= mx;
                    if (!mx) begin
                        to_min <= tmin;
                        to_max <= tmax;
                        over <= 1'b0;
                        fresh <= 1'b1;
                    end else begin
                        if (!z0)
                            to_min <= to_min - 1'b1;
                        if (o0)
                            over <= 1'b1;
                        else
                            to_max <= to_max - 1'b1;
                    end
                end else if (d1_n == 2'd2) begin
                    was <= my;
                    if (!my) begin
                        to_min <= tmin;
                        to_max <= tmax;
                        over <= 1'b0;
                        fresh <= 1'b1;
                    end else if (!mx) begin             // a match that begins with the second sample
                        to_min <= min0 ? 32'd0 : min_m1;
                        to_max <= max0 ? 32'd0 : max_m1;
                        over <= max0;
                        fresh <= 1'b1;
                    end else begin
                        to_min <= (z0 | z1) ? 32'd0 : to_min - 2'd2;
                        if (o0 | o1) begin
                            over <= 1'b1;
                            to_max <= 32'd0;
                        end else
                            to_max <= to_max - 2'd2;
                    end
                end
            end
            assign ev_x[g] = ex_r;
            assign ev_y[g] = ey_r;
        end
    endgenerate
    // Third: the stages in turn.  A sample meets one stage at most; the one that meets the last is the trigger
    // sample (t_x: it is d3_x, t_y: d3_y)
    reg [2:0] met = 0;                  // stages met so far
    reg fired = 0, t_x = 0, t_y = 0;
    (* IOB = "TRUE" *) reg trig_out_r = 0;
    wire waiting = (c_nstage != 3'd0) && !fired;
    wire hit_x = waiting && (d2_n != 2'd0) && ev_x[met[1:0]];
    wire [2:0] met_x = met + hit_x;
    wire fire_x = hit_x && (met_x == c_nstage);
    wire hit_y = waiting && !fire_x && (d2_n == 2'd2) && ev_y[met_x[1:0]];
    wire [2:0] met_y = met_x + hit_y;
    wire fire_y = hit_y && (met_y == c_nstage);
    always @(posedge clk) begin
        trig_out_r <= sampling && ((c_nstage == 3'd0) || fired || fire_x || fire_y);
        if (clear) begin
            met <= 3'd0;
            fired <= 1'b0;
            t_x <= 1'b0;
            t_y <= 1'b0;
        end else begin
            met <= met_y;
            t_x <= fire_x;
            t_y <= fire_y;
            if (fire_x || fire_y)
                fired <= 1'b1;
        end
    end

    // ---- which samples are part of the capture: every one until the trigger (the FIFO keeps the newest of them),
    // and from the trigger sample on as many as the sample limit says.  A capture without a trigger counts from
    // its first sample ----
    reg [1:0] a_n = 0;
    reg [15:0] a_x = 0, a_y = 0;
    reg a_tx = 0, a_ty = 0;             // a_x, or a_y, is the trigger sample
    reg counting = 0, limit_on = 0, stopped = 0;
    reg [39:0] samples = 0, remaining = 0;
    wire from_x = counting | t_x;
    wire two_count = from_x && (d3_n == 2'd2);
    wire one_count = (from_x && (d3_n == 2'd1)) || (!counting && t_y);
    wire few_left = limit_on && (remaining[39:2] == 38'd0);
    wire one_left = few_left && (remaining[1:0] == 2'd1);
    wire two_left = few_left && (remaining[1:0] == 2'd2);
    wire cut = two_count && one_left;                   // of the cycle's two samples only the first is wanted
    wire stop_now = !stopped && ((two_count && (one_left || two_left)) || (one_count && one_left));
    assign gate_stop = stopped | stop_now;
    always @(posedge clk) begin
        a_n <= stopped ? 2'd0 : cut ? 2'd1 : d3_n;
        a_x <= d3_x;
        a_y <= d3_y;
        a_tx <= !stopped && t_x;
        a_ty <= !stopped && t_y;
        if (clear) begin
            samples <= 40'd0;
            remaining <= r_limit;
            limit_on <= (r_limit != 40'd0);
            counting <= (nstage_in == 3'd0);
            stopped <= 1'b0;
        end else begin
            if (t_x || t_y)
                counting <= 1'b1;
            if (stop_now)
                stopped <= 1'b1;
            if (!stopped) begin
                if (two_count && !one_left) begin
                    samples <= samples + 2'd2;
                    remaining <= remaining - 2'd2;
                end else if (two_count || one_count) begin
                    samples <= samples + 1'b1;
                    remaining <= remaining - 1'b1;
                end
            end
        end
    end

    // ---- which of them equal the sample before: compared here, a stage ahead of the words.  The trigger sample
    // counts as different from the one before it, so that in the run-length format a word begins with it ----
    reg [1:0] b_n = 0;
    reg [15:0] b_x = 0, b_y = 0, last = 0;
    reg b_xy = 0, b_xl = 0;             // b_x equals b_y; b_x equals the sample before it
    reg b_tx = 0, b_ty = 0;
    always @(posedge clk) begin
        b_n <= a_n;
        b_x <= a_x;
        b_y <= a_y;
        b_xy <= (a_x == a_y) && !a_ty;
        b_xl <= (a_x == last) && !a_tx;
        b_tx <= a_tx && (a_n != 2'd0);
        b_ty <= a_ty && (a_n == 2'd2);
        if (a_n == 2'd2)
            last <= a_y;
        else if (a_n == 2'd1)
            last <= a_x;
    end
    // nothing more will come to the words
    wire upstream = sampling || (s_n != 2'd0) || (d1_n != 2'd0) || (d2_n != 2'd0) || (d3_n != 2'd0) || (a_n != 2'd0);

    // ---- samples to words: none, one or two in a cycle (two when both samples end a run) ----
    // t_mark: the trigger sample came by in the cycle before; t_pre of that cycle's words (0 ... 2) are before the
    // word that holds it, and in the raw format t_upper says that it is that word's upper half
    reg half = 0, have = 0;
    reg t_mark = 0, t_upper = 0;
    reg [1:0] t_pre = 0;
    reg [1:0] pushn = 0;                // how many words the cycle before made: din0, and din1 after it
    reg [15:0] lo = 0, cur = 0, rep = 0;
    reg [31:0] din0 = 0, din1 = 0;
    wire rep_full = (rep == 16'hFFFF);  // the run holds 65536 samples
    always @(posedge clk) begin
        pushn <= 2'd0;
        t_mark <= !clear && (b_tx || b_ty);
        if (!c_format) begin
            t_pre <= (b_ty && half) ? 2'd1 : 2'd0;
            t_upper <= half ? b_tx : b_ty;
        end else begin
            t_pre <= b_tx ? {1'b0, have} : (have && !(b_xl && !rep_full)) ? 2'd2 : 2'd1;
            t_upper <= 1'b0;
        end
        if (clear) begin
            half <= 1'b0;
            have <= 1'b0;
        end else if (b_n != 2'd0) begin
            if (!c_format) begin
                if (b_n == 2'd2) begin
                    pushn <= 2'd1;
                    if (!half)
                        din0 <= {b_y, b_x};
                    else begin
                        din0 <= {b_x, lo};
                        lo <= b_y;
                    end
                end else if (!half) begin
                    lo <= b_x;
                    half <= 1'b1;
                end else begin
                    din0 <= {b_x, lo};
                    pushn <= 2'd1;
                    half <= 1'b0;
                end
            end else if (b_n == 2'd1) begin
                if (!have) begin
                    cur <= b_x;
                    rep <= 16'd0;
                    have <= 1'b1;
                end else if (b_xl && !rep_full)
                    rep <= rep + 1'b1;
                else begin
                    din0 <= {rep, cur};
                    pushn <= 2'd1;
                    cur <= b_x;
                    rep <= 16'd0;
                end
            end else begin
                have <= 1'b1;
                if (!have) begin
                    if (b_xy) begin
                        cur <= b_x;
                        rep <= 16'd1;
                    end else begin
                        din0 <= {16'd0, b_x};
                        pushn <= 2'd1;
                        cur <= b_y;
                        rep <= 16'd0;
                    end
                end else if (b_xl && !rep_full) begin           // the first sample goes on with the run
                    if (!b_xy) begin
                        din0 <= {rep + 1'b1, cur};
                        pushn <= 2'd1;
                        cur <= b_y;
                        rep <= 16'd0;
                    end else if (rep == 16'hFFFE) begin         // and fills it: the second starts the next
                        din0 <= {16'hFFFF, cur};
                        pushn <= 2'd1;
                        rep <= 16'd0;
                    end else
                        rep <= rep + 2'd2;
                end else begin                                  // the run ended before the first sample
                    din0 <= {rep, cur};
                    if (b_xy) begin
                        pushn <= 2'd1;
                        cur <= b_x;
                        rep <= 16'd1;
                    end else begin
                        din1 <= {16'd0, b_x};
                        pushn <= 2'd2;
                        cur <= b_y;
                        rep <= 16'd0;
                    end
                end
            end
        end else if (!upstream) begin                           // the end: what is left goes out
            if (half) begin
                din0 <= {lo, lo};
                pushn <= 2'd1;
                half <= 1'b0;
            end else if (have) begin
                din0 <= {rep, cur};
                pushn <= 2'd1;
                have <= 1'b0;
            end
        end
    end

    // ---- the FIFO: words 0, 2, 4 ... in one memory and words 1, 3, 5 ... in the other, so that two words fit in
    // one cycle.  Nothing more goes in after the first word that found no room.  While the capture waits for its
    // trigger ("ring") nothing is read, and when the FIFO holds c_ring words each new word pushes the oldest out.
    // That ends with the words before the trigger sample's word, whose place (t_at) is kept: when the reading gets
    // there, the count of words read is the trigger word's number ----
    reg [FIFO_BITS:0] level = 0, fifo_max = 0;
    reg [FIFO_BITS-1:0] wp = 0, rp = 0, t_at = 0;
    reg ring = 0, t_placed = 0, t_half = 0, t_found = 0;
    reg [15:0] t_word = 0;
    wire flushed = !upstream && (b_n == 2'd0) && !half && !have && (pushn == 2'd0);
    wire [1:0] ring_in = !ring ? 2'd0 : t_mark ? t_pre : pushn;     // words that go into the ring in this cycle
    wire [1:0] ring_out = (level == c_ring) ? ring_in :
                          (level + 1'b1 == c_ring && ring_in == 2'd2) ? 2'd1 : 2'd0;
    wire room1 = !level[FIFO_BITS];                                 // for one word
    wire room2 = !level[FIFO_BITS] && !(&level[FIFO_BITS-1:0]);     // for two
    wire we0 = (pushn != 2'd0) && !overflow && room1;
    wire we1 = (pushn == 2'd2) && !overflow && room2;
    wire pop = (state == S_BURST) && (level != 0) && !ring;
    always @(posedge clk) begin
        if (clear) begin
            overflow <= 1'b0;
            wp <= 0;
            fifo_max <= 0;
            ring <= (nstage_in != 3'd0);
            t_placed <= 1'b0;
            t_half <= 1'b0;
        end else begin
            if (t_mark) begin
                ring <= 1'b0;
                t_placed <= 1'b1;
                t_half <= t_upper;
                t_at <= wp + t_pre;
            end else if (ring && flushed && state != S_START)       // sampling has ended without a trigger
                ring <= 1'b0;
            if ((pushn != 2'd0 && !room1) || (pushn == 2'd2 && !room2))
                overflow <= 1'b1;
            wp <= wp + we0 + we1;
            if (level > fifo_max)
                fifo_max <= level;
        end
    end
    always @(posedge clk) begin
        if (clear)
            level <= 0;
        else
            level <= level + we0 + we1 - pop - ring_out;
    end

    // the two memories: block RAM, read through two registers on the way to the pads
    (* ram_style = "block" *) reg [31:0] mem_e [0:(1 << (FIFO_BITS - 1)) - 1];
    (* ram_style = "block" *) reg [31:0] mem_o [0:(1 << (FIFO_BITS - 1)) - 1];
    wire [FIFO_BITS-1:0] wp1 = wp + 1'b1;
    wire we_e = wp[0] ? we1 : we0;
    wire we_o = wp[0] ? we0 : we1;
    wire [FIFO_BITS-2:0] wa_e = wp[0] ? wp1[FIFO_BITS-1:1] : wp[FIFO_BITS-1:1];
    wire [FIFO_BITS-2:0] wa_o = wp[FIFO_BITS-1:1];
    wire [31:0] wd_e = wp[0] ? din1 : din0;
    wire [31:0] wd_o = wp[0] ? din0 : din1;
    reg [31:0] q_e = 0, q_o = 0, mem_q2 = 0;
    reg rsel = 0;
    always @(posedge clk) begin
        if (we_e)
            mem_e[wa_e] <= wd_e;
        q_e <= mem_e[rp[FIFO_BITS-1:1]];
    end
    always @(posedge clk) begin
        if (we_o)
            mem_o[wa_o] <= wd_o;
        q_o <= mem_o[rp[FIFO_BITS-1:1]];
    end
    always @(posedge clk) begin
        rsel <= rp[0];
        mem_q2 <= rsel ? q_o : q_e;
    end

    // ---- the writer ----
    reg ctl_en = 0, dq_en = 0, pclk_en = 0;
    (* IOB = "TRUE" *) reg slwr_n = 1'b1;
    (* IOB = "TRUE" *) reg [31:0] dq_r = 32'd0;
    reg pop_d1 = 0, pop_d2 = 0;
    reg [15:0] left = 0;
    reg [47:0] words = 0;
    reg [7:0] starts = 0;
    reg [22:0] drain = 0;
    reg done = 0, aborted = 0;
    reg [19:0] led_count = 0;
    reg [39:0] cycles = 0;
    // nothing more will come from the sampler, and nothing is left on the way to the pads
    wire finished = flushed && !ring && (level == 0) && !pop_d1 && !pop_d2;
    wire gave_up = !go && (drain == DRAIN_MAX);

    always @(posedge clk) begin
        cycles <= cycles + 1'b1;
        pop_d1 <= pop;
        pop_d2 <= pop_d1;
        slwr_n <= ~pop_d2;
        if (pop_d2)
            dq_r <= mem_q2;
        if (led_count != 0)
            led_count <= led_count - 1'b1;
        if (pop) begin
            rp <= rp + 1'b1;
            words <= words + 1'b1;
            led_count <= LED_HOLD;
            if (t_placed && !t_found && rp == t_at) begin
                t_found <= 1'b1;
                t_word <= words[15:0];
            end
        end else if (ring_out != 2'd0)
            rp <= rp + ring_out;
        if (go || state == S_OFF)
            drain <= 0;
        else if (drain != DRAIN_MAX)
            drain <= drain + 1'b1;
        case (state)
        S_OFF: begin
            ctl_en <= 1'b0;
            dq_en <= 1'b0;
            pclk_en <= 1'b0;
            cnt <= 0;
            if (go) begin
                state <= S_START;
                c_format <= r_control[0];
                c_test <= r_control[1];
                c_shift <= r_control[7:4];
                c_div <= r_div;
                c_burst <= (r_burst == 16'd0) ? 16'd4096 : r_burst;
                c_state <= r_clock[0];
                c_rise <= r_clock[1];
                c_fall <= r_clock[2];
                c_clk <= r_clock[7:4];
                c_filt <= (nmax != 8'd0);
                c_nstage <= nstage_in;
                c_ring <= (r_ring > RING_MAX) ? RING_MAX : r_ring[FIFO_BITS:0];
                t_found <= 1'b0;
                t_word <= 16'd0;
                rp <= 0;
                words <= 0;
                done <= 1'b0;
                aborted <= 1'b0;
                starts <= starts + 1'b1;
            end
        end
        S_START: begin
            cnt <= cnt + 1'b1;
            ctl_en <= 1'b1;
            if (cnt == 8)
                dq_en <= 1'b1;
            if (cnt == 16)
                pclk_en <= 1'b1;
            if (!go) begin
                state <= S_STOP;
                cnt <= 0;
            end else if (cnt == START_WAIT - 1)
                state <= S_WAIT;
        end
        S_WAIT: begin
            if (finished) begin
                done <= 1'b1;
                state <= go ? S_DONE : S_STOP;
                cnt <= 0;
            end else if (gave_up) begin
                aborted <= 1'b1;
                state <= S_STOP;
                cnt <= 0;
            end else if (flaga) begin
                state <= S_BURST;
                left <= c_burst;
            end
        end
        S_BURST: begin
            if (pop) begin
                left <= left - 1'b1;
                if (left == 16'd1) begin
                    state <= S_HOLD;
                    cnt <= 0;
                end
            end else if (finished) begin
                done <= 1'b1;
                state <= go ? S_DONE : S_STOP;
                cnt <= 0;
            end else if (gave_up) begin
                aborted <= 1'b1;
                state <= S_STOP;
                cnt <= 0;
            end
        end
        S_HOLD: begin
            cnt <= cnt + 1'b1;
            if (cnt == HOLD - 1)
                state <= S_WAIT;
        end
        S_DONE: begin
            cnt <= 0;
            if (!go)
                state <= S_STOP;
        end
        S_STOP: begin
            cnt <= cnt + 1'b1;
            if (cnt == 16)
                pclk_en <= 1'b0;
            if (cnt == 24)
                dq_en <= 1'b0;
            if (cnt == 32) begin
                ctl_en <= 1'b0;
                state <= S_OFF;
            end
        end
        default:
            state <= S_OFF;
        endcase
    end

    // PCLK: low after the FPGA clock's rising edge, high after its falling edge when enabled
    wire pclk_q, pclk_t;
    ODDR2 #(.DDR_ALIGNMENT("NONE"), .INIT(1'b0), .SRTYPE("SYNC")) oddr_pclk (
        .Q(pclk_q), .C0(clk), .C1(~clk), .CE(1'b1), .D0(1'b0), .D1(pclk_en), .R(1'b0), .S(1'b0));
    // the pin's enable goes through a flip-flop of the same kind: a Spartan-6 output that an ODDR2 drives takes its
    // enable from an ODDR2 too (map's error LIT:675 otherwise).  1 releases the pin
    ODDR2 #(.DDR_ALIGNMENT("NONE"), .INIT(1'b1), .SRTYPE("SYNC")) oddr_pclk_t (
        .Q(pclk_t), .C0(clk), .C1(~clk), .CE(1'b1), .D0(~ctl_en), .D1(~ctl_en), .R(1'b0), .S(1'b0));

    assign fx3_pclk  = pclk_t ? 1'bz : pclk_q;
    assign fx3_ctl0  = ctl_en ? 1'b0 : 1'bz;        // SLCS#
    assign fx3_ctl1  = ctl_en ? slwr_n : 1'bz;      // SLWR#
    assign fx3_ctl2  = ctl_en ? 1'b1 : 1'bz;        // SLOE#
    assign fx3_ctl3  = ctl_en ? 1'b1 : 1'bz;        // SLRD#
    assign fx3_ctl7  = ctl_en ? 1'b1 : 1'bz;        // PKTEND#
    assign fx3_ctl11 = ctl_en ? 1'b0 : 1'bz;        // A1
    assign fx3_ctl12 = ctl_en ? 1'b0 : 1'bz;        // A0
    assign fx3_dq    = dq_en ? dq_r : 32'bz;

    // ---- outputs ----
    assign led_arm  = run ? waiting : hz;
    assign led_trig = (sampling && !waiting) || (led_count != 0);
    assign dbg      = {flaga, run, sampling, khz};
    assign trig_out = trig_out_r;

    // ---- what a read over I2C returns ----
    reg [7:0] sn_status = 0, sn_more = 0, sn_trig = 0;
    reg [15:0] sn_tword = 0;
    reg [15:0] sn_in = 0, sn_max = 0;
    reg [39:0] sn_samples = 0;
    reg [47:0] sn_words = 0;
    always @(posedge clk) begin
        if (i2c_rd_begin) begin
            sn_status <= {state, overflow, done, sampling, run, locked_s[1]};
            sn_more <= {6'd0, flaga, aborted};
            sn_trig <= {1'b0, met, ring, t_half, t_found, fired};
            sn_tword <= t_word;
            sn_in <= in_l[15:0];
            sn_max <= fifo_max;
            sn_samples <= samples;
            sn_words <= words;
        end
    end
    // (through a flip-flop: the address is steady for a quarter of an I2C bit before its register is wanted)
    reg [7:0] rd_mux;
    always @(posedge clk)
        i2c_rd_data <= rd_mux;
    always @(*) begin
        case (i2c_rd_addr)
        8'h00: rd_mux = 8'h4C;
        8'h01: rd_mux = 8'h42;
        8'h02: rd_mux = 8'h16;
        8'h03: rd_mux = VERSION;
        8'h04: rd_mux = r_control;
        8'h05: rd_mux = sn_status;
        8'h06: rd_mux = sn_in[7:0];
        8'h07: rd_mux = sn_in[15:8];
        8'h08: rd_mux = r_div[7:0];
        8'h09: rd_mux = r_div[15:8];
        8'h0A: rd_mux = r_div[23:16];
        8'h0C: rd_mux = r_duty_a[7:0];
        8'h0D: rd_mux = {4'd0, r_duty_a[11:8]};
        8'h0E: rd_mux = r_duty_b[7:0];
        8'h0F: rd_mux = {4'd0, r_duty_b[11:8]};
        8'h10: rd_mux = r_limit[7:0];
        8'h11: rd_mux = r_limit[15:8];
        8'h12: rd_mux = r_limit[23:16];
        8'h13: rd_mux = r_limit[31:24];
        8'h14: rd_mux = r_limit[39:32];
        8'h18: rd_mux = sn_samples[7:0];
        8'h19: rd_mux = sn_samples[15:8];
        8'h1A: rd_mux = sn_samples[23:16];
        8'h1B: rd_mux = sn_samples[31:24];
        8'h1C: rd_mux = sn_samples[39:32];
        8'h20: rd_mux = r_burst[7:0];
        8'h21: rd_mux = r_burst[15:8];
        8'h22: rd_mux = sn_max[7:0];
        8'h23: rd_mux = sn_max[15:8];
        8'h24: rd_mux = sn_words[7:0];
        8'h25: rd_mux = sn_words[15:8];
        8'h26: rd_mux = sn_words[23:16];
        8'h27: rd_mux = sn_words[31:24];
        8'h28: rd_mux = sn_words[39:32];
        8'h29: rd_mux = sn_words[47:40];
        8'h2A: rd_mux = sn_more;
        8'h2B: rd_mux = FIFO_BITS;
        8'h2C: rd_mux = SAMPLE_MHZ;
        8'h2D: rd_mux = STAGES;
        8'h30: rd_mux = r_clock;
        8'h31: rd_mux = {5'd0, r_nstage};
        8'h32: rd_mux = r_ring[7:0];
        8'h33: rd_mux = r_ring[15:8];
        8'h34: rd_mux = sn_trig;
        8'h36: rd_mux = sn_tword[7:0];
        8'h37: rd_mux = sn_tword[15:8];
        default: rd_mux = 8'd0;
        endcase
        if (i2c_rd_addr[7:2] == 6'b001110)
            rd_mux = r_tmode[{i2c_rd_addr[1:0], 3'd0} +: 8];
        if (i2c_rd_addr[7:6] == 2'b01)
            rd_mux = r_trig[{i2c_rd_addr[5:0], 3'd0} +: 8];
        if (i2c_rd_addr[7:4] == 4'h8)
            rd_mux = r_glitch[{i2c_rd_addr[3:0], 3'd0} +: 8];
    end

    // ---- JTAG USER1 ----
    wire jt_capture, jt_sel, jt_shift, jt_tck_pin, jt_tdi, tck;
    reg [N-1:0] sr = 0;
    BSCAN_SPARTAN6 #(.JTAG_CHAIN(1)) bscan (
        .CAPTURE(jt_capture), .DRCK(), .RESET(), .RUNTEST(), .SEL(jt_sel), .SHIFT(jt_shift), .TCK(jt_tck_pin),
        .TDI(jt_tdi), .TMS(), .UPDATE(), .TDO(sr[0]));
    BUFG bufg_tck (.I(jt_tck_pin), .O(tck));

    // the copy for JTAG: made in the 100 MHz clock a few cycles after a read begins, steady long before the next
    reg req = 0;
    reg [2:0] req_sync = 0;
    reg [3:0] copies = 0;
    reg [1:0] scl_s = 0, sda_s = 0;
    reg [N-1:33] snap = 0;
    wire [15:0] max16 = fifo_max;
    wire [N-1:33] live = {
        cycles[39:8],                                               // word 7
        others_s[1], 3'd0, r_duty_b, 4'd0, r_duty_a,                // word 6
        max16, r_control, samples[39:32],                           // word 5
        samples[31:0],                                              // word 4
        i2c_hits, words[47:32],                                     // word 3
        words[31:0],                                                // word 2
        in_l[15:0], copies, flaga, sda_s[1], scl_s[1], init_s[1], state, overflow, done, sampling, run};   // word 1
    always @(posedge clk) begin
        scl_s <= {scl_s[0], fx3_scl};
        sda_s <= {sda_s[0], fx3_sda};
        req_sync <= {req_sync[1:0], req};
        if (req_sync[2] ^ req_sync[1]) begin
            snap <= live;
            copies <= copies + 1'b1;
        end
    end
    always @(posedge tck) begin
        if (jt_sel && jt_capture) begin
            sr <= {snap, locked, MARKER};
            req <= ~req;
        end else if (jt_sel && jt_shift)
            sr <= {jt_tdi, sr[N-1:1]};
    end
endmodule

// A bit as it was (tap + 1) cycles ago, 1 ... 128: a shift register with one output that can be moved.  ISE makes
// four 32-bit shift registers of it, a slice in all.
module tap128 (
    input  wire clk,
    input  wire d,
    input  wire [6:0] tap,
    output wire q
);
    reg [127:0] sr = 128'd0;
    always @(posedge clk)
        sr <= {sr[126:0], d};
    assign q = sr[tap];
endmodule
