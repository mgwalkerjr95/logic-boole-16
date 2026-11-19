// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: CERN-OHL-S-2.0
//
// Test bench for fast_top.v: run it with "sh sim_fast.sh", which tries several values of the parameters below.  It
// checks itself and ends with PASS or FAIL.
//
// The FX3 and the I2C master are those of capture2_tb.v: one thread of the FX3's synchronous slave FIFO, which
// takes a word on each rising edge of PCLK with SLCS# and SLWR# low into a buffer of buf_words; FLAGA shows "a
// buffer is there" LAT cycles late and FLAG_DELAY ns after the edge; a word written while no buffer is there is an
// overrun.  It checks what the FX3's datasheet asks of the lines: 2 ns of setup and 0.5 ns of hold around PCLK's
// rising edge, PCLK high and low for 4 ns or more.
//
// What a capture must be is worked out here from the pins alone.  The test bench writes down the 16 pins at every
// rising edge of the pads' fast clock, the edges at which the pads take them.  The words that arrive are made into
// frames and the frames into samples, and those must be the pins written down, one after the other with none left
// out, from a place near where the design was asked to sample: every edge with 4 inputs, every second one with 8,
// every fourth with 16.  The pins change at the fast clock's falling edges, so what a pad takes is never in doubt.
`timescale 1ns / 1ps
module fast_tb;
    parameter LAT = 3;                  // cycles from the last word of a buffer to FLAGA falling
    parameter real FLAG_DELAY = 7.0;    // ns from PCLK's edge to the flag's pin
    parameter HOLD = 20;                // the design's parameter
    parameter START_WAIT = 300;
    parameter QUICK = 0;                // 1: the first captures only
    localparam FIFO_BITS = 8;           // 256 words: small, so that the captures here fill it
    localparam DRAIN_MAX = 3000;
    localparam [6:0] ADDR = 7'h2A;
    integer TQ = 250;                   // ns: a quarter of a bit at 1 MHz

    reg clk24 = 0;
    always #20.833 clk24 = ~clk24;

    reg run = 0;                        // the FX3's GPIO
    reg flaga_pin = 0;
    wire pclk;
    wire [31:0] dq;
    wire slcs_n, slwr_n, sloe_n, slrd_n, pktend_n, a1, a0, ctl5, ctl8;
    wire init_b;
    reg [15:0] pins = 16'hFFFF;         // the comparators' outputs, pod B's above pod A's: low = above the threshold
    reg trig_in = 0;
    reg [8:0] pif = 9'd0;               // the Pi's nine lines: GPIO2, 3, 14, 15, 10, 9, 11, 8, 7
    wire pwm_a, pwm_b, trig_out, led_arm, led_trig;
    wire [3:0] dbg;
    reg scl = 1, m_sda = 1;             // the master's side of the I2C bus
    wire sda;
    pullup (sda);
    assign sda = m_sda ? 1'bz : 1'b0;

    fast_top #(.HALF_SECOND(24000), .HALF_MS(12), .START_WAIT(START_WAIT), .HOLD(HOLD), .LED_HOLD(50),
               .FIFO_BITS(FIFO_BITS), .DRAIN_MAX(DRAIN_MAX)) dut (
        .clk24(clk24), .fx3_pclk(pclk), .cha(pins[7:0]), .chb(pins[15:8]), .trig_in(trig_in),
        .pwm_a(pwm_a), .pwm_b(pwm_b), .trig_out(trig_out), .led_arm(led_arm), .led_trig(led_trig), .dbg(dbg),
        .fx3_ctl0(slcs_n), .fx3_ctl1(slwr_n), .fx3_ctl2(sloe_n), .fx3_ctl3(slrd_n), .fx3_ctl4(flaga_pin),
        .fx3_ctl5(ctl5), .fx3_ctl6(run), .fx3_ctl7(pktend_n), .fx3_ctl8(ctl8), .fx3_ctl11(a1),
        .fx3_ctl12(a0), .fx3_dq(dq), .fx3_scl(scl), .fx3_sda(sda), .init_b(init_b),
        .pif_gpio2(pif[0]), .pif_gpio3(pif[1]), .pif_gpio14(pif[2]), .pif_gpio15(pif[3]), .pif_gpio10(pif[4]),
        .pif_gpio9(pif[5]), .pif_gpio11(pif[6]), .pif_gpio8(pif[7]), .pif_gpio7(pif[8]));

    integer errors = 0;
    task check(input ok, input [639:0] what);
        begin
            if (!ok) begin
                errors = errors + 1;
                if (errors <= 20)
                    $display("FAIL at %0t ns: %0s", $time, what);
            end
        end
    endtask

    // ---- the FX3's side ----
    integer buf_words = 4096, switch_time = 40, fill = 0, switch_left = 0;
    integer overruns = 0, edges = 0, rx_n = 0;
    reg ready = 0, host_reads = 1;
    reg [15:0] pipe = 0;
    reg [31:0] rx [0:(1 << 19) - 1];
    realtime t_change = 0, t_rise = -100, t_fall = -100;

    always @(posedge pclk) begin
        check($realtime - t_fall >= 4.0 || edges == 0, "PCLK low for less than 4 ns");
        check($realtime - t_change >= 2.0, "DQ or SLWR# changed less than 2 ns before PCLK rose");
        t_rise = $realtime;
        edges = edges + 1;
        if (slcs_n === 1'b0 && slwr_n === 1'b0) begin
            check(sloe_n === 1'b1 && slrd_n === 1'b1 && pktend_n === 1'b1, "SLOE#, SLRD# or PKTEND# not high in a write");
            check(a1 === 1'b0 && a0 === 1'b0, "the address is not 0 in a write");
            check(^dq !== 1'bx, "a word with unknown bits");
            if (!ready)
                overruns = overruns + 1;
            else begin
                rx[rx_n] = dq;
                rx_n = rx_n + 1;
                fill = fill + 1;
                if (fill == buf_words) begin
                    fill = 0;
                    ready = 0;
                    switch_left = switch_time;
                end
            end
        end else
            check(slwr_n !== 1'bx && slcs_n !== 1'bx, "SLCS# or SLWR# unknown at PCLK's rising edge");
        if (!ready && host_reads) begin
            if (switch_left <= 1)
                ready = 1;
            else
                switch_left = switch_left - 1;
        end
        pipe <= {pipe[14:0], ready};
        flaga_pin <= #(FLAG_DELAY) (LAT == 0) ? ready : pipe[(LAT == 0) ? 0 : LAT - 1];
    end
    always @(negedge pclk) begin
        check($realtime - t_rise >= 4.0, "PCLK high for less than 4 ns");
        t_fall = $realtime;
    end
    always @(dq or slwr_n) begin
        t_change = $realtime;
        check($realtime - t_rise >= 0.5, "DQ or SLWR# changed less than 0.5 ns after PCLK rose");
    end

    // ---- the pins as the pads take them: at every rising edge of the fast clock.  1 = above the threshold ----
    reg [15:0] slog [0:(1 << 22) - 1];
    integer sl_n = 0, sl_req = 0;       // sl_req: how many had been written down when the design was asked to sample
    reg logging = 0;
    always @(posedge dut.ioclk) begin
        if (logging) begin
            slog[sl_n] = ~pins;
            sl_n = sl_n + 1;
        end
    end
    always @(posedge dut.req)
        sl_req = sl_n;

    // the pins change at the fast clock's falling edges: at one edge in "gap", one of those in stim_mask
    integer seed = 7, gap = 2, b;
    reg stim = 0;
    reg [15:0] stim_mask = 16'hFFFF;
    always @(negedge dut.ioclk) begin
        if (stim && ({$random(seed)} % gap) == 0) begin
            b = {$random(seed)} % 16;
            if (stim_mask[b])
                pins[b] = ~pins[b];
        end
    end

    integer out_rises = 0, out_falls = 0;
    always @(posedge trig_out) out_rises = out_rises + 1;
    always @(negedge trig_out) out_falls = out_falls + 1;

    // ---- the words as frames ----
    reg [15:0] fr [0:(1 << 21) - 1];
    integer fr_n = 0;
    // format 0: two frames a word.  1: a frame and how many more like it; a run ends only where the next frame
    // differs or at 65536 frames
    task frames_of(input integer format, input [639:0] what);
        integer i, k;
        reg [31:0] w;
        reg bad;
        begin
            fr_n = 0;
            bad = 0;
            for (i = 0; i < rx_n; i = i + 1) begin
                w = rx[i];
                if (format == 0) begin
                    fr[fr_n] = w[15:0];
                    fr[fr_n + 1] = w[31:16];
                    fr_n = fr_n + 2;
                end else begin
                    if (i > 0 && rx[i - 1][15:0] === w[15:0] && rx[i - 1][31:16] !== 16'hFFFF)
                        bad = 1;
                    for (k = 0; k <= w[31:16]; k = k + 1) begin
                        fr[fr_n] = w[15:0];
                        fr_n = fr_n + 1;
                    end
                end
            end
            check(!bad, what);
        end
    endtask

    // The frames against the pins written down.  mode: register 08.  Looks for the place the capture begins at;
    // first_at says how many edges of the fast clock after the request that is.
    integer first_at = 0;
    task compare(input [4:0] mode, input [639:0] what);
        integer o, f, t, per, qi, found, at;
        reg [15:0] got, want, mask;
        reg ok;
        begin
            per = (mode[1:0] == 2'd2) ? 4 : (mode[1:0] == 2'd1) ? 2 : 1;
            found = 0;
            at = 0;
            // (from a little before the request: the pads' samples take four cycles to become a frame)
            for (o = (sl_req > 40) ? sl_req - 40 : 0; o < sl_req + 400 && !found; o = o + 1) begin
                ok = 1;
                for (f = 0; f < fr_n && ok; f = f + 1)
                    for (t = 0; t < per && ok; t = t + 1) begin
                        // which of the frame's four edges sample t is, and where its inputs are
                        if (per == 4) begin
                            qi = mode[4] ? 3 - t : t;
                            got = (fr[f] >> (4 * t)) & 16'h000F;
                            want = (slog[o + 4 * f + qi] >> (4 * mode[3:2])) & 16'h000F;
                        end else if (per == 2) begin
                            qi = mode[4] ? 2 - 2 * t : 1 + 2 * t;
                            got = (fr[f] >> (8 * t)) & 16'h00FF;
                            want = (slog[o + 4 * f + qi] >> (8 * mode[2])) & 16'h00FF;
                        end else begin
                            qi = mode[4] ? 0 : 3;
                            got = fr[f];
                            want = slog[o + 4 * f + qi];
                        end
                        if (o + 4 * f + qi >= sl_n || got !== want)
                            ok = 0;
                    end
                if (ok) begin
                    found = 1;
                    at = o;
                end
            end
            first_at = at - sl_req;
            if (!found)
                $display("FAIL at %0t ns: %0s: %0d frames are not the pins from any place after the request", $time,
                         what, fr_n);
            check(found, what);
        end
    endtask

    // every line to the FX3 is released: only the pull-up or pull-down holds it ("Pu"; a driven line shows "St")
    reg [23:0] sv;
    integer k;
    task strength(input [23:0] want, input [639:0] when_);
        begin
            check(sv == want, when_);
        end
    endtask
    task released(input [639:0] when_);
        begin
            $sformat(sv, "%v", pclk);     strength("Pu0", when_);
            $sformat(sv, "%v", slcs_n);   strength("Pu1", when_);
            $sformat(sv, "%v", slwr_n);   strength("Pu1", when_);
            $sformat(sv, "%v", sloe_n);   strength("Pu1", when_);
            $sformat(sv, "%v", slrd_n);   strength("Pu1", when_);
            $sformat(sv, "%v", pktend_n); strength("Pu1", when_);
            $sformat(sv, "%v", a1);       strength("Pu0", when_);
            $sformat(sv, "%v", a0);       strength("Pu0", when_);
            $sformat(sv, "%v", ctl5);     strength("Pu0", when_);
            $sformat(sv, "%v", ctl8);     strength("Pu0", when_);
            for (k = 0; k < 32; k = k + 1) begin
                $sformat(sv, "%v", dq[k]);
                strength("Pu0", when_);
            end
        end
    endtask

    // ---- the I2C master ----
    reg ack;
    reg [63:0] rd;
    task i2c_start;                     // also a repeated start: SDA is let go while SCL is low
        begin
            m_sda = 1;
            #TQ scl = 1;
            #TQ m_sda = 0;
            #TQ scl = 0;
            #TQ;
        end
    endtask
    task i2c_stop;
        begin
            m_sda = 0;
            #TQ scl = 1;
            #TQ m_sda = 1;
            #(2 * TQ);
        end
    endtask
    task i2c_out(input [7:0] d);        // ack: the slave answered
        integer i;
        begin
            for (i = 7; i >= 0; i = i - 1) begin
                m_sda = d[i];
                #TQ scl = 1;
                #(2 * TQ) scl = 0;
                #TQ;
            end
            m_sda = 1;
            #TQ scl = 1;
            #TQ ack = (sda === 1'b0);
            #TQ scl = 0;
            #TQ;
        end
    endtask
    task i2c_in(output [7:0] d, input more);
        integer i;
        begin
            m_sda = 1;
            for (i = 7; i >= 0; i = i - 1) begin
                #TQ scl = 1;
                #TQ d[i] = sda;
                #TQ scl = 0;
                #TQ;
            end
            m_sda = ~more;
            #TQ scl = 1;
            #(2 * TQ) scl = 0;
            #TQ m_sda = 1;
        end
    endtask
    task reg_write(input [7:0] a, input [63:0] v, input integer n);
        integer i;
        begin
            i2c_start;
            i2c_out({ADDR, 1'b0});
            check(ack, "no answer to the address in a write");
            i2c_out(a);
            check(ack, "no answer to the register's number");
            for (i = 0; i < n; i = i + 1) begin
                i2c_out(v[8 * i +: 8]);
                check(ack, "no answer to a byte written");
            end
            i2c_stop;
        end
    endtask
    task reg_read(input [7:0] a, input integer n);      // into rd, the first byte lowest
        integer i;
        reg [7:0] b8;
        begin
            rd = 0;
            i2c_start;
            i2c_out({ADDR, 1'b0});
            check(ack, "no answer to the address before a read");
            i2c_out(a);
            i2c_start;
            i2c_out({ADDR, 1'b1});
            check(ack, "no answer to the address in a read");
            for (i = 0; i < n; i = i + 1) begin
                i2c_in(b8, i != n - 1);
                rd[8 * i +: 8] = b8;
            end
            i2c_stop;
        end
    endtask

    // ---- a capture ----
    // what the firmware does before one: the settings, a fresh buffer in the FX3
    task setup(input [7:0] control, input [4:0] mode, input [39:0] limit, input [15:0] burst);
        begin
            reg_write(8'h04, control, 1);
            reg_write(8'h08, mode, 1);
            reg_write(8'h10, limit, 5);
            reg_write(8'h20, burst, 2);
            buf_words = burst;
            fill = 0;
            ready = 1;
            switch_left = 0;
            rx_n = 0;
            sl_n = 0;
            sl_req = 0;
            host_reads = 1;
        end
    endtask
    task wait_done(input integer limit_us);             // as the firmware does: the status over I2C
        integer t;
        begin
            t = 0;
            rd = 0;
            while (!rd[3] && t < limit_us) begin
                #5000 t = t + 5;
                reg_read(8'h05, 1);
                t = t + 40;
            end
            check(rd[3], "the capture was not done in time");
        end
    endtask
    task wait_off(input integer limit_us);
        integer t;
        begin
            t = 0;
            while (dut.state != 3'd0 && t < limit_us) begin
                #1000 t = t + 1;
            end
            check(dut.state == 3'd0, "the design did not stop after RUN fell");
            #500;
        end
    endtask

    // the counts the design gives after a capture
    reg [39:0] n_frames;
    reg [47:0] n_words;
    reg [7:0] status;
    task counts;
        begin
            #1000;                                      // the frames' count comes over every 64 cycles of sclk
            reg_read(8'h05, 1);
            status = rd[7:0];
            reg_read(8'h18, 5);
            n_frames = rd[39:0];
            reg_read(8'h24, 6);
            n_words = rd[47:0];
        end
    endtask

    // A capture of that many frames of the pins, compared with them.  each: one pin changes at one falling edge
    // of the fast clock in that many
    task counted(input [7:0] control, input [4:0] mode, input integer n, input integer each,
                 input [639:0] what);
        begin
            setup(control, mode, n, 512);                // small buffers: each of these captures fills several,
                                                        // so the flag's delay and the hold-off are in all of them
            gap = each;
            stim = 1;
            logging = 1;
            run = 1;
            wait_done(20000);
            stim = 0;
            logging = 0;
            counts;
            check(status[4:0] == 5'b01011, "the status after a capture that ended at its limit");
            check(n_frames == n, "the frames taken are not the limit");
            check(n_words == rx_n, "the words the design counted are not the words that arrived");
            frames_of(control[0], what);
            if (!control[0] && (n & 1)) begin           // an odd number of frames: the last word repeats the last
                check(fr_n == n + 1 && fr[fr_n - 1] === fr[fr_n - 2], "an odd capture's last word");
                fr_n = fr_n - 1;
            end
            check(fr_n == n, "the words do not hold as many frames as were taken");
            compare(mode, what);
            check(first_at > -40 && first_at < 100, "the first sample is not from where the request came");
            run = 0;
            wait_off(200);
            released("after a capture");
        end
    endtask

    // A capture of the test pattern: frame t is (t >> shift) & 0xFFFF
    task pattern(input [7:0] control, input integer n, input [639:0] what);
        integer i, shift;
        reg bad;
        begin
            shift = control[7:4];
            setup(control, 5'd2, n, 4096);
            run = 1;
            wait_done(20000);
            counts;
            check(n_frames == n && n_words == rx_n, "the counts of a test-pattern capture");
            frames_of(control[0], what);
            if (!control[0] && (n & 1))
                fr_n = fr_n - 1;
            bad = (fr_n != n);
            for (i = 0; i < fr_n && !bad; i = i + 1)
                if (fr[i] !== ((i >> shift) & 16'hFFFF)) begin
                    bad = 1;
                    $display("FAIL at %0t ns: %0s: frame %0d is %h", $time, what, i, fr[i]);
                end
            check(!bad, what);
            if (control[0] && shift != 0)               // a word for each value of the counter
                check(rx_n == (n + (1 << shift) - 1) >> shift, "the words of a run-length test pattern");
            run = 0;
            wait_off(200);
        end
    endtask

    integer i, n_before;
    initial begin
        #4000;
        released("before the first capture");
        reg_read(8'h00, 4);
        check(rd[31:0] == 32'h0146424C, "the design's name and version");
        reg_read(8'h05, 1);
        check(rd[7:0] == 8'h01, "the status before the first capture");
        reg_read(8'h2B, 3);
        check(rd[7:0] == FIFO_BITS && rd[23:8] == 16'd750, "the FIFO's size and the sample rate");
        reg_write(8'h0C, 16'h0123, 2);
        reg_write(8'h0E, 16'h0FED, 2);
        reg_read(8'h0C, 4);
        check(rd[31:0] == 32'h0FED0123 && dut.r_duty_a == 12'h123 && dut.r_duty_b == 12'hFED, "the thresholds");

        // 4 inputs, 8 inputs, 16: raw with busy pins (the limit odd, then even), run-length with quiet ones
        counted(8'h00, 5'b00010, 3001, 2, "raw, A0 ... A3");
        $display("the first sample: %0d edges of the fast clock after the request", first_at);
        counted(8'h01, 5'b00010, 20000, 24, "run-length, A0 ... A3");
        if (!QUICK) begin
            TQ = 625;
            counted(8'h00, 5'b00110, 3000, 2, "raw, A4 ... A7");
            TQ = 250;
            counted(8'h00, 5'b01010, 2999, 2, "raw, B0 ... B3");
            counted(8'h00, 5'b01110, 3000, 2, "raw, B4 ... B7");
            counted(8'h01, 5'b01110, 20000, 24, "run-length, B4 ... B7");
            counted(8'h00, 5'b00001, 3001, 2, "raw, pod A");
            counted(8'h00, 5'b00101, 3000, 2, "raw, pod B");
            counted(8'h01, 5'b00001, 20000, 40, "run-length, pod A");
            counted(8'h01, 5'b00101, 20000, 40, "run-length, pod B");
            counted(8'h00, 5'b00000, 3001, 2, "raw, 16 inputs");
            counted(8'h01, 5'b00000, 20000, 60, "run-length, 16 inputs");
            // the four samples of a cycle in the opposite order
            counted(8'h00, 5'b10010, 3000, 2, "raw, A0 ... A3, the order turned round");
            counted(8'h00, 5'b10101, 3000, 2, "raw, pod B, the order turned round");
            counted(8'h00, 5'b10000, 3000, 2, "raw, 16 inputs, the order turned round");
            // the shortest captures
            for (i = 1; i <= 5; i = i + 1) begin
                counted(8'h00, 5'b00010, i, 2, "raw, a few frames");
                counted(8'h01, 5'b00110, i, 3, "run-length, a few frames");
            end
            check(out_rises == out_falls && out_rises >= 20, "TRIG OUT is high while the design samples");

            // the test pattern
            pattern(8'h02, 5001, "the test pattern, raw");
            pattern(8'h03, 300, "the test pattern, run-length, every frame another");
            pattern(8'h33, 20001, "the test pattern, run-length, shifted by 3");
            pattern(8'h82, 3000, "the test pattern, raw, shifted by 8");
            pattern(8'h83, 3000, "the test pattern, run-length, shifted by 8");

            // a run of more than 65536 frames: two words
            setup(8'h01, 5'b00010, 70000, 4096);
            pins = 16'hFFF5;
            #100 run = 1;
            wait_done(20000);
            counts;
            check(n_frames == 70000 && rx_n == 2 && rx[0] === 32'hFFFFAAAA && rx[1] === 32'h116FAAAA,
                  "a run of 70000 frames");
            run = 0;
            wait_off(200);
            // the inputs now, and the pins the design does not use
            pins = 16'h5AC3;
            pif = 9'h155;
            trig_in = 1;
            #2000;
            reg_read(8'h06, 2);
            check(rd[15:0] == 16'hA53C, "the inputs now");
            reg_read(8'h2E, 2);
            check(rd[15:0] == 16'h9155, "the pins the design does not use");
            pins = 16'hFFFF;
            pif = 9'd0;
            trig_in = 0;

            // RUN falls in a capture with no limit: what was taken arrives, and nothing else
            setup(8'h00, 5'b00010, 0, 4096);
            gap = 2;
            stim = 1;
            logging = 1;
            run = 1;
            #(10 * START_WAIT + 40000);
            run = 0;
            wait_off(400);
            stim = 0;
            logging = 0;
            counts;
            check(status[4:0] == 5'b01001, "the status after RUN fell in a capture");
            check(n_words == rx_n && n_frames > 5000, "the counts after RUN fell in a capture");
            frames_of(0, "raw, ended by RUN");
            if (n_frames & 1)
                fr_n = fr_n - 1;
            check(fr_n == n_frames, "the frames after RUN fell in a capture");
            compare(5'b00010, "raw, ended by RUN");
            released("after RUN fell in a capture");

            // the same in the run-length format, where the last run is still being counted
            setup(8'h01, 5'b00101, 0, 4096);
            gap = 40;
            stim = 1;
            logging = 1;
            run = 1;
            #(10 * START_WAIT + 60000);
            run = 0;
            wait_off(400);
            stim = 0;
            logging = 0;
            counts;
            frames_of(1, "run-length, ended by RUN");
            check(n_words == rx_n && fr_n == n_frames, "the frames after RUN fell in a run-length capture");
            compare(5'b00101, "run-length, ended by RUN");

            // RUN falls around the request to sample, at every place in 40 cycles of the 100 MHz clock and a
            // little further each time: whatever arrives is whole, and the design ends
            for (i = -8; i < 72; i = i + 1) begin
                setup(8'h00, 5'b00010, 0, 4096);
                gap = 2;
                stim = 1;
                logging = 1;
                run = 1;
                #(10 * START_WAIT + 5 * i + 3.7);
                run = 0;
                wait_off(400);
                stim = 0;
                logging = 0;
                counts;
                frames_of(0, "raw, RUN falls around the request");
                if (n_frames & 1)
                    fr_n = fr_n - 1;
                check(n_words == rx_n && fr_n == n_frames && !status[4],
                      "the counts when RUN falls around the request");
                if (fr_n != 0)
                    compare(5'b00010, "raw, RUN falls around the request");
            end
            counted(8'h00, 5'b00010, 3001, 2, "raw, A0 ... A3, after those");

            // the computer does not read: the FIFO fills, the design says overflow and stops sampling, and what
            // arrives when the computer reads again is whole up to its last frame
            setup(8'h00, 5'b00001, 0, 1024);
            gap = 2;
            stim = 1;
            logging = 1;
            host_reads = 0;
            run = 1;
            #(10 * START_WAIT + 60000);
            reg_read(8'h05, 1);
            check(rd[4] && !rd[2] && !rd[3], "overflow, and no more sampling");
            stim = 0;
            logging = 0;
            host_reads = 1;
            wait_done(20000);
            counts;
            check(rx_n >= 1024 + (1 << FIFO_BITS) - 8 && rx_n <= 1024 + (1 << FIFO_BITS),
                  "an overflowed capture has the buffer's words and the FIFO's");
            frames_of(0, "raw, overflow");
            check(n_words == rx_n && fr_n < n_frames && fr_n + 10 > n_frames, "the frames of an overflowed capture");
            compare(5'b00001, "raw, overflow");
            reg_read(8'h22, 2);
            check(rd[15:0] >= (1 << FIFO_BITS) - 8 && rd[15:0] <= (1 << FIFO_BITS), "the most words the FIFO held");
            run = 0;
            wait_off(200);

            // the same, and the computer never reads: after RUN has fallen the design throws the rest away, and
            // the next capture has nothing of it
            setup(8'h01, 5'b00010, 0, 1024);
            gap = 3;
            stim = 1;
            host_reads = 0;
            run = 1;
            #(10 * START_WAIT + 30000);
            run = 0;
            n_before = rx_n;
            wait_off(DRAIN_MAX / 50);
            stim = 0;
            reg_read(8'h2A, 1);
            check(rd[0] && rx_n == n_before, "what the FX3 did not take is thrown away");
            released("after giving up");
            counted(8'h00, 5'b00010, 3001, 2, "raw, A0 ... A3, after a capture that was thrown away");
            counted(8'h01, 5'b00001, 20000, 40, "run-length, pod A, after those");
        end

        $display("overruns %0d, other failures %0d", overruns, errors);
        if (errors == 0 && overruns == 0)
            $display("PASS");
        else
            $display("FAIL");
        $finish;
    end
endmodule
