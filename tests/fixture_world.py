"""A small machine built from tests/fixtures: a fake home (the native stores)
and two fake drives, a Windows system volume ``WinC`` with the profile
``Users/user`` and a data volume ``DataF`` with ``Proj/My Site``, the folder
the Windows fixtures recorded as ``F:\\Proj\\My Site``. Nothing here touches a
real store, starts a process or notifies the desktop."""

import json
import os
import shutil
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FIXTURES, Sandbox  # noqa: E402

from agent_sessions import api, launch  # noqa: E402
from agent_sessions.fsutil import listdir_ro  # noqa: E402
from agent_sessions.providers.antigravity import Antigravity  # noqa: E402
from agent_sessions.providers.claude import Claude  # noqa: E402
from agent_sessions.providers.codex import Codex  # noqa: E402
from agent_sessions.providers.gemini import Gemini  # noqa: E402
from agent_sessions.providers.grok import Grok  # noqa: E402

REAL = {
    "antigravity": Antigravity,
    "claude": Claude,
    "codex": Codex,
    "gemini": Gemini,
    "grok": Grok,
}
TOOLS = ("agy", "claude", "codex", "grok", launch.LAUNCHER)

# Listed sessions of the Windows fixtures (tests/fixtures/README.md).
CLAUDE_WIN = "214de9ed-5aa4-4788-8a9f-cec8d18b3a3d"  # has a side directory
CLAUDE_WIN_PLAIN = "69669cf3-d7ae-4ba4-a028-c6abfd2bd622"
CODEX_WIN = "01a04ca1-6e27-7817-9f54-07105f962073"
GROK_WIN = "01a0a4c9-5206-7636-8fb7-5bef47aa8954"
AGY_WIN = "1ca54b8b-127f-4f86-9cf4-98d04217f5ce"  # cwd F:\Proj\My Site
AGY_NO_CWD = "45184a9e-8a4e-4f3f-8c4c-f0e85aeecaaf"
GEMINI_WIN = "5b0c3e6a-1f2d-4c7b-9a8e-0d1c2b3a4f5e"
# Listed sessions of the native fixtures.
CLAUDE_NATIVE = "3f47bd5d-9d03-4733-9887-2452632e5d1a"  # cwd /home/user/My Proj
CODEX_SEGMENTED = "01a0e538-c7bb-77dd-885b-0c4d54663241"  # base file + one segment
CODEX_NATIVE = "01a0e914-cf2b-7dcd-a58f-cb54c3f3e8c7"
GROK_NATIVE = "01a0ec08-671b-72e4-b2fb-47b426cf36e6"  # cwd /home/user/proj


class World:
    """``foreign``: providers whose Windows fixture goes on the fake drive.
    ``native``: providers whose native fixture goes into the fake home; the
    paths those recorded (``/home/user/…``) are mapped onto the fake home by
    a path-map override, and ``proj`` and ``My Proj`` exist there."""

    def __init__(self, case, foreign=tuple(REAL), native=()):
        self.case = case
        self.box = Sandbox(case, REAL)
        self.home = self.box.home
        self.c = self.box.mount("WinC")
        (self.c / "Windows").mkdir()
        self.profile = self.c / "Users" / "user"
        self.profile.mkdir(parents=True)
        self.f = self.box.mount("DataF")
        self.site = self.f / "Proj" / "My Site"
        self.site.mkdir(parents=True)
        for name in foreign:
            shutil.copytree(FIXTURES / name / "windows", self.profile, dirs_exist_ok=True)
        for name in native:
            shutil.copytree(FIXTURES / name / "native", self.home, dirs_exist_ok=True)
        if native:
            for folder in ("proj", "My Proj"):
                (self.home / folder).mkdir(exist_ok=True)
            self.override({"/home/user": str(self.home)})
        self.installed = set(TOOLS)
        which = mock.patch("shutil.which", side_effect=self._which)
        which.start()
        case.addCleanup(which.stop)
        self.started: list[tuple[list[str], str]] = []  # (command, cwd) of each launch
        self.notified: list[str] = []
        for name, hook in (
            ("starter", lambda command, cwd: self.started.append((command, cwd))),
            ("notifier", self.notified.append),
        ):
            patch = mock.patch.object(launch, name, hook)
            patch.start()
            case.addCleanup(patch.stop)

    def _which(self, name, *args, **kwargs):
        return f"/usr/bin/{name}" if name in self.installed else None

    def override(self, mapping: dict) -> None:
        """Write the path-map override file of the fake home."""
        path = self.home / ".config" / "agent-sessions" / "pathmap.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(mapping), encoding="utf-8")

    def scan(self):
        """Scan and return the fresh index."""
        api.scan(deep=True)
        return api.load_index()

    def session(self, session_id, origin=None, index=None):
        """The listed session with this id (and origin, when given)."""
        index = index if index is not None else api.load_index()
        found = [s for s in index.sessions if s.id == session_id and origin in (None, s.origin)]
        self.case.assertEqual(len(found), 1, f"{session_id} {origin}: {found}")
        return found[0]


def tree_state(root) -> dict:
    """path -> (kind, size, mtime_ns, atime_ns) of everything below ``root``.
    Looking moves no access time: ``lstat``, and listings without atime."""
    state = {}
    stack = [os.fspath(root)]
    while stack:
        current = stack.pop()
        for name in listdir_ro(current):
            path = os.path.join(current, name)
            st = os.lstat(path)
            is_dir = os.path.isdir(path) and not os.path.islink(path)
            state[path] = (
                "d" if is_dir else "f",
                0 if is_dir else st.st_size,
                st.st_mtime_ns,
                st.st_atime_ns,
            )
            if is_dir:
                stack.append(path)
    return state


def age_access_times(root) -> None:
    """Give every file and directory below ``root`` an access time older than
    its modification time: with ``relatime`` a read that is not O_NOATIME
    would then move it."""
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(base, name)
            os.utime(path, ns=(1_000_000_000, os.lstat(path).st_mtime_ns))
    os.utime(root, ns=(1_000_000_000, os.lstat(root).st_mtime_ns))


def files_below(root) -> list[str]:
    """Relative paths of the files below ``root``, sorted."""
    found = []
    for base, _dirs, files in os.walk(root):
        found += [os.path.relpath(os.path.join(base, name), root) for name in files]
    return sorted(found)
