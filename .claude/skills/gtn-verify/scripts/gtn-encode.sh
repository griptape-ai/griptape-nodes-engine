#!/usr/bin/env bash
# Encode frames captured by a dev-browser script into an mp4, then clear them.
#
#   gtn-encode.sh [--fps N | --duration-ms N] [--out PATH] [--prefix NAME] [--keep]
#
# Pairs with the storyboard/smooth-flow patterns: a single dev-browser script
# drives the app and calls saveScreenshot(..., "gtnrec_00000.jpg") as it goes,
# then this turns the frames into video.
#
# --duration-ms is the real elapsed wall-clock time the capture loop ran for
# (the smooth-flow pattern prints this). When given, fps is computed as
# frame_count / (duration_ms / 1000) instead of taken from --fps, so playback
# speed matches how long the interaction actually took — this is what makes a
# smooth-flow recording look real-time instead of arbitrarily sped up or
# slowed down. --fps is still the right choice for the plain storyboard
# pattern, where frames are one-per-action rather than wall-clock-sampled.
set -euo pipefail

FPS=4
DURATION_MS=""
OUT="/tmp/gtn-storyboard.mp4"
PREFIX="gtnrec_"
KEEP=0
TMP="${HOME}/.dev-browser/tmp"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fps)          FPS="$2";         shift 2 ;;
    --duration-ms)  DURATION_MS="$2"; shift 2 ;;
    --out)          OUT="$2";         shift 2 ;;
    --prefix)       PREFIX="$2";      shift 2 ;;
    --keep)         KEEP=1;           shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

COUNT=$(ls "${TMP}/${PREFIX}"*.jpg 2>/dev/null | wc -l | tr -d ' ')
if [[ "${COUNT}" -eq 0 ]]; then
  echo "ERROR: no frames matching ${TMP}/${PREFIX}*.jpg" >&2
  exit 1
fi

if [[ -n "${DURATION_MS}" ]]; then
  FPS=$(awk -v c="${COUNT}" -v d="${DURATION_MS}" 'BEGIN { printf "%.3f", c / (d / 1000) }')
fi

ffmpeg -y -loglevel error \
  -framerate "${FPS}" -pattern_type glob -i "${TMP}/${PREFIX}*.jpg" \
  -c:v libx264 -pix_fmt yuv420p -vf "scale=1280:-2" \
  "${OUT}"

[[ "${KEEP}" -eq 1 ]] || rm -f "${TMP}/${PREFIX}"*.jpg
echo "frames=${COUNT} fps=${FPS} out=${OUT}"
