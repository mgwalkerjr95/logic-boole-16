# The two files the software loads into the board

They are built from other parts of this repository, and each comes under the terms of what it is built from, not
under the host software's GPL. `firmware.img` is not in the repository: the software downloads it from the
repository's firmware-2 release and keeps it on the computer, or you build it and `make install` puts it here.

| File | Built from | Terms |
|---|---|---|
| `gateware.bin` | The FPGA design in `gateware/`, with Xilinx ISE 14.7 | CERN-OHL-S v2. Copyright (C) 2026 Michael Walker |
| `firmware.img` | `firmware/firmware.c`, with Infineon's EZ-USB FX3 SDK 1.3.5 | `firmware.c`: MIT, Copyright (c) 2026 Michael Walker. The image also holds code of the SDK's libraries, which is Infineon's and stays under the license of Infineon's SDK |

`gateware/README.md` and `firmware/README.md` say how each is built.
