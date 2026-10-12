#!/usr/bin/env bash
# Take back what ./install.sh of this checkout put into the home directory:
#   1. ~/.local/bin/agent-sessions, when it is a symbolic link to this
#      checkout's bin/agent-sessions (anything else there is left alone)
#   2. the desktop launcher in ~/.local/share/applications, when it is the
#      file this checkout installs, byte for byte
#
#   ./uninstall.sh                  the two above
#   ./uninstall.sh --purge          also the program's own data, after a question:
#                                   ~/.cache/agent-sessions (the index),
#                                   ~/.local/state/agent-sessions (the import
#                                   journal, the log, exported transcripts) and
#                                   ~/.config/agent-sessions (drive letters set
#                                   by hand)
#   ./uninstall.sh --purge --yes    the same without the question
#
# Never touched: the session stores (~/.claude, ~/.codex, ~/.grok, ~/.gemini),
# imported sessions in them included, and this checkout with its .venv.
set -euo pipefail

usage() {
  echo "usage: ./uninstall.sh [--purge [--yes]]"
}

die() {
  echo "uninstall.sh: $*" >&2
  exit 1
}

purge=0
yes=0
for arg in "$@"; do
  case "$arg" in
    --purge) purge=1 ;;
    --yes) yes=1 ;;
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
case "$HOME" in
  /*) ;;
  *) die "HOME is not an absolute path: $HOME" ;;
esac

if [ "$purge" -eq 1 ] && [ "$yes" -ne 1 ] && [ ! -t 0 ]; then
  # Said before anything is removed, not half way through.
  die "not a terminal to ask on; pass --yes to purge without the question"
fi

repo="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

# --- 1. the command ------------------------------------------------------------

link="$HOME/.local/bin/agent-sessions"
if [ -L "$link" ]; then
  if [ "$(readlink -f "$link")" = "$repo/bin/agent-sessions" ]; then
    rm -f -- "$link"
    echo "removed $link"
  else
    echo "left $link: it points to $(readlink "$link"), not into this checkout"
  fi
elif [ -e "$link" ]; then
  echo "left $link: it is not a symbolic link, so ./install.sh did not put it there"
else
  echo "no $link"
fi

# --- 2. the desktop launcher ---------------------------------------------------

apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
desktop="$apps/agent-sessions.desktop"
if [ -L "$desktop" ] || [ ! -e "$desktop" ]; then
  if [ -L "$desktop" ]; then
    echo "left $desktop: it is a symbolic link, so ./install.sh did not put it there"
  else
    echo "no $desktop"
  fi
elif cmp -s -- "$desktop" "$repo/share/agent-sessions.desktop"; then
  rm -f -- "$desktop"
  echo "removed $desktop"
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q "$apps" || true
  fi
else
  echo "left $desktop: it is not the launcher this checkout installs"
fi

# --- 3. the program's own data, only when asked --------------------------------

if [ "$purge" -eq 1 ]; then
  found=()
  for dir in "$HOME/.cache/agent-sessions" "$HOME/.local/state/agent-sessions" \
    "$HOME/.config/agent-sessions"; do
    # A symbolic link is not followed: what it points to is not ours to remove.
    if [ -d "$dir" ] && [ ! -L "$dir" ]; then
      found+=("$dir")
    fi
  done
  if [ "${#found[@]}" -eq 0 ]; then
    echo "nothing to purge: no cache, state or configuration directory"
  else
    echo "to be removed, with everything in them:"
    printf '  %s\n' "${found[@]}"
    if [ "$yes" -ne 1 ]; then
      printf 'Remove them? [y/N] ' >&2
      read -r answer || answer=""
      case "$answer" in
        y | Y | yes | YES) ;;
        *)
          echo "nothing was purged"
          exit 1
          ;;
      esac
    fi
    for dir in "${found[@]}"; do
      rm -rf -- "$dir"
      echo "removed $dir"
    done
  fi
fi

echo "left as they are: the session stores, and this checkout with its .venv"
echo "to remove the checkout too: rm -rf $(printf '%q' "$repo")"
