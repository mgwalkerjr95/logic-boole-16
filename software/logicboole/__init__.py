# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""Host software for the Logic Boole 16, a 16-channel logic analyzer on USB 3.

    import logicboole

    with logicboole.connect() as board:            # loads the board if it has just been plugged in
        board.set_thresholds(1.65, 0.9)            # volts: pod A, pod B
        cap = board.capture(rate=10e6, seconds=2)

    if cap.ok:
        cap.save("bus.sr")                         # for PulseView; or .vcd, .csv, .lb16

The same from the command line: "logicboole capture bus.sr --rate 10M --time 2 -a 1.65 -b 0.9".
"""
from .board import Board, BoardError, connect, look
from .capture import CHANNELS, Capture

__version__ = "1.1.0"
__all__ = ["Board", "BoardError", "Capture", "CHANNELS", "connect", "look"]
