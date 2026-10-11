#!/usr/bin/env bash
# Set agent-sessions up for this user. Safe to run again.
#   1. ./.venv with the pinned Textual (requirements-tui.txt), for the TUI
#   2. ~/.local/bin/agent-sessions -> bin/agent-sessions
#   3. the desktop launcher in ~/.local/share/applications
# Nothing is installed system-wide.
set -euo pipefail

repo="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

if [ ! -x "$repo/.venv/bin/python" ]; then
  python3 -m venv "$repo/.venv"
fi
"$repo/.venv/bin/python" -m pip install --quiet --disable-pip-version-check \
  -r "$repo/requirements-tui.txt"
echo "venv: $repo/.venv ($("$repo/.venv/bin/python" -c \
  'from importlib.metadata import version; print("textual", version("textual"))'))"

mkdir -p "$HOME/.local/bin"
ln -sfn "$repo/bin/agent-sessions" "$HOME/.local/bin/agent-sessions"
echo "agent-sessions -> $repo/bin/agent-sessions"

apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$apps"
install -m 644 "$repo/share/agent-sessions.desktop" "$apps/agent-sessions.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q "$apps" || true
fi
echo "desktop launcher: $apps/agent-sessions.desktop"
