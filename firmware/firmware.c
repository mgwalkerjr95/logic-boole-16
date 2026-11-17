/*
 * Copyright (c) 2026 Michael Walker
 * SPDX-License-Identifier: MIT
 *
 * Firmware for the FX3 on the Logic Boole 16, revision C.  The host software loads it into the FX3's RAM over USB
 * every time the board is plugged in; nothing is written to the board.  What a program on the computer needs to
 * know of it is in docs/protocol.md.
 *
 * Built against Infineon's EZ-USB FX3 SDK 1.3.5, which is licensed by Infineon and is not part of this repository:
 * the Makefile takes its headers, libraries, start-up code and linker script from where the SDK is installed.  The
 * slave FIFO interface is the SDK's own description of it, cyfxgpif_syncsf.h of its example
 * slavefifo_examples/slfifosync, included from there.
 *
 * The FPGA samples its 16 inputs, makes the bus clock PCLK, 100 MHz, and writes 32-bit words into the FX3's
 * synchronous slave FIFO, thread 0.  A DMA channel passes them to bulk endpoint 1 IN with no code in between.
 * The FPGA counts the words it writes into each of the FX3's buffers and waits for FLAGA (CTL4) before the next
 * buffer, so the buffer's size (request C2) and the FPGA's register 20 must agree.  The register starts at 4096
 * words, 16 KB.  The settings of a capture (format, sample rate, how many samples, the thresholds) are registers in
 * the FPGA, which this firmware reads and writes as the master of the I2C bus between the two chips, at 100 kHz.
 *
 *   GPIO 0-15, 33-44, 46-49   DQ0-31       the GPIF block's; the FX3 never drives them (SLOE# stays high)
 *   GPIO 16                   PCLK         in
 *   GPIO 17-20, 24, 28, 29    SLCS#, SLWR#, SLOE#, SLRD#, PKTEND#, A1, A0 (CTL0-3, 7, 11, 12)   in
 *   GPIO 21, 22               FLAGA, FLAGB (CTL4, CTL5)   out once the interface is started
 *   GPIO 23                   RUN  (CTL6)  out, a GPIO taken from the GPIF block: high = the FPGA captures
 *   GPIO 26                   PROGRAM_B    input, or pulled low; never driven high (4.7k pull-up to 1.8 V)
 *   GPIO 27                   DONE         input
 *   GPIO 45                   INIT_B       input
 *   GPIO 50, 51               CCLK, DIN    outputs only between "configure: begin" and "configure: end"
 *   GPIO 58, 59               SCL, SDA     the I2C block's
 *   GPIO 55                   UART TX      115200 baud, on J3 pin 3 (not fitted): a few status lines
 *
 * USB: vendor ID 04B4, product ID 00F1 (the IDs that Infineon's SDK examples use), one vendor interface with one
 * bulk endpoint, 1 IN (bursts of 16 packets at 5 Gbit/s).  The device names itself "Logic Boole 16", made by
 * "Logic Boole", and its serial number is the number that Infineon puts into each FX3, as 16 hexadecimal digits
 * (see serial_number() below).  Vendor requests to the device, on endpoint 0:
 *   B0 in    64 bytes of status (layout at info() below)
 *   B3 out   configure: begin (the interface and the stream are stopped first).  PROGRAM_B low for 2 ms, then
 *            waits for INIT_B to go high
 *   B4 out   configure: 1 ... 4096 bytes of the FPGA image, shifted out on DIN and CCLK, each byte's bit 7 first
 *   B5 out   configure: end.  Clocks on until DONE is high, then 128 clocks more for the FPGA's start-up
 *   B6 out   wValue = 0: reconnect as a USB 2.0 device; 1: reconnect allowing 5 Gbit/s
 *   B7 out   back to the FX3's boot loader (a full reset)
 *   B8 in    258 bytes: the USB driver's event log.  The place in it where the driver writes next (16 bits), then
 *            the log's 256 bytes
 *   B9 in    16 bytes about the 5 Gbit/s link (layout at link_info() below).  Reading it clears the error counts
 *   BA in / out   1 ... 4096 bytes of test data on endpoint 0, wValue = the seed (see pattern() below).  In: the
 *            pattern is sent.  Out: what arrives is compared with the pattern, and B9 counts the bytes that differ
 *   C0 out   start the interface: the parallel port's block, the slave FIFO description, its state machine
 *   C1 out   stop the interface (the stream first)
 *   C2 out   start the stream: wValue = a buffer's size in KB (1 ... 63), wIndex = how many buffers.  Starts the
 *            interface afresh (as C1, C0) and makes the DMA channel; the error counts start again.  RUN stays low
 *   C3 out   stop the stream: RUN low, a wait of up to 0.1 s for the FPGA to stop, then the channel and what it
 *            held are thrown away
 *   C4 out   wValue bit 0 = RUN.  RUN is only raised while the stream is started
 *   C5 out   the end of the data, with RUN low: waits (up to 0.1 s) until the FPGA has sent what it held, then
 *            the buffer that the FPGA left part-filled is passed on as it is, then an empty one, so that the
 *            computer's waiting read ends with a short or an empty packet and every word that the FPGA wrote has
 *            been read
 *   D0 out   write the FPGA's registers: wValue = the first register, then 1 ... 64 bytes
 *   D1 in    read the FPGA's registers: wValue = the first register, wLength = 1 ... 64 bytes
 * A request that fails is answered with a stall; the status then holds the SDK's status of what failed.
 *
 * Time is counted in the operating system's ticks.  A tick is 32 cycles of the FX3's 32.768 kHz clock, 0.977 ms.
 *
 * It returns to the boot loader by itself if the computer sends no request in the first 30 seconds, if USB cannot be
 * started (after 15 seconds), or if it stops running (the FX3's watchdog, 8 seconds).
 */
#include <cyu3system.h>
#include <cyu3os.h>
#include <cyu3error.h>
#include <cyu3usb.h>
#include <cyu3usbconst.h>
#include <cyu3uart.h>
#include <cyu3gpio.h>
#include <cyu3utils.h>
#include <cyu3dma.h>
#include <cyu3pib.h>
#include <cyu3gpif.h>
#include <cyu3i2c.h>
#include <gpio_regs.h>

/* The SDK's description of the synchronous slave FIFO interface, in its 32-bit form.  It defines its tables, so it
 * is included once, here. */
#define CY_FX_SLFIFO_GPIF_16_32BIT_CONF_SELECT 1
#include "cyfxgpif_syncsf.h"

#define VERSION             2       /* 2: the device's name and its serial number */
#define LOG_SIZE            256
#define STACK               0x1000
#define PRIORITY            8
#define EVENT_SETUP         1
#define EVENT_DOWN          2       /* the connection was reset or lost: the stream has to go */
#define WATCHDOG_MS         8000
#define FIRST_REQUEST_MS    30000   /* in ticks: 29 s */
#define EP_IN               0x81
#define WATERMARK           6       /* words; FLAGB only, which the FPGA does not use */

#define GPIO_RUN            23
#define GPIO_PROG_B         26
#define GPIO_DONE           27
#define GPIO_INIT_B         45
#define GPIO_CCLK           50
#define GPIO_DIN            51

#define FPGA_I2C            0x2A    /* the 7-bit address of the FPGA's registers on the I2C bus */
#define REG_VERSION         0x03
#define REG_STATUS          0x05    /* bits 7:5: the writer's state, 0 = off */

/* pin modes */
#define PIN_IN              0
#define PIN_LOW             1       /* only the low-side driver */
#define PIN_HIGH            2       /* only the high-side driver */
#define PIN_OUT_LOW         3       /* both drivers, for a pin that then changes level */
#define PIN_OUT_HIGH        4

/* a simple GPIO's register with both drivers and the input stage on; bit 0 is the level driven */
#define REG_OUT (CY_U3P_LPP_GPIO_ENABLE | CY_U3P_LPP_GPIO_DRIVE_LO_EN | CY_U3P_LPP_GPIO_DRIVE_HI_EN | \
                 CY_U3P_LPP_GPIO_INPUT_EN)

/* configuration flags in the status */
#define CFG_INIT_LOW        0x01    /* INIT_B was low while PROGRAM_B was */
#define CFG_DONE_LOW        0x02    /* DONE was low while PROGRAM_B was */
#define CFG_READY           0x04    /* INIT_B went high after PROGRAM_B was released: the FPGA waits for data */
#define CFG_INIT_FELL       0x08    /* INIT_B and DONE were low after a block: the FPGA found a CRC error */
#define CFG_DONE            0x10    /* DONE went high */
#define CFG_ENDED           0x20    /* "configure: end" has run */

#define POST_USB_START      0x01
#define POST_DESCRIPTOR     0x02
#define POST_CONNECT        0x03

static CyU3PThread thread;
static CyU3PEvent event;
static volatile uint32_t setup0, setup1;        /* the request the thread is to handle */
static uint8_t ep0[4096] __attribute__ ((aligned (32)));
static uint8_t usb_log[LOG_SIZE];
static CyU3PDmaChannel channel;

static CyBool_t heard;                          /* the computer has sent a request */
static CyBool_t configured;                     /* the host has selected the configuration */
static CyBool_t gpio_ok, gpif_on, channel_on, run_on;
static uint32_t st_matrix, st_gpio, st_uart, st_pin, st_last, st_ep, st_i2c;
static uint8_t pin_failed, pin_failures;
static uint16_t ev_reset, ev_linkfail, ev_recovery;
static uint32_t cfg_flags, cfg_bytes, cfg_wait, cfg_clocks, cfg_ms;
static uint32_t data_bad;                       /* bytes of request BA's test data that arrived wrong */
static volatile uint32_t pib_errors;            /* what the parallel port's block reported since the stream began */
static volatile uint16_t pib_first, pib_last;
static uint16_t buf_kb, buf_count;

/* ---- USB descriptors ---------------------------------------------------------------------------------------- */

#define SERIAL_AT           16      /* where a device descriptor names its serial number's string */
#define SERIAL_STRING       3

/* The two device descriptors are not constant: usb_start() puts the serial number's string into them if the chip
 * gives a serial number. */
static uint8_t dscr_device_ss[] __attribute__ ((aligned (32))) = {
    0x12, CY_U3P_USB_DEVICE_DESCR,
    0x00, 0x03,                     /* USB 3.0 */
    0x00, 0x00, 0x00,               /* class, subclass, protocol: see the interface */
    0x09,                           /* endpoint 0: 2^9 = 512 bytes */
    0xB4, 0x04, 0xF1, 0x00,         /* 04B4:00F1 */
    VERSION, 0x03,                  /* device release 3.xx, where xx is VERSION */
    0x01, 0x02, 0x00,               /* strings: manufacturer, product, and no serial number until usb_start() */
    0x01                            /* one configuration */
};

static uint8_t dscr_device_hs[] __attribute__ ((aligned (32))) = {
    0x12, CY_U3P_USB_DEVICE_DESCR,
    0x10, 0x02,                     /* USB 2.1: the host may ask for the BOS descriptor */
    0x00, 0x00, 0x00,
    0x40,                           /* endpoint 0: 64 bytes */
    0xB4, 0x04, 0xF1, 0x00,
    VERSION, 0x03,
    0x01, 0x02, 0x00,
    0x01
};

static const uint8_t dscr_bos[] __attribute__ ((aligned (32))) = {
    0x05, CY_U3P_BOS_DESCR, 0x16, 0x00, 0x02,
    0x07, CY_U3P_DEVICE_CAPB_DESCR, CY_U3P_USB2_EXTN_CAPB_TYPE,
    0x02, 0x00, 0x00, 0x00,         /* link power management */
    0x0A, CY_U3P_DEVICE_CAPB_DESCR, CY_U3P_SS_USB_CAPB_TYPE,
    0x00,
    0x0E, 0x00,                     /* speeds: full, high, 5 Gbit/s */
    0x03,                           /* everything works from full speed up */
    0x0A,                           /* U1 exit latency, microseconds */
    0xFF, 0x07                      /* U2 exit latency */
};

static const uint8_t dscr_qualifier[] __attribute__ ((aligned (32))) = {
    0x0A, CY_U3P_USB_DEVQUAL_DESCR, 0x00, 0x02, 0x00, 0x00, 0x00, 0x40, 0x01, 0x00
};

/* One vendor interface with one bulk endpoint.  Bus powered, up to 0.5 A. */
static const uint8_t dscr_config_ss[] __attribute__ ((aligned (32))) = {
    0x09, CY_U3P_USB_CONFIG_DESCR, 0x1F, 0x00, 0x01, 0x01, 0x00, 0x80,
    0x3E,                           /* 62 x 8 mA = 496 mA */
    0x09, CY_U3P_USB_INTRFC_DESCR, 0x00, 0x00, 0x01, 0xFF, 0x00, 0x00, 0x00,
    0x07, CY_U3P_USB_ENDPNT_DESCR, EP_IN, CY_U3P_USB_EP_BULK, 0x00, 0x04, 0x00,     /* 1024-byte packets */
    0x06, CY_U3P_SS_EP_COMPN_DESCR, 0x0F, 0x00, 0x00, 0x00                          /* bursts of 16 */
};

static const uint8_t dscr_config_hs[] __attribute__ ((aligned (32))) = {
    0x09, CY_U3P_USB_CONFIG_DESCR, 0x19, 0x00, 0x01, 0x01, 0x00, 0x80,
    0xFA,                           /* 250 x 2 mA = 500 mA */
    0x09, CY_U3P_USB_INTRFC_DESCR, 0x00, 0x00, 0x01, 0xFF, 0x00, 0x00, 0x00,
    0x07, CY_U3P_USB_ENDPNT_DESCR, EP_IN, CY_U3P_USB_EP_BULK, 0x00, 0x02, 0x00      /* 512-byte packets */
};

static const uint8_t dscr_config_fs[] __attribute__ ((aligned (32))) = {
    0x09, CY_U3P_USB_CONFIG_DESCR, 0x19, 0x00, 0x01, 0x01, 0x00, 0x80,
    0xFA,
    0x09, CY_U3P_USB_INTRFC_DESCR, 0x00, 0x00, 0x01, 0xFF, 0x00, 0x00, 0x00,
    0x07, CY_U3P_USB_ENDPNT_DESCR, EP_IN, CY_U3P_USB_EP_BULK, 0x40, 0x00, 0x00      /* 64-byte packets */
};

static const uint8_t dscr_langid[] __attribute__ ((aligned (32))) = {
    0x04, CY_U3P_USB_STRING_DESCR, 0x09, 0x04
};

static const uint8_t dscr_manufacturer[] __attribute__ ((aligned (32))) = {      /* "Logic Boole" */
    0x18, CY_U3P_USB_STRING_DESCR,
    'L', 0, 'o', 0, 'g', 0, 'i', 0, 'c', 0, ' ', 0, 'B', 0, 'o', 0, 'o', 0, 'l', 0, 'e', 0
};

static const uint8_t dscr_product[] __attribute__ ((aligned (32))) = {           /* "Logic Boole 16" */
    0x1E, CY_U3P_USB_STRING_DESCR,
    'L', 0, 'o', 0, 'g', 0, 'i', 0, 'c', 0, ' ', 0, 'B', 0, 'o', 0, 'o', 0, 'l', 0, 'e', 0, ' ', 0, '1', 0, '6', 0
};

/* The serial number, 16 characters: filled in by serial_number(). */
#define SERIAL_DIGITS       16
static uint8_t dscr_serial[2 + 2 * SERIAL_DIGITS] __attribute__ ((aligned (32)));

/* The FX3's die ID: a number that each chip is given when it is made, in two registers of 32 bits, the low half
 * first.  Infineon's SDK does not name them: the address is the one given on Infineon's forum, and the one that
 * other FX3 firmware reads its serial number from (LimeSDR-USB, FreeSRP). */
#define DIE_ID              ((uvint32_t *)0xE0055010)

/* Makes the serial number's string from the die ID, as 16 hexadecimal digits with bits 63-60 first.  CyFalse if
 * the chip gives none (the registers cannot be read, or hold only zeros or only ones): the device then has no
 * serial number, which is better than one that several boards share. */
static CyBool_t
serial_number (void)
{
    static const char digit[] = "0123456789ABCDEF";
    uint32_t id[2] = { 0, 0 };
    uint32_t i;

    if (CyU3PReadDeviceRegisters (DIE_ID, 2, id) != CY_U3P_SUCCESS)
        return CyFalse;
    if (((id[0] == 0) && (id[1] == 0)) || ((id[0] == 0xFFFFFFFF) && (id[1] == 0xFFFFFFFF)))
        return CyFalse;
    dscr_serial[0] = (uint8_t)sizeof (dscr_serial);
    dscr_serial[1] = CY_U3P_USB_STRING_DESCR;
    for (i = 0; i < SERIAL_DIGITS; i++)
    {
        dscr_serial[2 + 2 * i] = (uint8_t)digit[(id[1 - i / 8] >> (28 - 4 * (i % 8))) & 0xF];
        dscr_serial[3 + 2 * i] = 0;
    }
    return CyTrue;
}

/* ---- pins --------------------------------------------------------------------------------------------------- */

static CyU3PReturnStatus_t
pin_mode (uint8_t g, uint8_t mode)
{
    CyU3PGpioSimpleConfig_t c;

    c.outValue    = ((mode == PIN_HIGH) || (mode == PIN_OUT_HIGH)) ? CyTrue : CyFalse;
    c.driveLowEn  = ((mode == PIN_LOW) || (mode == PIN_OUT_LOW) || (mode == PIN_OUT_HIGH)) ? CyTrue : CyFalse;
    c.driveHighEn = ((mode == PIN_HIGH) || (mode == PIN_OUT_LOW) || (mode == PIN_OUT_HIGH)) ? CyTrue : CyFalse;
    c.inputEn     = CyTrue;         /* always: the status then shows the level that is really on the pin */
    c.intrMode    = CY_U3P_GPIO_NO_INTR;
    return CyU3PGpioSetSimpleConfig (g, &c);
}

static CyBool_t
pin_read (uint8_t g)
{
    return (GPIO->lpp_gpio_simple[g] & CY_U3P_LPP_GPIO_IN_VALUE) ? CyTrue : CyFalse;
}

static void
gpio_start (void)
{
    /* RUN, PROGRAM_B and DONE are on control lines of the GPIF block: taken from it as GPIOs.  The other three
     * are GPIOs by the IO matrix. */
    static const uint8_t taken[] = { GPIO_RUN, GPIO_PROG_B, GPIO_DONE };
    static const uint8_t all[] = { GPIO_RUN, GPIO_PROG_B, GPIO_DONE, GPIO_INIT_B, GPIO_CCLK, GPIO_DIN };
    CyU3PGpioClock_t clock;
    CyU3PReturnStatus_t st;
    uint8_t i;

    clock.fastClkDiv = 2;
    clock.slowClkDiv = 0;
    clock.simpleDiv  = CY_U3P_GPIO_SIMPLE_DIV_BY_2;
    clock.clkSrc     = CY_U3P_SYS_CLK;
    clock.halfDiv    = 0;
    st_gpio = CyU3PGpioInit (&clock, NULL);
    if (st_gpio != CY_U3P_SUCCESS)
        return;
    for (i = 0; i < sizeof (all); i++)
    {
        st = CY_U3P_SUCCESS;
        if (i < sizeof (taken))
            st = CyU3PDeviceGpioOverride (all[i], CyTrue);
        if (st == CY_U3P_SUCCESS)       /* RUN is driven low from the start; the rest are inputs */
            st = pin_mode (all[i], (all[i] == GPIO_RUN) ? PIN_OUT_LOW : PIN_IN);
        if (st != CY_U3P_SUCCESS)
        {
            if (pin_failures == 0)
            {
                st_pin = st;
                pin_failed = all[i];
            }
            pin_failures++;
        }
    }
    gpio_ok = (pin_failures == 0) ? CyTrue : CyFalse;
}

/* ---- the FPGA's registers, over I2C ------------------------------------------------------------------------- */

static void
i2c_start (void)
{
    CyU3PI2cConfig_t c;

    st_i2c = CyU3PI2cInit ();
    if (st_i2c != CY_U3P_SUCCESS)
        return;
    CyU3PMemSet ((uint8_t *)&c, 0, sizeof (c));
    c.bitRate    = 100000;
    c.isDma      = CyFalse;
    c.busTimeout = 0xFFFFFFFF;
    c.dmaTimeout = 0xFFFF;
    st_i2c = CyU3PI2cSetConfig (&c, NULL);
}

/* After a transfer that failed (no answer: the FPGA is not configured, or holds another design) the block is
 * started again, so that the next transfer finds it idle. */
static CyU3PReturnStatus_t
i2c_result (CyU3PReturnStatus_t st)
{
    if (st != CY_U3P_SUCCESS)
    {
        CyU3PI2cDeInit ();
        i2c_start ();
    }
    return st;
}

static CyU3PReturnStatus_t
reg_write (uint8_t reg, uint8_t *data, uint32_t n)
{
    CyU3PI2cPreamble_t pre;

    if (st_i2c != CY_U3P_SUCCESS)
        return st_i2c;
    pre.buffer[0] = (uint8_t)(FPGA_I2C << 1);
    pre.buffer[1] = reg;
    pre.length    = 2;
    pre.ctrlMask  = 0x0000;
    return i2c_result (CyU3PI2cTransmitBytes (&pre, data, n, 0));
}

static CyU3PReturnStatus_t
reg_read (uint8_t reg, uint8_t *data, uint32_t n)
{
    CyU3PI2cPreamble_t pre;

    if (st_i2c != CY_U3P_SUCCESS)
        return st_i2c;
    pre.buffer[0] = (uint8_t)(FPGA_I2C << 1);
    pre.buffer[1] = reg;
    pre.buffer[2] = (uint8_t)((FPGA_I2C << 1) | 1);
    pre.length    = 3;
    pre.ctrlMask  = 0x0002;         /* a start again after the register's number */
    return i2c_result (CyU3PI2cReceiveBytes (&pre, data, n, 0));
}

/* Waits until the FPGA's writer is off: it has sent what it held, stopped PCLK and let go of the lines, which it
 * does within 40 ms of RUN's fall whatever happens.  If its registers do not answer, waits 50 ms instead. */
static void
fpga_off (void)
{
    uint8_t status;
    uint32_t i;

    for (i = 0; i < 50; i++)
    {
        if (reg_read (REG_STATUS, &status, 1) != CY_U3P_SUCCESS)
        {
            CyU3PThreadSleep (50);
            return;
        }
        if ((status >> 5) == 0)
            return;
        CyU3PThreadSleep (2);
    }
}

static void
uart_start (void)
{
    CyU3PUartConfig_t c;

    st_uart = CyU3PUartInit ();
    if (st_uart != CY_U3P_SUCCESS)
        return;
    CyU3PMemSet ((uint8_t *)&c, 0, sizeof (c));
    c.baudRate = CY_U3P_UART_BAUDRATE_115200;
    c.stopBit  = CY_U3P_UART_ONE_STOP_BIT;
    c.parity   = CY_U3P_UART_NO_PARITY;
    c.txEnable = CyTrue;
    c.rxEnable = CyFalse;
    c.flowCtrl = CyFalse;
    c.isDma    = CyTrue;
    st_uart = CyU3PUartSetConfig (&c, NULL);
    if (st_uart == CY_U3P_SUCCESS)
        st_uart = CyU3PUartTxSetBlockXfer (0xFFFFFFFF);
    if (st_uart == CY_U3P_SUCCESS)
        st_uart = CyU3PDebugInit (CY_U3P_LPP_SOCKET_UART_CONS, 8);
    if (st_uart == CY_U3P_SUCCESS)
        CyU3PDebugPreamble (CyFalse);
}

/* ---- leaving ------------------------------------------------------------------------------------------------ */

static void
to_boot_loader (void)
{
    CyU3PConnectState (CyFalse, CyTrue);
    CyU3PThreadSleep (200);
    CyU3PDeviceReset (CyFalse);     /* a full reset: the pins float, the boot loader in the ROM starts */
    for (;;)
        ;
}

/* USB could not be started: there is nothing to report with but the UART.  Waits 15 seconds, then gives up. */
static void
post (uint8_t code)
{
    uint32_t t;

    CyU3PDebugPrint (2, "capture 1: USB failed at step %d, status %d\r\n", code, st_last);
    for (t = 0; t < 150; t++)
    {
        CyU3PThreadSleep (100);
        CyU3PSysWatchDogClear ();
    }
    to_boot_loader ();
}

/* ---- the slave FIFO and the stream -------------------------------------------------------------------------- */

static void
levels (void)
{
    GPIO->lpp_gpio_simple[GPIO_RUN] = REG_OUT | (run_on ? 1 : 0);
}

/* In interrupt context.  The argument holds the block's error code and the state machine's. */
static void
pib_cb (CyU3PPibIntrType type, uint16_t arg)
{
    if (type == CYU3P_PIB_INTR_ERROR)
    {
        if (pib_errors == 0)
            pib_first = arg;
        pib_last = arg;
        pib_errors++;
    }
}

static void
stream_stop (void)
{
    run_on = CyFalse;
    levels ();
    if (!channel_on)
        return;
    fpga_off ();
    CyU3PDmaChannelReset (&channel);
    CyU3PUsbFlushEp (EP_IN);
    CyU3PDmaChannelDestroy (&channel);
    channel_on = CyFalse;
}

static void
gpif_stop (void)
{
    stream_stop ();
    if (!gpif_on)
        return;
    CyU3PGpifDisable (CyTrue);
    CyU3PPibDeInit ();
    gpif_on = CyFalse;
}

static CyU3PReturnStatus_t
gpif_start (void)
{
    CyU3PPibClock_t clock;
    CyU3PReturnStatus_t st;

    if (gpif_on)
        return CY_U3P_SUCCESS;
    clock.clkDiv      = 2;
    clock.clkSrc      = CY_U3P_SYS_CLK;
    clock.isHalfDiv   = CyFalse;
    clock.isDllEnable = CyFalse;
    st = CyU3PPibInit (CyTrue, &clock);
    if (st != CY_U3P_SUCCESS)
        return st;
    CyU3PPibRegisterCallback (pib_cb, CYU3P_PIB_INTR_ERROR);
    st = CyU3PGpifLoad (&CyFxGpifConfig);
    if (st == CY_U3P_SUCCESS)
        st = CyU3PGpifSocketConfigure (0, CY_U3P_PIB_SOCKET_0, WATERMARK, CyFalse, 1);
    if (st == CY_U3P_SUCCESS)
        st = CyU3PGpifSMStart (RESET, ALPHA_RESET);
    if (st != CY_U3P_SUCCESS)
    {
        CyU3PGpifDisable (CyTrue);
        CyU3PPibDeInit ();
        return st;
    }
    gpif_on = CyTrue;
    return CY_U3P_SUCCESS;
}

static CyU3PReturnStatus_t
stream_start (uint16_t kb, uint16_t count)
{
    CyU3PDmaChannelConfig_t c;
    CyU3PReturnStatus_t st;

    if (!gpif_on || channel_on || !configured || (st_ep != CY_U3P_SUCCESS))
        return CY_U3P_ERROR_INVALID_SEQUENCE;
    /* The interface is started afresh for every stream.  Left running from one stream to the next, with the FPGA's
     * PCLK stopped in between, the FX3 refuses a run of words somewhere in the next stream's first buffer and
     * reports one overrun.  After that the FPGA's bursts and the FX3's buffers are out of step, and about a
     * buffer's worth of words is lost over the following buffers before the two fall into step again.  It happens
     * to every stream but the first after C0, with a new DMA channel or the old one, with or without request
     * C5. */
    CyU3PGpifDisable (CyTrue);
    CyU3PPibDeInit ();
    gpif_on = CyFalse;
    st = gpif_start ();
    if (st != CY_U3P_SUCCESS)
        return st;
    CyU3PMemSet ((uint8_t *)&c, 0, sizeof (c));
    c.size      = (uint16_t)(kb * 1024);
    c.count     = count;
    c.prodSckId = CY_U3P_PIB_SOCKET_0;
    c.consSckId = CY_U3P_UIB_SOCKET_CONS_1;
    c.dmaMode   = CY_U3P_DMA_MODE_BYTE;
    st = CyU3PDmaChannelCreate (&channel, CY_U3P_DMA_TYPE_AUTO, &c);
    if (st != CY_U3P_SUCCESS)
        return st;
    CyU3PUsbFlushEp (EP_IN);
    st = CyU3PDmaChannelSetXfer (&channel, 0);
    if (st != CY_U3P_SUCCESS)
    {
        CyU3PDmaChannelDestroy (&channel);
        return st;
    }
    pib_errors = 0;
    pib_first = 0;
    pib_last = 0;
    buf_kb = kb;
    buf_count = count;
    channel_on = CyTrue;
    return CY_U3P_SUCCESS;
}

/* The endpoint, as the connection's speed has it.  Called when the host selects the configuration. */
static void
ep_config (void)
{
    CyU3PEpConfig_t e;
    CyU3PUSBSpeed_t speed = CyU3PUsbGetSpeed ();

    CyU3PMemSet ((uint8_t *)&e, 0, sizeof (e));
    e.enable   = CyTrue;
    e.epType   = CY_U3P_USB_EP_BULK;
    e.burstLen = (speed == CY_U3P_SUPER_SPEED) ? 16 : 1;
    e.pcktSize = (speed == CY_U3P_SUPER_SPEED) ? 1024 : ((speed == CY_U3P_HIGH_SPEED) ? 512 : 64);
    st_ep = CyU3PSetEpConfig (EP_IN, &e);
}

/* ---- FPGA configuration, slave serial (Xilinx UG380) --------------------------------------------------------- */

/* DIN is set, then CCLK rises (the FPGA takes DIN there) and falls.  Each level is written twice so that it lasts
 * at least one register write, whatever the compiler makes of the loop. */
static void
shift (const uint8_t *p, uint32_t n)
{
    uvint32_t *din = &GPIO->lpp_gpio_simple[GPIO_DIN];
    uvint32_t *clk = &GPIO->lpp_gpio_simple[GPIO_CCLK];
    uint32_t b, i;

    while (n--)
    {
        b = *p++;
        for (i = 0; i < 8; i++)
        {
            *din = REG_OUT | ((b >> 7) & 1);
            *din = REG_OUT | ((b >> 7) & 1);
            b <<= 1;
            *clk = REG_OUT | 1;
            *clk = REG_OUT | 1;
            *clk = REG_OUT;
        }
    }
}

static void
cfg_begin (void)
{
    uint32_t i;

    gpif_stop ();                               /* the FPGA's lines float while it is configured */
    cfg_flags = 0;
    cfg_bytes = 0;
    cfg_ms = 0;
    cfg_clocks = 0xFFFFFFFF;
    pin_mode (GPIO_CCLK, PIN_OUT_LOW);
    pin_mode (GPIO_DIN, PIN_OUT_HIGH);
    pin_mode (GPIO_PROG_B, PIN_LOW);            /* at least 500 ns: this is 2 ms */
    CyU3PThreadSleep (2);
    if (!pin_read (GPIO_INIT_B))
        cfg_flags |= CFG_INIT_LOW;
    if (!pin_read (GPIO_DONE))
        cfg_flags |= CFG_DONE_LOW;
    pin_mode (GPIO_PROG_B, PIN_IN);             /* the 4.7k pull-up raises it */
    for (i = 0; i < 1000; i++)                  /* the FPGA clears its memory: 4 ms at most, 100 ms allowed */
    {
        if (pin_read (GPIO_INIT_B))
            break;
        CyU3PBusyWait (100);
    }
    cfg_wait = i;                               /* in units of 0.1 ms */
    if (i < 1000)
        cfg_flags |= CFG_READY;
    CyU3PThreadSleep (2);
    CyU3PDebugPrint (2, "capture 1: configure begin, flags %x, INIT_B high after %d00 us\r\n", cfg_flags, cfg_wait);
}

static void
cfg_data (uint32_t n)
{
    uint32_t t0 = CyU3PGetTime ();

    shift (ep0, n);
    cfg_ms += CyU3PGetTime () - t0;
    cfg_bytes += n;
    /* Until DONE, INIT_B low means a CRC error.  After DONE it is an ordinary FPGA pin, and an unused one: the
     * FPGA's pull-down then works against the board's 4.7k pull-up and the FX3 reads it low. */
    if (!pin_read (GPIO_DONE) && !pin_read (GPIO_INIT_B))
        cfg_flags |= CFG_INIT_FELL;
}

static void
cfg_end (void)
{
    static const uint8_t ones[16] = {
        0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF
    };
    uint32_t i;

    for (i = 0; i < 4096; i++)                  /* up to 32768 clocks more, DIN high, until DONE */
    {
        if (pin_read (GPIO_DONE))
            break;
        shift (ones, 1);
    }
    if (i < 4096)
    {
        cfg_flags |= CFG_DONE;
        cfg_clocks = 8 * i;
    }
    shift (ones, 16);                           /* the start-up sequence ends a few clocks after DONE */
    cfg_flags |= CFG_ENDED;
    pin_mode (GPIO_CCLK, PIN_IN);
    pin_mode (GPIO_DIN, PIN_IN);
    CyU3PDebugPrint (2, "capture 1: configure end, flags %x, %d bytes in %d ms\r\n", cfg_flags, cfg_bytes, cfg_ms);
}

/* ---- USB ---------------------------------------------------------------------------------------------------- */

static void
put16 (uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

static void
put32 (uint8_t *p, uint32_t v)
{
    put16 (p, v);
    put16 (p + 2, v >> 16);
}

/* The 64 bytes of request B0, little-endian:
 *    0  "LBC2"                       4  version (16 bits)        6  USB speed: 1 full, 2 high, 3 5 Gbit/s
 *    7  bit 0 the pins are set up, 1 the UART is, 2 the host has configured the device, 3 the interface is
 *       started, 4 the stream is, 5 RUN, 6 the FPGA's registers answer
 *    8  status of the IO matrix, 10 of the GPIO block, 12 of the UART, 14 of the first pin that could not be set
 *       up, 16 of the last request that failed, 18 of the endpoint (16 bits each; 0 = success)
 *   20  ticks since the start
 *   24  the levels on GPIO 31-0     28  on GPIO 60-32
 *   32  configuration: bytes shifted          36  flags (CFG_... above; 16 bits)
 *   38  how long INIT_B took to rise, in 0.1 ms (16 bits; 1000 = it did not)
 *   40  clocks after the last byte until DONE (FFFFFFFF = it did not rise)
 *   44  ticks spent shifting (16 bits)        46  5 Gbit/s link failures (8 bits)     47  USB resets (8 bits)
 *   48  errors of the parallel port's block since the stream began: a word written with no buffer to take it, and
 *       the like.  52 the first one's code, 54 the last one's (16 bits each: the block's error, CyU3PPibErrorType,
 *       in bits 5:0, the state machine's in bits 14:10)
 *   56  the stream's buffer size in KB (8 bits), 57 how many buffers (8 bits), 58 that pin which could not be set
 *       up, 59 how many could not
 *   60  status of the I2C block, or of the last transfer on it that failed (16 bits; 0 = success)
 *   62  the FPGA's register 03, its design's version; 0 if its registers do not answer (they are not asked while
 *       DONE is low)
 *   63  the FPGA's register 05, its status: bit 0 the PLL has locked, 1 RUN, 2 sampling, 3 done, 4 overflow,
 *       7:5 the writer's state
 */
static void
info (uint8_t *p)
{
    uint32_t v0 = 0, v1 = 0;
    uint8_t reg[3] = { 0, 0, 0 };
    CyU3PReturnStatus_t st = CY_U3P_ERROR_NOT_CONFIGURED;

    CyU3PMemSet (p, 0, 64);
    p[0] = 'L';
    p[1] = 'B';
    p[2] = 'C';
    p[3] = '2';
    put16 (p + 4, VERSION);
    p[6] = (uint8_t)CyU3PUsbGetSpeed ();
    CyU3PGpioGetIOValues (&v0, &v1);
    if (gpio_ok && (v0 & (1u << GPIO_DONE)))
        st = reg_read (REG_VERSION, reg, 3);
    if (st != CY_U3P_SUCCESS)
        reg[0] = reg[2] = 0;
    p[7] = (gpio_ok ? 1 : 0) | ((st_uart == CY_U3P_SUCCESS) ? 2 : 0) | (configured ? 4 : 0) | (gpif_on ? 8 : 0) |
        (channel_on ? 16 : 0) | (run_on ? 32 : 0) | ((st == CY_U3P_SUCCESS) ? 64 : 0);
    put16 (p + 8, st_matrix);
    put16 (p + 10, st_gpio);
    put16 (p + 12, st_uart);
    put16 (p + 14, st_pin);
    put16 (p + 16, st_last);
    put16 (p + 18, st_ep);
    put32 (p + 20, CyU3PGetTime ());
    put32 (p + 24, v0);
    put32 (p + 28, v1);
    put32 (p + 32, cfg_bytes);
    put16 (p + 36, cfg_flags);
    put16 (p + 38, cfg_wait);
    put32 (p + 40, cfg_clocks);
    put16 (p + 44, (cfg_ms > 0xFFFF) ? 0xFFFF : cfg_ms);
    p[46] = (ev_linkfail > 255) ? 255 : (uint8_t)ev_linkfail;
    p[47] = (ev_reset > 255) ? 255 : (uint8_t)ev_reset;
    put32 (p + 48, pib_errors);
    put16 (p + 52, pib_first);
    put16 (p + 54, pib_last);
    p[56] = (uint8_t)buf_kb;
    p[57] = (uint8_t)buf_count;
    p[58] = pin_failed;
    p[59] = pin_failures;
    put16 (p + 60, st_i2c);
    p[62] = reg[0];
    p[63] = reg[2];
}

/* Request BA's test data: xorshift32 from the seed, the top byte of each step.  With check, counts the bytes of p
 * that differ from it; without, writes it to p. */
static uint32_t
pattern (uint8_t *p, uint16_t n, uint16_t seed, CyBool_t check)
{
    uint32_t x = ((uint32_t)seed + 1) * 0x9E3779B1u;    /* never 0: the constant is odd */
    uint32_t bad = 0;
    uint16_t i;

    for (i = 0; i < n; i++)
    {
        x ^= x << 13;
        x ^= x >> 17;
        x ^= x << 5;
        if (!check)
            p[i] = (uint8_t)(x >> 24);
        else if (p[i] != (uint8_t)(x >> 24))
            bad++;
    }
    return bad;
}

/* The link report of request B9, 16 bytes, little-endian:
 *    0  status of the SDK's error count query (0 = success; it fails when the connection is not at 5 Gbit/s)
 *    4  PHY errors since the last B9 (16 bits, stops at FFFF): symbols that did not decode, CRC errors, elastic
 *       buffer over- and underflow, training sequence errors, loss of lock
 *    6  link errors since the last B9 (16 bits, stops at FFFF): acknowledgments and credits missing or late
 *    8  times the link entered recovery since the start (16 bits)    10  link failures since the start (16 bits)
 *   12  the link's state (CyU3PUsbLinkState_t; FF = not known)       13  USB speed, as in the status
 *   14  bytes of BA's test data that arrived wrong since the start (16 bits, stops at FFFF)
 */
static void
link_info (uint8_t *p)
{
    uint16_t phy = 0, lnk = 0;
    uint32_t state = 0xFF;

    put32 (p, CyU3PUsbGetErrorCounts (&phy, &lnk));
    put16 (p + 4, phy);
    put16 (p + 6, lnk);
    put16 (p + 8, ev_recovery);
    put16 (p + 10, ev_linkfail);
    if (CyU3PUsbGetDevProperty (CY_U3P_USB_PROP_LINKSTATE, &state) != CY_U3P_SUCCESS)
        state = 0xFF;
    p[12] = (uint8_t)state;
    p[13] = (uint8_t)CyU3PUsbGetSpeed ();
    put16 (p + 14, (data_bad > 0xFFFF) ? 0xFFFF : data_bad);
}

static void
refuse (void)
{
    CyU3PUsbStall (0, CyTrue, CyFalse);
}

/* A vendor request, in the thread: the requests that take long would hold up the USB driver in its callback. */
static void
request (void)
{
    uint32_t s0 = setup0, s1 = setup1;
    uint8_t  type    = (uint8_t)(s0 & CY_U3P_USB_REQUEST_TYPE_MASK);
    uint8_t  code    = (uint8_t)((s0 & CY_U3P_USB_REQUEST_MASK) >> CY_U3P_USB_REQUEST_POS);
    uint16_t value   = (uint16_t)((s0 & CY_U3P_USB_VALUE_MASK) >> CY_U3P_USB_VALUE_POS);
    uint16_t index   = (uint16_t)((s1 & CY_U3P_USB_INDEX_MASK) >> CY_U3P_USB_INDEX_POS);
    uint16_t length  = (uint16_t)((s1 & CY_U3P_USB_LENGTH_MASK) >> CY_U3P_USB_LENGTH_POS);
    CyBool_t in      = (type & 0x80) ? CyTrue : CyFalse;
    CyU3PReturnStatus_t st = CY_U3P_SUCCESS;
    uint16_t got = 0;

    heard = CyTrue;
    if (in)
    {
        if ((code == 0xB0) && (length >= 64))
        {
            info (ep0);
            st = CyU3PUsbSendEP0Data (64, ep0);
        }
        else if ((code == 0xB8) && (length >= (LOG_SIZE + 2)))
        {
            put16 (ep0, CyU3PUsbGetEventLogIndex ());
            CyU3PMemCopy (ep0 + 2, usb_log, LOG_SIZE);
            st = CyU3PUsbSendEP0Data (LOG_SIZE + 2, ep0);
        }
        else if ((code == 0xB9) && (length >= 16))
        {
            link_info (ep0);
            st = CyU3PUsbSendEP0Data (16, ep0);
        }
        else if ((code == 0xBA) && (length >= 1) && (length <= sizeof (ep0)))
        {
            pattern (ep0, length, value, CyFalse);
            st = CyU3PUsbSendEP0Data (length, ep0);
        }
        else if ((code == 0xD1) && (length >= 1) && (length <= 64) && (value <= 255))
        {
            st = reg_read ((uint8_t)value, ep0, length);
            if (st == CY_U3P_SUCCESS)
                st = CyU3PUsbSendEP0Data (length, ep0);
            else
                refuse ();
        }
        else
            refuse ();
    }
    else if (!gpio_ok && (code != 0xB6) && (code != 0xB7))
        refuse ();                              /* the pins could not be set up: the status says why */
    else switch (code)
    {
    case 0xB3:
        if (length == 0)
        {
            cfg_begin ();
            CyU3PUsbAckSetup ();
        }
        else
            refuse ();
        break;

    case 0xB4:
        if ((length >= 1) && (length <= sizeof (ep0)) && (cfg_flags & CFG_READY) && !(cfg_flags & CFG_ENDED))
        {
            st = CyU3PUsbGetEP0Data (length, ep0, &got);
            if ((st == CY_U3P_SUCCESS) && (got == length))
                cfg_data (length);
            else if (st == CY_U3P_SUCCESS)
                st = CY_U3P_ERROR_BAD_SIZE;
        }
        else
            refuse ();
        break;

    case 0xB5:
        if ((length == 0) && (cfg_flags & CFG_READY) && !(cfg_flags & CFG_ENDED))
        {
            cfg_end ();
            CyU3PUsbAckSetup ();
        }
        else
            refuse ();
        break;

    case 0xB6:
        if ((length == 0) && (value <= 1))
        {
            CyU3PUsbAckSetup ();
            CyU3PThreadSleep (100);
            stream_stop ();
            CyU3PConnectState (CyFalse, CyTrue);
            CyU3PThreadSleep (500);
            st = CyU3PConnectState (CyTrue, value ? CyTrue : CyFalse);
        }
        else
            refuse ();
        break;

    case 0xB7:
        if (length == 0)
        {
            CyU3PUsbAckSetup ();
            CyU3PThreadSleep (100);
            gpif_stop ();
            to_boot_loader ();
        }
        else
            refuse ();
        break;

    case 0xBA:
        if ((length >= 1) && (length <= sizeof (ep0)))
        {
            st = CyU3PUsbGetEP0Data (length, ep0, &got);
            if ((st == CY_U3P_SUCCESS) && (got == length))
                data_bad += pattern (ep0, length, value, CyTrue);
            else if (st == CY_U3P_SUCCESS)
                st = CY_U3P_ERROR_BAD_SIZE;
        }
        else
            refuse ();
        break;

    case 0xC0:
        st = (length == 0) ? gpif_start () : CY_U3P_ERROR_BAD_ARGUMENT;
        if (st == CY_U3P_SUCCESS)
            CyU3PUsbAckSetup ();
        else
            refuse ();
        break;

    case 0xC1:
        if (length == 0)
        {
            gpif_stop ();
            CyU3PUsbAckSetup ();
        }
        else
            refuse ();
        break;

    case 0xC2:
        st = ((length == 0) && (value >= 1) && (value <= 63) && (index >= 1) && (index <= 255)) ?
            stream_start (value, index) : CY_U3P_ERROR_BAD_ARGUMENT;
        if (st == CY_U3P_SUCCESS)
            CyU3PUsbAckSetup ();
        else
            refuse ();
        break;

    case 0xC3:
        if (length == 0)
        {
            stream_stop ();
            CyU3PUsbAckSetup ();
        }
        else
            refuse ();
        break;

    case 0xC4:
        if ((length == 0) && (value <= 1) && (channel_on || !(value & 1)))
        {
            run_on = (value & 1) ? CyTrue : CyFalse;
            levels ();
            CyU3PUsbAckSetup ();
        }
        else
            refuse ();
        break;

    case 0xC5:
        if ((length == 0) && channel_on && !run_on)
        {
            fpga_off ();
            st = CyU3PDmaChannelSetWrapUp (&channel);
            CyU3PThreadSleep (2);
            if (st == CY_U3P_SUCCESS)
                st = CyU3PDmaChannelSetWrapUp (&channel);
        }
        else
            st = CY_U3P_ERROR_BAD_ARGUMENT;
        if (st == CY_U3P_SUCCESS)
            CyU3PUsbAckSetup ();
        else
            refuse ();
        break;

    case 0xD0:
        if ((length >= 1) && (length <= 64) && (value <= 255))
        {
            st = CyU3PUsbGetEP0Data (length, ep0, &got);
            if ((st == CY_U3P_SUCCESS) && (got == length))
                st = reg_write ((uint8_t)value, ep0, length);
            else if (st == CY_U3P_SUCCESS)
                st = CY_U3P_ERROR_BAD_SIZE;
        }
        else
            refuse ();
        break;

    default:
        refuse ();
        break;
    }
    if (st != CY_U3P_SUCCESS)
        st_last = st;
}

static CyBool_t
setup_cb (uint32_t s0, uint32_t s1)
{
    uint8_t  type    = (uint8_t)(s0 & CY_U3P_USB_REQUEST_TYPE_MASK);
    uint8_t  code    = (uint8_t)((s0 & CY_U3P_USB_REQUEST_MASK) >> CY_U3P_USB_REQUEST_POS);
    uint16_t value   = (uint16_t)((s0 & CY_U3P_USB_VALUE_MASK) >> CY_U3P_USB_VALUE_POS);
    uint16_t index   = (uint16_t)((s1 & CY_U3P_USB_INDEX_MASK) >> CY_U3P_USB_INDEX_POS);

    if (((type & CY_U3P_USB_TYPE_MASK) == CY_U3P_USB_VENDOR_RQT) &&
            ((type & CY_U3P_USB_TARGET_MASK) == CY_U3P_USB_TARGET_DEVICE))
    {
        setup0 = s0;
        setup1 = s1;
        CyU3PEventSet (&event, EVENT_SETUP, CYU3P_EVENT_OR);
        return CyTrue;
    }
    if ((type & CY_U3P_USB_TYPE_MASK) != CY_U3P_USB_STANDARD_RQT)
        return CyFalse;
    /* The driver answers the standard requests, except these.  At 5 Gbit/s the host may suspend and resume the
     * interface: accepted once the device is configured. */
    if (((type & CY_U3P_USB_TARGET_MASK) == CY_U3P_USB_TARGET_INTF) &&
            ((code == CY_U3P_USB_SC_SET_FEATURE) || (code == CY_U3P_USB_SC_CLEAR_FEATURE)) && (value == 0))
    {
        if (configured)
            CyU3PUsbAckSetup ();
        else
            CyU3PUsbStall (0, CyTrue, CyFalse);
        return CyTrue;
    }
    /* "Clear halt" on the endpoint: nothing here ever halts it, so only its sequence number starts again. */
    if (((type & CY_U3P_USB_TARGET_MASK) == CY_U3P_USB_TARGET_ENDPT) && (code == CY_U3P_USB_SC_CLEAR_FEATURE) &&
            (value == CY_U3P_USBX_FS_EP_HALT) && (index == EP_IN) && configured)
    {
        CyU3PUsbStall (EP_IN, CyFalse, CyTrue);
        CyU3PUsbAckSetup ();
        return CyTrue;
    }
    return CyFalse;
}

static void
event_cb (CyU3PUsbEventType_t type, uint16_t data)
{
    (void)data;
    switch (type)
    {
    case CY_U3P_USB_EVENT_SETCONF:
        configured = CyTrue;
        CyU3PUsbLPMDisable ();          /* the link stays in U0: a stream gains nothing from the low-power states */
        ep_config ();
        break;
    case CY_U3P_USB_EVENT_RESET:
        ev_reset++;
        configured = CyFalse;
        CyU3PEventSet (&event, EVENT_DOWN, CYU3P_EVENT_OR);
        break;
    case CY_U3P_USB_EVENT_DISCONNECT:
        configured = CyFalse;
        CyU3PEventSet (&event, EVENT_DOWN, CYU3P_EVENT_OR);
        break;
    case CY_U3P_USB_EVENT_USB3_LNKFAIL:
        ev_linkfail++;
        break;
    case CY_U3P_USB_EVENT_LNK_RECOVERY:
        ev_recovery++;
        break;
    default:
        break;
    }
}

static void
usb_start (void)
{
    static const struct { CyU3PUSBSetDescType_t type; uint8_t index; const uint8_t *data; } table[] = {
        { CY_U3P_USB_SET_SS_DEVICE_DESCR, 0, dscr_device_ss },
        { CY_U3P_USB_SET_HS_DEVICE_DESCR, 0, dscr_device_hs },
        { CY_U3P_USB_SET_SS_BOS_DESCR,    0, dscr_bos },
        { CY_U3P_USB_SET_DEVQUAL_DESCR,   0, dscr_qualifier },
        { CY_U3P_USB_SET_SS_CONFIG_DESCR, 0, dscr_config_ss },
        { CY_U3P_USB_SET_HS_CONFIG_DESCR, 0, dscr_config_hs },
        { CY_U3P_USB_SET_FS_CONFIG_DESCR, 0, dscr_config_fs },
        { CY_U3P_USB_SET_STRING_DESCR,    0, dscr_langid },
        { CY_U3P_USB_SET_STRING_DESCR,    1, dscr_manufacturer },
        { CY_U3P_USB_SET_STRING_DESCR,    2, dscr_product }
    };
    uint32_t i;

    st_last = CyU3PUsbStart ();
    if (st_last != CY_U3P_SUCCESS)
        post (POST_USB_START);
    CyU3PMemSet (usb_log, 0, LOG_SIZE);
    CyU3PUsbInitEventLog (usb_log, LOG_SIZE);
    CyU3PUsbRegisterSetupCallback (setup_cb, CyTrue);
    CyU3PUsbRegisterEventCallback (event_cb);
    /* a serial number that cannot be made or set is done without: the device descriptors then name none */
    if (serial_number () &&
        (CyU3PUsbSetDesc (CY_U3P_USB_SET_STRING_DESCR, SERIAL_STRING, dscr_serial) == CY_U3P_SUCCESS))
        dscr_device_ss[SERIAL_AT] = dscr_device_hs[SERIAL_AT] = SERIAL_STRING;
    for (i = 0; i < (sizeof (table) / sizeof (table[0])); i++)
    {
        st_last = CyU3PUsbSetDesc (table[i].type, table[i].index, (uint8_t *)table[i].data);
        if (st_last != CY_U3P_SUCCESS)
            post (POST_DESCRIPTOR);
    }
    st_last = CyU3PConnectState (CyTrue, CyTrue);       /* 5 Gbit/s if the cable and the port carry it */
    if (st_last != CY_U3P_SUCCESS)
        post (POST_CONNECT);
}

/* ---- start -------------------------------------------------------------------------------------------------- */

static void
app (uint32_t input)
{
    uint32_t got;

    (void)input;
    CyU3PSysWatchDogConfigure (CyTrue, WATCHDOG_MS);
    uart_start ();
    gpio_start ();
    i2c_start ();
    /* The FX3 drives only the two flags and its two GPIOs toward the FPGA, on lines without series resistors:
     * the lowest of its four drive strengths. */
    CyU3PSetPportDriveStrength (CY_U3P_DS_QUARTER_STRENGTH);
    CyU3PDebugPrint (2, "\r\ncapture 1, version %d: IO matrix %d, GPIO %d, pins %d\r\n", VERSION, st_matrix,
            st_gpio, pin_failures);
    usb_start ();
    for (;;)
    {
        if (CyU3PEventGet (&event, EVENT_SETUP | EVENT_DOWN, CYU3P_EVENT_OR_CLEAR, &got, 10) == CY_U3P_SUCCESS)
        {
            if (got & EVENT_DOWN)
                stream_stop ();
            if (got & EVENT_SETUP)
                request ();
        }
        CyU3PSysWatchDogClear ();
        if (!heard && (CyU3PGetTime () > FIRST_REQUEST_MS))
            to_boot_loader ();
    }
}

/* Before the operating system runs there is nothing to report with: wait two seconds and reset. */
static void
give_up (void)
{
    uint32_t i;

    for (i = 0; i < 2000; i++)
        CyU3PBusyWait (1000);
    CyU3PDeviceReset (CyFalse);
    for (;;)
        ;
}

void
CyFxApplicationDefine (void)
{
    void *stack;

    if (CyU3PEventCreate (&event) != 0)
        give_up ();
    stack = CyU3PMemAlloc (STACK);
    if ((stack == NULL) || (CyU3PThreadCreate (&thread, "21:capture2", app, 0, stack, STACK, PRIORITY, PRIORITY,
                    CYU3P_NO_TIME_SLICE, CYU3P_AUTO_START) != 0))
        give_up ();
}

int
main (void)
{
    CyU3PSysClockConfig_t clock;
    CyU3PIoMatrixConfig_t io;

    clock.setSysClk400  = CyTrue;       /* 403.2 MHz: the SDK asks for it when the GPIF bus runs at 100 MHz */
    clock.cpuClkDiv     = 2;
    clock.dmaClkDiv     = 2;
    clock.mmioClkDiv    = 2;
    clock.useStandbyClk = CyFalse;      /* no 32 kHz clock on this board */
    clock.clkSrc        = CY_U3P_SYS_CLK;
    if (CyU3PDeviceInit (&clock) != CY_U3P_SUCCESS)
        give_up ();
    if (CyU3PDeviceCacheControl (CyTrue, CyFalse, CyFalse) != CY_U3P_SUCCESS)
        give_up ();

    /* A 32-bit GPIF bus, which moves the UART to GPIO 53-56.  I2S is left off so that its pins, and GPIO 45, are
     * GPIOs. */
    CyU3PMemSet ((uint8_t *)&io, 0, sizeof (io));
    io.isDQ32Bit = CyTrue;
    io.s0Mode    = CY_U3P_SPORT_INACTIVE;
    io.s1Mode    = CY_U3P_SPORT_INACTIVE;
    io.useUart   = CyTrue;
    io.useI2C    = CyTrue;
    io.useI2S    = CyFalse;
    io.useSpi    = CyFalse;
    io.lppMode   = CY_U3P_IO_MATRIX_LPP_DEFAULT;
    io.gpioSimpleEn[0] = 0;
    io.gpioSimpleEn[1] = (1u << (GPIO_INIT_B - 32)) | (1u << (GPIO_CCLK - 32)) | (1u << (GPIO_DIN - 32));
    st_matrix = CyU3PDeviceConfigureIOMatrix (&io);
    if (st_matrix != CY_U3P_SUCCESS)
    {
        /* Not accepted: the least that can be asked for.  The status carries the refusal to the computer. */
        io.useUart = CyFalse;
        io.gpioSimpleEn[1] = 0;
        if (CyU3PDeviceConfigureIOMatrix (&io) != CY_U3P_SUCCESS)
            give_up ();
    }
    CyU3PKernelEntry ();
    return 0;
}
