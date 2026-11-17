// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: CERN-OHL-S-2.0
//
// An I2C slave with a register pointer, as serial EEPROMs and sensors have, for capture2_top.v.
//   write:  START, address + W, the pointer, then any number of bytes: each goes to the register at the pointer,
//           which then moves on by one
//   read:   START, address + W, the pointer, START again, address + R, then bytes from the pointer on; the master
//           ends with a NACK and STOP
// SCL and SDA pass two flip-flops and a filter: a new level counts once it has held for FILTER clock cycles, so
// the slow edges of a pulled-up line and spikes shorter than that do nothing.  SDA is changed only after SCL's
// falling edge has passed the filter.  SCL is never held low.
module i2c_slave #(
    parameter [6:0] ADDRESS = 7'h2A,
    parameter FILTER = 16
) (
    input  wire clk,
    input  wire scl,
    input  wire sda,
    output reg  sda_low = 1'b0,         // 1: pull SDA low
    output reg  wr = 1'b0,              // for one cycle: wr_data goes to register wr_addr
    output reg  [7:0] wr_addr = 8'd0,
    output reg  [7:0] wr_data = 8'd0,
    output reg  rd_begin = 1'b0,        // for one cycle: a read has been addressed (copy what must be read as one)
    output wire [7:0] rd_addr,          // the register a read is at ...
    input  wire [7:0] rd_data,          // ... and what it holds
    output reg  [15:0] hits = 16'd0     // how often the address has matched
);
    reg [1:0] scl_m = 2'b11, sda_m = 2'b11;
    reg scl_f = 1'b1, sda_f = 1'b1, scl_d = 1'b1, sda_d = 1'b1;
    reg [7:0] scl_n = 8'd0, sda_n = 8'd0;
    always @(posedge clk) begin
        scl_m <= {scl_m[0], scl};
        sda_m <= {sda_m[0], sda};
        if (scl_m[1] == scl_f)
            scl_n <= 8'd0;
        else if (scl_n == FILTER - 1) begin
            scl_f <= scl_m[1];
            scl_n <= 8'd0;
        end else
            scl_n <= scl_n + 1'b1;
        if (sda_m[1] == sda_f)
            sda_n <= 8'd0;
        else if (sda_n == FILTER - 1) begin
            sda_f <= sda_m[1];
            sda_n <= 8'd0;
        end else
            sda_n <= sda_n + 1'b1;
        scl_d <= scl_f;
        sda_d <= sda_f;
    end
    wire scl_rise = scl_f & ~scl_d;
    wire scl_fall = ~scl_f & scl_d;
    wire start = scl_f & scl_d & ~sda_f & sda_d;       // SDA falls while SCL is high
    wire stop  = scl_f & scl_d & sda_f & ~sda_d;       // SDA rises while SCL is high

    localparam [2:0] IDLE = 3'd0, ADDR = 3'd1, ACK_A = 3'd2, WBYTE = 3'd3, WACK = 3'd4, RBYTE = 3'd5, RACK = 3'd6;
    reg [2:0] st = IDLE;
    reg [3:0] n = 4'd0;
    reg [7:0] sh = 8'd0, ptr = 8'd0;
    reg rw = 1'b0, first = 1'b0, nack = 1'b0;
    assign rd_addr = ptr;

    always @(posedge clk) begin
        wr <= 1'b0;
        rd_begin <= 1'b0;
        if (start) begin
            st <= ADDR;
            n <= 4'd0;
            sda_low <= 1'b0;
        end else if (stop) begin
            st <= IDLE;
            sda_low <= 1'b0;
        end else case (st)
        ADDR, WBYTE:                    // eight bits in, then the acknowledge is put out as SCL falls
            if (scl_rise) begin
                if (n != 4'd8) begin
                    sh <= {sh[6:0], sda_f};
                    n <= n + 1'b1;
                end
            end else if (scl_fall && n == 4'd8) begin
                if (st == WBYTE) begin
                    sda_low <= 1'b1;
                    st <= WACK;
                    if (first) begin
                        ptr <= sh;
                        first <= 1'b0;
                    end else begin
                        wr <= 1'b1;
                        wr_addr <= ptr;
                        wr_data <= sh;
                        ptr <= ptr + 1'b1;
                    end
                end else if (sh[7:1] == ADDRESS) begin
                    sda_low <= 1'b1;
                    st <= ACK_A;
                    rw <= sh[0];
                    first <= 1'b1;
                    rd_begin <= sh[0];
                    hits <= hits + 1'b1;
                end else
                    st <= IDLE;
            end
        ACK_A:
            if (scl_fall) begin
                n <= 4'd0;
                if (rw) begin
                    st <= RBYTE;
                    sh <= rd_data;
                    sda_low <= ~rd_data[7];
                end else begin
                    st <= WBYTE;
                    sda_low <= 1'b0;
                end
            end
        WACK:
            if (scl_fall) begin
                st <= WBYTE;
                n <= 4'd0;
                sda_low <= 1'b0;
            end
        RBYTE:                          // bit 7 is out; each falling edge puts out the next, the eighth lets go
            if (scl_fall) begin
                if (n == 4'd7) begin
                    sda_low <= 1'b0;
                    st <= RACK;
                end else begin
                    sh <= {sh[6:0], 1'b0};
                    sda_low <= ~sh[6];
                    n <= n + 1'b1;
                end
            end
        RACK:                           // the master's acknowledge: another byte, or the end
            if (scl_rise) begin
                nack <= sda_f;
                if (!sda_f)
                    ptr <= ptr + 1'b1;
            end else if (scl_fall) begin
                if (nack)
                    st <= IDLE;
                else begin
                    st <= RBYTE;
                    n <= 4'd0;
                    sh <= rd_data;
                    sda_low <= ~rd_data[7];
                end
            end
        default: ;
        endcase
    end
endmodule
