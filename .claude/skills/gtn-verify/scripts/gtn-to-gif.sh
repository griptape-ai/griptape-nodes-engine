#!/usr/bin/env bash
# Convert a recording to a small GIF for inline preview in Claude Code chat.
#
#   gtn-to-gif.sh --in IN.mp4 --out OUT.gif [--fps 8] [--width 480]
#
# Claude Code cannot render mp4/webm inline anywhere (VS Code panel, Read
# tool, or Artifacts — Artifacts are CSP-locked to the point video sources
# aren't practical). GIF is a plain image format, so it renders inline via
# Read exactly like a PNG/JPG does. Use this when the user needs to *see* the
# motion in the conversation; keep the source mp4 as the archival deliverable
# (better quality, smaller file) since the GIF is a lossy, chat-sized preview.
#
# Two-pass palette approach: ffmpeg's generic GIF encoder looks noticeably
# banded/dithered on UI screenshots (flat colors, sharp text) without this.
set -euo pipefail

IN=""
OUT="/tmp/gtn-preview.gif"
FPS=8
WIDTH=480

while [[ $# -gt 0 ]]; do
  case "$1" in
    --in)    IN="$2";    shift 2 ;;
    --out)   OUT="$2";   shift 2 ;;
    --fps)   FPS="$2";   shift 2 ;;
    --width) WIDTH="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -n "${IN}" ]] || { echo "ERROR: --in is required" >&2; exit 1; }
[[ -f "${IN}" ]] || { echo "ERROR: input file not found: ${IN}" >&2; exit 1; }

PALETTE="$(mktemp -t gtn-gif-palette).png"
trap 'rm -f "${PALETTE}"' EXIT

ffmpeg -y -loglevel error -i "${IN}" \
  -vf "fps=${FPS},scale=${WIDTH}:-1:flags=lanczos,palettegen" \
  "${PALETTE}"

ffmpeg -y -loglevel error -i "${IN}" -i "${PALETTE}" \
  -filter_complex "fps=${FPS},scale=${WIDTH}:-1:flags=lanczos[x];[x][1:v]paletteuse" \
  "${OUT}"

echo "fps=${FPS} width=${WIDTH} out=${OUT}"
