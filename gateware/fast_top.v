// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: CERN-OHL-S-2.0
//
// The fast FPGA design of the Logic Boole 16, board revision C: fewer inputs, more samples a second.  It is loaded
// in place of capture2_top.v when a capture asks for it, and does one thing: it takes samples and sends them.  It
// has no trigger, no glitch filter, no capture clock and no Raspberry Pi lines.  Built from this file,
// i2c_slave.v, pins.ucf and fast.ucf by "build.sh fast", and simulated by sim_fast.sh.  What the computer sees of
// its registers and of its stream is in ../docs/protocol.md.
//
// How it samples.  Each input pad has a 1:4 deserializer (ISERDES2).  A second PLL makes 750 MHz from the board's
// 100 MHz, a BUFPLL carries that to the pads of bank 3, where all 16 inputs are, and each pad takes its input at
// every rising edge of it: a sample every 1.33 ns.  The pads hand their samples to the rest of the design four at
// a time, in a clock of a quarter of that (187.5 MHz, "sclk").  Per cycle of sclk the design keeps 16 bits, a
// frame, and which 16 bits depends on the mode (register 08):
//   4 inputs     all four samples of four inputs: 750 million samples a second
//                bits 3:0 the earliest sample, 7:4 the next, 11:8, 15:12 the latest
//   8 inputs     every second sample of one pod's eight: 375 million a second
//                bits 7:0 the earlier sample, 15:8 the later
//   16 inputs    every fourth sample of all 16: 187.5 million a second.  Bits 7:0 pod A, 15:8 pod B
// Within a sample the inputs are in the order of their numbers, the lowest in the lowest bit; 1 = above the pod's
// threshold.  The four inputs are A0 ... A3, A4 ... A7, B0 ... B3 or B4 ... B7, the eight are pod A or pod B.
//
// The frames go through a FIFO of 8192 words to the FX3, 32 bits a word, in one of two formats:
//   raw          bits 15:0 a frame, bits 31:16 the next one.  If the capture ends on an odd frame the last word's
//                upper half repeats it.  The samples are then in the order of the stream's bits: a sample is the
//                next 4 bits, the next byte or the next two bytes
//   run-length   bits 15:0 a frame, bits 31:16 how many more frames were equal to it (0 ... 65535): a word when a
//                frame differs from the one before, and one every 65536 frames while none does
// The raw format makes 93.75 million words a second in every mode, which is close to what the board passes on
// (the FX3 takes at most one word in a cycle of the 100 MHz clock, and USB carries a little less).  The
// run-length format makes a word for each frame that holds a change, so twice as many when every frame does.
// Sampling ends when RUN falls, when the frame limit is reached, or when a word finds the FIFO full (the overflow).
// What is in the FIFO then still goes to the FX3, so the words that arrive are the capture from its first sample
// on with nothing missing in between.
//
// Two clocks.  Everything the FX3 sees (the registers, the FIFO's read side, the writer, PCLK) is in the 100 MHz
// clock "clk", as in capture2_top.v, and that part is taken from there unchanged.  The sampler, the words and the
// FIFO's write side are in sclk.  Between the two go: the FIFO's two pointers in Gray code, each through two
// flip-flops; single bits (the request to sample, "idle", "sampling", "overflow"), each through two flip-flops;
// the counts for the registers, copied every 64 cycles of sclk into a register that the other side takes once it
// has stood still; and the settings, which the FX3 writes while RUN is low and which do not change from 4096
// cycles before the first sample to the end of the capture.  fast.ucf tells the tools not to time those paths.
//
// The lines to the FX3 are those of capture2_top.v: PCLK out, the inverse of clk; DQ31 ... 0 out; CTL0 SLCS# low,
// CTL1 SLWR# low in each cycle that carries a word, CTL2 SLOE#, CTL3 SLRD# and CTL7 PKTEND# high, CTL11 / CTL12
// A1 / A0 low; CTL4 FLAGA in; CTL6 RUN in.  A burst is the words of one FX3 buffer (register 20); after its last
// word the flag is ignored for HOLD cycles, and the next burst starts when the flag is high.
//
// Registers, on the FX3's I2C bus at address 0x2A (i2c_slave.v).  Numbers of more than one byte have their low
// byte first.  The registers that capture2_top.v has too are at the same addresses.
//   00 ... 03  R   4C 42 46 and the design's version, 01.  (capture2_top.v has 4C 42 16: that tells the two apart)
//   04         RW  bit 0: the format, 0 raw, 1 run-length.  Bit 1: a test pattern in place of the inputs, a
//                  counter of the frames taken, of which bits 7:4 say how far it is shifted right (0: every
//                  frame differs from the one before; 8: every 256th)
//   05         R   status.  Bit 0 both PLLs have locked, 1 RUN, 2 sampling, 3 done (sampling has ended and every
//                  word has gone to the FX3), 4 overflow, 7:5 the writer's state (0 off, 1 starting, 2 waiting for
//                  the FX3's flag, 3 writing, 4 holding off after a buffer, 5 stopping, 6 done and waiting for RUN
//                  to fall)
//   06, 07     R   the inputs now: pod A, pod B
//   08         RW  the mode.  Bits 1:0: 0 16 inputs, 1 8 inputs, 2 4 inputs.  Bits 3:2: which of them (8 inputs:
//                  0 pod A, 1 pod B; 4 inputs: 0 A0 ... A3, 1 A4 ... A7, 2 B0 ... B3, 3 B4 ... B7).  Bit 4: the
//                  four samples of a cycle are taken in the opposite order.  0 is the order Xilinx's user guide
//                  gives (UG381: the last bit received is the top one, Q4); 1 is there to check that on a board
//   0C, 0D     RW  pod A's threshold: the PWM's duty cycle in 1/4096 (12 bits).  It changes when 0D is written
//   0E, 0F     RW  pod B's, likewise with 0F.  Both start at 2048: 0.311 V at TP34 / TP35, 1.56 V at the probe
//   10 ... 14  RW  the frame limit (40 bits); 0: none
//   18 ... 1C  R   frames taken in this capture (40 bits)
//   20, 21     RW  words in one FX3 buffer (its size in bytes / 4; 4096; 0 counts as 4096)
//   22, 23     R   the most words the FIFO has held in this capture
//   24 ... 29  R   words written to the FX3 in this capture (48 bits)
//   2A         R   bit 0: the rest was thrown away after RUN fell; bit 1 FLAGA
//   2B         R   13: the FIFO holds 2^13 words
//   2C, 2D     R   750: the samples a second with 4 inputs, in millions (16 bits)
//   2E, 2F     R   the pins this design does not use, as they are now: bits 8:0 the Pi's lines GPIO2, 3, 14, 15,
//                  10, 9, 11, 8, 7; bit 12 INIT_B, 13 CTL5, 14 CTL8, 15 TRIG IN
// The settings at 04, 08, 10 and 20 are taken as RUN rises; the thresholds at once (the filter behind the PWM
// needs 120 ms to settle).  Unused registers read 0.
//
// The other outputs: LED ARM blinks at 1 Hz and is off while RUN is high; LED TRIG is on while the design samples
// and while words go to the FX3; DBG0 1 kHz, DBG1 sampling, DBG2 RUN, DBG3 FLAGA; TRIG OUT is high while the
// design samples (it rises and falls a few cycles of the 100 MHz clock after the first and the last sample).
module fast_top #(
    parameter HALF_SECOND = 50000000,   // cycles of the 100 MHz clock; the test bench shortens these
    parameter HALF_MS = 50000,
    parameter START_WAIT = 4096,        // cycles from RUN to the request to sample (PCLK starts at the 17th)
    parameter HOLD = 20,                // cycles after a buffer's last word in which the flag is ignored
    parameter LED_HOLD = 1000000,       // cycles LED TRIG stays on after a word
    parameter FIFO_BITS = 13,           // the FIFO holds 2^FIFO_BITS words
    parameter DRAIN_MAX = 4000000,      // cycles after RUN fell in which the FX3 must take what is left
    parameter I2C_FILTER = 16,
    parameter S_MULT = 15,              // the sample clock: 100 MHz x S_MULT / S_DIV, 400 ... 950 MHz, and
    parameter S_DIV = 2,                // 100 MHz / S_DIV at least 19 MHz
    parameter [15:0] SAMPLE_MHZ = 16'd750
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
    localparam [7:0] VERSION = 8'd1;
    localparam [FIFO_BITS:0] DEPTH = 1 << FIFO_BITS;
    localparam [2:0] S_OFF = 3'd0, S_START = 3'd1, S_WAIT = 3'd2, S_BURST = 3'd3, S_HOLD = 3'd4, S_STOP = 3'd5,
                     S_DONE = 3'd6;

    // What the lines read while nothing drives them, as in capture2_top.v
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

    // ---- the 100 MHz clock: 24 MHz x 25 / 6 (the PLL's oscillator runs at 600 MHz), as in capture2_top.v ----
    wire clk24_pad, pll_fb, clk_pll, clk, locked1;
    IBUFG ibufg_clk24 (.I(clk24), .O(clk24_pad));
    PLL_BASE #(
        .BANDWIDTH("OPTIMIZED"), .CLK_FEEDBACK("CLKFBOUT"), .COMPENSATION("INTERNAL"), .CLKIN_PERIOD(41.667),
        .REF_JITTER(0.010), .DIVCLK_DIVIDE(1), .CLKFBOUT_MULT(25), .CLKFBOUT_PHASE(0.0), .CLKOUT0_DIVIDE(6),
        .CLKOUT0_PHASE(0.0), .CLKOUT0_DUTY_CYCLE(0.5)
    ) pll (
        .CLKIN(clk24_pad), .CLKFBIN(pll_fb), .CLKFBOUT(pll_fb), .RST(1'b0), .CLKOUT0(clk_pll), .CLKOUT1(),
        .CLKOUT2(), .CLKOUT3(), .CLKOUT4(), .CLKOUT5(), .LOCKED(locked1));
    BUFG bufg_clk (.I(clk_pll), .O(clk));

    // ---- the sample clock: 100 MHz x 15 / 2 = 750 MHz for the pads, and a quarter of it for the rest.  The
    // BUFPLL carries the fast clock to the pads of bank 3 and makes the strobe that marks every fourth of its
    // cycles, the one in which a pad hands over its four samples ----
    wire pll2_fb, pll2_fast, pll2_div, sclk, locked2, ioclk, strobe, io_lock;
    PLL_BASE #(
        .BANDWIDTH("OPTIMIZED"), .CLK_FEEDBACK("CLKFBOUT"), .COMPENSATION("INTERNAL"), .CLKIN_PERIOD(10.0),
        .REF_JITTER(0.010), .DIVCLK_DIVIDE(S_DIV), .CLKFBOUT_MULT(S_MULT), .CLKFBOUT_PHASE(0.0),
        .CLKOUT0_DIVIDE(1), .CLKOUT0_PHASE(0.0), .CLKOUT0_DUTY_CYCLE(0.5),
        .CLKOUT1_DIVIDE(4), .CLKOUT1_PHASE(0.0), .CLKOUT1_DUTY_CYCLE(0.5)
    ) pll2 (
        .CLKIN(clk), .CLKFBIN(pll2_fb), .CLKFBOUT(pll2_fb), .RST(~locked1), .CLKOUT0(pll2_fast),
        .CLKOUT1(pll2_div), .CLKOUT2(), .CLKOUT3(), .CLKOUT4(), .CLKOUT5(), .LOCKED(locked2));
    BUFG bufg_sclk (.I(pll2_div), .O(sclk));
    BUFPLL #(.DIVIDE(4), .ENABLE_SYNC("TRUE")) bufpll (
        .PLLIN(pll2_fast), .GCLK(sclk), .LOCKED(locked2), .IOCLK(ioclk), .LOCK(io_lock), .SERDESSTROBE(strobe));

    // ---- the registers ----
    reg [7:0] r_control = 8'd0;
    reg [4:0] r_mode = 5'd0;
    reg [11:0] r_duty_a = 12'd2048, r_duty_b = 12'd2048;
    reg [7:0] duty_a_low = 8'd0, duty_b_low = 8'd0;
    reg [39:0] r_limit = 40'd0;
    reg [15:0] r_burst = 16'd4096;

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
            8'h08: r_mode <= i2c_wr_data[4:0];
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
            default: ;
            endcase
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

    // ---- the lines from the FX3 and the pins this design does not use: two flip-flops each ----
    (* IOB = "TRUE" *) reg flaga_m = 0;
    (* IOB = "TRUE" *) reg run_m = 0;
    reg flaga = 0, run = 0;
    wire [12:0] spare = {trig_in, fx3_ctl8, fx3_ctl5, init_b, pif_gpio7, pif_gpio8, pif_gpio11, pif_gpio9,
                         pif_gpio10, pif_gpio15, pif_gpio14, pif_gpio3, pif_gpio2};
    reg [12:0] spare_m = 0, spare_s = 0;
    (* ASYNC_REG = "TRUE" *) reg [1:0] lock_s = 0;
    always @(posedge clk) begin
        flaga_m <= fx3_ctl4;
        run_m <= fx3_ctl6;
        flaga <= flaga_m;
        run <= run_m;
        spare_m <= spare;
        spare_s <= spare_m;
        lock_s <= {lock_s[0], locked1 & io_lock};
    end
    wire go = run & lock_s[1];

    // ---- the writer's state, the settings of the capture, and the request to the sampler ----
    reg [2:0] state = S_OFF;
    reg [15:0] cnt = 0;
    wire clear = (state == S_OFF) && go;                                    // a capture begins
    wire begin_sampling = (state == S_START) && (cnt == START_WAIT - 1) && go;
    reg c_format = 0, c_test = 0;           // these six are read by the sampler, in its own clock: they stand
    reg [3:0] c_shift = 0;                  // still from long before "req" rises until after "idle" has come back
    reg [4:0] c_mode = 0;
    reg [39:0] c_limit = 0;
    reg [15:0] c_burst = 16'd4096;
    reg req = 0;                            // sample: to the sampler
    reg started = 0;                        // the sampler has seen it
    wire idle_c;                            // from the sampler, in this clock: it does nothing and holds nothing
    reg [1:0] idle_d = 2'b11;               // two cycles later still, so that its last word's pointer is here first
    wire seen_busy = !idle_c && (idle_d == 2'b00);
    wire flushed = started && idle_d[1];    // nothing more will come from the sampler

    // =================================================================================================================
    // The sampler, in sclk
    // =================================================================================================================

    // ---- the pads.  q[4 * n + 3] is the latest of input n's four samples, q[4 * n] the earliest ----
    (* ASYNC_REG = "TRUE" *) reg [3:0] ser_rst = 4'hF;
    always @(posedge sclk)
        ser_rst <= {ser_rst[2:0], ~io_lock};
    wire [15:0] pad = {chb, cha};
    wire [63:0] q;
    generate
        for (g = 0; g < 16; g = g + 1) begin : in_ser
            ISERDES2 #(
                .DATA_WIDTH(4), .DATA_RATE("SDR"), .BITSLIP_ENABLE("FALSE"), .SERDES_MODE("NONE"),
                .INTERFACE_TYPE("RETIMED")
            ) ser (
                .D(pad[g]), .CE0(1'b1), .CLK0(ioclk), .CLK1(1'b0), .IOCE(strobe), .RST(ser_rst[3]), .CLKDIV(sclk),
                .SHIFTIN(1'b0), .BITSLIP(1'b0), .FABRICOUT(), .Q1(q[4 * g]), .Q2(q[4 * g + 1]), .Q3(q[4 * g + 2]),
                .Q4(q[4 * g + 3]), .DFB(), .CFB0(), .CFB1(), .VALID(), .INCDEC(), .SHIFTOUT());
        end
    endgenerate

    // ---- the request, in this clock ----
    (* ASYNC_REG = "TRUE" *) reg [1:0] req_m = 0;
    reg req_s = 0, req_d = 0;
    always @(posedge sclk) begin
        req_m <= {req_m[0], req};
        req_s <= req_m[1];
        req_d <= req_s;
    end
    wire start = req_s && !req_d;

    // ---- the samples in order.  d[4 * n + t]: input n's sample t of this cycle, 0 the earliest; 1 = above the
    // threshold (the comparators invert) ----
    reg [63:0] d = 64'd0;
    wire [63:0] q_turned;                                           // each input's four samples, the latest first
    generate
        for (g = 0; g < 16; g = g + 1) begin : turn
            assign q_turned[4 * g + 3:4 * g] = {q[4 * g], q[4 * g + 1], q[4 * g + 2], q[4 * g + 3]};
        end
    endgenerate
    always @(posedge sclk)
        d <= c_mode[4] ? ~q_turned : ~q;

    // ---- a frame.  four[k]: the four samples of inputs 4k ... 4k + 3.  eight[k]: samples 1 and 3 of pod k's
    // inputs.  all: sample 3 of all 16 ----
    wire [63:0] four;                                               // four[16 * k + 15:16 * k] is "four[k]"
    wire [31:0] eight;
    wire [15:0] all;
    generate
        for (g = 0; g < 4; g = g + 1) begin : pick4
            assign four[16 * g + 15:16 * g] = {d[16 * g + 15], d[16 * g + 11], d[16 * g + 7], d[16 * g + 3],
                              d[16 * g + 14], d[16 * g + 10], d[16 * g + 6], d[16 * g + 2],
                              d[16 * g + 13], d[16 * g + 9], d[16 * g + 5], d[16 * g + 1],
                              d[16 * g + 12], d[16 * g + 8], d[16 * g + 4], d[16 * g + 0]};
        end
        for (g = 0; g < 2; g = g + 1) begin : pick8
            assign eight[16 * g + 15:16 * g] = {d[32 * g + 31], d[32 * g + 27], d[32 * g + 23], d[32 * g + 19],
                               d[32 * g + 15], d[32 * g + 11], d[32 * g + 7], d[32 * g + 3],
                               d[32 * g + 29], d[32 * g + 25], d[32 * g + 21], d[32 * g + 17],
                               d[32 * g + 13], d[32 * g + 9], d[32 * g + 5], d[32 * g + 1]};
        end
        for (g = 0; g < 16; g = g + 1) begin : pick16
            assign all[g] = d[4 * g + 3];
        end
    endgenerate
    wire [15:0] four_now = (c_mode[3:2] == 2'd0) ? four[15:0] : (c_mode[3:2] == 2'd1) ? four[31:16] :
                           (c_mode[3:2] == 2'd2) ? four[47:32] : four[63:48];
    wire [15:0] eight_now = c_mode[2] ? eight[31:16] : eight[15:0];
    wire [15:0] probes = (c_mode[1:0] == 2'd2) ? four_now : (c_mode[1:0] == 2'd1) ? eight_now : all;

    // ---- the sampler: while "on", every cycle takes a frame.  It starts at the request, and ends when the
    // request falls, with the last frame of the limit, or when a word finds the FIFO full ----
    reg on = 0;
    reg limited = 0, last = 0;              // last: the frame taken in this cycle is the limit's last
    reg [39:0] left = 0, frames = 0;
    reg [30:0] tcount = 0;                  // the test pattern: a counter of the frames taken
    reg [15:0] frame = 0, frame_d = 0;      // the frame, and the one before it
    reg v1 = 0, v2 = 0;                     // frame holds a frame of the capture; frame_d does
    reg same = 0;                           // frame_d equals the frame before it
    reg overflow_s = 0;
    wire push_lost;                         // a word found the FIFO full (below)
    wire [30:0] tshift = tcount >> c_shift;
    always @(posedge sclk) begin
        if (start) begin
            on <= 1'b1;
            limited <= (c_limit != 40'd0);
            last <= (c_limit == 40'd1);
            left <= c_limit;
            frames <= 40'd0;
            tcount <= 31'd0;
        end else if (on) begin
            left <= left - 1'b1;
            last <= (left == 40'd2);
            frames <= frames + 1'b1;
            tcount <= tcount + 1'b1;
            if (!req_s || (limited && last) || push_lost || overflow_s)
                on <= 1'b0;
        end
        v1 <= on;
        frame <= c_test ? tshift[15:0] : probes;
        v2 <= v1;
        frame_d <= frame;
        same <= (frame == frame_d);
    end

    // ---- frames to words.  A word is made from frame_d, a cycle after the frame was taken ----
    reg half = 0, have = 0;
    reg [15:0] lo = 0, cur = 0, rep = 0;
    reg push = 0;
    reg [31:0] word = 0;
    wire rep_full = (rep == 16'hFFFF);      // the run holds 65536 frames
    always @(posedge sclk) begin
        push <= 1'b0;
        if (start) begin
            half <= 1'b0;
            have <= 1'b0;
        end else if (v2) begin
            if (!c_format) begin
                if (!half) begin
                    lo <= frame_d;
                    half <= 1'b1;
                end else begin
                    word <= {frame_d, lo};
                    push <= 1'b1;
                    half <= 1'b0;
                end
            end else if (!have) begin
                cur <= frame_d;
                rep <= 16'd0;
                have <= 1'b1;
            end else if (same && !rep_full)
                rep <= rep + 1'b1;
            else begin
                word <= {rep, cur};
                push <= 1'b1;
                cur <= frame_d;
                rep <= 16'd0;
            end
        end else if (!on && !v1) begin                              // the end: what is left goes out
            if (half) begin
                word <= {lo, lo};
                push <= 1'b1;
                half <= 1'b0;
            end else if (have) begin
                word <= {rep, cur};
                push <= 1'b1;
                have <= 1'b0;
            end
        end
    end

    // ---- the FIFO's write side.  The pointers count on from capture to capture: the FIFO is empty when they are
    // equal.  The read pointer comes over in Gray code and is made a number again, two cycles late, so "room"
    // is taken with 8 words to spare: nothing more goes in after the first word that found none ----
    (* ram_style = "block" *) reg [31:0] mem [0:(1 << FIFO_BITS) - 1];
    reg [FIFO_BITS:0] wbin = 0, wgray = 0;
    wire [FIFO_BITS:0] wnext = wbin + 1'b1;
    reg [FIFO_BITS:0] rgray = 0;                                    // the read pointer, in clk (below)
    (* ASYNC_REG = "TRUE" *) reg [FIFO_BITS:0] rg_m = 0, rg_s = 0;
    reg [FIFO_BITS:0] rbin_w = 0, level_w = 0, fifo_max_s = 0;
    reg room = 1'b1;
    wire [FIFO_BITS:0] rbin_of_gray;
    generate
        for (g = 0; g <= FIFO_BITS; g = g + 1) begin : g2b_w
            assign rbin_of_gray[g] = ^(rg_s >> g);
        end
    endgenerate
    wire we = push && !overflow_s && room;
    assign push_lost = push && !overflow_s && !room;
    always @(posedge sclk) begin
        if (we) begin
            mem[wbin[FIFO_BITS-1:0]] <= word;
            wbin <= wnext;
            wgray <= wnext ^ (wnext >> 1);
        end
        rg_m <= rgray;
        rg_s <= rg_m;
        rbin_w <= rbin_of_gray;
        level_w <= wbin - rbin_w;
        room <= (level_w < DEPTH - 8);
        if (start) begin
            overflow_s <= 1'b0;
            fifo_max_s <= 0;
        end else begin
            if (push_lost)
                overflow_s <= 1'b1;
            if (level_w > fifo_max_s)
                fifo_max_s <= level_w;
        end
    end

    // ---- idle: the sampler does nothing and holds nothing, and has not for 16 cycles (so that the other clock
    // sees it go low at every capture, however short) ----
    reg [4:0] quiet = 0;
    reg idle = 1'b1;
    wire busy = start || on || v1 || v2 || half || have || push;
    always @(posedge sclk) begin
        if (busy)
            quiet <= 5'd16;
        else if (quiet != 5'd0)
            quiet <= quiet - 1'b1;
        idle <= !busy && (quiet == 5'd0);
    end

    // ---- the counts for the registers: copied every 64 cycles, and "tick" changes when the copy is made ----
    reg [5:0] tdiv = 0;
    reg tick = 0;
    reg [39:0] hold_frames = 0;
    reg [FIFO_BITS:0] hold_max = 0;
    reg [15:0] hold_in = 0;
    always @(posedge sclk) begin
        tdiv <= tdiv + 1'b1;
        if (tdiv == 6'd0) begin
            hold_frames <= frames;
            hold_max <= fifo_max_s;
            hold_in <= all;
            tick <= ~tick;
        end
    end

    // =================================================================================================================
    // The FX3's side, in clk
    // =================================================================================================================

    // ---- from the sampler ----
    (* ASYNC_REG = "TRUE" *) reg [1:0] idle_m = 2'b11, on_m = 0, ovf_m = 0;
    (* ASYNC_REG = "TRUE" *) reg [2:0] tick_m = 0;
    reg [39:0] x_frames = 0;
    reg [FIFO_BITS:0] x_max = 0;
    reg [15:0] x_in = 0;
    assign idle_c = idle_m[1];
    wire sampling = on_m[1];
    wire overflow = ovf_m[1] && started;        // (before the sampler has begun, these are the last capture's)
    always @(posedge clk) begin
        idle_m <= {idle_m[0], idle};
        idle_d <= {idle_d[0], idle_c};
        on_m <= {on_m[0], on};
        ovf_m <= {ovf_m[0], overflow_s};
        tick_m <= {tick_m[1:0], tick};
        if (tick_m[2] ^ tick_m[1])              // the copy has stood still for two cycles and stays for 300 ns more
            x_in <= hold_in;
        if (clear) begin
            x_frames <= 40'd0;
            x_max <= 0;
        end else if ((tick_m[2] ^ tick_m[1]) && started) begin
            x_frames <= hold_frames;
            x_max <= hold_max;
        end
    end

    // ---- the FIFO's read side.  When a capture begins the read pointer jumps to the write pointer: what an
    // earlier capture left (the FX3 did not take it) is gone ----
    reg [FIFO_BITS:0] rbin = 0;
    (* ASYNC_REG = "TRUE" *) reg [FIFO_BITS:0] wg_m = 0, wg_s = 0;
    reg empty = 1'b1;
    wire pop = (state == S_BURST) && !empty;
    wire [FIFO_BITS:0] wbin_of_gray;
    generate
        for (g = 0; g <= FIFO_BITS; g = g + 1) begin : g2b_r
            assign wbin_of_gray[g] = ^(wg_s >> g);
        end
    endgenerate
    reg [FIFO_BITS:0] wbin_c = 0;                                   // the write pointer as a number, a cycle late
    wire [FIFO_BITS:0] rnext = clear ? wbin_c : rbin + pop;
    wire [FIFO_BITS:0] rgnext = rnext ^ (rnext >> 1);
    reg [31:0] mem_q = 0, mem_q2 = 0;
    always @(posedge clk) begin
        wg_m <= wgray;
        wg_s <= wg_m;
        wbin_c <= wbin_of_gray;
        rbin <= rnext;
        rgray <= rgnext;
        empty <= (rgnext == wg_s);
        mem_q <= mem[rbin[FIFO_BITS-1:0]];
        mem_q2 <= mem_q;
    end

    // ---- the writer, as in capture2_top.v ----
    reg ctl_en = 0, dq_en = 0, pclk_en = 0;
    (* IOB = "TRUE" *) reg slwr_n = 1'b1;
    (* IOB = "TRUE" *) reg [31:0] dq_r = 32'd0;
    reg pop_d1 = 0, pop_d2 = 0;
    reg [15:0] left_b = 0;
    reg [47:0] words = 0;
    reg [22:0] drain = 0;
    reg done = 0, aborted = 0;
    reg [19:0] led_count = 0;
    // nothing more will come from the sampler, and nothing is left on the way to the pads
    wire finished = flushed && empty && !pop_d1 && !pop_d2;
    wire gave_up = !go && (drain == DRAIN_MAX);

    always @(posedge clk) begin
        pop_d1 <= pop;
        pop_d2 <= pop_d1;
        slwr_n <= ~pop_d2;
        if (pop_d2)
            dq_r <= mem_q2;
        if (led_count != 0)
            led_count <= led_count - 1'b1;
        if (pop) begin
            words <= words + 1'b1;
            led_count <= LED_HOLD;
        end
        if (go || state == S_OFF)
            drain <= 0;
        else if (drain != DRAIN_MAX)
            drain <= drain + 1'b1;
        // the request: it rises once, 4096 cycles into the capture, and falls when the sampler has ended by itself
        // or when RUN has fallen, but not before the sampler has seen it
        if (clear)
            started <= 1'b0;
        if (state == S_OFF || state == S_STOP)
            req <= 1'b0;
        else if (begin_sampling)
            req <= 1'b1;
        else if (req && seen_busy) begin
            started <= 1'b1;
            if (!go)
                req <= 1'b0;
        end else if (started && (flushed || !go))
            req <= 1'b0;
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
                c_mode <= r_mode;
                c_limit <= r_limit;
                c_burst <= (r_burst == 16'd0) ? 16'd4096 : r_burst;
                words <= 0;
                done <= 1'b0;
                aborted <= 1'b0;
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
                left_b <= c_burst;
            end
        end
        S_BURST: begin
            if (pop) begin
                left_b <= left_b - 1'b1;
                if (left_b == 16'd1) begin
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
    reg trig_out_r = 0;
    always @(posedge clk)
        trig_out_r <= sampling;
    assign led_arm  = run ? 1'b0 : hz;
    assign led_trig = sampling || (led_count != 0);
    assign dbg      = {flaga, run, sampling, khz};
    assign trig_out = trig_out_r;

    // ---- what a read over I2C returns: the status and the counts are copied as the read is addressed ----
    reg [7:0] sn_status = 0, sn_more = 0;
    reg [15:0] sn_in = 0, sn_max = 0, sn_spare = 0;
    reg [39:0] sn_frames = 0;
    reg [47:0] sn_words = 0;
    always @(posedge clk) begin
        if (i2c_rd_begin) begin
            sn_status <= {state, overflow, done, sampling, run, lock_s[1]};
            sn_more <= {6'd0, flaga, aborted};
            sn_in <= x_in;
            sn_max <= x_max;
            sn_spare <= {spare_s[12:9], 3'd0, spare_s[8:0]};
            sn_frames <= x_frames;
            sn_words <= words;
        end
    end
    reg [7:0] rd_mux;
    always @(posedge clk)
        i2c_rd_data <= rd_mux;
    always @(*) begin
        case (i2c_rd_addr)
        8'h00: rd_mux = 8'h4C;
        8'h01: rd_mux = 8'h42;
        8'h02: rd_mux = 8'h46;
        8'h03: rd_mux = VERSION;
        8'h04: rd_mux = r_control;
        8'h05: rd_mux = sn_status;
        8'h06: rd_mux = sn_in[7:0];
        8'h07: rd_mux = sn_in[15:8];
        8'h08: rd_mux = {3'd0, r_mode};
        8'h0C: rd_mux = r_duty_a[7:0];
        8'h0D: rd_mux = {4'd0, r_duty_a[11:8]};
        8'h0E: rd_mux = r_duty_b[7:0];
        8'h0F: rd_mux = {4'd0, r_duty_b[11:8]};
        8'h10: rd_mux = r_limit[7:0];
        8'h11: rd_mux = r_limit[15:8];
        8'h12: rd_mux = r_limit[23:16];
        8'h13: rd_mux = r_limit[31:24];
        8'h14: rd_mux = r_limit[39:32];
        8'h18: rd_mux = sn_frames[7:0];
        8'h19: rd_mux = sn_frames[15:8];
        8'h1A: rd_mux = sn_frames[23:16];
        8'h1B: rd_mux = sn_frames[31:24];
        8'h1C: rd_mux = sn_frames[39:32];
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
        8'h2C: rd_mux = SAMPLE_MHZ[7:0];
        8'h2D: rd_mux = SAMPLE_MHZ[15:8];
        8'h2E: rd_mux = sn_spare[7:0];
        8'h2F: rd_mux = sn_spare[15:8];
        default: rd_mux = 8'd0;
        endcase
    end
endmodule
