#!/usr/bin/env bash
# Collapse static "thinking pause" stretches out of an already-recorded video,
# without touching moving sections.
#
#   gtn-trim-pauses.sh --in IN.mp4 --out OUT.mp4 [--sensitivity low|medium|high]
#
# Fallback for recordings that unavoidably span multiple agent turns (e.g. a
# native gtn-record-screen.sh capture running across several tool calls, where
# real thinking time between calls becomes dead air in the footage). Prefer
# the smooth-flow single-script pattern in SKILL.md when the whole interaction
# can be pre-planned — it never records the dead air in the first place, which
# is strictly better than trimming it out after the fact.
#
# Mechanism: mpdecimate drops frames that are near-duplicates of the previous
# kept frame (i.e. nothing visibly changed), then setpts re-times the
# remaining frames back-to-back so the gap left by the dropped frames
# collapses instead of leaving the video paused-then-jump-cut.
set -euo pipefail

IN=""
OUT="/tmp/gtn-trimmed.mp4"
SENSITIVITY="medium"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --in)          IN="$2";          shift 2 ;;
    --out)         OUT="$2";         shift 2 ;;
    --sensitivity) SENSITIVITY="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -n "${IN}" ]] || { echo "ERROR: --in is required" >&2; exit 1; }
[[ -f "${IN}" ]] || { echo "ERROR: input file not found: ${IN}" >&2; exit 1; }

# hi/lo are 8x8 SAD thresholds per mpdecimate's own scale (max 64*64); frac is
# the fraction of blocks allowed to exceed lo before a frame counts as
# changed. Higher sensitivity = smaller thresholds = more frames judged
# "changed" = fewer pauses removed, less risk of eating slow-but-real motion.
case "${SENSITIVITY}" in
  low)    HI=768; LO=320; FRAC=0.5  ;;
  medium) HI=512; LO=256; FRAC=0.33 ;;
  high)   HI=256; LO=128; FRAC=0.2  ;;
  *) echo "ERROR: --sensitivity must be low, medium, or high" >&2; exit 1 ;;
esac

ffmpeg -y -loglevel error -i "${IN}" \
  -vf "mpdecimate=hi=${HI}:lo=${LO}:frac=${FRAC},setpts=N/FRAME_RATE/TB" \
  -an \
  "${OUT}"

echo "sensitivity=${SENSITIVITY} out=${OUT}"
