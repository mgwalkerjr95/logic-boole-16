# Firmware

The firmware for the Infineon EZ-USB FX3 (CYUSB3014), the USB controller of the Logic Boole 16.

| File | What it is |
|---|---|
| `firmware.c` | The firmware, in one source file |
| `Makefile` | Builds it against Infineon's FX3 SDK |

## What it does

- **Loads the FPGA.** The host software sends the FPGA design over USB and the firmware shifts it into the FPGA
  (slave serial configuration).
- **Carries the samples.** The FPGA writes 32-bit words into the FX3's synchronous slave FIFO interface. A DMA
  channel passes them to a USB bulk endpoint, with no code in the data path.
- **Starts and stops a capture** with the RUN line to the FPGA.
- **Reads and writes the FPGA's registers** over I2C for the host software. The settings of a capture (format,
  sample rate, number of samples, thresholds) are registers in the FPGA.
- **Reports its status**: the state of the pins, of the FPGA and of the stream, and the error counts of the USB
  link.
- **Names the board on USB**: "Logic Boole 16", with a serial number made from the FX3's own ID, so that the
  computer can tell one board from another.

The host software drives all of this with vendor requests on endpoint 0. The requests, the status and the format
of the sample stream are described in [../docs/protocol.md](../docs/protocol.md). The comment at the top of
`firmware.c` lists the FX3's pins and the requests.

## How it gets onto the board

The board has no flash memory for the FX3: the position for one, U13, is left empty. When the board is plugged in,
the FX3 starts in the boot loader that is built into the chip and waits for the computer. The host software then
loads `firmware.img` into the FX3's RAM over USB and starts it. This happens every time the board is plugged in.

Nothing is written to the board, so a bad build cannot brick it. Unplug the board and plug it in again, and the
FX3 is back in its boot loader.

The firmware also returns to the boot loader by itself: when the host software asks for it, when the computer
sends no request in the first 30 seconds, and when the firmware stops running (the FX3's watchdog, 8 seconds).

## Building

A built image, made from this `firmware.c` with the SDK and the toolchain named below, is a file of the
repository's [firmware-2 release](https://github.com/mgwalkerjr95/logic-boole-16/releases/tag/firmware-2): the host software downloads it once, checks it and keeps it. It is not
in the repository itself, because it holds code of Infineon's SDK. Building is only needed after a change to
`firmware.c`, or to make the image yourself.

### What is needed

**Infineon's EZ-USB FX3 SDK, version 1.3.5.** Download the Linux package from Infineon's
[EZ-USB FX3 Software Development Kit](https://www.infineon.com/design-resources/development-tools/sdk/usb-controllers-sdk/ez-usb-fx3-software-development-kit)
page. It is used on macOS too: only its firmware part is needed, the archive `fx3_firmware_linux.tar.gz` inside
the download. Unpacked, it is a folder named `cyfx3sdk`.

    mkdir ~/fx3-sdk-1.3.5
    tar -xzf ezusbfx3sdk_1.3.5_Linux_x32-x64.tar.gz fx3_firmware_linux.tar.gz
    tar -xzf fx3_firmware_linux.tar.gz -C ~/fx3-sdk-1.3.5

The SDK is Infineon's and comes under Infineon's license. None of its files is in this repository: the Makefile
takes its headers, libraries, start-up code, linker script and image converter from where the SDK is installed.
An image built with it holds code of its libraries.

**Arm's GNU toolchain for arm-none-eabi.** Download the "AArch32 bare-metal target (arm-none-eabi)" package for
your computer from Arm's [GNU toolchain downloads](https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads)
and unpack it. The firmware was built with release 15.2.rel1.

**GNU make and a C compiler** (`cc`) for the computer itself. The C compiler builds the SDK's image converter.
The build has been run on macOS; the Makefile uses nothing that Linux does not have.

### Where the Makefile looks

| Variable | Default | What it is |
|---|---|---|
| `FX3FWROOT` | `$(HOME)/fx3-sdk-1.3.5/cyfx3sdk` | The SDK's `cyfx3sdk` folder |
| `ARMGCC_INSTALL_PATH` | `$(HOME)/.local/opt/arm-gnu-toolchain-15.2.rel1-darwin-arm64-arm-none-eabi` | The toolchain's folder, the one that holds `bin/arm-none-eabi-gcc`. The default is Arm's package for macOS on Apple silicon, unpacked in `~/.local/opt` |
| `ARMGCC_VERSION` | `15.2.1` | The toolchain's GCC version: the name of the folder in `lib/gcc/arm-none-eabi` |

Set them on the command line, or in the environment, if the SDK or the toolchain is somewhere else:

    make ARMGCC_INSTALL_PATH=$HOME/arm-gnu-toolchain-15.2.rel1-x86_64-arm-none-eabi

### Build

    make

This compiles `firmware.c` and the SDK's start-up files, links them with the SDK's libraries, and converts the
result to `build/firmware.img`, the image that the FX3's boot loader takes. `make clean` removes `build/`.

### Install

    make install

This copies `build/firmware.img` to `../software/logicboole/data/firmware.img`. An image there is used in place
of the downloaded one, as it is, the next time the board is plugged in. Git ignores that file. Remove it to go
back to the released image.

The released image is named in `../software/logicboole/board.py`: `FIRMWARE_URL`, `FIRMWARE_SHA256` and
`FIRMWARE_VERSION`. A new release of the firmware changes those three.

### Trying a build

To run a build on the board without installing it:

    ../logicboole load --firmware build/firmware.img

This sends a running firmware back to the boot loader, loads the image given, and loads the FPGA again. Then
`../logicboole selftest` checks the whole path from the FPGA to the computer with the FPGA's test pattern, every
word compared.

## License

Copyright (c) 2026 Michael Walker. `firmware.c` and the `Makefile` are under the MIT license: see
[LICENSE](LICENSE).

That covers this folder's files. An image built from them also holds code of the libraries of Infineon's SDK,
which is Infineon's and stays under the license of Infineon's SDK.
