#!/usr/bin/env bash
# Symlink the launcher into ~/.local/bin. Safe to run again.
set -euo pipefail

repo="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
mkdir -p "$HOME/.local/bin"
ln -sfn "$repo/bin/agent-sessions" "$HOME/.local/bin/agent-sessions"
echo "agent-sessions -> $repo/bin/agent-sessions"
