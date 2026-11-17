# Copyright (c) 2026 Michael Walker
# SPDX-License-Identifier: MIT
#
# Builds the FX3's firmware for the Logic Boole 16:
#
#     make            builds build/firmware.img
#     make install    copies it to ../software/logicboole/data/firmware.img, where the host software takes it from
#     make clean      removes build/
#
# Needs Infineon's EZ-USB FX3 SDK 1.3.5 (the "fx3_firmware_linux" part of the Linux download, unpacked) and Arm's
# GNU toolchain for arm-none-eabi: see README.md.  The SDK's start-up code, its ThreadX port file and its image
# converter are compiled from where the SDK is installed: nothing of it is copied into this repository.
#
# Three variables say where the two are.  Set them on the command line or in the environment if the defaults
# below do not fit:
#
#     make FX3FWROOT=/opt/cyfx3sdk ARMGCC_INSTALL_PATH=/opt/arm-gnu-toolchain ARMGCC_VERSION=15.2.1

# The SDK's folder "cyfx3sdk", the one that holds fw_build, fw_lib, firmware and util.
FX3FWROOT ?= $(HOME)/fx3-sdk-1.3.5/cyfx3sdk

# The toolchain's folder, the one that holds bin/arm-none-eabi-gcc.  The default is where Arm's 15.2.rel1 package
# for macOS on Apple silicon unpacks to under $(HOME)/.local/opt.
ARMGCC_INSTALL_PATH ?= $(HOME)/.local/opt/arm-gnu-toolchain-15.2.rel1-darwin-arm64-arm-none-eabi

# The toolchain's GCC version: the name of the one folder in $(ARMGCC_INSTALL_PATH)/lib/gcc/arm-none-eabi, where
# libgcc.a is taken from.
ARMGCC_VERSION ?= 15.2.1

CYCONFOPT = fx3_release
export ARMGCC_INSTALL_PATH
export PATH := $(ARMGCC_INSTALL_PATH)/bin:$(PATH)

OUT = build
MODULE = $(OUT)/firmware
SDK = $(FX3FWROOT)/fw_build/fx3_fw
INSTALL_DIR = ../software/logicboole/data

all: $(MODULE).img

# "make clean" needs neither the SDK nor the toolchain
ifneq ($(MAKECMDGOALS),clean)
ifeq ($(wildcard $(SDK)/fx3_build_config.mak),)
$(error No FX3 SDK at FX3FWROOT = $(FX3FWROOT): see README.md)
endif
ifeq ($(wildcard $(ARMGCC_INSTALL_PATH)/lib/gcc/arm-none-eabi/$(ARMGCC_VERSION)/libgcc.a),)
$(error No arm-none-eabi GCC $(ARMGCC_VERSION) at ARMGCC_INSTALL_PATH = $(ARMGCC_INSTALL_PATH): see README.md)
endif

# the SDK's compiler and linker settings; they define COMPILE, ASSEMBLE and LINK
include $(SDK)/fx3_build_config.mak
endif

$(OUT):
	mkdir -p $(OUT)

# the slave FIFO interface's description is the SDK's, included from its example's folder
$(OUT)/firmware.o: USER_CFLAGS = -Wextra -Werror -I$(FX3FWROOT)/firmware/slavefifo_examples/slfifosync
$(OUT)/firmware.o: firmware.c | $(OUT)
	$(COMPILE)

$(OUT)/cyfxtx.o: $(SDK)/cyfxtx.c | $(OUT)
	$(COMPILE)

$(OUT)/cyfx_gcc_startup.o: $(SDK)/cyfx_gcc_startup.S | $(OUT)
	$(ASSEMBLE)

$(MODULE).elf: $(OUT)/cyfx_gcc_startup.o $(OUT)/firmware.o $(OUT)/cyfxtx.o
	$(LINK)

# the SDK's converter from the linked program to an image for the FX3's boot loader, built with this computer's cc
$(OUT)/elf2img: $(FX3FWROOT)/util/elf2img/elf2img.c | $(OUT)
	cc -O2 -w -o $@ $<

$(MODULE).img: $(MODULE).elf $(OUT)/elf2img
	$(OUT)/elf2img -i $< -o $@

install: $(MODULE).img
	mkdir -p $(INSTALL_DIR)
	cp $(MODULE).img $(INSTALL_DIR)/firmware.img

clean:
	rm -rf $(OUT)

.PHONY: all install clean
