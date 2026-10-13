#!/usr/bin/env bash
# Take back what ./install.sh of this checkout put into the home directory:
#   1. ~/.local/bin/agent-sessions, when it is a symbolic link to this
#      checkout's bin/agent-sessions (anything else there is left alone)
#   2. the desktop launcher in ~/.local/share/applications
#      ($XDG_DATA_HOME/applications when that names an absolute path), when it
#      is the file this checkout installs or one an earlier version installed
#      (LAUNCHERS_BEFORE), byte for byte; an edited one is left
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
# With AGENT_SESSIONS_STATE or AGENT_SESSIONS_CACHE set, --purge also takes the
# program's files out of the directory each names: the files it writes, by
# name, and nothing else. Such a directory is removed only when nothing else
# is left in it.
#
# Never touched: the session stores (~/.claude, ~/.codex, ~/.grok, ~/.gemini),
# imported sessions in them included, and this checkout with its .venv. Not
# taken back either: pip's download cache, and mimeinfo.cache in the
# applications directory (update-desktop-database rewrites it).
set -euo pipefail

# The SHA-256 of every share/agent-sessions.desktop an earlier version of
# install.sh installed; the same list as there.
LAUNCHERS_BEFORE=(
  17be0e461b31543ada8de33e12eafb471fd88af4145c06d6f86741dd9589131e # 0.1.0 to 0.2.0: no TryExec
)

usage() {
  echo "usage: ./uninstall.sh [--purge [--yes]]"
}

die() {
  echo "uninstall.sh: $*" >&2
  exit 1
}

# The SHA-256 of a file; nothing where no tool computes one.
sha256() {
  local sum=""
  if command -v sha256sum >/dev/null 2>&1; then
    read -r sum _ < <(sha256sum -- "$1") || true
  fi
  echo "$sum"
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

# The same checks as in install.sh: both scripts must mean the same places.
[ -n "${HOME:-}" ] || die "HOME is not set"
case "$HOME" in
  /*) ;;
  *) die "HOME is not an absolute path: $HOME" ;;
esac
data="${XDG_DATA_HOME:-}"
case "$data" in
  "" | /*) ;;
  *)
    # The XDG Base Directory Specification: a relative path is invalid and ignored.
    echo "warning: XDG_DATA_HOME is not an absolute path ($data); using $HOME/.local/share" >&2
    data=""
    ;;
esac
data="${data:-$HOME/.local/share}"

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

apps="$data/applications"
desktop="$apps/agent-sessions.desktop"
ours=0
if [ -f "$desktop" ] && [ ! -L "$desktop" ]; then
  if cmp -s -- "$desktop" "$repo/share/agent-sessions.desktop"; then
    ours=1
  else
    have="$(sha256 "$desktop")"
    for known in "${LAUNCHERS_BEFORE[@]}"; do
      if [ -n "$have" ] && [ "$have" = "$known" ]; then
        ours=1
      fi
    done
  fi
fi
if [ -L "$desktop" ]; then
  echo "left $desktop: it is a symbolic link, so ./install.sh did not put it there"
elif [ ! -e "$desktop" ]; then
  echo "no $desktop"
elif [ "$ours" -eq 1 ]; then
  rm -f -- "$desktop"
  echo "removed $desktop"
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q "$apps" || true
  fi
else
  echo "left $desktop: it is not a launcher ./install.sh installed"
fi

# --- 3. the program's own data, only when asked --------------------------------

if [ "$purge" -eq 1 ]; then
  shopt -s nullglob # a pattern that matches nothing names nothing
  whole=()          # directories that are the program's, with everything in them
  files=()          # the program's files in a directory the environment names
  holders=()        # those directories, innermost first: removed when left empty
  notes=()          # what was looked at and left, with the reason

  for dir in "$HOME/.cache/agent-sessions" "$HOME/.local/state/agent-sessions" \
    "$HOME/.config/agent-sessions"; do
    # A symbolic link is not followed: what it points to is not ours to remove.
    if [ -d "$dir" ] && [ ! -L "$dir" ]; then
      whole+=("$dir")
    fi
  done

  # True when $2, the directory the variable $1 names, is to be looked into.
  named() {
    local dir
    case "$2" in
      /*) ;;
      *)
        notes+=("left $2: $1 is not an absolute path")
        return 1
        ;;
    esac
    if [ -L "$2" ]; then
      notes+=("left $2: it is a symbolic link ($1)")
      return 1
    fi
    [ -d "$2" ] || return 1
    for dir in ${whole[@]+"${whole[@]}"}; do
      if [ "$dir" = "$2" ]; then
        return 1 # one of the three above: it goes whole
      fi
    done
    return 0
  }

  # Add those of the paths given that are files (or links: the link goes, not
  # what it points to) to `files`; true when there was one.
  ours() {
    local file any=1
    for file in "$@"; do
      if [ -L "$file" ] || [ -f "$file" ]; then
        files+=("$file")
        any=0
      fi
    done
    return "$any"
  }

  # The state directory: the log and its rotations, the lock file, the import
  # journal (and the file of that name from before it), exported transcripts.
  state="${AGENT_SESSIONS_STATE:-}"
  if [ -n "$state" ]; then
    [ "$state" = "/" ] || state="${state%/}"
    if named AGENT_SESSIONS_STATE "$state"; then
      held=0
      if [ -d "$state/handoff" ] && [ ! -L "$state/handoff" ]; then
        if ours "$state"/handoff/*-*.md "$state"/handoff/*-*.md.*.tmp; then
          holders+=("$state/handoff")
          held=1
        fi
      fi
      if ours "$state/agent-sessions.log" "$state"/agent-sessions.log.[0-9] \
        "$state/agent-sessions.lock" "$state/imports.jsonl" "$state/imports.json"; then
        held=1
      fi
      if [ "$held" -eq 1 ]; then
        holders+=("$state")
      fi
    fi
  fi

  # The cache: the index, in the directory named or, when the name ends in
  # .json, that file itself; and what a write that was cut short left beside it.
  cache="${AGENT_SESSIONS_CACHE:-}"
  if [ -n "$cache" ]; then
    case "$cache" in
      *.json) index="$cache" ;;
      *)
        [ "$cache" = "/" ] || cache="${cache%/}"
        index="$cache/index.json"
        ;;
    esac
    dir="$(dirname -- "$index")"
    if named AGENT_SESSIONS_CACHE "$dir"; then
      if ours "$index" "$index".[0-9]*.tmp; then
        holders+=("$dir")
      fi
    fi
  fi

  if [ "${#whole[@]}" -eq 0 ] && [ "${#files[@]}" -eq 0 ]; then
    echo "nothing to purge: no cache, state or configuration directory"
  else
    if [ "${#whole[@]}" -gt 0 ]; then
      echo "to be removed, with everything in them:"
      printf '  %s\n' "${whole[@]}"
    fi
    if [ "${#files[@]}" -gt 0 ]; then
      echo "to be removed, the program's own files and nothing else:"
      printf '  %s\n' "${files[@]}"
      echo "and, when nothing else is left in them:"
      printf '  %s\n' "${holders[@]}"
    fi
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
    for file in ${files[@]+"${files[@]}"}; do
      rm -f -- "$file"
      echo "removed $file"
    done
    for dir in ${holders[@]+"${holders[@]}"}; do
      if rmdir -- "$dir" 2>/dev/null; then
        echo "removed $dir"
      else
        echo "left $dir: other files are in it"
      fi
    done
    for dir in ${whole[@]+"${whole[@]}"}; do
      rm -rf -- "$dir"
      echo "removed $dir"
    done
  fi
  for note in ${notes[@]+"${notes[@]}"}; do
    echo "$note"
  done
fi

echo "left as they are: the session stores, and this checkout with its .venv"
echo "to remove the checkout too: rm -rf $(printf '%q' "$repo")"
