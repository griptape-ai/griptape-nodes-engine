#!/usr/bin/env bash
# Check for and install the CLI prerequisites this skill needs on macOS and
# Linux.
#
#   gtn-install-prereqs.sh [--dry-run]
#
# Installs: ffmpeg, python3, node/npm, the dev-browser CLI (+ its bundled
# Chromium), and — on Linux under a Wayland session — wf-recorder.
#
# Does NOT install:
#   - Homebrew, if missing on macOS (its installer runs a script fetched from
#     the internet with elevated access — left to the user to opt into).
#   - The Griptape Nodes Desktop app itself (a GUI installer download, not a
#     package-manager package). This script only checks whether it's present
#     and points at https://griptapenodes.com otherwise.
#   - macOS Screen Recording permission — that's a one-time manual grant in
#     System Settings, nothing here can flip it.
#
# On Windows, use gtn-install-prereqs.ps1 instead.
set -euo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

OS="$(uname -s)"

log() { echo "$@" >&2; }

run() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "would run: $*"
    return 0
  fi
  "$@"
}

detect_pkg_manager() {
  case "${OS}" in
    Darwin)
      command -v brew >/dev/null 2>&1 && { echo "brew"; return 0; }
      ;;
    Linux)
      command -v apt-get >/dev/null 2>&1 && { echo "apt"; return 0; }
      command -v dnf >/dev/null 2>&1    && { echo "dnf"; return 0; }
      command -v pacman >/dev/null 2>&1 && { echo "pacman"; return 0; }
      command -v zypper >/dev/null 2>&1 && { echo "zypper"; return 0; }
      ;;
  esac
  return 1
}

PKG_MGR="$(detect_pkg_manager || true)"

if [[ "${PKG_MGR}" == "apt" ]]; then
  log "updating apt package lists..."
  run sudo apt-get update -y || true
fi

# $1: package name (assumed the same across managers for everything this
# script installs — true for ffmpeg/python3/wf-recorder, handled specially
# for node below).
pkg_install() {
  local pkg="$1"
  case "${PKG_MGR}" in
    brew)   run brew install "${pkg}" ;;
    apt)    run sudo apt-get install -y "${pkg}" ;;
    dnf)    run sudo dnf install -y "${pkg}" ;;
    pacman) run sudo pacman -Sy --noconfirm "${pkg}" ;;
    zypper) run sudo zypper install -y "${pkg}" ;;
    *)
      log "ERROR: no supported package manager found (brew/apt/dnf/pacman/zypper) to install '${pkg}'."
      return 1
      ;;
  esac
}

check() {
  local name="$1" probe="$2" install_fn="$3"
  if eval "${probe}" >/dev/null 2>&1; then
    log "[ok] ${name}"
  else
    log "[missing] ${name} — installing..."
    "${install_fn}" || true
    if eval "${probe}" >/dev/null 2>&1; then
      log "[installed] ${name}"
    else
      log "[FAILED] ${name} — install it manually"
    fi
  fi
}

install_ffmpeg()  { pkg_install ffmpeg; }
install_python3() { pkg_install python3; }

install_node() {
  case "${PKG_MGR}" in
    apt) pkg_install nodejs && pkg_install npm ;;
    *)   pkg_install node ;;
  esac
}

check "ffmpeg"  "command -v ffmpeg"  install_ffmpeg
check "python3" "command -v python3" install_python3
check "npm"     "command -v npm"     install_node

if command -v dev-browser >/dev/null 2>&1; then
  log "[ok] dev-browser"
elif command -v npm >/dev/null 2>&1; then
  log "[missing] dev-browser — installing via npm..."
  run npm install -g dev-browser || true
  if command -v dev-browser >/dev/null 2>&1; then
    log "[installed] dev-browser — fetching its bundled Chromium..."
    run dev-browser install || true
  else
    log "[FAILED] dev-browser"
  fi
else
  log "[FAILED] dev-browser — npm is not available, install Node.js first"
fi

if [[ "${OS}" == "Linux" && "${XDG_SESSION_TYPE:-}" == "wayland" ]]; then
  if command -v wf-recorder >/dev/null 2>&1; then
    log "[ok] wf-recorder"
  else
    log "[optional] wf-recorder — needed only for native screen recording, and only works on wlroots compositors (Sway, Hyprland, river); installing..."
    pkg_install wf-recorder || log "[skip] wf-recorder not available via ${PKG_MGR:-<none>} — screen recording will need an X11/XWayland session instead"
  fi
fi

case "${OS}" in
  Darwin)
    if [[ -d "/Applications/Griptape Nodes.app" ]]; then
      log "[ok] Griptape Nodes Desktop app"
    else
      log "[missing] Griptape Nodes Desktop app — download it from https://griptapenodes.com (not auto-installed by this script)"
    fi
    log "[manual] Screen Recording permission — try gtn-record-screen.sh once; if it fails, grant it in System Settings -> Privacy & Security -> Screen Recording"
    ;;
  Linux)
    if command -v griptape-nodes-desktop >/dev/null 2>&1 || [[ -n "${GTN_DESKTOP_BIN:-}" && -x "${GTN_DESKTOP_BIN}" ]]; then
      log "[ok] Griptape Nodes Desktop app"
    else
      log "[missing] Griptape Nodes Desktop app — download it from https://griptapenodes.com, or set GTN_DESKTOP_BIN if it's already installed somewhere non-standard"
    fi
    ;;
esac

if command -v claude >/dev/null 2>&1; then
  if claude mcp list 2>/dev/null | grep -q "griptape-nodes"; then
    log "[ok] griptape-nodes MCP server registered"
  else
    log "[missing] griptape-nodes MCP server — with the engine running, register it:"
    log "    claude mcp add --transport http griptape-nodes http://localhost:8125/mcp/ --scope user"
  fi
else
  log "[skip] claude CLI not found, cannot check MCP server registration"
fi

log "done."
