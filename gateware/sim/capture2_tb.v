// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: CERN-OHL-S-2.0
//
// Test bench for capture2_top.v: run it with "sh sim.sh", which tries several values of the parameters below.  It
// checks itself and ends with PASS or FAIL.
//
// The FX3 here is one thread of its synchronous slave FIFO: it takes a word on each rising edge of PCLK with SLCS#
// and SLWR# low, into a buffer of buf_words; a full buffer is replaced after switch_time cycles if the "host"
// reads; FLAGA shows "a buffer is there" LAT cycles late and FLAG_DELAY ns after the edge; a word written while no
// buffer is there is an overrun.  It keeps every word.  It checks what the FX3's datasheet asks of the lines: 2 ns
// of setup and 0.5 ns of hold around PCLK's rising edge, PCLK high and low for 4 ns or more.  An I2C master at
// 400 kHz writes and reads the registers.  The samples that are part of a capture are taken from inside the design
// (a_n, a_x, a_y) and compared with what the words say, sample by sample.  That the samples are the probes every
// 5 ns, in order, is checked with a count on the probes that goes up every 5 ns between the clock's edges ("ramp").
// The trigger is tried on the test pattern, where the trigger sample is known beforehand, and on the probes; the
// glitch filter is compared with a second description of it here, sample by sample.
`timescale 1ns / 1ps
module capture2_tb;
    parameter LAT = 3;                  // cycles from the last word of a buffer to FLAGA falling
    parameter real FLAG_DELAY = 7.0;    // ns from PCLK's edge to the flag's pin
    parameter HOLD = 20;                // the design's parameter
    parameter START_WAIT = 300;
    parameter QUICK = 0;                // 1: the first two captures only
    parameter ONLY = 0;                 // 1: only the captures before group 12; 12 ... 21: only that group
    localparam FIFO_BITS = 8;           // 256 words: small, so that the captures here fill it
    localparam DRAIN_MAX = 3000;
    localparam [6:0] ADDR = 7'h2A;
    integer TQ = 625;                   // ns: a quarter of a bit at 400 kHz; the later captures use 1 MHz

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
    // the Pi's nine lines in the order of register 2E's bits: GPIO2, 3, 14, 15, 10, 9, 11, 8, 7.  1 = high
    reg [8:0] pif = 9'd0;
    reg [8:0] pi_sel = 9'd0;            // which of them the test has put in place of probes (register 2E)
    wire pwm_a, pwm_b, trig_out, led_arm, led_trig;
    wire [3:0] dbg;
    reg scl = 1, m_sda = 1;             // the master's side of the I2C bus
    wire sda;
    pullup (sda);
    assign sda = m_sda ? 1'bz : 1'b0;

    capture2_top #(.HALF_SECOND(24000), .HALF_MS(12), .START_WAIT(START_WAIT), .HOLD(HOLD), .LED_HOLD(50),
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

    // ---- the samples of the capture (before a trigger: every sample taken, of which the FIFO keeps the newest) ----
    reg [15:0] ex [0:(1 << 21) - 1];
    integer ex_n = 0, two_words = 0;    // two_words: cycles in which the design made two words
    integer ex_trig = -1;               // which of them is the trigger sample
    integer ex_base = 0;                // which of them the first word begins with
    always @(posedge dut.clk) begin
        if (dut.a_n != 2'd0) begin
            if (dut.a_tx)
                ex_trig = ex_n;
            ex[ex_n] = dut.a_x;
            ex_n = ex_n + 1;
        end
        if (dut.a_n == 2'd2) begin
            if (dut.a_ty)
                ex_trig = ex_n;
            ex[ex_n] = dut.a_y;
            ex_n = ex_n + 1;
        end
        if (dut.pushn == 2'd2)
            two_words = two_words + 1;
    end
    realtime t_out = 0;                 // when TRIG OUT last rose
    always @(posedge trig_out)
        t_out = $realtime;

    // The words against the samples.  delivered: how many samples the words hold.
    integer delivered = 0;
    task compare(input integer format, input [639:0] what);
        integer i, j, k, r;
        reg [31:0] w;
        reg bad;
        begin
            j = ex_base;
            bad = 0;
            for (i = 0; i < rx_n && !bad; i = i + 1) begin
                w = rx[i];
                if (format == 0) begin
                    if (j >= ex_n || w[15:0] !== ex[j])
                        bad = 1;
                    j = j + 1;
                    if (j < ex_n) begin
                        if (w[31:16] !== ex[j])
                            bad = 1;
                        j = j + 1;
                    end else if (i != rx_n - 1 || w[31:16] !== w[15:0])     // the odd sample at the end, repeated
                        bad = 1;
                end else begin
                    r = w[31:16];
                    for (k = 0; k <= r && !bad; k = k + 1) begin
                        if (j >= ex_n || w[15:0] !== ex[j])
                            bad = 1;
                        j = j + 1;
                    end
                    // a run ends only where the sample changes, at 65536 samples, or before the trigger sample
                    if (!bad && r != 65535 && j < ex_n && ex[j] === w[15:0] && j != ex_trig)
                        bad = 1;
                end
                if (bad)
                    $display("FAIL at %0t ns: %0s: word %0d (%h) against sample %0d", $time, what, i, w, j);
            end
            check(!bad, what);
            delivered = j - ex_base;
        end
    endtask


    // with the ramp on the probes: each sample is the one before plus "step"
    task ramp_check(input integer step, input [639:0] what);
        integer i;
        reg [15:0] want;
        reg bad;
        begin
            bad = 0;
            for (i = 1; i < ex_n && !bad; i = i + 1) begin
                want = ex[i - 1] + step;
                if (ex[i] !== want) begin
                    bad = 1;
                    $display("FAIL at %0t ns: %0s: sample %0d is %0d after %0d", $time, what, i, ex[i], ex[i - 1]);
                end
            end
            check(!bad && ex_n > 1, what);
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
    task driven(input [639:0] when_);
        begin
            $sformat(sv, "%v", slcs_n);   strength("St0", when_);
            $sformat(sv, "%v", sloe_n);   strength("St1", when_);
            $sformat(sv, "%v", slrd_n);   strength("St1", when_);
            $sformat(sv, "%v", pktend_n); strength("St1", when_);
            $sformat(sv, "%v", a1);       strength("St0", when_);
            $sformat(sv, "%v", a0);       strength("St0", when_);
            for (k = 0; k < 32; k = k + 1) begin
                $sformat(sv, "%v", dq[k]);
                check(sv == "St0" || sv == "St1", when_);
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
        reg [7:0] b;
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
                i2c_in(b, i != n - 1);
                rd[8 * i +: 8] = b;
            end
            i2c_stop;
        end
    endtask

    // ---- the inputs: one of the 16 changes, at times that have nothing to do with the clock ----
    integer seed = 7, gap = 300, b;
    reg stim = 0;
    reg [15:0] stim_mask = 16'hFFFF;    // which of them
    always begin
        #(1 + ({$random(seed)} % (2 * gap)));
        if (stim) begin
            b = {$random(seed)} % 16;
            if (stim_mask[b])
                pins[b] = ~pins[b];
        end
    end

    // a circuit with a clock: A0 is its clock, and B0 ... B7 count up 2 ns after each of the clock's rising edges
    reg sclk_on = 0;
    real sclk_half = 10.3;
    reg [7:0] sdata = 0;
    always begin
        #(sclk_half - 2.0);
        if (sclk_on)
            pins[0] = ~pins[0];
        #2.0;
        if (sclk_on && pins[0] == 1'b0) begin           // the comparator's output fell: the input rose
            sdata = sdata + 1'b1;
            pins[15:8] = ~sdata;
        end
    end

    // an input above its threshold for w samples of 5 ns, from 2.5 ns after the next edge of the clock
    task pulse(input integer ch, input integer w);
        begin
            @(dut.clk);
            #2.5 pins[ch] = 1'b0;
            repeat (w) @(dut.clk);
            #2.5 pins[ch] = 1'b1;
        end
    endtask

    // what the glitch filter gets and what it gives, every 5 ns, for the comparison with the description below
    reg [16:0] xlog [0:(1 << 17) - 1];
    reg [15:0] glog [0:(1 << 17) - 1];
    integer xl_n = 0;
    reg logging = 0;
    always @(posedge dut.clk) begin
        if (logging && xl_n < (1 << 17) - 2) begin
            xlog[xl_n] = dut.in_e;
            xlog[xl_n + 1] = dut.in_l;
            glog[xl_n] = dut.g_e;
            glog[xl_n + 1] = dut.g_l;
            xl_n = xl_n + 2;
        end
    end

    // the ramp: the probes show a count that goes up 2.5 ns after each edge of the design's clock, so every 5 ns.
    // Where a Pi line stands in for a probe (pi_sel), the line has the count's bit and the probe has the wrong
    // level; every other Pi line has the wrong level too.  So the count is whole only if the design takes each
    // bit from where register 2E says.
    reg ramp = 0;
    reg [15:0] ramp_count = 0;
    always @(dut.clk) begin
        if (ramp) begin
            #2.5 ramp_count = ramp_count + 1'b1;
            pins = ~ramp_count ^ {7'd0, pi_sel};
            pif = ramp_count[8:0] ^ ~pi_sel;
        end
    end

    // what the firmware does before a capture: the settings, a fresh buffer in the FX3
    task setup(input [7:0] control, input [23:0] divider, input [39:0] limit, input [15:0] burst);
        begin
            reg_write(8'h04, control, 1);
            reg_write(8'h08, divider, 3);
            reg_write(8'h10, limit, 5);
            reg_write(8'h20, burst, 2);
            buf_words = burst;
            fill = 0;
            ready = 1;
            switch_left = 0;
            rx_n = 0;
            ex_n = 0;
            ex_trig = -1;
            ex_base = 0;
        end
    endtask
    task wait_rx(input integer n, input integer limit_us);
        integer t;
        begin
            t = 0;
            while (rx_n < n && t < limit_us) begin
                #1000 t = t + 1;
            end
            check(rx_n >= n, "the words did not arrive in time");
        end
    endtask
    task wait_done(input integer limit_us);             // as the firmware does: the status over I2C
        integer t;
        begin
            t = 0;
            rd = 0;
            while (!rd[3] && t < limit_us) begin
                #20000 t = t + 20;
                reg_read(8'h05, 1);
                t = t + 100;
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

    // ---- the trigger's registers ----
    task stage(input integer k, input [7:0] mode, input [15:0] mask, input [15:0] value, input [15:0] rise,
               input [15:0] fall, input [31:0] tmin, input [31:0] tmax);
        begin
            reg_write(8'h38 + k, mode, 1);
            reg_write(8'h40 + 16 * k, {fall, rise, value, mask}, 8);
            reg_write(8'h48 + 16 * k, {tmax, tmin}, 8);
        end
    endtask
    task trigger(input integer stages, input integer keep);
        begin
            reg_write(8'h31, stages, 1);
            reg_write(8'h32, keep, 2);
        end
    endtask
    // After a capture with a trigger: the trigger's status (t_stat) and the trigger word's number (t_num); then the
    // words against the samples, the first word being as many samples before the trigger sample as the words
    // before the trigger word hold (pre)
    reg [7:0] t_stat;
    integer t_num, pre;
    task compare_trig(input integer format, input [639:0] what);
        integer i;
        begin
            reg_read(8'h34, 4);
            t_stat = rd[7:0];
            t_num = rd[31:16];
            check(t_stat[0] && t_stat[1] && !t_stat[3], "a capture with a trigger: not triggered, or the trigger word not found");
            check(ex_trig >= 0, "a capture with a trigger: no trigger sample inside the design");
            pre = 0;
            for (i = 0; i < t_num && i < rx_n; i = i + 1)
                pre = pre + (format == 0 ? 2 : rx[i][31:16] + 1);
            if (format == 0 && t_stat[2])
                pre = pre + 1;
            check(format == 0 || !t_stat[2], "run-length: the trigger sample is not the first of its word");
            ex_base = ex_trig - pre;
            check(ex_base >= 0, "more samples before the trigger word than were taken");
            if (ex_base < 0)
                ex_base = 0;
            compare(format, what);
        end
    endtask
    // the test pattern with a trigger on the sample that equals "value": keep words are kept before it, and
    // "limit" samples follow from it.  "each": the pattern goes up every that many samples (the control's shift)
    task pattern_trigger(input [7:0] control, input [23:0] divider, input integer value, input integer keep,
                         input integer limit, input integer each);
        integer want_pre, want_words;
        begin
            stage(0, 8'd0, 16'hFFFF, value, 16'd0, 16'd0, 32'd0, 32'd0);
            trigger(1, keep);
            setup(control, divider, limit, 16'd4096);
            run = 1;
            wait_done(3000);
            compare_trig(control[0], "the test pattern with a trigger");
            check(ex[ex_trig] === value && (ex_trig == 0 || ex[ex_trig - 1] !== value),
                  "the test pattern with a trigger: the trigger sample is not the first with the value");
            check(ex_trig == value * each, "the test pattern with a trigger: the trigger sample's number");
            check(ex_n - ex_trig == limit, "the test pattern with a trigger: the samples from the trigger on are not the limit");
            check(delivered == ex_n - ex_base, "the test pattern with a trigger: not every sample from the first word on arrived");
            reg_read(8'h18, 5);
            check(rd[39:0] == limit, "the test pattern with a trigger: the count of samples from the trigger on");
            // how many words the FIFO kept: all there were, or as many as asked for
            if (control[0])
                want_words = (ex_trig + each - 1) / each;
            else
                want_words = ex_trig / 2;
            if (want_words > keep)
                want_words = keep;
            check(t_num == want_words, "the test pattern with a trigger: the number of words kept before the trigger word");
            reg_read(8'h05, 1);
            check(rd[7:0] == 8'hCB, "the test pattern with a trigger: done without an overflow");
            run = 0;
            wait_off(100);
        end
    endtask

    // a capture with the stages as they are written: the trigger sample must be number "want" of those taken
    task trig_capture(input [7:0] control, input [23:0] divider, input integer stages, input integer keep,
                      input integer limit, input integer want, input [639:0] what);
        begin
            trigger(stages, keep);
            setup(control, divider, limit, 16'd4096);
            run = 1;
            wait_done(3000);
            compare_trig(control[0], what);
            if (ex_trig != want)
                $display("    %0s: the trigger sample is number %0d, not %0d", what, ex_trig, want);
            check(ex_trig == want, what);
            check(ex_n - ex_trig == limit && delivered == ex_n - ex_base, what);
            run = 0;
            wait_off(100);
        end
    endtask

    // Pulses of 3, 7 and 12 samples on A5, the first of them "phase" samples later, and a trigger on a pulse's
    // length.  Kind 1: the trigger sample must be the first after a pulse of want samples.  Kind 2: the want-th
    // sample of a pulse.  want 0: no trigger
    task pulse_capture(input [7:0] mode, input [31:0] tmin, input [31:0] tmax, input integer phase,
                       input integer want, input [639:0] what);
        integer k;
        reg good;
        begin
            stage(0, mode, 16'h0020, 16'h0020, 16'd0, 16'd0, tmin, tmax);
            trigger(1, 200);
            setup(8'h01, 24'd0, 40'd100, 16'd4096);
            run = 1;
            #((START_WAIT + 40) * 10);
            repeat (100 + phase) @(dut.clk);
            pulse(5, 3);
            repeat (9) @(dut.clk);
            pulse(5, 7);
            repeat (9) @(dut.clk);
            pulse(5, 12);
            if (want == 0) begin
                #3000;
                reg_read(8'h34, 1);
                check(rd[7:0] == 8'h08, what);
                check(rx_n == 0, what);
                run = 0;
                wait_off(1000);
            end else begin
                wait_done(2000);
                compare_trig(1, what);
                good = (ex_trig > want + 1);
                if (good && mode[1:0] == 2'd1) begin
                    if (ex[ex_trig][5] !== 1'b0 || ex[ex_trig - want - 1][5] !== 1'b0)
                        good = 0;
                    for (k = 1; k <= want; k = k + 1)
                        if (ex[ex_trig - k][5] !== 1'b1)
                            good = 0;
                end else if (good) begin
                    for (k = 0; k < want; k = k + 1)
                        if (ex[ex_trig - k][5] !== 1'b1)
                            good = 0;
                    if (ex[ex_trig - want][5] !== 1'b0)
                        good = 0;
                end
                check(good, what);
                run = 0;
                wait_off(100);
            end
        end
    endtask

    // a state capture of n samples of the circuit with a clock.  "clock" is register 30
    task state_capture(input [7:0] clock, input [7:0] control, input integer n, input [639:0] what);
        integer k;
        reg good;
        reg [7:0] step;
        begin
            reg_write(8'h30, clock, 1);
            setup(control, 24'd5, n, 16'd4096);         // the divider does nothing in a state capture
            run = 1;
            wait_done(8000);
            compare(control[0], what);
            check(delivered == n && ex_n == n, what);
            good = 1;
            for (k = 1; k < n; k = k + 1) begin
                step = ex[k][15:8] - ex[k - 1][15:8];
                if (ex[k][7:1] !== 7'd0)
                    good = 0;
                // before a rising edge the clock is low; the count went up after the rising edge before
                if (clock[2:1] == 2'b01 && (ex[k][0] !== 1'b0 || step !== 8'd1))
                    good = 0;
                if (clock[2:1] == 2'b10 && (ex[k][0] !== 1'b1 || step !== 8'd1))
                    good = 0;
                if (clock[2:1] == 2'b11 && (ex[k][0] === ex[k - 1][0] || step !== {7'd0, ex[k][0]}))
                    good = 0;
                if (!good && errors < 20) begin
                    $display("    %0s: sample %0d is %h after %h", what, k, ex[k], ex[k - 1]);
                    k = n;
                end
            end
            check(good, what);
            reg_read(8'h18, 5);
            check(rd[39:0] == n, what);
            run = 0;
            wait_off(100);
        end
    endtask

    // The glitch filter, described a second time: an input's level changes once n samples in a row show the new
    // one, and every input is as late as the one with the largest n.  Compared with what the design made of the
    // inputs it logged, which appears 6 samples later
    reg [7:0] gl_n [0:15];
    task filters(input [127:0] v);
        integer c;
        begin
            reg_write(8'h80, v[63:0], 8);
            reg_write(8'h88, v[127:64], 8);
            for (c = 0; c < 16; c = c + 1)
                gl_n[c] = v[8 * c +: 8];
            #2000;
        end
    endtask
    task filter_check(input [639:0] what);
        integer c, q, n, w, cnt, bad_at, largest;
        reg y, xv, want;
        begin
            bad_at = -1;
            largest = 0;
            for (c = 0; c < 16; c = c + 1)
                if (gl_n[c] > largest)
                    largest = gl_n[c];
            check(dut.c_filt == (largest != 0), what);
            for (c = 0; c < 16; c = c + 1) begin
                n = gl_n[c];
                w = largest - n;
                y = xlog[0][c];
                cnt = 0;
                for (q = 0; q + 6 < xl_n; q = q + 1) begin
                    xv = (q >= w) ? xlog[q - w][c] : xlog[0][c];
                    want = (n == 0) ? xv : y;
                    if (q >= 600 && glog[q + 6][c] !== want && bad_at < 0) begin
                        bad_at = q + 6;
                        $display("    %0s: input %0d at logged sample %0d is %b", what, c, bad_at, glog[q + 6][c]);
                    end
                    if (xv == y)
                        cnt = 0;
                    else if (cnt + 1 >= n) begin
                        y = xv;
                        cnt = 0;
                    end else
                        cnt = cnt + 1;
                end
            end
            check(bad_at < 0 && xl_n > 2000, what);
        end
    endtask
    // in the samples of the capture, input c never stays at a level for fewer than n samples
    task min_runs(input integer c, input integer n, input [639:0] what);
        integer k, len, shortest, changes;
        begin
            len = 0;
            shortest = 1 << 30;
            changes = 0;
            for (k = 1; k < ex_n; k = k + 1) begin
                len = len + 1;
                if (ex[k][c] !== ex[k - 1][c]) begin
                    if (changes > 0 && len < shortest)
                        shortest = len;
                    changes = changes + 1;
                    len = 0;
                end
            end
            if (shortest < n)
                $display("    %0s: input %0d stays for %0d samples only", what, c, shortest);
            check(shortest >= n && changes > 20, what);
        end
    endtask

    integer i, e0, n_high_a, n_high_b, cnt_ev;
    realtime t_ev;
    reg [39:0] s0;
    reg ok;

    // a capture of n samples with the ramp on the probes, raw: every sample "divider + 1" counts after the one before
    task ramp_capture(input [23:0] divider, input integer n);
        begin
            setup(8'h00, divider, n, 16'd4096);
            run = 1;
            wait_done(3000);
            check(rx_n == (n + 1) / 2, "the ramp: the number of words");
            compare(0, "the ramp, raw");
            check(delivered == n && ex_n == n, "the ramp: every sample is in the words");
            ramp_check(divider + 1, "the ramp: the samples are not the probes at every (divider + 1) x 5 ns");
            run = 0;
            wait_off(100);
        end
    endtask

    // a capture of n samples of the test pattern at 200 MS/s; the pattern goes up every "each" samples
    task short_capture(input [7:0] control, input integer n, input integer each);
        integer j;
        reg good;
        begin
            reg_write(8'h04, control, 1);
            reg_write(8'h10, n, 5);
            rx_n = 0;
            ex_n = 0;
            run = 1;
            wait_done(1000);
            compare(control[0], "a short capture");
            check(delivered == n && ex_n == n, "a short capture: every sample is in the words");
            good = 1;
            for (j = 0; j < n; j = j + 1)
                if (ex[j] !== j / each)
                    good = 0;
            check(good, "a short capture: the samples are not the test pattern");
            if (control[0])
                check(rx_n == (n + each - 1) / each, "a short capture, run-length: a word for each value");
            else
                check(rx_n == (n + 1) / 2, "a short capture, raw: a word for two samples");
            reg_read(8'h18, 5);
            check(rd[39:0] == n, "a short capture: the count of samples");
            run = 0;
            wait_off(100);
        end
    endtask

    initial begin
        // ---- off: nothing is driven, no clock ----
        #5000;
        check(dut.locked === 1'b1, "the PLL has not locked");
        released("a line to the FX3 is driven while RUN is low");
        check(init_b === 1'b1, "INIT_B is not pulled up");
        check(edges == 0, "PCLK ran before RUN");

        // ---- the registers ----
        reg_read(8'h00, 4);
        check(rd[31:0] == 32'h0516424C, "the identity is not 4C 42 16 05");
        reg_read(8'h0C, 4);
        check(rd[31:0] == {16'd2048, 16'd2048}, "the thresholds do not start at 2048");
        reg_read(8'h20, 2);
        check(rd[15:0] == 16'd4096, "the buffer size does not start at 4096");
        reg_read(8'h2B, 2);
        check(rd[7:0] == FIFO_BITS, "the FIFO's size");
        check(rd[15:8] == 8'd200, "the fastest sample rate is not given as 200");
        reg_read(8'h2D, 1);
        check(rd[7:0] == 8'd4, "the number of trigger stages is not given as 4");
        // the trigger's and the filter's registers read back, and nothing next to them changes
        reg_write(8'h30, 64'hA5, 1);
        reg_write(8'h31, 64'h03, 1);
        reg_write(8'h32, 64'h0123, 2);
        reg_write(8'h38, 64'h0B0A0908, 4);
        reg_write(8'h40, 64'h0807060504030201, 8);
        reg_write(8'h48, 64'h100F0E0D0C0B0A09, 8);
        reg_write(8'h78, 64'h8877665544332211, 8);
        reg_write(8'h80, 64'h0000000000FA0005, 8);
        reg_write(8'h8F, 64'h07, 1);
        reg_read(8'h30, 8);
        check(rd[31:0] == 32'h0123_03_A5 && rd[63:32] == 32'd0, "registers 30 ... 33 read back, 34 ... 37 at rest");
        reg_read(8'h38, 8);
        check(rd[31:0] == 32'h0B0A0908, "the stages' modes read back");
        reg_read(8'h40, 8);
        check(rd == 64'h0807060504030201, "stage 0, first half, read back");
        reg_read(8'h48, 8);
        check(rd == 64'h100F0E0D0C0B0A09, "stage 0, second half, read back");
        reg_read(8'h50, 8);
        check(rd == 64'd0, "stage 1 changed with stage 0");
        reg_read(8'h78, 8);
        check(rd == 64'h8877665544332211, "stage 3, second half, read back");
        reg_read(8'h80, 8);
        check(rd == 64'h0000000000FA0005, "the glitch filters of pod A read back");
        reg_read(8'h88, 8);
        check(rd == 64'h0700000000000000, "the glitch filters of pod B read back");
        #2000;
        check(dut.nmax == 8'hFA && dut.gwait[7:0] == 8'hF5 && dut.gwait[23:16] == 8'h00 && dut.gwait[127:120] == 8'hF3,
              "the filters' waits are not the largest N less each input's own");
        reg_write(8'h30, 64'd0, 4);
        reg_write(8'h38, 64'd0, 4);
        reg_write(8'h40, 64'd0, 8);
        reg_write(8'h48, 64'd0, 8);
        reg_write(8'h78, 64'd0, 8);
        reg_write(8'h80, 64'd0, 8);
        reg_write(8'h88, 64'd0, 8);
        reg_write(8'h04, 64'h12, 1);
        reg_write(8'h08, 64'h030201, 3);
        reg_write(8'h0C, {16'd3000, 16'd1000}, 4);
        reg_write(8'h10, 64'h0504030201, 5);
        reg_write(8'h20, 64'd1000, 2);
        pins = 16'h5AC3;
        reg_read(8'h04, 8);             // 04 ... 0B in one read
        check(rd[7:0] == 8'h12, "register 04 read back");
        check(rd[15:8] == 8'h01, "the status while idle: locked and nothing else");
        check(rd[31:16] == ~16'h5AC3, "the inputs as the registers show them");
        check(rd[55:32] == 24'h030201 && rd[63:56] == 8'h00, "the divider read back, and 0 from a register not used");
        reg_read(8'h0C, 4);
        check(rd[31:0] == {16'd3000, 16'd1000}, "the thresholds read back");
        reg_read(8'h10, 5);
        check(rd[39:0] == 40'h0504030201, "the sample limit read back");
        reg_read(8'h20, 2);
        check(rd[15:0] == 16'd1000, "the buffer size read back");
        reg_write(8'h0C, 64'h55, 1);
        check(dut.r_duty_a == 12'd1000, "a threshold changed before its high byte was written");
        pins = 16'hFFFF;
        i2c_start;
        i2c_out({7'h50, 1'b0});
        check(!ack, "an answer to another device's address");
        i2c_stop;
        n_high_a = 0;
        n_high_b = 0;
        for (i = 0; i < 4096; i = i + 1) begin
            @(posedge dut.clk);
            n_high_a = n_high_a + pwm_a;
            n_high_b = n_high_b + pwm_b;
        end
        check(n_high_a == 1000 && n_high_b == 3000, "the thresholds' duty cycles are not 1000 and 3000 of 4096");

        if (ONLY == 0 || ONLY == 1) begin
        // ---- 1: the test pattern, raw, 100 MS/s, an odd number of samples; buffers changing at different speeds ----
        setup(8'h02, 24'd1, 40'd24581, 16'd4096);
        run = 1;
        #200;
        check(slwr_n === 1'b1 && slcs_n === 1'b0, "SLWR# high and SLCS# low before PCLK starts");
        driven("a line to the FX3 is not driven after RUN rose");
        wait_rx(4096, 400);
        switch_time = 3;
        wait_rx(2 * 4096, 400);
        switch_time = 200;
        wait_done(2000);
        check(rx_n == 12291, "24581 samples are not 12291 words");
        ok = 1;
        for (i = 0; i < 12290; i = i + 1)
            if (rx[i] !== {i[14:0], 1'b1, i[14:0], 1'b0})
                ok = 0;
        check(ok && rx[12290] === {16'd24580, 16'd24580}, "the test pattern's words");
        compare(0, "the test pattern, raw");
        check(delivered == 24581 && ex_n == 24581, "the test pattern: samples in the words, samples taken");
        reg_read(8'h18, 5);
        check(rd[39:0] == 40'd24581, "the count of samples");
        reg_read(8'h24, 6);
        check(rd[47:0] == 48'd12291, "the count of words");
        reg_read(8'h05, 1);
        check(rd[7:0] == 8'hCB, "the status after the limit: done, RUN high, waiting for RUN to fall");
        check(led_arm === 1'b0, "LED ARM is on in a capture without a trigger");
        e0 = edges;
        #1000;
        check(edges > e0 + 90, "PCLK stopped before RUN fell");
        run = 0;
        wait_off(100);
        e0 = edges;
        #2000;
        check(edges == e0, "PCLK still runs after RUN fell");
        released("a line to the FX3 is still driven after RUN fell");
        reg_read(8'h05, 1);
        check(rd[7:0] == 8'h09, "the status after RUN fell: done, off");

        // ---- 2: the probes, run-length, 200 MS/s, small buffers changing at different speeds, the host pausing ----
        setup(8'h01, 24'd0, 40'd0, 16'd64);
        gap = 300;
        stim = 1;
        run = 1;
        switch_time = 40;
        #150000;
        switch_time = 3;
        #100000;
        switch_time = 300;
        #100000;
        host_reads = 0;
        #40000;
        host_reads = 1;
        #100000;
        run = 0;
        wait_off(1000);
        stim = 0;
        compare(1, "the probes, run-length");
        check(delivered == ex_n && ex_n > 90000, "the probes, run-length: every sample is in the words");
        check(rx_n > 500 && rx_n < 4000, "the probes, run-length: about a word for every change");
        reg_read(8'h18, 5);
        check(rd[39:0] == ex_n, "the count of samples after RUN fell");
        reg_read(8'h05, 1);
        check(rd[7:0] == 8'h09, "no overflow with a short pause of the host");
        released("a line to the FX3 is still driven after the second capture");
        pins = 16'hFFFF;
        end

        if (!QUICK && (ONLY == 0 || ONLY == 1)) begin
            // ---- 3: the test pattern, raw, 200 MS/s: a word in every cycle, as many as the FX3 can take.  Its
            // buffers change quickly here, and three of them leave room in the FIFO ----
            switch_time = 3;
            setup(8'h02, 24'd0, 40'd24581, 16'd4096);
            run = 1;
            wait_done(2000);
            check(rx_n == 12291, "200 MS/s: 24581 samples are not 12291 words");
            ok = 1;
            for (i = 0; i < 12290; i = i + 1)
                if (rx[i] !== {i[14:0], 1'b1, i[14:0], 1'b0})
                    ok = 0;
            check(ok && rx[12290] === {16'd24580, 16'd24580}, "200 MS/s: the test pattern's words");
            compare(0, "the test pattern, raw, 200 MS/s");
            check(delivered == 24581 && ex_n == 24581, "200 MS/s: samples in the words, samples taken");
            reg_read(8'h18, 5);
            check(rd[39:0] == 40'd24581, "200 MS/s: the count of samples");
            reg_read(8'h05, 1);
            check(rd[7:0] == 8'hCB, "200 MS/s: done without an overflow");
            reg_read(8'h22, 2);
            check(rd[15:0] > 16'd20 && rd[15:0] < 16'd200, "200 MS/s, raw: the FIFO fills only while buffers change");
            run = 0;
            wait_off(100);
            switch_time = 40;

            // ---- 4: a count on the probes that goes up every 5 ns: the samples are the count at every
            // (divider + 1)th step, none missing, none twice, in order ----
            ramp = 1;
            #100;
            ramp_capture(24'd0, 6001);
            ramp_capture(24'd1, 2001);
            ramp_capture(24'd2, 2001);
            ramp_capture(24'd3, 2001);
            ramp_capture(24'd4, 2000);
            ramp_capture(24'd7, 20001);
            ramp_capture(24'd10, 2001);
            // and run-length, where every sample then makes a word: 301 words in 151 cycles
            two_words = 0;
            setup(8'h01, 24'd0, 40'd301, 16'd4096);
            run = 1;
            wait_done(1000);
            check(rx_n == 301, "the ramp, run-length: a word for every sample");
            compare(1, "the ramp, run-length");
            check(delivered == 301 && ex_n == 301, "the ramp, run-length: every sample is in the words");
            ramp_check(1, "the ramp, run-length: the samples are not the probes every 5 ns");
            check(two_words >= 149, "the ramp, run-length: two words in a cycle");
            run = 0;
            wait_off(100);
            ramp = 0;
            #100;
            pins = 16'hFFFF;

            // ---- 5: the probes changing every 12 ns or so, run-length, 200 MS/s: many cycles make two words ----
            two_words = 0;
            setup(8'h01, 24'd0, 40'd40001, 16'd4096);
            gap = 12;
            stim = 1;
            run = 1;
            wait_done(2000);
            stim = 0;
            compare(1, "busy probes, run-length");
            check(delivered == 40001 && ex_n == 40001, "busy probes: every sample is in the words");
            check(two_words > 1000 && rx_n > 8000, "busy probes: too few changes for this test");
            reg_read(8'h05, 1);
            check(rd[7:0] == 8'hCB, "busy probes: done without an overflow");
            run = 0;
            wait_off(100);

            // ---- 6: quiet inputs, run-length: a word every 65536 samples.  At 200 MS/s the samples are counted
            // two at a time: first from the capture's start, so a run is full after an even number of cycles ----
            pins = 16'hA5F0;
            #100;
            setup(8'h01, 24'd0, 40'd200000, 16'd4096);
            run = 1;
            wait_done(4000);
            check(rx_n == 4 && rx[0] === 32'hFFFF5A0F && rx[1] === 32'hFFFF5A0F && rx[2] === 32'hFFFF5A0F &&
                  rx[3] === {16'd3391, 16'h5A0F}, "200000 samples of quiet inputs are not three full runs and one of 3392");
            compare(1, "quiet inputs, run-length");
            check(delivered == 200000, "quiet inputs: every sample is in the words");
            run = 0;
            wait_off(100);
            // then with an input that changes between a cycle's two samples: the runs after it fill in mid-cycle
            setup(8'h01, 24'd0, 40'd200000, 16'd4096);
            run = 1;
            #100000;
            @(posedge dut.clk);
            #7.5 pins = 16'hA5F1;
            wait_done(4000);
            check(rx_n == 4 && rx[0][15:0] === 16'h5A0F && rx[0][16] === 1'b0 && rx[1] === 32'hFFFF5A0E &&
                  rx[2] === 32'hFFFF5A0E && rx[3][15:0] === 16'h5A0E &&
                  rx[0][31:16] + rx[3][31:16] + 2 + 2 * 65536 == 200000,
                  "quiet inputs with a change in mid-cycle: not an odd run, two full ones and the rest");
            compare(1, "quiet inputs with a change in mid-cycle");
            check(delivered == 200000, "quiet inputs with a change: every sample is in the words");
            run = 0;
            wait_off(100);
            // and at 100 MS/s, a sample in each cycle
            pins = 16'hA5F0;
            #100;
            setup(8'h01, 24'd1, 40'd200000, 16'd4096);
            run = 1;
            wait_done(5000);
            check(rx_n == 4 && rx[0] === 32'hFFFF5A0F && rx[1] === 32'hFFFF5A0F && rx[2] === 32'hFFFF5A0F &&
                  rx[3] === {16'd3391, 16'h5A0F}, "100 MS/s: 200000 samples of quiet inputs are not three full runs and one of 3392");
            compare(1, "quiet inputs, run-length, 100 MS/s");
            check(delivered == 200000, "100 MS/s, quiet inputs: every sample is in the words");
            run = 0;
            wait_off(100);
            pins = 16'hFFFF;

            // ---- 7: the sample limit at 200 MS/s, 1 ... 6 samples: raw; run-length with every sample different;
            // run-length with samples in pairs ----
            reg_write(8'h08, 24'd0, 3);
            reg_write(8'h20, 16'd4096, 2);
            buf_words = 4096;
            fill = 0;
            ready = 1;
            for (i = 1; i <= 6; i = i + 1) begin
                short_capture(8'h02, i, 1);
                short_capture(8'h03, i, 1);
                short_capture(8'h13, i, 2);
            end
            check(fill == 45, "the short captures: 12 + 21 + 12 words in all");

            // ---- 8: the host does not read: overflow, and what was taken before it arrives whole ----
            setup(8'h03, 24'd1, 40'd0, 16'd4096);
            host_reads = 0;
            run = 1;
            #150000;
            reg_read(8'h05, 1);
            check(rd[4] && !rd[2] && !rd[3], "overflow, sampling stopped, not done while the FX3 takes nothing");
            s0 = dut.samples;
            #20000;
            check(dut.samples == s0, "samples are still taken after the overflow");
            check(rx_n == 4096, "the FX3's one buffer");
            host_reads = 1;
            wait_done(1000);
            check(rd[4], "the overflow is still shown when done");
            check(rx_n == 4096 + 256, "the FX3's buffer and the FIFO's 256 words");
            compare(1, "the words before the overflow");
            check(delivered == rx_n && delivered < ex_n, "the words before the overflow are the capture's first samples");
            reg_read(8'h22, 2);
            check(rd[15:0] == 16'd256, "the FIFO's highest level");
            run = 0;
            wait_off(100);

            // ---- 9: 200 MS/s, run-length, every sample different: two words in every cycle, twice what the FX3
            // takes.  Overflow though the host reads, and what arrived is the capture's beginning ----
            setup(8'h03, 24'd0, 40'd0, 16'd4096);
            run = 1;
            wait_done(1000);
            check(rd[4], "two words a cycle: no overflow");
            check(rx_n > 480 && rx_n < 560, "two words a cycle: about 512 words before the FIFO is full");
            compare(1, "two words a cycle: the words before the overflow");
            check(delivered == rx_n && delivered < ex_n, "two words a cycle: the words are the capture's first samples");
            reg_read(8'h22, 2);
            // (255 when the word that found no room was the second of two and the FX3 took one in that cycle)
            check(rd[15:0] == 16'd255 || rd[15:0] == 16'd256, "two words a cycle: the FIFO's highest level");
            run = 0;
            wait_off(100);

            // ---- 10: 200 MS/s, raw, and an FX3 whose buffers change slowly: overflow at the second change ----
            switch_time = 200;
            setup(8'h02, 24'd0, 40'd0, 16'd4096);
            run = 1;
            wait_done(2000);
            check(rd[4], "raw at 200 MS/s with slow buffers: no overflow");
            check(rx_n > 8192 && rx_n < 3 * 4096, "raw at 200 MS/s with slow buffers: two buffers and the FIFO");
            compare(0, "raw at 200 MS/s: the words before the overflow");
            check(delivered == 2 * rx_n && delivered < ex_n, "raw at 200 MS/s: the words are the capture's first samples");
            run = 0;
            wait_off(100);
            switch_time = 40;

            // ---- 11: RUN falls while the FX3 takes nothing: the design gives up and lets go of the lines ----
            setup(8'h03, 24'd1, 40'd0, 16'd4096);
            host_reads = 0;
            run = 1;
            #150000;
            run = 0;
            #5000;
            driven("the lines were released while words were still waiting");
            #(DRAIN_MAX * 10);
            wait_off(100);
            released("a line to the FX3 is still driven after the design gave up");
            reg_read(8'h2A, 1);
            check(rd[0], "giving up is not shown");
            reg_read(8'h05, 1);
            check(rd[7:0] == 8'h11, "the status after giving up: overflow, not done");
            host_reads = 1;
        end

        if (!QUICK) begin
            switch_time = 40;
            buf_words = 4096;
            TQ = 250;                           // the FX3 writes at 400 kHz; this keeps the test short
            if (ONLY == 0 || ONLY == 12) begin
            // ---- 12: a trigger on the test pattern, where the trigger sample is known: the first that equals a
            // value.  Run-length with every sample a word: words kept before it, none kept, fewer there than
            // asked for, the first sample of all; at 200, 100 and 66.7 MS/s ----
            pattern_trigger(8'h03, 24'd0, 1000, 40, 300, 1);
            pattern_trigger(8'h03, 24'd0, 1001, 40, 300, 1);
            pattern_trigger(8'h03, 24'd0, 1001, 0, 300, 1);
            pattern_trigger(8'h03, 24'd0, 25, 40, 300, 1);
            pattern_trigger(8'h03, 24'd0, 0, 40, 7, 1);
            pattern_trigger(8'h03, 24'd0, 1, 40, 7, 1);
            pattern_trigger(8'h03, 24'd1, 1000, 100, 501, 1);
            pattern_trigger(8'h03, 24'd1, 1001, 100, 500, 1);
            pattern_trigger(8'h03, 24'd2, 333, 5, 64, 1);
            check(rx_n == 69 && rx[0] === {16'd0, 16'd328} && rx[5] === {16'd0, 16'd333},
                  "a trigger at 66.7 MS/s: 5 words kept, then the trigger sample's");
            // raw: the trigger sample in either half of its word
            pattern_trigger(8'h02, 24'd0, 1000, 50, 500, 1);
            check(!t_stat[2] && rx[50] === {16'd1001, 16'd1000}, "raw: the trigger sample is not its word's lower half");
            pattern_trigger(8'h02, 24'd0, 1001, 50, 501, 1);
            check(t_stat[2] && rx[50] === {16'd1001, 16'd1000}, "raw: the trigger sample is not its word's upper half");
            pattern_trigger(8'h02, 24'd1, 1000, 50, 500, 1);
            pattern_trigger(8'h02, 24'd1, 1001, 50, 500, 1);
            pattern_trigger(8'h02, 24'd0, 1, 50, 2, 1);
            pattern_trigger(8'h02, 24'd0, 0, 0, 1, 1);
            // run-length with samples alike in twos and fours: the trigger sample begins a word
            pattern_trigger(8'h13, 24'd0, 500, 100, 600, 2);
            pattern_trigger(8'h23, 24'd0, 250, 3, 600, 4);
            pattern_trigger(8'h23, 24'd1, 250, 3, 601, 4);
            // the sample limit from the trigger sample on: 1 ... 5 samples, the trigger sample first or second
            // in its cycle
            for (i = 1; i <= 5; i = i + 1) begin
                pattern_trigger(8'h03, 24'd0, 200, 10, i, 1);
                pattern_trigger(8'h03, 24'd0, 201, 10, i, 1);
                pattern_trigger(8'h02, 24'd0, 200, 10, i, 1);
                pattern_trigger(8'h02, 24'd0, 201, 10, i, 1);
                pattern_trigger(8'h13, 24'd0, 100, 10, i, 2);
            end

            end
            if (ONLY == 0 || ONLY == 13) begin
            // ---- 13: stages in turn, edges, and conditions that last, on the test pattern ----
            stage(0, 8'd0, 16'hFFFF, 16'd500, 16'd0, 16'd0, 32'd0, 32'd0);
            stage(1, 8'd0, 16'hFFFF, 16'd501, 16'd0, 16'd0, 32'd0, 32'd0);
            stage(2, 8'd0, 16'hFFFF, 16'd502, 16'd0, 16'd0, 32'd0, 32'd0);
            stage(3, 8'd0, 16'hFFFF, 16'd600, 16'd0, 16'd0, 32'd0, 32'd0);
            trig_capture(8'h03, 24'd0, 3, 30, 200, 502, "three stages met by three samples in a row");
            trig_capture(8'h03, 24'd0, 4, 30, 200, 600, "four stages");
            trig_capture(8'h02, 24'd0, 2, 30, 200, 501, "two stages, raw");
            trig_capture(8'h03, 24'd1, 3, 30, 200, 502, "three stages at 100 MS/s");
            stage(0, 8'd0, 16'hFFFF, 16'd501, 16'd0, 16'd0, 32'd0, 32'd0);
            stage(1, 8'd0, 16'hFFFF, 16'd502, 16'd0, 16'd0, 32'd0, 32'd0);
            stage(2, 8'd0, 16'hFFFF, 16'd503, 16'd0, 16'd0, 32'd0, 32'd0);
            trig_capture(8'h03, 24'd0, 3, 30, 200, 503, "three stages, the first met by a cycle's second sample");
            stage(0, 8'd0, 16'hFFFF, 16'd500, 16'd0, 16'd0, 32'd0, 32'd0);
            stage(1, 8'd0, 16'hFFFF, 16'd500, 16'd0, 16'd0, 32'd0, 32'd0);
            trig_capture(8'h13, 24'd0, 2, 30, 200, 1001, "two stages alike are met by two samples, not by one");
            trig_capture(8'h13, 24'd1, 2, 30, 200, 1001, "two stages alike at 100 MS/s");
            stage(0, 8'd0, 16'hFFFF, 16'd300, 16'd0, 16'd0, 32'd0, 32'd0);
            stage(1, 8'd0, 16'h00FF, 16'h0010, 16'd0, 16'd0, 32'd0, 32'd0);
            trig_capture(8'h03, 24'd0, 2, 30, 200, 528, "a stage is looked for only after the one before it is met");
            // edges
            stage(0, 8'd0, 16'h0000, 16'h0000, 16'h0010, 16'h0000, 32'd0, 32'd0);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 16, "a rising edge");
            stage(0, 8'd0, 16'h0F00, 16'h0300, 16'h0010, 16'h0000, 32'd0, 32'd0);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 784, "a rising edge while other inputs match");
            stage(0, 8'd0, 16'h0000, 16'h0000, 16'h0000, 16'h0008, 32'd0, 32'd0);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 16, "a falling edge");
            trig_capture(8'h03, 24'd1, 1, 30, 200, 16, "a falling edge at 100 MS/s");
            trig_capture(8'h02, 24'd0, 1, 30, 200, 16, "a falling edge, raw");
            stage(0, 8'd0, 16'h0000, 16'h0000, 16'h0001, 16'h0001, 32'd0, 32'd0);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 1, "either edge: the capture's second sample");
            stage(0, 8'd0, 16'h0000, 16'h0000, 16'h0002, 16'h0000, 32'd0, 32'd0);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 2, "a rising edge at the third sample");
            // kind 2: the inputs have matched for so many samples.  Bits 15:3 are 0x20 in samples 256 ... 263
            stage(0, 8'd2, 16'hFFF8, 16'h0100, 16'd0, 16'd0, 32'd5, 32'd0);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 260, "matched for 5 samples");
            stage(0, 8'd2, 16'hFFF8, 16'h0100, 16'd0, 16'd0, 32'd6, 32'd0);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 261, "matched for 6 samples");
            trig_capture(8'h03, 24'd1, 1, 30, 200, 261, "matched for 6 samples at 100 MS/s");
            trig_capture(8'h02, 24'd0, 1, 30, 200, 261, "matched for 6 samples, raw");
            stage(0, 8'd2, 16'hFFF8, 16'h0100, 16'd0, 16'd0, 32'd1, 32'd0);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 256, "matched for 1 sample");
            stage(0, 8'd2, 16'hFFF8, 16'h0100, 16'd0, 16'd0, 32'd8, 32'd0);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 263, "matched for 8 samples");
            stage(0, 8'd2, 16'hFFF8, 16'h0108, 16'd0, 16'd0, 32'd7, 32'd0);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 270, "matched for 7 samples from a cycle's first sample");
            stage(0, 8'd2, 16'h0008, 16'h0000, 16'd0, 16'd0, 32'd5, 32'd0);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 4, "matched for 5 samples from the capture's first");
            // after another stage: the inputs had matched for 2 samples before that stage was met, and still do
            stage(0, 8'd0, 16'hFFFF, 16'd259, 16'd0, 16'd0, 32'd0, 32'd0);
            stage(1, 8'd2, 16'hFFF8, 16'h0100, 16'd0, 16'd0, 32'd2, 32'd0);
            trig_capture(8'h03, 24'd0, 2, 30, 200, 260, "levels that had lasted before the stage before was met");
            trig_capture(8'h03, 24'd1, 2, 30, 200, 260, "levels that had lasted before the stage before was met, 100 MS/s");
            stage(0, 8'd0, 16'hFFFF, 16'd258, 16'd0, 16'd0, 32'd0, 32'd0);
            trig_capture(8'h03, 24'd0, 2, 30, 200, 259, "the same, met by a cycle's second sample");
            stage(1, 8'd2, 16'hFFF8, 16'h0100, 16'd0, 16'd0, 32'd6, 32'd0);
            trig_capture(8'h03, 24'd0, 2, 30, 200, 261, "levels that last long enough only after the stage before was met");
            stage(1, 8'd2, 16'hFFF8, 16'h0100, 16'd0, 16'd0, 32'd0, 32'd0);
            trig_capture(8'h03, 24'd0, 2, 30, 200, 259, "levels for 0 samples: as for 1");
            // kind 1: a pulse of so many samples has ended
            stage(0, 8'd1, 16'hFFF8, 16'h0100, 16'd0, 16'd0, 32'd8, 32'd8);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 264, "a pulse of 8 samples has ended");
            trig_capture(8'h03, 24'd1, 1, 30, 200, 264, "a pulse of 8 samples has ended, 100 MS/s");
            stage(0, 8'd1, 16'hFFF8, 16'h0100, 16'd0, 16'd0, 32'd1, 32'd100);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 264, "a pulse of 1 ... 100 samples has ended");
            stage(0, 8'd1, 16'hFFF8, 16'h0100, 16'd0, 16'd0, 32'd0, 32'hFFFFFFFF);
            trig_capture(8'h02, 24'd0, 1, 30, 200, 264, "a pulse of any length has ended, raw");
            stage(0, 8'd1, 16'hFFFE, 16'h0102, 16'd0, 16'd0, 32'd2, 32'd2);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 260, "a pulse of 2 samples has ended");
            stage(0, 8'd1, 16'hFFFE, 16'h0104, 16'd0, 16'd0, 32'd1, 32'd2);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 262, "a pulse of 2 samples that began with a cycle's first sample");
            stage(0, 8'd1, 16'h0003, 16'h0003, 16'd0, 16'd0, 32'd1, 32'd1);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 4, "a pulse of 1 sample has ended");
            stage(0, 8'd1, 16'h0003, 16'h0002, 16'd0, 16'd0, 32'd1, 32'd1);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 3, "a pulse of 1 sample, the other way round in the cycle");
            // samples 0 ... 7 match from the first sample on: that pulse began before the capture.  16 ... 23 count
            stage(0, 8'd1, 16'h0008, 16'h0000, 16'd0, 16'd0, 32'd1, 32'd100);
            trig_capture(8'h03, 24'd0, 1, 30, 200, 24, "a pulse that had begun before the capture does not count");
            // 8 samples are too many for "at most 7" and too few for "at least 9": bits 15:3 next match after
            // 65536 samples, so bits 4:3 are taken: 01 in samples 8 ... 15, 40 ... 47 and so on
            stage(0, 8'd1, 16'h0018, 16'h0008, 16'd0, 16'd0, 32'd1, 32'd7);
            stage(1, 8'd0, 16'hFFFF, 16'd100, 16'd0, 16'd0, 32'd0, 32'd0);
            trigger(1, 30);
            setup(8'h03, 24'd0, 40'd200, 16'd4096);
            run = 1;
            #20000;
            reg_read(8'h34, 1);
            check(rd[7:0] == 8'h08 && rx_n == 0, "pulses of 8 samples met a stage that wants 7 at most");
            run = 0;
            wait_off(1000);
            stage(0, 8'd1, 16'h0018, 16'h0008, 16'd0, 16'd0, 32'd9, 32'd20);
            setup(8'h03, 24'd0, 40'd200, 16'd4096);
            run = 1;
            #20000;
            reg_read(8'h34, 1);
            check(rd[7:0] == 8'h08 && rx_n == 0, "pulses of 8 samples met a stage that wants 9 at least");
            run = 0;
            wait_off(1000);

            end
            if (ONLY == 0 || ONLY == 14) begin
            // ---- 14: the probes changing every 12 ns or so while the capture waits, B7 alone quiet.  Then B7
            // rises once: in either half of a clock cycle ----
            for (i = 0; i < 2; i = i + 1) begin
                stage(0, 8'd0, 16'h0000, 16'h0000, 16'h8000, 16'h0000, 32'd0, 32'd0);
                trigger(1, 16);
                setup(8'h01, 24'd0, 40'd2000, 16'd4096);
                gap = 12;
                stim_mask = 16'h7FFF;
                stim = 1;
                run = 1;
                #100000;
                reg_read(8'h34, 1);
                if (rd[7:0] != 8'h08)
                    $display("    waiting for the trigger: register 34 is %h; %0d words arrived; samples %0d, the trigger sample %0d (%h after %h, %h)",
                             rd[7:0], rx_n, ex_n, ex_trig, ex_trig >= 0 ? ex[ex_trig] : 16'd0, ex_trig >= 1 ? ex[ex_trig - 1] : 16'd0,
                             ex_trig >= 2 ? ex[ex_trig - 2] : 16'd0);
                check(rd[7:0] == 8'h08, "waiting for the trigger: the trigger's status is not waiting alone");
                reg_read(8'h05, 1);
                check(rd[2] && !rd[3] && !rd[4], "waiting: sampling, not done, no overflow though the probes are busy");
                check(rx_n == 0, "words went to the FX3 before the trigger");
                check(led_arm === 1'b1 && led_trig === 1'b0 && trig_out === 1'b0, "the LEDs and TRIG OUT while waiting");
                if (i == 0)
                    @(posedge dut.clk);
                else
                    @(negedge dut.clk);
                #3.1 pins[15] = 1'b0;
                t_ev = $realtime;
                #200;
                check(led_arm === 1'b0 && led_trig === 1'b1 && trig_out === 1'b1, "the LEDs and TRIG OUT after the trigger");
                wait_done(2000);
                stim = 0;
                compare_trig(1, "busy probes with a trigger");
                check(ex[ex_trig][15] === 1'b1 && ex[ex_trig - 1][15] === 1'b0, "the trigger sample is not the first with B7 high");
                check(t_num == 16, "busy probes with a trigger: 16 words kept");
                check(ex_n - ex_trig == 2000 && delivered == ex_n - ex_base, "busy probes with a trigger: 2000 samples from the trigger on");
                check(ex_trig > 15000, "busy probes with a trigger: too few samples before it for this test");
                if (!(t_out - t_ev > 60.0 && t_out - t_ev < 70.0))
                    $display("    TRIG OUT rose %0.1f ns after the input", t_out - t_ev);
                check(t_out - t_ev > 60.0 && t_out - t_ev < 70.0, "TRIG OUT did not rise 6 cycles after the pads took the trigger sample");
                check(trig_out === 1'b0, "TRIG OUT is still high after the last sample");
                run = 0;
                wait_off(100);
                pins = 16'hFFFF;
                stim_mask = 16'hFFFF;
            end

            end
            if (ONLY == 0 || ONLY == 15) begin
            // ---- 15: pulses of 3, 7 and 12 samples on A5 and triggers on their length ----
            for (i = 0; i < 2; i = i + 1) begin
                pulse_capture(8'd1, 32'd6, 32'd8, i, 7, "a pulse of 6 ... 8 samples");
                pulse_capture(8'd1, 32'd1, 32'd3, i, 3, "a pulse of 1 ... 3 samples");
                pulse_capture(8'd1, 32'd7, 32'd7, i, 7, "a pulse of 7 samples");
                pulse_capture(8'd1, 32'd8, 32'hFFFFFFFF, i, 12, "a pulse of 8 samples or more");
                pulse_capture(8'd1, 32'd12, 32'd12, i, 12, "a pulse of 12 samples");
                pulse_capture(8'd1, 32'd4, 32'd6, i, 0, "a pulse of 4 ... 6 samples: there is none");
                pulse_capture(8'd1, 32'd8, 32'd11, i, 0, "a pulse of 8 ... 11 samples: there is none");
                pulse_capture(8'd1, 32'd13, 32'd1000, i, 0, "a pulse of 13 samples or more: there is none");
                pulse_capture(8'd2, 32'd10, 32'd0, i, 10, "high for 10 samples");
                pulse_capture(8'd2, 32'd4, 32'd0, i, 4, "high for 4 samples");
                pulse_capture(8'd2, 32'd3, 32'd0, i, 3, "high for 3 samples");
                pulse_capture(8'd2, 32'd12, 32'd0, i, 12, "high for 12 samples");
                pulse_capture(8'd2, 32'd13, 32'd0, i, 0, "high for 13 samples: never");
            end

            end
            if (ONLY == 0 || ONLY == 16) begin
            // ---- 16: TRIG IN, with the count on the probes that says when each sample was taken ----
            ramp = 1;
            #100;
            for (i = 0; i < 4; i = i + 1) begin
                // rising, falling; then with a filter on B7, which makes every input and TRIG IN 250 or 249
                // samples late; the count stays below 32768 for 160 us, so B7 does not change
                if (i == 2)
                    filters({8'd250, 120'd0});
                if (i == 3)
                    filters({8'd249, 120'd0});
                trig_in = i[0];
                stage(0, i[0] ? 8'h0B : 8'h07, 16'd0, 16'd0, 16'd0, 16'd0, 32'd0, 32'd0);
                trigger(1, 60);
                setup(8'h00, 24'd0, 40'd200, 16'd4096);
                run = 1;
                #((START_WAIT + 40) * 10);
                #(4137.3 + 3.3 * i);
                check(rx_n == 0, "TRIG IN: words before the trigger");
                trig_in = ~i[0];
                t_ev = $realtime;
                cnt_ev = ramp_count;
                wait_done(2000);
                compare_trig(0, "TRIG IN");
                if (!(ex[ex_trig] === cnt_ev[15:0] || ex[ex_trig] === cnt_ev[15:0] + 16'd1))
                    $display("    TRIG IN changed at count %0d, the trigger sample is %0d", cnt_ev, ex[ex_trig]);
                check(ex[ex_trig] === cnt_ev[15:0] || ex[ex_trig] === cnt_ev[15:0] + 16'd1,
                      "TRIG IN: the trigger sample is not the one taken as it changed");
                if (i < 2)
                    check(t_out - t_ev > 60.0 && t_out - t_ev < 70.0, "TRIG IN: TRIG OUT did not rise 6 cycles later");
                else
                    check(t_out - t_ev > 60.0 + 20.0 + 5.0 * (252 - i) && t_out - t_ev < 70.0 + 20.0 + 5.0 * (252 - i),
                          "TRIG IN with a filter: TRIG OUT did not come later by the filter's time and two cycles");
                ramp_check(1, "TRIG IN: the samples are not the probes every 5 ns");
                run = 0;
                wait_off(100);
            end
            // the edge that is not asked for does nothing; at 50 MS/s the first sample kept after the edge is it
            trig_in = 0;
            stage(0, 8'h07, 16'd0, 16'd0, 16'd0, 16'd0, 32'd0, 32'd0);
            trigger(1, 60);
            setup(8'h00, 24'd3, 40'd200, 16'd4096);
            run = 1;
            #((START_WAIT + 40) * 10);
            #1000 trig_in = 1;
            wait_done(2000);
            run = 0;
            wait_off(100);
            trig_in = 1;
            stage(0, 8'h07, 16'd0, 16'd0, 16'd0, 16'd0, 32'd0, 32'd0);
            setup(8'h00, 24'd3, 40'd200, 16'd4096);
            run = 1;
            #((START_WAIT + 40) * 10);
            #3000 trig_in = 0;
            #3000;
            reg_read(8'h34, 1);
            check(rd[7:0] == 8'h08 && rx_n == 0, "TRIG IN falling met a stage that waits for it to rise");
            #777.7 trig_in = 1;
            cnt_ev = ramp_count;
            wait_done(2000);
            compare_trig(0, "TRIG IN at 50 MS/s");
            check(ex[ex_trig] - cnt_ev[15:0] <= 16'd4, "TRIG IN at 50 MS/s: the trigger sample is not the first kept after the edge");
            ramp_check(4, "TRIG IN at 50 MS/s: the samples are not the probes every 20 ns");
            run = 0;
            wait_off(100);
            trig_in = 0;
            trigger(0, 0);
            // the ramp through the filter's shift registers: with every input as late as the others, the count
            // arrives whole
            ramp_capture(24'd0, 6001);
            filters({8'd250, 120'd0});
            ramp_capture(24'd0, 6001);
            ramp_capture(24'd2, 2001);
            filters(128'd0);
            ramp = 0;
            #100;
            pins = 16'hFFFF;

            end
            if (ONLY == 0 || ONLY == 17) begin
            // ---- 17: a capture that is ended before its trigger: the words kept arrive ----
            stage(0, 8'd0, 16'hFFFF, 16'hFFFF, 16'd0, 16'd0, 32'd0, 32'd0);
            trigger(1, 20);
            setup(8'h03, 24'd1, 40'd0, 16'd4096);
            run = 1;
            #100000;
            check(rx_n == 0, "ended before the trigger: words before the end");
            run = 0;
            wait_off(1000);
            reg_read(8'h34, 1);
            check(rd[3:0] == 4'h0, "ended before the trigger: the status says triggered, or waiting");
            check(rx_n == 20, "ended before the trigger: not the 20 words kept");
            ex_base = ex_n - 20;
            compare(1, "ended before the trigger");
            check(delivered == 20, "ended before the trigger: the words are not the last samples taken");
            reg_read(8'h18, 5);
            check(rd[39:0] == 40'd0, "ended before the trigger: samples counted");
            reg_read(8'h05, 1);
            check(rd[7:0] == 8'h09, "ended before the trigger: done, off");

            end
            if (ONLY == 0 || ONLY == 18) begin
            // ---- 18: every sample a word, twice what the FX3 takes: the FIFO overflows soon after the trigger,
            // and what arrived is whole, with the trigger word where the design says ----
            stage(0, 8'd0, 16'hFFFF, 16'd3000, 16'd0, 16'd0, 32'd0, 32'd0);
            trigger(1, 100);
            setup(8'h03, 24'd0, 40'd0, 16'd4096);
            run = 1;
            wait_done(2000);
            check(rd[4], "two words a cycle after a trigger: no overflow");
            compare_trig(1, "overflow after a trigger");
            check(t_num == 100 && ex_trig == 3000 && rx[100] === {16'd0, 16'd3000}, "overflow after a trigger: the trigger word");
            check(delivered == rx_n && rx_n > 300 && rx_n < 500, "overflow after a trigger: the words that arrived");
            run = 0;
            wait_off(100);
            // the most words that can be kept: the FIFO's size less 8, also when more are asked for
            trigger(1, 60000);
            setup(8'h03, 24'd1, 40'd50, 16'd4096);
            run = 1;
            wait_done(3000);
            compare_trig(1, "as many words kept as the FIFO allows");
            check(t_num == (1 << FIFO_BITS) - 8 && ex_trig == 3000, "the most words kept is not the FIFO's size less 8");
            reg_read(8'h05, 1);
            check(rd[7:0] == 8'hCB, "as many words kept as the FIFO allows: done without an overflow");
            run = 0;
            wait_off(100);
            trigger(0, 0);

            end
            if (ONLY == 0 || ONLY == 19) begin
            // ---- 19: state capture.  A0 is the clock of a circuit whose count on B0 ... B7 changes 2 ns after
            // the clock's rising edge: at each edge the sample kept is the count from before it ----
            sdata = 0;
            pins = 16'hFFFF;
            sclk_half = 10.3;                           // 48.5 MHz: its edges fall anywhere in the 100 MHz cycle
            sclk_on = 1;
            #1000;
            state_capture(8'h03, 8'h01, 3000, "state capture at rising edges, run-length");
            state_capture(8'h03, 8'h00, 3001, "state capture at rising edges, raw");
            state_capture(8'h05, 8'h01, 3000, "state capture at falling edges");
            state_capture(8'h07, 8'h01, 3000, "state capture at both edges");
            state_capture(8'h07, 8'h00, 3000, "state capture at both edges, raw");
            sclk_half = 10.0;                           // 50 MHz
            #1001.7;
            state_capture(8'h03, 8'h01, 3000, "state capture at 50 MHz");
            state_capture(8'h07, 8'h01, 3000, "state capture at both edges of 50 MHz");
            sclk_half = 37.9;
            #1000;
            state_capture(8'h03, 8'h01, 1000, "state capture at 13 MHz");
            state_capture(8'h07, 8'h00, 1000, "state capture at both edges of 13 MHz, raw");
            // with a trigger: the count reaches 100
            sclk_half = 10.3;
            #1000;
            reg_write(8'h30, 8'h03, 1);
            stage(0, 8'd0, 16'hFF00, 16'h6400, 16'd0, 16'd0, 32'd0, 32'd0);
            trigger(1, 10);
            setup(8'h01, 24'd0, 40'd50, 16'd4096);
            run = 1;
            wait_done(8000);
            compare_trig(1, "state capture with a trigger");
            check(ex[ex_trig][15:8] === 8'd100 && ex[ex_trig - 1][15:8] === 8'd99 && t_num == 10 &&
                  ex_n - ex_trig == 50, "state capture with a trigger: the trigger sample is not the count of 100");
            check(rx_n == 60 && rx[0][15:8] === 8'd90 && rx[59][15:8] === 8'd149, "state capture with a trigger: the words");
            run = 0;
            wait_off(100);
            trigger(0, 0);
            // no clock: no samples, and the capture ends when RUN falls
            sclk_on = 0;
            #100;
            pins = 16'hFFFF;
            setup(8'h01, 24'd0, 40'd50, 16'd4096);
            run = 1;
            #50000;
            reg_read(8'h18, 5);
            check(rd[39:0] == 40'd0 && rx_n == 0, "a state capture took samples without a clock");
            run = 0;
            wait_off(1000);
            reg_write(8'h30, 8'h00, 1);

            end
            if (ONLY == 0 || ONLY == 20) begin
            // ---- 20: the glitch filter.  Six inputs change every 70 ns or so each, often for a sample or two
            // only, and five of them have filters of 1 ... 9 samples: what the design makes of them is compared
            // with the description above, input by input and sample by sample; then the capture itself ----
            filters({8'd0, 8'd0, 8'd0, 8'd2, 8'd0, 8'd0, 8'd9, 8'd1, 8'd0, 8'd0, 8'd7, 8'd0, 8'd0, 8'd4, 8'd0, 8'd0});
            setup(8'h01, 24'd0, 40'd20000, 16'd4096);
            xl_n = 0;
            logging = 1;
            run = 1;
            #((START_WAIT + 40) * 10);
            gap = 4;
            stim_mask = 16'h1325;                       // A0, A2, A5, B0, B1, B4
            stim = 1;
            wait_done(3000);
            stim = 0;
            stim_mask = 16'hFFFF;
            logging = 0;
            filter_check("the glitch filter");
            compare(1, "a capture through the glitch filter");
            check(delivered == 20000 && ex_n == 20000, "a capture through the glitch filter: every sample is in the words");
            min_runs(2, 4, "the filter of 4 samples on A2");
            min_runs(5, 7, "the filter of 7 samples on A5");
            min_runs(9, 9, "the filter of 9 samples on B1");
            min_runs(12, 2, "the filter of 2 samples on B4");
            min_runs(0, 1, "A0, without a filter");
            run = 0;
            wait_off(100);
            pins = 16'hFFFF;
            // and with long filters, odd and even: 250 and 37 samples
            filters({8'd0, 8'd0, 8'd0, 8'd0, 8'd0, 8'd0, 8'd37, 8'd0, 8'd0, 8'd0, 8'd0, 8'd0, 8'd250, 8'd0, 8'd0, 8'd0});
            setup(8'h01, 24'd0, 40'd40000, 16'd4096);
            xl_n = 0;
            logging = 1;
            run = 1;
            #((START_WAIT + 40) * 10);
            gap = 12;
            stim = 1;
            wait_done(3000);
            stim = 0;
            logging = 0;
            filter_check("the glitch filter, 250 and 37 samples");
            compare(1, "a capture through long glitch filters");
            check(delivered == 40000, "a capture through long glitch filters: every sample is in the words");
            run = 0;
            wait_off(100);
            pins = 16'hFFFF;
            filters(128'd0);
            // the filters off again: the inputs go to the sampler directly
            setup(8'h01, 24'd0, 40'd2000, 16'd4096);
            xl_n = 0;
            logging = 1;
            run = 1;
            #((START_WAIT + 40) * 10);
            stim = 1;
            wait_done(3000);
            stim = 0;
            logging = 0;
            check(dut.c_filt === 1'b0, "the filters are off, but the design uses them");
            ok = 1;
            for (i = 600; i + 2 < xl_n; i = i + 1)
                if (glog[i + 2] !== xlog[i][15:0])
                    ok = 0;
            check(ok && xl_n > 2000, "without filters the sampler does not see the inputs a cycle later");
            run = 0;
            wait_off(100);
            pins = 16'hFFFF;
            end

            // ---- 21: the Pi's lines in place of probes.  The count is on the probes and on the Pi's lines as
            // the ramp says above: all nine lines, none, some, and one at a time ----
            if (ONLY == 0 || ONLY == 21) begin
            reg_read(8'h2E, 2);
            check(rd[15:0] == 16'd0, "the Pi's lines are not off at the start");
            reg_write(8'h2E, 16'hFFFF, 2);
            reg_read(8'h2E, 2);
            check(rd[15:0] == 16'h01FF, "register 2E does not keep its nine bits");
            pif = 9'h155;
            #100;
            reg_read(8'h06, 2);
            check(rd[15:0] == 16'h0155, "the inputs now, with the Pi's lines in place of A0 ... B0");
            reg_write(8'h2E, 16'h0000, 2);
            #100;
            reg_read(8'h06, 2);
            check(rd[15:0] == 16'h0000, "the inputs now, with the Pi's lines off again");
            for (i = 0; i < 13; i = i + 1) begin
                pi_sel = (i == 0) ? 9'h1FF : (i == 1) ? 9'h000 : (i == 2) ? 9'h0A5 : (i == 3) ? 9'h15A
                         : 9'd1 << (i - 4);
                reg_write(8'h2E, pi_sel, 2);
                ramp = 1;
                #100;
                ramp_capture(24'd0, 4001);
                if (i < 4)
                    ramp_capture(24'd2, 2001);
                ramp = 0;
                #100;
                pins = 16'hFFFF;
                pif = 9'd0;
            end
            // run-length, a trigger on a Pi line's edge and a glitch filter on it: SCL (bit 1, in place of A1)
            // rises 40 samples of 5 ns after the capture's start and has a pulse of 2 samples before that
            pi_sel = 9'h003;
            reg_write(8'h2E, pi_sel, 2);
            filters({112'd0, 8'd4, 8'd0});
            stage(0, 8'h00, 16'h0002, 16'h0002, 16'h0002, 16'h0000, 32'd0, 32'd0);
            trigger(1, 16);
            setup(8'h01, 24'd0, 40'd200, 16'd4096);
            run = 1;
            #((START_WAIT + 40) * 10);
            @(dut.clk);
            #2.5 pif[1] = 1'b1;
            repeat (2) @(dut.clk);
            #2.5 pif[1] = 1'b0;
            repeat (30) @(dut.clk);
            #2.5 pif[1] = 1'b1;
            pins[1] = 1'b1;                     // the probe A1 itself stays below its threshold
            wait_done(3000);
            check(ex_trig > 0, "a trigger on a Pi line: none");
            check(ex[ex_trig][1] === 1'b1 && ex[ex_trig - 1][1] === 1'b0, "a trigger on a Pi line: not at its rising edge");
            ok = 1;
            for (i = 0; i < ex_trig; i = i + 1)
                if (ex[i][1] !== 1'b0)
                    ok = 0;
            check(ok, "a glitch filter on a Pi line: the short pulse came through");
            compare_trig(1, "a trigger on a Pi line");
            run = 0;
            wait_off(100);
            trigger(0, 0);
            filters(128'd0);
            pif = 9'd0;
            pi_sel = 9'd0;
            reg_write(8'h2E, 16'h0000, 2);
            end
        end

        $display("overruns %0d, other failures %0d", overruns, errors);
        if (errors == 0 && overruns == 0)
            $display("PASS");
        else
            $display("FAIL");
        $finish;
    end
endmodule
