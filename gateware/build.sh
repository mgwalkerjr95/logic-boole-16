#!/bin/bash
# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# Builds an FPGA bitstream with ISE 14.7 on Linux (x86-64): xst, ngdbuild, map, par, trce, bitgen.
#
#   ./build.sh                                              the design the board is used with: capture2_top.v
#   ./build.sh fast                                         the fast design: fast_top.v
#   ISE_SETTINGS=/somewhere/14.7/ISE_DS/settings64.sh ./build.sh        ISE not installed in /opt/Xilinx
#
# Everything is written to build/ (the fast design: build-fast/), which is emptied first.  With NAME capture2 or
# fast:
#   NAME.bin        the bitstream that the FX3 loads into the FPGA
#   NAME.bit        the same with ISE's header in front
#   NAME_jtag.bit   the same routed design for loading over JTAG: its start-up sequence runs on TCK, because
#                   nothing clocks CCLK then
#   NAME.syr, .bld, _map.mrp, .par, .twr, _pad.txt      the tools' reports
#   build.log       everything the tools printed
# The build stops at the first tool that fails, and before bitgen if the timing report shows a timing error.
set -e
cd "$(dirname "$0")"
ISE_SETTINGS=${ISE_SETTINGS:-/opt/Xilinx/14.7/ISE_DS/settings64.sh}
PART=xc6slx9-2-tqg144
NAME=${1:-capture2}
case "$NAME" in
    capture2) OUT=build ;;
    fast) OUT=build-fast ;;
    *) echo "build.sh: the designs are capture2 (the default) and fast" >&2; exit 1 ;;
esac

[ -f "$ISE_SETTINGS" ] || { echo "build.sh: $ISE_SETTINGS not found: set ISE_SETTINGS to ISE 14.7's settings64.sh" >&2; exit 1; }
set --                                  # settings64.sh takes this script's argument for its own otherwise
. "$ISE_SETTINGS" > /dev/null
command -v xst > /dev/null || { echo "build.sh: xst is not on the PATH after reading $ISE_SETTINGS" >&2; exit 1; }

rm -rf $OUT
mkdir $OUT
cd $OUT

# The sources, the constraints (the board's pins, then what this design adds to them) and xst's options.
for f in ../${NAME}_top.v ../i2c_slave.v; do echo "verilog work $f"; done > $NAME.prj
cat ../pins.ucf ../$NAME.ucf > all.ucf
echo "run -ifn $NAME.prj -ofn $NAME -ofmt NGC -p $PART -top ${NAME}_top" > $NAME.xst

step() {    # step COMMAND...: runs it with its messages added to build.log
    echo "$1 ..."
    "$@" >> build.log 2>&1 || { tail -n 30 build.log; echo "build.sh: $1 failed: see $OUT/build.log" >&2; exit 1; }
}
step xst -ifn $NAME.xst -ofn $NAME.syr
step ngdbuild -p $PART -uc all.ucf $NAME.ngc $NAME.ngd
step map -p $PART -w -o ${NAME}_map.ncd $NAME.ngd $NAME.pcf
step par -w ${NAME}_map.ncd $NAME.ncd $NAME.pcf
step trce -v 10 -u 10 -o $NAME.twr $NAME.ncd $NAME.pcf
grep -q '^Timing errors: 0 ' $NAME.twr || {
    grep '^Timing errors:' $NAME.twr || true
    echo "build.sh: the design does not meet its timing constraints: see $OUT/$NAME.twr.  No bitstream was made" >&2
    exit 1
}
step bitgen -w -g Binary:Yes $NAME.ncd $NAME.bit $NAME.pcf
step bitgen -w -g StartUpClk:JtagClk $NAME.ncd ${NAME}_jtag.bit $NAME.pcf

grep '^Timing errors:' $NAME.twr
if [ "$NAME" = capture2 ]; then
    echo "The 100 MHz clock (10 ns):$(sed -n '/^Timing constraint: TS_clk_pll/,/Minimum period is/p' $NAME.twr | tail -n 1)"
else        # each clock and each crossing, with the shortest period or the longest delay the routed design has
    grep -E '^Timing constraint: |Minimum period is|Maximum delay is' $NAME.twr
fi
echo "$OUT/$NAME.bin: $(wc -c < $NAME.bin | tr -d ' ') bytes"
