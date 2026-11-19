#!/bin/sh
# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# Simulates fast_top.v with Icarus Verilog against a model of the FX3's slave FIFO and an I2C master: prints PASS
# or FAIL.  The whole test runs once.  Its first two captures are then repeated with the FX3's flag from 0 to 8
# cycles late and its edge anywhere in the clock cycle, because the design must not depend on either.  The last run
# shortens the design's hold-off to less than the flag's delay: the model must then report overruns, which shows
# that it would catch them.  The compiled test benches and the log of every run go to sim/build/.  A few minutes
# on a computer with eight cores; "sh sim_fast.sh 1" runs one at a time.
#
# The pads' deserializers, the second PLL and its clock buffer are sim/xilinx_stubs.v's models of them, not
# Xilinx's: what this shows is the design around them.
set -eu
cd "$(dirname "$0")"
command -v iverilog > /dev/null && command -v vvp > /dev/null || { echo "sim_fast.sh: Icarus Verilog (iverilog, vvp) is not installed"; exit 1; }
mkdir -p sim/build
rm -f sim/build/fast_run_*
LOG=sim/build/fast_tb.log
AT_ONCE=${1:-$(getconf _NPROCESSORS_ONLN 2> /dev/null || echo 2)}
n=0
run() {     # run WANT PARAMETERS...   in the background; what it said goes to sim/build/fast_run_N.txt
    n=$((n + 1))
    (
        k=$n; want=$1; shift
        args=""
        for p in "$@"; do args="$args -Pfast_tb.$p"; done
        iverilog -g2005 -s fast_tb $args -o sim/build/fast_run_$k.vvp sim/xilinx_stubs.v i2c_slave.v fast_top.v \
            sim/fast_tb.v 2>&1 | { grep -v 'coerced to inout' || true; }     # the pull-ups on inputs
        out=$(vvp -n sim/build/fast_run_$k.vvp | grep -v 'finish called')
        { echo "== $*"; echo "$out"; } > sim/build/fast_run_$k.log
        last=$(echo "$out" | tail -1)
        if [ "$last" = "$want" ]; then
            echo "$want as expected: $*  ($(echo "$out" | tail -2 | head -1))" > sim/build/fast_run_$k.txt
        else
            { echo "$out" | tail -25; echo "FAIL: with $* the test bench said $last, not $want"; } > sim/build/fast_run_$k.txt
        fi
    ) &
    [ $((n % AT_ONCE)) -ne 0 ] || wait
}
run PASS LAT=3 FLAG_DELAY=7.0
for lat in 0 1 2 4 6 8; do
    for delay in 0.6 3.0 7.0 9.4 13.0; do
        run PASS LAT=$lat FLAG_DELAY=$delay QUICK=1
    done
done
run FAIL LAT=6 FLAG_DELAY=7.0 HOLD=2 QUICK=1
wait
: > "$LOG"
failed=0
k=1
while [ $k -le $n ]; do
    cat sim/build/fast_run_$k.log >> "$LOG"
    cat sim/build/fast_run_$k.txt
    ! grep -q '^FAIL: with' sim/build/fast_run_$k.txt || failed=1
    k=$((k + 1))
done
rm -f sim/build/fast_run_*
[ $failed -eq 0 ] || exit 1
echo PASS
