#!/usr/bin/env bash
# Set agent-sessions up for this user, from this checkout. Safe to run again.
#   1. ./.venv with the pinned Textual (requirements-tui.txt), for the TUI
#   2. ~/.local/bin/agent-sessions -> bin/agent-sessions
#   3. the desktop launcher in ~/.local/share/applications
#      ($XDG_DATA_HOME/applications when that names an absolute path)
# Nothing is installed system-wide. ./uninstall.sh takes 2 and 3 back.
#
# What the tools this script runs write besides those three: pip keeps its
# download cache (~/.cache/pip, unless pip is configured otherwise), and
# update-desktop-database, where it exists, rewrites mimeinfo.cache in the
# applications directory.
#
# What is already there is not taken over without a word:
#   - a link at ~/.local/bin/agent-sessions that points somewhere else is left
#     and the script stops before it makes anything (--force points it here);
#     a file there that is no link is never replaced
#   - a desktop launcher that is neither the one this checkout installs nor
#     one an earlier version installed (LAUNCHERS_BEFORE) is someone's own,
#     with another terminal in its Exec line for example: it is kept, and the
#     script says so (--force replaces it)
#
#   ./install.sh          what a user needs
#   ./install.sh --dev    also the development tools (ruff, mypy, coverage) and
#                         the project itself, editable, as `make dev` does
#   ./install.sh --force  replace a link or a launcher that is not this checkout's
#
# PYTHON names the interpreter the venv is made with (default: python3).
set -euo pipefail

# The SHA-256 of every share/agent-sessions.desktop an earlier version of this
# script installed, oldest first. A launcher that is one of these was never
# edited, so a newer one may take its place. When the desktop file changes,
# add the hash of the version it replaces here and in uninstall.sh
# (tests/test_install_scripts.py compares both lists with the git history).
LAUNCHERS_BEFORE=(
  17be0e461b31543ada8de33e12eafb471fd88af4145c06d6f86741dd9589131e # 0.1.0 to 0.2.0: no TryExec
)

usage() {
  echo "usage: ./install.sh [--dev] [--force]"
}

die() {
  echo "install.sh: $*" >&2
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

dev=0
force=0
for arg in "$@"; do
  case "$arg" in
    --dev) dev=1 ;;
    --force) force=1 ;;
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

# The same checks as in uninstall.sh: both scripts must mean the same places.
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

repo="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
python="${PYTHON:-python3}"
venv="$repo/.venv"
bin="$HOME/.local/bin"
link="$bin/agent-sessions"
desktop="$repo/share/agent-sessions.desktop"
apps="$data/applications"
installed="$apps/agent-sessions.desktop"

# --- the interpreter ---------------------------------------------------------

command -v "$python" >/dev/null 2>&1 ||
  die "$python was not found; agent-sessions needs Python 3.11 or newer"
found="$("$python" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')" ||
  die "$python could not be run"
# The floor is checked here, on the numbers, not left to the interpreter asked.
IFS=. read -r major minor _ <<<"$found"
[[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]] ||
  die "$python did not say which Python it is (it printed: $found)"
if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 11 ]; }; then
  die "$python is Python $found; agent-sessions needs Python 3.11 or newer (PYTHON=/path/to/python ./install.sh uses another one)"
fi
"$python" -c 'import venv, ensurepip' 2>/dev/null ||
  die "$python has no venv module with pip (on Debian and Ubuntu: install python3-venv)"

# --- what is in the two places already: looked at before anything is made ----

link_was="" # where a link that is not ours pointed, when it is being replaced
if [ -L "$link" ]; then
  if [ "$(readlink -f "$link")" != "$repo/bin/agent-sessions" ]; then
    link_was="$(readlink "$link")"
    [ "$force" -eq 1 ] ||
      die "$link points to $link_was, not into this checkout; it was left as it is (--force points it here)"
  fi
elif [ -e "$link" ]; then
  # Not ours: a file someone put there (a `pip install --user`, a copy).
  die "$link exists and is not a symbolic link; move it away and run ./install.sh again"
fi

# new: nothing there. same: ours already. older: an earlier version's.
# link, other: someone's own, kept unless --force.
launcher="new"
if [ -L "$installed" ]; then
  launcher="link"
elif [ -d "$installed" ]; then
  die "$installed is a directory; move it away and run ./install.sh again"
elif [ -e "$installed" ]; then
  launcher="other"
  if cmp -s -- "$installed" "$desktop"; then
    launcher="same"
  else
    have="$(sha256 "$installed")"
    for known in "${LAUNCHERS_BEFORE[@]}"; do
      if [ -n "$have" ] && [ "$have" = "$known" ]; then
        launcher="older"
      fi
    done
  fi
fi

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

mkdir -p "$bin"
ln -sfn "$repo/bin/agent-sessions" "$link"
if [ -n "$link_was" ]; then
  echo "agent-sessions -> $repo/bin/agent-sessions (replaced the link to $link_was)"
else
  echo "agent-sessions -> $repo/bin/agent-sessions"
fi
case ":${PATH:-}:" in
  *":$bin:"*) ;;
  *) echo "warning: $bin is not on PATH; add it, or run $link" >&2 ;;
esac

# --- 3. the desktop launcher ---------------------------------------------------

if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$desktop" || die "$desktop is not a valid desktop file"
fi
replaced=""
case "$launcher" in
  older) replaced=" (replaced the launcher of an earlier version)" ;;
  link) replaced=" (replaced a symbolic link to $(readlink "$installed"))" ;;
  other) replaced=" (replaced a launcher that was not this checkout's)" ;;
esac
if [ "$force" -ne 1 ] && { [ "$launcher" = "link" ] || [ "$launcher" = "other" ]; }; then
  if [ "$launcher" = "link" ]; then
    echo "kept $installed: it is a symbolic link, not a launcher this script installed (--force replaces it)"
  else
    echo "kept $installed: it is not a launcher this script installed (--force replaces it)"
  fi
else
  mkdir -p "$apps"
  if [ "$launcher" = "link" ]; then
    rm -f -- "$installed" # the link itself: what it points to is not written
  fi
  if [ "$launcher" != "same" ]; then
    install -m 644 "$desktop" "$installed"
  fi
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q "$apps" || true
  fi
  echo "desktop launcher: $installed$replaced"
  if ! command -v omarchy-launch-tui >/dev/null 2>&1; then
    echo "warning: the desktop launcher starts the TUI with omarchy-launch-tui, which is" \
      "not on PATH; start agent-sessions from a terminal instead" >&2
  fi
fi
