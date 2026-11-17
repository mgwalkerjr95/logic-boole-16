#!/bin/bash
# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# Builds the FPGA's bitstream with ISE 14.7 on Linux (x86-64): xst, ngdbuild, map, par, trce, bitgen.
#
#   ./build.sh                                              ISE installed in /opt/Xilinx
#   ISE_SETTINGS=/somewhere/14.7/ISE_DS/settings64.sh ./build.sh
#
# Everything is written to build/, which is emptied first:
#   capture2.bin        the bitstream that the FX3 loads into the FPGA
#   capture2.bit        the same with ISE's header in front
#   capture2_jtag.bit   the same routed design for loading over JTAG: its start-up sequence runs on TCK, because
#                       nothing clocks CCLK then
#   capture2.syr, .bld, _map.mrp, .par, .twr, _pad.txt      the tools' reports
#   build.log           everything the tools printed
# The build stops at the first tool that fails, and before bitgen if the timing report shows a timing error.
set -e
cd "$(dirname "$0")"
ISE_SETTINGS=${ISE_SETTINGS:-/opt/Xilinx/14.7/ISE_DS/settings64.sh}
PART=xc6slx9-2-tqg144

[ -f "$ISE_SETTINGS" ] || { echo "build.sh: $ISE_SETTINGS not found: set ISE_SETTINGS to ISE 14.7's settings64.sh" >&2; exit 1; }
. "$ISE_SETTINGS" > /dev/null
command -v xst > /dev/null || { echo "build.sh: xst is not on the PATH after reading $ISE_SETTINGS" >&2; exit 1; }

rm -rf build
mkdir build
cd build

# The sources, the constraints (the board's pins, then what this design adds to them) and xst's options.
for f in ../capture2_top.v ../i2c_slave.v; do echo "verilog work $f"; done > capture2.prj
cat ../pins.ucf ../capture2.ucf > all.ucf
echo "run -ifn capture2.prj -ofn capture2 -ofmt NGC -p $PART -top capture2_top" > capture2.xst

step() {    # step COMMAND...: runs it with its messages added to build.log
    echo "$1 ..."
    "$@" >> build.log 2>&1 || { tail -n 30 build.log; echo "build.sh: $1 failed: see build/build.log" >&2; exit 1; }
}
step xst -ifn capture2.xst -ofn capture2.syr
step ngdbuild -p $PART -uc all.ucf capture2.ngc capture2.ngd
step map -p $PART -w -o capture2_map.ncd capture2.ngd capture2.pcf
step par -w capture2_map.ncd capture2.ncd capture2.pcf
step trce -v 10 -u 10 -o capture2.twr capture2.ncd capture2.pcf
grep -q '^Timing errors: 0 ' capture2.twr || {
    grep '^Timing errors:' capture2.twr || true
    echo "build.sh: the design does not meet its timing constraints: see build/capture2.twr.  No bitstream was made" >&2
    exit 1
}
step bitgen -w -g Binary:Yes capture2.ncd capture2.bit capture2.pcf
step bitgen -w -g StartUpClk:JtagClk capture2.ncd capture2_jtag.bit capture2.pcf

grep '^Timing errors:' capture2.twr
echo "The 100 MHz clock (10 ns):$(sed -n '/^Timing constraint: TS_clk_pll/,/Minimum period is/p' capture2.twr | tail -n 1)"
echo "build/capture2.bin: $(wc -c < capture2.bin | tr -d ' ') bytes"
