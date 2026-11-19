// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: CERN-OHL-S-2.0
//
// Stand-ins for the Xilinx primitives of capture2_top.v and fast_top.v, for the simulation only.  The JTAG block's
// outputs come from its "t_" registers, which a test bench can drive; capture2_tb.v leaves them at 0.  The PLL here
// makes its output's period from its parameters and "locks" 2 us after the start; it does not follow its input
// clock.  Its second output is its first divided down, for a whole even ratio of the two dividers only.
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
    parameter real CLKFBOUT_PHASE = 0.0, CLKOUT0_PHASE = 0.0, CLKOUT0_DUTY_CYCLE = 0.5,
    parameter integer CLKOUT1_DIVIDE = 1,
    parameter real CLKOUT1_PHASE = 0.0, CLKOUT1_DUTY_CYCLE = 0.5
) (
    input CLKIN, CLKFBIN, RST,
    output CLKFBOUT, CLKOUT2, CLKOUT3, CLKOUT4, CLKOUT5,
    output reg CLKOUT0, CLKOUT1, LOCKED
);
    localparam real HALF = CLKIN_PERIOD * DIVCLK_DIVIDE * CLKOUT0_DIVIDE / CLKFBOUT_MULT / 2.0;
    localparam integer RATIO = CLKOUT1_DIVIDE / CLKOUT0_DIVIDE;
    localparam DIVIDED = (RATIO >= 2) && (RATIO % 2 == 0) && (RATIO * CLKOUT0_DIVIDE == CLKOUT1_DIVIDE);
    assign CLKFBOUT = 0, CLKOUT2 = 0, CLKOUT3 = 0, CLKOUT4 = 0, CLKOUT5 = 0;
    integer rises = 0;
    initial begin
        CLKOUT0 = 0;
        CLKOUT1 = 0;
        LOCKED = 0;
        #2000 LOCKED = 1;
        forever #(HALF) CLKOUT0 = ~CLKOUT0;
    end
    // the second output changes at every (RATIO / 2)th rising edge of the first: its rising edges are the first's
    always @(posedge CLKOUT0) if (DIVIDED) begin
        rises = rises + 1;
        if (rises == RATIO / 2) begin
            rises = 0;
            CLKOUT1 <= ~CLKOUT1;
        end
    end
endmodule

// The clock buffer of a bank's pads: the fast clock goes through, and the strobe is high in the cycle of the fast
// clock that ends where GCLK, the fast clock divided by DIVIDE, rises.  LOCK follows the PLL's LOCKED.
module BUFPLL #(parameter integer DIVIDE = 1, parameter ENABLE_SYNC = "TRUE") (
    output IOCLK,
    output reg LOCK, SERDESSTROBE,
    input GCLK, LOCKED, PLLIN
);
    assign IOCLK = PLLIN;
    integer at = 0;                     // which of GCLK's DIVIDE edges of the fast clock this one is; 0: GCLK rises
    reg g_was = 0;
    initial begin
        LOCK = 0;
        SERDESSTROBE = 0;
    end
    always @(posedge PLLIN) begin
        at = (GCLK && !g_was) ? 1 : (at + 1) % DIVIDE;     // GCLK is seen high from the edge after its own
        g_was <= GCLK;
        SERDESSTROBE <= (at == DIVIDE - 1);
        LOCK <= LOCKED;
    end
endmodule

// A pad's 1:4 deserializer, as fast_top.v uses it and no other way: single data rate, four bits, no cascade, no
// bit slip.  The pad takes D at each rising edge of CLK0; at the edge where IOCE is high the last four go to a
// holding register, and at CLKDIV's rising edge they appear on Q1 ... Q4, Q4 being the one taken last.
module ISERDES2 #(
    parameter integer DATA_WIDTH = 4,       // (the defaults are what is modelled, not Xilinx's)
    parameter DATA_RATE = "SDR", BITSLIP_ENABLE = "FALSE", SERDES_MODE = "NONE", INTERFACE_TYPE = "RETIMED"
) (
    output CFB0, CFB1, DFB, FABRICOUT, INCDEC, SHIFTOUT, VALID,
    output reg Q1, Q2, Q3, Q4,
    input BITSLIP, CE0, CLK0, CLK1, CLKDIV, D, IOCE, RST, SHIFTIN
);
    assign CFB0 = 0, CFB1 = 0, DFB = 0, FABRICOUT = 0, INCDEC = 0, SHIFTOUT = 0, VALID = 0;
    reg [3:0] sr = 0, held = 0;
    initial begin
        {Q4, Q3, Q2, Q1} = 0;
        if (DATA_WIDTH != 4 || DATA_RATE != "SDR" || BITSLIP_ENABLE != "FALSE" || SERDES_MODE != "NONE"
            || INTERFACE_TYPE != "RETIMED") begin
            $display("FAIL: this ISERDES2 models 4 bits, SDR, RETIMED, no bit slip and no cascade only");
            $finish;
        end
    end
    always @(posedge CLK0) if (CE0) begin
        sr <= {D, sr[3:1]};
        if (IOCE)
            held <= {D, sr[3:1]};
    end
    always @(posedge CLKDIV)
        {Q4, Q3, Q2, Q1} <= RST ? 4'd0 : held;
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
