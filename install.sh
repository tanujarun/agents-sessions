#!/usr/bin/env bash
# Set agent-sessions up for this user, from this checkout. Safe to run again.
#   1. ./.venv with the pinned Textual (requirements-tui.txt), for the TUI
#   2. ~/.local/bin/agent-sessions -> bin/agent-sessions
#   3. the desktop launcher in ~/.local/share/applications
# Nothing is installed system-wide, and nothing outside those three places is
# written. ./uninstall.sh takes 2 and 3 back.
#
#   ./install.sh          what a user needs
#   ./install.sh --dev    also the development tools (ruff, mypy, coverage) and
#                         the project itself, editable, as `make dev` does
#
# PYTHON names the interpreter the venv is made with (default: python3).
set -euo pipefail

usage() {
  echo "usage: ./install.sh [--dev]"
}

die() {
  echo "install.sh: $*" >&2
  exit 1
}

dev=0
for arg in "$@"; do
  case "$arg" in
    --dev) dev=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

[ -n "${HOME:-}" ] || die "HOME is not set"

repo="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
python="${PYTHON:-python3}"
venv="$repo/.venv"

# --- the interpreter ---------------------------------------------------------

command -v "$python" >/dev/null 2>&1 ||
  die "$python was not found; agent-sessions needs Python 3.11 or newer"
found="$("$python" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')" ||
  die "$python could not be run"
"$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' ||
  die "$python is Python $found; agent-sessions needs Python 3.11 or newer (PYTHON=/path/to/python ./install.sh uses another one)"
"$python" -c 'import venv, ensurepip' 2>/dev/null ||
  die "$python has no venv module with pip (on Debian and Ubuntu: install python3-venv)"

# --- 1. the venv ---------------------------------------------------------------

if [ ! -x "$venv/bin/python" ]; then
  "$python" -m venv "$venv" || die "could not create $venv"
fi
"$venv/bin/python" -m pip --version >/dev/null 2>&1 ||
  die "$venv has no pip; remove $venv and run ./install.sh again"
if [ "$dev" -eq 1 ]; then
  "$venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    -e "${repo}[tui,dev]" || die "pip could not install the project with its development tools"
else
  "$venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    -r "$repo/requirements-tui.txt" || die "pip could not install Textual (requirements-tui.txt)"
fi
echo "venv: $venv ($("$venv/bin/python" -c \
  'from importlib.metadata import version; print("textual", version("textual"))'))"

# --- 2. the command ------------------------------------------------------------

bin="$HOME/.local/bin"
link="$bin/agent-sessions"
mkdir -p "$bin"
if [ -e "$link" ] && [ ! -L "$link" ]; then
  # Not ours: a file someone put there (a `pip install --user`, a copy).
  die "$link exists and is not a symbolic link; move it away and run ./install.sh again"
fi
ln -sfn "$repo/bin/agent-sessions" "$link"
echo "agent-sessions -> $repo/bin/agent-sessions"
case ":${PATH:-}:" in
  *":$bin:"*) ;;
  *) echo "warning: $bin is not on PATH; add it, or run $link" >&2 ;;
esac

# --- 3. the desktop launcher ---------------------------------------------------

desktop="$repo/share/agent-sessions.desktop"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$desktop" || die "$desktop is not a valid desktop file"
fi
mkdir -p "$apps"
install -m 644 "$desktop" "$apps/agent-sessions.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q "$apps" || true
fi
echo "desktop launcher: $apps/agent-sessions.desktop"
if ! command -v omarchy-launch-tui >/dev/null 2>&1; then
  echo "warning: the desktop launcher starts the TUI with omarchy-launch-tui, which is" \
    "not on PATH; start agent-sessions from a terminal instead" >&2
fi
