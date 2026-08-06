#!/usr/bin/env bash
# Record page content from Griptape Nodes to an mp4, by capturing a JPEG frame
# loop over CDP and encoding with ffmpeg.
#
#   gtn-record.sh [--target desktop|web] [--secs N] [--fps N] [--out PATH]
#
# Captures page pixels only — no native window chrome. Use gtn-record-screen.sh
# for that.
#
# To record an interaction rather than a static screen, run this in the
# background and drive the app with a separate dev-browser script while it
# captures.
set -euo pipefail

TARGET="desktop"
SECS=8
FPS=6
OUT="/tmp/gtn-recording.mp4"
PORT="${GTN_CDP_PORT:-9222}"
PROFILE="${GTN_WEB_PROFILE:-gtn-web}"
TMP="${HOME}/.dev-browser/tmp"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="$2"; shift 2 ;;
    --secs)   SECS="$2";   shift 2 ;;
    --fps)    FPS="$2";    shift 2 ;;
    --out)    OUT="$2";    shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

FRAMES=$(( SECS * FPS ))
DELAY_MS=$(( 1000 / FPS ))
# Screenshots are not instant, so allow generous headroom over the nominal
# duration before the daemon kills the script.
SCRIPT_TIMEOUT=$(( SECS * 3 + 60 ))

# Stale frames from a previous run would be swept into this recording by the
# glob, so clear them first.
rm -f "${TMP}"/gtnrec_*.jpg 2>/dev/null || true
mkdir -p "${TMP}"

read -r -d '' LOOP <<EOF || true
const N = ${FRAMES};
for (let i = 0; i < N; i++) {
  const buf = await target.screenshot({ type: "jpeg", quality: 70 });
  await saveScreenshot(buf, "gtnrec_" + String(i).padStart(5, "0") + ".jpg");
  await target.waitForTimeout(${DELAY_MS});
}
console.log("frames=" + N);
EOF

if [[ "${TARGET}" == "desktop" ]]; then
  dev-browser --connect "http://localhost:${PORT}" --timeout "${SCRIPT_TIMEOUT}" <<EOF
const pages = await browser.listPages();
const target = await browser.getPage(pages[0].id);
${LOOP}
EOF
else
  dev-browser --browser "${PROFILE}" --timeout "${SCRIPT_TIMEOUT}" <<EOF
const target = await browser.getPage("hosted");
${LOOP}
EOF
fi

COUNT=$(ls "${TMP}"/gtnrec_*.jpg 2>/dev/null | wc -l | tr -d ' ')
if [[ "${COUNT}" -eq 0 ]]; then
  echo "ERROR: no frames captured" >&2
  exit 1
fi

ffmpeg -y -loglevel error \
  -framerate "${FPS}" -pattern_type glob -i "${TMP}/gtnrec_*.jpg" \
  -c:v libx264 -pix_fmt yuv420p -vf "scale=1280:-2" \
  "${OUT}"

rm -f "${TMP}"/gtnrec_*.jpg 2>/dev/null || true
echo "frames=${COUNT} fps=${FPS} out=${OUT}"
