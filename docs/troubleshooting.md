# Troubleshooting

Start with `logicboole check`. It checks this computer and the board in a few seconds, and each line that
finds something missing says what to do. The tables below give the same advice, and cover what `check` cannot see.

Anything odd that is not listed here: unplug the board and plug it in again. It keeps nothing when it loses power,
so it is then exactly as it came out of the box, and the next command loads it again in about three seconds.

## The board is not found or cannot be opened

| What you see | What to do |
|---|---|
| "The board: not on USB." or "No board on USB." | Try another cable and another port. A cable made only for charging carries no data. On a Raspberry Pi, use a blue USB 3 socket |
| "macOS has not let the board connect" | Run the command again and choose Allow when macOS asks "Allow accessory to connect?". The setting is "Allow accessories to connect", under Privacy & Security in System Settings |
| "The board is on USB, but this user may not open it" (Linux) | Install the USB rule: run the line the message prints (three commands joined by `&&`), once, then unplug the board and plug it in again |
| "The board's firmware is not on this computer yet and could not be downloaded" | The software downloads the FX3's firmware (about 110 kB) the first time it loads a board. Connect to the internet once, or download the file the message names on another computer and save it where the message says |
| "A device with the board's USB IDs runs other firmware" | `logicboole load` loads this software's firmware and FPGA design in its place. Or unplug the board and plug it in again |
| "Another program is capturing from the board." or "Another program has the board open." | The app, an agent's MCP server or another command is using the board: one program captures at a time. Wait for it to finish, or end it. `logicboole load` takes the board back from it, and so does unplugging the board and plugging it in again |
| "A capture was left running by a program that has ended; stopped it." | Nothing to do. A command, the app or an agent was stopped in the middle of a capture, and this command ended that capture before going on |
| "The board runs firmware version 1; this software comes with version 2: loading it." | Nothing to do. The board stayed powered while the software was updated, and it is loaded with this software's version. The same goes for the FPGA's design |
| "The board left USB (unplugged, the cable, or a hub)." | Connect it again and run the command again: the board is loaded again by itself. A capture without a trigger keeps what arrived before, with a problem listed |
| "USB: 480 Mbit/s" | The cable or the port is USB 2. The board works, and carries about a ninth of what USB 3 carries: busy signals at high rates overflow. Use a USB 3 port and a USB 3 cable |
| "Windows is not supported yet" | The board needs a driver on Windows that it does not have. Use macOS or Linux (a Raspberry Pi too) |

## The app

| What you see | What to do |
|---|---|
| "Port 8016 cannot be used ... is the app running already?" | The app is running in another terminal, or another program has the port. Close the other one, or start this one with `logicboole --port 8020` |
| "The app's server has stopped: the "logicboole" program is not running any more." | `logicboole` was stopped (Ctrl-C, or its terminal closed). A capture it was running was ended first, so the board is free. Start it again and reload the page |
| A strip marked SIMULATED: "Simulated signals: no board on USB" | The app found no board, and shows its own simulated signals. Plug the board in: the app finds it by itself and makes it the source. If it does not, see the first table |
| An empty scope: "Press Start to capture" | The app opens this way with a board on USB. Set the thresholds, a rate and a length, and press Start or the space bar |
| "Open the app as http://localhost:8016, or by this computer's address." | The page was opened under a name the app does not take, which keeps other web sites from reaching it. Use `localhost` on the same computer. From another computer (with `--host 0.0.0.0`), use one of the addresses `logicboole` printed when it started: the board computer's own name, such as `http://raspberrypi.local:8016`, or its address, such as `http://192.168.1.20:8016` |
| A threshold is refused: "Threshold A is 3.30 V, and this board takes 0.30 to 3.00 V." | Each pod's threshold is in volts at the probe, and a V1.0.0 board takes 0.3 V to 3 V. Set it to about half the logic voltage: 1.65 V for 3.3 V logic. `logicboole info` gives the board's range |
| A capture waits for its trigger, and the page was reloaded or closed | A reloaded page takes the wait over, with Stop. A wait that no page has watched for 10 seconds is ended, and the board is free |

## A capture

| What you see | What to do |
|---|---|
| A channel that should be busy shows no changes | In the order worth checking: the signal is on another input, or the probe is not making contact (an open input reads 0); no ground lead joins the board and the circuit; the threshold is outside the signal's swing (a 1.8 V signal never crosses 2.5 V); the circuit was idle during the capture. `logicboole inputs` shows the 16 levels now |
| An input stays at 1 | The threshold is below the signal's low level, or the line is held high. Set the pod's threshold to about half the logic voltage: 1.65 V for 3.3 V logic, 0.9 V for 1.8 V |
| "Waiting for the trigger (B0=f).  Ctrl-C stops." and nothing more | The board has not seen the trigger yet. Check the channel and the edge, and that the event happens while it waits. Ctrl-C ends the wait and keeps what the board saw last; `--timeout 10` ends it by itself |
| "NOT TRIGGERED (...): these are the last ... samples the board saw." | The trigger did not come before Ctrl-C or the timeout. The capture is the moments before the wait ended, not the event. After `--timeout` the command exits with status 3 |
| "A pulse of 50ns is shorter than one sample, which is 100 ns at 10 MS/s" | Times in a trigger, and in `find`, are counted in samples. Give a longer time, or capture at a higher rate |
| "A threshold of 3.3 V is outside what the board takes, 0.3 V to 3 V" | Set the threshold to about half the logic voltage: 1.65 V for 3.3 V logic, 2.5 V for 5 V logic |
| "OVERFLOW" | The signals changed faster than the board and USB carry, or the computer did not take the data fast enough. The capture is whole up to where it ended. Lower the rate, shorten the capture, use USB 3, or use `--raw` for a short capture of a signal that changes at nearly every sample. The command exits with status 3 |
| "ended early: it had grown too large" | The computer holds a capture in its memory, up to a 32nd of it (256 MB on a computer with 8 GB). The capture is whole up to where it ended. Shorten it or lower the rate |
| "WRONG: ..." | The capture failed its checks and is not to be used. Take it again; if it happens again, try another port and cable, then run `logicboole selftest` |
| "An .lb16 file holds 2^32 samples at most" | That is 21.47 s at 200 MS/s, 71.6 minutes at 1 MS/s. Save the capture as `.sr`, `.vcd` or `.csv`, or make it shorter or slower |
| "... is not a capture file" | The commands that read captures read `.lb16` files, the command line's and the app's, and sigrok sessions (`.sr`) |

## Decoding

| What you see | What to do |
|---|---|
| "sigrok-cli was not found." | The command line and agents decode through sigrok's decoders. Install them with `brew install sigrok-cli` (macOS) or `sudo apt install sigrok-cli` (Debian, Ubuntu, Raspberry Pi OS). The app decodes I²C, SPI and serial without it |
| "i2c needs: sda=CHANNEL" or another decoder's needs | `logicboole decoders NAME` lists the decoder's channels and options |
| A decoder finds nothing | Check that the capture shows the bus at all (`logicboole measure FILE`), and that the rate is at least 4 times the bus's bit rate |

## AI agents

| What you see | What to do |
|---|---|
| The agent does not know the board | Install the skill: `logicboole skill --install` puts it where Claude Code finds it. Run it again after updating the software. From a copy of the repository, `cp -r software/skills/logic-boole-16 ~/.claude/skills/` does the same |
| The agent's program cannot start the MCP server | A program started from the Dock or a menu may not search the folders a terminal does. Give the full path of `logicboole` as the command (`which logicboole` prints it), with `mcp` as its argument |
| `decode` works in a terminal, but the agent's server says "sigrok-cli was not found." | The same cause. Set `LOGICBOOLE_SIGROK_CLI` to the full path that `which sigrok-cli` prints, in the server's environment (`"env"` in the program's MCP settings) |

## Checking the board itself

`logicboole selftest` has the FPGA send a known pattern in place of the inputs, from 1 MS/s to 200 MS/s, and
compares every word that arrives. It needs nothing connected to the probes and takes about half a minute. A board
that passes carries data correctly from the FPGA to the computer; what it does not check is the inputs themselves.

## Help

Write to support@nearfieldsupply.com, or open an issue at https://github.com/mgwalkerjr95/logic-boole-16/issues.
Include what `logicboole --version` and `logicboole info --json` print.
