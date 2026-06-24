"""Sequence support built on `fileseq`.

The public entry point for scanning is `ScanSequencesRequest` on the engine's
event bus (see `griptape_nodes.retained_mode.events.os_events`). This module
exports the data shapes (`Sequence`, `SequenceEntry`, `MissingItemPolicy`,
`MissingItemError`) but not the scan function itself — that lives on the bus
to keep disk I/O async-friendly and observable.

The scanner provides a thin wrapper over `fileseq.FileSequence` that:

- Routes all filesystem I/O through `ListDirectoryRequest` (no `os.scandir` calls).
- Drops negative numbers at scan time (intentional — they're a footgun in
  downstream tools).
- Adds explicit user-facing policy options for handling gaps within a number
  range (`ABORT`, `SPLIT`, `SKIP`, `FILL_NEAREST`).
- Exposes both the integer key and the zero-padded string form per Sequence
  entry, so downstream nodes can present either form.

`fileseq` is used in `pad_style=PAD_STYLE_HASH1` mode throughout — this is
mandatory for our use case because the default (HASH4) interprets `####` as
16 zeros, not 4. The internal scanner enforces that style.

Sequence tokens inside directory components (e.g. `v_####_final/beauty.png`)
are NOT supported — fileseq cannot parse them, and we don't reimplement that
case here.
"""

from griptape_nodes.common.sequences.models import (
    InvalidSubsetBoundsError,
    InvalidTemplateError,
    MissingItemError,
    MissingItemPolicy,
    NoTokenBehavior,
    Sequence,
    SequenceEntry,
    SequenceScanOptions,
)

__all__ = [
    "InvalidSubsetBoundsError",
    "InvalidTemplateError",
    "MissingItemError",
    "MissingItemPolicy",
    "NoTokenBehavior",
    "Sequence",
    "SequenceEntry",
    "SequenceScanOptions",
]
