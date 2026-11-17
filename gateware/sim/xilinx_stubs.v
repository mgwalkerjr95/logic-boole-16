// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: CERN-OHL-S-2.0
//
// Stand-ins for the Xilinx primitives of capture2_top.v, for the simulation only.  The JTAG block's outputs come
// from its "t_" registers, which a test bench can drive; capture2_tb.v leaves them at 0.  The PLL here makes its
// output's period from its parameters and "locks" 2 us after the start; it does not follow its input clock.
`timescale 1ns / 1ps
module PULLDOWN (output O);
    pulldown (O);
endmodule

module PULLUP (output O);
    pullup (O);
endmodule

module BUFG (output O, input I);
    assign O = I;
endmodule

module IBUFG (output O, input I);
    assign O = I;
endmodule

module PLL_BASE #(
    parameter BANDWIDTH = "OPTIMIZED", CLK_FEEDBACK = "CLKFBOUT", COMPENSATION = "INTERNAL",
    parameter real CLKIN_PERIOD = 10.0, REF_JITTER = 0.01,
    parameter integer DIVCLK_DIVIDE = 1, CLKFBOUT_MULT = 1, CLKOUT0_DIVIDE = 1,
    parameter real CLKFBOUT_PHASE = 0.0, CLKOUT0_PHASE = 0.0, CLKOUT0_DUTY_CYCLE = 0.5
) (
    input CLKIN, CLKFBIN, RST,
    output CLKFBOUT, CLKOUT1, CLKOUT2, CLKOUT3, CLKOUT4, CLKOUT5,
    output reg CLKOUT0, LOCKED
);
    localparam real HALF = CLKIN_PERIOD * DIVCLK_DIVIDE * CLKOUT0_DIVIDE / CLKFBOUT_MULT / 2.0;
    assign CLKFBOUT = 0, CLKOUT1 = 0, CLKOUT2 = 0, CLKOUT3 = 0, CLKOUT4 = 0, CLKOUT5 = 0;
    initial begin
        CLKOUT0 = 0;
        LOCKED = 0;
        #2000 LOCKED = 1;
        forever #(HALF) CLKOUT0 = ~CLKOUT0;
    end
endmodule

module ODDR2 #(parameter DDR_ALIGNMENT = "NONE", parameter INIT = 1'b0, parameter SRTYPE = "SYNC") (
    output reg Q,
    input C0, C1, CE, D0, D1, R, S
);
    initial Q = INIT;
    always @(posedge C0) if (CE) Q <= D0;
    always @(posedge C1) if (CE) Q <= D1;
endmodule

// Only DDR_ALIGNMENT "C0" is modelled: Q0 is the input at C0's rising edge, and Q1, which changes at that edge too,
// is the input at C1's rising edge before it
module IDDR2 #(parameter DDR_ALIGNMENT = "NONE", parameter INIT_Q0 = 1'b0, parameter INIT_Q1 = 1'b0,
               parameter SRTYPE = "SYNC") (
    output reg Q0, Q1,
    input C0, C1, CE, D, R, S
);
    reg at_c1;
    initial begin
        Q0 = INIT_Q0;
        Q1 = INIT_Q1;
        at_c1 = INIT_Q1;
        if (DDR_ALIGNMENT != "C0") begin
            $display("FAIL: this IDDR2 models DDR_ALIGNMENT C0 only");
            $finish;
        end
    end
    always @(posedge C1) if (CE && $time > 0) at_c1 <= D;   // at time 0 C1 comes out of "unknown": not an edge
    always @(posedge C0) if (CE) begin
        Q0 <= D;
        Q1 <= at_c1;
    end
endmodule

module BSCAN_SPARTAN6 #(parameter JTAG_CHAIN = 1) (
    output CAPTURE, DRCK, RESET, RUNTEST, SEL, SHIFT, TCK, TDI, TMS, UPDATE,
    input TDO
);
    reg t_capture = 0, t_sel = 0, t_shift = 0, t_tck = 0, t_tdi = 0;
    assign CAPTURE = t_capture, SEL = t_sel, SHIFT = t_shift, TCK = t_tck, TDI = t_tdi;
    assign DRCK = 0, RESET = 0, RUNTEST = 0, TMS = 0, UPDATE = 0;
endmodule
