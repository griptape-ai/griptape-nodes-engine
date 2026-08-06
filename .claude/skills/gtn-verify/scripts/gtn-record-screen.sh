#!/usr/bin/env bash
# Record the actual screen, including native window chrome, menus and file
# dialogs that CDP screenshots cannot see.
#
#   gtn-record-screen.sh [--secs N] [--out PATH] [--display N] [--no-focus]
#
# macOS: requires Screen Recording permission for the terminal application
#   (System Settings -> Privacy & Security -> Screen Recording). Without it
#   macOS silently hands back a black or desktop-picture-only frame, so this
#   script probes first and fails loudly instead.
#
# Linux: tries Wayland first (wf-recorder), then falls back to X11 (ffmpeg
#   x11grab). wf-recorder only works on wlroots-based compositors (Sway,
#   Hyprland, river) — it does not support the portal-based capture GNOME/KDE
#   Wayland require. On those desktops, run this from an X11 session (or
#   XWayland) instead. This path is best effort and has not been exercised
#   against a real Linux install of the app.
#
# Windows: use gtn-record-screen.ps1 instead (ffmpeg gdigrab).
set -euo pipefail

SECS=10
OUT="/tmp/gtn-screen.mp4"
DISPLAY_IDX=""
FOCUS=1
OS="$(uname -s)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --secs)     SECS="$2";        shift 2 ;;
    --out)      OUT="$2";         shift 2 ;;
    --display)  DISPLAY_IDX="$2"; shift 2 ;;
    --no-focus) FOCUS=0;          shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

command -v ffmpeg >/dev/null || { echo "ERROR: ffmpeg not installed" >&2; exit 1; }

record_macos() {
  if [[ -z "${DISPLAY_IDX}" ]]; then
    # avfoundation lists capture devices on stderr; screen devices are named
    # "Capture screen N". Take the first one.
    # Device enumeration writes the list to stderr and then exits non-zero (there
    # is no real input to open), and `head` closing early would SIGPIPE the
    # producer — both trip `pipefail`. So swallow the status and let awk pick the
    # first match without an extra pipeline stage.
    DISPLAY_IDX=$(ffmpeg -f avfoundation -list_devices true -i "" 2>&1 \
      | awk 'match($0, /\[[0-9]+\] Capture screen/) { s = substr($0, RSTART + 1, RLENGTH); sub(/\].*/, "", s); print s; exit }') || true
  fi
  [[ -n "${DISPLAY_IDX}" ]] || { echo "ERROR: no avfoundation screen device found" >&2; exit 1; }

  # Permission probe: screencapture writes a zero-length file when denied.
  local probe
  probe="$(mktemp -t gtnprobe).png"
  screencapture -x "${probe}" 2>/dev/null || true
  if [[ ! -s "${probe}" ]]; then
    rm -f "${probe}"
    cat >&2 <<'MSG'
ERROR: Screen Recording permission is not granted to this terminal.
Grant it in System Settings -> Privacy & Security -> Screen Recording,
then fully restart the terminal application and retry.
MSG
    exit 1
  fi
  rm -f "${probe}"

  if [[ "${FOCUS}" -eq 1 ]]; then
    osascript -e 'tell application "Griptape Nodes" to activate' >/dev/null 2>&1 || true
    sleep 1
  fi

  echo "recording screen ${DISPLAY_IDX} for ${SECS}s..." >&2
  # -pixel_format nv12 matches what the avfoundation screen device actually
  # offers; without it ffmpeg still works by auto-negotiating, but prints a
  # misleading "not supported" warning for the yuv420p it defaults to.
  ffmpeg -y -loglevel error \
    -f avfoundation -capture_cursor 1 -pixel_format nv12 -framerate 30 -i "${DISPLAY_IDX}:none" \
    -t "${SECS}" -c:v libx264 -preset veryfast -pix_fmt yuv420p -vf "scale=1600:-2" \
    "${OUT}"

  echo "out=${OUT} secs=${SECS} display=${DISPLAY_IDX}"
}

record_linux_wayland() {
  if [[ "${FOCUS}" -eq 1 ]]; then
    echo "note: window focus is not automated on Wayland (external clients can't move focus); ensure the app is visible yourself" >&2
  fi

  echo "recording Wayland output${DISPLAY_IDX:+ ${DISPLAY_IDX}} for ${SECS}s via wf-recorder..." >&2
  # wf-recorder has no built-in duration flag; SIGINT tells it to finalize
  # the container cleanly, unlike SIGTERM/SIGKILL which can leave it corrupt.
  local -a args=(-f "${OUT}" -c libx264)
  [[ -n "${DISPLAY_IDX}" ]] && args+=(-o "${DISPLAY_IDX}")
  timeout --signal=INT "${SECS}" wf-recorder "${args[@]}" || true

  [[ -s "${OUT}" ]] || { echo "ERROR: wf-recorder produced no output" >&2; exit 1; }
  echo "out=${OUT} secs=${SECS}"
}

record_linux_x11() {
  local x11_display="${DISPLAY_IDX:-${DISPLAY:-:0}}"
  local size=""
  if command -v xdpyinfo >/dev/null 2>&1; then
    size=$(DISPLAY="${x11_display}" xdpyinfo 2>/dev/null | awk '/dimensions:/{print $2; exit}')
  fi
  if [[ -z "${size}" ]]; then
    echo "warn: could not detect display size (xdpyinfo missing or failed), defaulting to 1920x1080" >&2
    size="1920x1080"
  fi

  if [[ "${FOCUS}" -eq 1 ]] && command -v wmctrl >/dev/null 2>&1; then
    wmctrl -a "Griptape Nodes" 2>/dev/null || true
    sleep 1
  fi

  echo "recording X11 display ${x11_display} (${size}) for ${SECS}s..." >&2
  ffmpeg -y -loglevel error \
    -f x11grab -video_size "${size}" -framerate 30 -i "${x11_display}" \
    -t "${SECS}" -c:v libx264 -preset veryfast -pix_fmt yuv420p -vf "scale=1600:-2" \
    "${OUT}"

  echo "out=${OUT} secs=${SECS} display=${x11_display}"
}

record_linux() {
  if [[ "${XDG_SESSION_TYPE:-}" == "wayland" ]] && command -v wf-recorder >/dev/null 2>&1; then
    record_linux_wayland
  elif [[ -n "${DISPLAY:-}" || -n "${DISPLAY_IDX}" ]]; then
    record_linux_x11
  else
    cat >&2 <<MSG
ERROR: no working screen-capture backend found.
Wayland: install wf-recorder (only works on wlroots compositors: Sway, Hyprland, river).
X11: ensure \$DISPLAY is set (works via XWayland too).
MSG
    exit 1
  fi
}

case "${OS}" in
  Darwin) record_macos ;;
  Linux)  record_linux ;;
  *) echo "ERROR: unsupported OS '${OS}' — on Windows use gtn-record-screen.ps1." >&2; exit 1 ;;
esac
