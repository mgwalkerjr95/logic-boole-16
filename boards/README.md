# Board versions

One folder for each version of the board. A version's folder keeps the documents and the design files of the boards
that carry its mark, and does not change when a later version comes.

| Version | On the back | Built | Thresholds | Folder |
|---|---|---|---|---|
| V1.0.0 | REV C · 2026 | November 2026, five boards | 0.3 to 3 V | [v1.0.0/](v1.0.0/) |

Each folder has the same two parts:

| Path | What it is |
|---|---|
| `docs/` | The datasheet and the quick start guide, each as a PDF and as the page it is printed from |
| `hardware/` | The KiCad project, the schematic as a PDF, the parts list, the fabrication files, a 3D model and renders |

What every version shares is at the top of the repository: the [software](../software/), the
[FPGA design](../gateware/), the [firmware](../firmware/) and the [protocol](../docs/protocol.md).
