#!/usr/bin/env bash
# Bring Griptape Nodes Desktop up with CDP exposed on port 9222 and wait until
# the editor frame has attached. Idempotent: exits immediately if CDP is
# already live.
#
# Relaunching the app discards unsaved canvas state, so only run this when a
# restart is acceptable.
#
# macOS and Linux only — on Windows use gtn-desktop-up.ps1 instead (there is
# no bash-native equivalent for process management there).
set -euo pipefail

PORT="${GTN_CDP_PORT:-9222}"
APP="Griptape Nodes"
CDP="http://127.0.0.1:${PORT}"
OS="$(uname -s)"
case "${OS}" in
  Darwin|Linux) ;;
  *) echo "ERROR: unsupported OS '${OS}' — on Windows use gtn-desktop-up.ps1." >&2; exit 1 ;;
esac

cdp_up() { curl -s -m 2 "${CDP}/json/version" >/dev/null 2>&1; }

editor_attached() {
  curl -s -m 3 "${CDP}/json/list" 2>/dev/null \
    | python3 -c "import json,sys; sys.exit(0 if any(t.get('url','').startswith('gtn-editor://') for t in json.load(sys.stdin)) else 1)" 2>/dev/null
}

# Linux ships no app bundle (Velopack packages a plain "griptape-nodes-desktop"
# binary), so the install path isn't fixed the way "/Applications/*.app" is.
# Try an explicit override, then PATH, then the handful of layouts Velopack /
# common packaging conventions are known to use — best effort, not verified
# against a real Linux install of this app.
resolve_linux_bin() {
  if [[ -n "${GTN_DESKTOP_BIN:-}" ]]; then
    echo "${GTN_DESKTOP_BIN}"
    return 0
  fi
  if command -v griptape-nodes-desktop >/dev/null 2>&1; then
    command -v griptape-nodes-desktop
    return 0
  fi
  local candidates=(
    "${HOME}/.local/share/ai.griptape.nodes.desktop/current/griptape-nodes-desktop"
    "${HOME}/.local/opt/griptape-nodes-desktop/griptape-nodes-desktop"
    "/opt/Griptape Nodes/griptape-nodes-desktop"
  )
  local c
  for c in "${candidates[@]}"; do
    [[ -x "${c}" ]] && { echo "${c}"; return 0; }
  done
  return 1
}

is_running() {
  case "${OS}" in
    Darwin) pgrep -f "${APP}.app/Contents/MacOS/griptape-nodes-desktop" >/dev/null ;;
    Linux)  pgrep -f "griptape-nodes-desktop" >/dev/null ;;
  esac
}

quit_gracefully() {
  case "${OS}" in
    Darwin)
      osascript -e "quit app \"${APP}\"" >/dev/null 2>&1 || true
      ;;
    Linux)
      # Electron has no OS-level graceful-quit signal on Linux the way macOS
      # has Apple Events; SIGTERM is the best available approximation and may
      # not always flush unsaved state before the process exits.
      pkill -TERM -f "griptape-nodes-desktop" 2>/dev/null || true
      ;;
  esac
}

force_kill() {
  case "${OS}" in
    Darwin) pkill -f "${APP}.app" || true ;;
    Linux)  pkill -KILL -f "griptape-nodes-desktop" || true ;;
  esac
}

launch() {
  case "${OS}" in
    Darwin)
      open -a "${APP}" --args --remote-debugging-port="${PORT}"
      ;;
    Linux)
      local bin
      bin="$(resolve_linux_bin)" || {
        echo "ERROR: could not find the griptape-nodes-desktop binary. Set GTN_DESKTOP_BIN to its full path." >&2
        exit 1
      }
      nohup "${bin}" --remote-debugging-port="${PORT}" >/dev/null 2>&1 &
      disown
      ;;
    *)
      echo "ERROR: unsupported OS '${OS}' — on Windows use gtn-desktop-up.ps1." >&2
      exit 1
      ;;
  esac
}

if cdp_up; then
  echo "cdp=already-live port=${PORT}"
else
  if is_running; then
    echo "quitting running app (no CDP port)..." >&2
    quit_gracefully
    for _ in $(seq 1 20); do
      is_running || break
      sleep 1
    done
    if is_running; then
      echo "graceful quit timed out, force killing" >&2
      force_kill
      sleep 3
    fi
  fi

  echo "launching with --remote-debugging-port=${PORT}..." >&2
  launch

  for _ in $(seq 1 40); do
    cdp_up && break
    sleep 1
  done
  cdp_up || { echo "ERROR: CDP never came up on ${PORT}" >&2; exit 1; }
  echo "cdp=launched port=${PORT}"
fi

# The editor webview only attaches once the engine has booted. First launch
# after an update can take a while.
for _ in $(seq 1 90); do
  editor_attached && break
  sleep 2
done

if editor_attached; then
  echo "editor=attached"
else
  echo "editor=not-attached (engine may still be starting, or is stopped)" >&2
fi

curl -s -m 3 "${CDP}/json/list" \
  | python3 -c "import json,sys; [print('target', t['type'], t['url'][:100]) for t in json.load(sys.stdin)]"
