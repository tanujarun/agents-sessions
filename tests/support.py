"""Shared test scaffolding: a sandbox (fake home, cache, mounts) and a fake
provider. Nothing here touches a real store."""

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest import mock

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from agent_sessions import launch, logs, mounts, providers  # noqa: E402
from agent_sessions.fsutil import home  # noqa: E402
from agent_sessions.model import Candidate, Session  # noqa: E402
from agent_sessions.providers.base import (  # noqa: E402
    Provider,
    clean_title,
    parse_ts,
    warn,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# A net below every test: the engine writes its log into the state directory
# from many places, and a test that reaches one outside a ``Sandbox`` must
# not write the user's real one. ``Sandbox`` puts its own directory over
# this for the length of a test.
if "AGENT_SESSIONS_STATE" not in os.environ:
    _stray_state = tempfile.mkdtemp(prefix="agent-sessions-test-state-")
    os.environ["AGENT_SESSIONS_STATE"] = _stray_state
    atexit.register(shutil.rmtree, _stray_state, ignore_errors=True)


def _no_mount(argv):
    raise AssertionError(f"a test tried to start {argv!r}; no test mounts anything")


# The same kind of net for mounting: nothing a test does may start the real
# ``udisksctl``. A test of ``mounts.mount_volume`` puts its own recorder here
# (``FakeDisks``).
mounts.starter = _no_mount


def write_jsonl(path, records) -> Path:
    """Write ``records`` as JSON lines, non-ASCII as it is (the way the
    tools write them); parents are created."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return path


def claude_transcript(
    store_root, session_id, cwd, prompt="hello", reply="hi", project="proj", tool=None
) -> Path:
    """A minimal Claude transcript, one prompt and one reply, written to
    ``<store>/projects/<project>/<session_id>.jsonl``."""
    content = [{"type": "text", "text": reply}]
    if tool is not None:
        content.append({"type": "tool_use", "name": tool})
    return write_jsonl(
        Path(store_root) / "projects" / project / f"{session_id}.jsonl",
        [
            {
                "type": "user",
                "parentUuid": None,
                "isSidechain": False,
                "cwd": cwd,
                "timestamp": "2026-10-01T10:00:00.000Z",
                "message": {"role": "user", "content": prompt},
            },
            {
                "type": "assistant",
                "parentUuid": "p",
                "timestamp": "2026-10-01T10:00:05.000Z",
                "message": {"id": "m1", "role": "assistant", "content": content},
            },
        ],
    )


class FakeProvider(Provider):
    """A store is a ``.fake`` directory with ``sessions/``; a candidate is a
    ``sessions/<hint>.json`` file holding the conversation fields."""

    name = "fake"
    label = "Fake"
    binary = "fake-agent"
    app_id = "org.test.fake"
    resumable = True
    dir_names = {".fake"}
    # This tool's ids are short names (``a``, ``n1``), not UUIDs.
    id_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
    parsed: list = []  # id hints, in call order; reset by Sandbox

    def native_store(self):
        return home() / ".fake"

    def is_store(self, path):
        return (path / "sessions").is_dir()

    def candidates(self, store):
        files = Path(store.root, "sessions").glob("*.json")
        return [Candidate(f.stem, [str(f)]) for f in files]

    def parse(self, store, c):
        type(self).parsed.append(c.id_hint)
        try:
            data = json.loads(Path(c.paths[0]).read_text(encoding="utf-8"))
        except ValueError:
            warn("not JSON", c.paths[0])
            return None
        if data.get("boom"):
            raise RuntimeError("boom")
        if not data.get("listed", True):
            return None
        return Session.for_store(
            store,
            id=data.get("id", c.id_hint),
            title=clean_title(data.get("title")),
            cwd=data.get("cwd"),
            started=parse_ts(data.get("started")),
            updated=parse_ts(data.get("updated")),
            messages=data.get("messages"),
            files=list(c.paths),
            note=data.get("note"),
        )


class OtherProvider(FakeProvider):
    name = "other"
    label = "Other"
    binary = "other-agent"
    dir_names = {".other"}
    parsed: list = []

    def native_store(self):
        return home() / ".other"


WRAPPER = (
    "#!/bin/bash\n"
    "export MISE_MINIMUM_RELEASE_AGE=0\n"
    'mise use -g --quiet "{spec}" || exit 1\n'
    'exec mise x "{spec}" -- "{tool}" "$@"\n'
)  # the text of the wrappers in ~/.local/bin (docs/FORMATS.md "What is on PATH")


class FakePath:
    """What PATH holds, for the length of a test: ``shutil.which`` answers
    from ``directory``. A command that "is installed" is a small file there
    that is no script (``launch.tool_state`` reads it and takes it for a
    program); ``wrapper`` puts an installing wrapper in its place.

    ``installed``: the commands that exist; None means every command asked
    for exists, except those in ``missing``."""

    def __init__(self, case, directory, installed=None, missing=()):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.installed = None if installed is None else set(installed)
        self.missing = set(missing)
        launch.forget_tools()
        case.addCleanup(launch.forget_tools)
        patch = mock.patch("shutil.which", side_effect=self.which)
        patch.start()
        case.addCleanup(patch.stop)

    def which(self, name, *args, **kwargs):
        if name in self.missing or (self.installed is not None and name not in self.installed):
            return None
        path = self.directory / name
        if not path.exists():
            path.write_bytes(b"\x7fELF a program, for the tests\n")
            path.chmod(0o755)
        return str(path)

    def wrapper(self, tool, spec=None, text=None) -> Path:
        """Put the installing wrapper for ``tool`` on the fake PATH (or a
        script with ``text``), and count the command as there."""
        path = self.directory / tool
        path.write_text(text or WRAPPER.format(spec=spec or tool, tool=tool), encoding="utf-8")
        path.chmod(0o755)
        self.missing.discard(tool)
        if self.installed is not None:
            self.installed.add(tool)
        launch.forget_tools()
        return path


class Sandbox:
    """Temp dir with a fake home, cache and state; env overrides set; the
    provider registry replaced. Use ``self.box = Sandbox(self)`` in setUp."""

    def __init__(self, case, registry=None):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="agent-sessions-test-")))
        case.addCleanup(self._remove)
        self.home = self.root / "home" / "user"
        self.home.mkdir(parents=True)
        self.cache = self.root / "cache"
        env = mock.patch.dict(
            os.environ,
            {
                "AGENT_SESSIONS_HOME": str(self.home),
                "AGENT_SESSIONS_CACHE": str(self.cache),
                "AGENT_SESSIONS_STATE": str(self.root / "state"),
                "AGENT_SESSIONS_NOTIFY": "0",  # a failed launch must not notify the desktop
                # What mise has installed on this machine is not the test's business.
                "MISE_DATA_DIR": str(self.root / "mise"),
            },
        )
        env.start()
        case.addCleanup(env.stop)
        if registry is None:
            registry = {"fake": FakeProvider}
        reg = mock.patch.dict(providers._REGISTRY, registry, clear=True)
        reg.start()
        case.addCleanup(reg.stop)
        FakeProvider.parsed = []
        OtherProvider.parsed = []
        self.mounts: list[dict] = []
        self.set_mounts()

    def _remove(self):
        logs.close()  # the log file is in the state directory that goes now
        for base, dirs, _files in os.walk(self.root):
            for name in dirs:
                try:
                    os.chmod(os.path.join(base, name), 0o700)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)

    def set_mounts(self, *mounts):
        """``mounts``: ``(path, label)`` pairs, or dicts as
        ``AGENT_SESSIONS_MOUNTS`` takes them (``uuid``, ``fs_label``, …); the
        fake home's parent is always mounted as ``Linux``."""
        self.mounts = [{"path": str(self.root / "home"), "label": "Linux"}]
        for mount in mounts:
            if not isinstance(mount, dict):
                mount = {"path": mount[0], "label": mount[1]}
            self.mounts.append(dict(mount, path=str(mount["path"])))
        os.environ["AGENT_SESSIONS_MOUNTS"] = json.dumps(self.mounts)

    def mount(self, name, label=None, at=None, **identity) -> Path:
        """Create a directory under the sandbox (or take ``at``) and add it
        to the mounts; ``identity`` is ``uuid``, ``fs_label``, ``fstype``."""
        path = Path(at) if at is not None else self.root / "media" / name
        path.mkdir(parents=True, exist_ok=True)
        self.set_mounts(*self.mounts[1:], dict(identity, path=path, label=label or name))
        return path

    def unmount(self, path):
        self.set_mounts(*(m for m in self.mounts[1:] if m["path"] != str(path)))

    def store(self, parent, name=".fake") -> Path:
        """Create an empty fake store under ``parent``; return its root."""
        root = Path(parent) / name
        (root / "sessions").mkdir(parents=True, exist_ok=True)
        return root

    def session(self, store_root, hint, **fields) -> Path:
        """Write one candidate file into a fake store."""
        path = Path(store_root) / "sessions" / f"{hint}.json"
        fields.setdefault("title", f"title {hint}")
        fields.setdefault("updated", "2026-10-01T10:00:00.000Z")
        path.write_text(json.dumps(fields), encoding="utf-8")
        return path


class FakeProcess:
    """What ``mounts.starter`` returns, for a test: it ends with ``code``
    and the two outputs, or never answers (``hangs``) until it is
    terminated; a ``stubborn`` one has to be killed."""

    def __init__(self, code=0, out=b"", err=b"", hangs=False, stubborn=False):
        self.returncode = None
        self.result = (code, out, err)
        self.hangs = hangs or stubborn
        self.ends_on = "kill" if stubborn else "terminate"
        self.calls: list = []  # what was done to it, in order

    def communicate(self, timeout=None):
        self.calls.append(("communicate", timeout))
        if self.hangs and self.ends_on not in self.calls and "kill" not in self.calls:
            raise subprocess.TimeoutExpired("udisksctl", timeout)
        self.returncode = self.result[0]
        return self.result[1], self.result[2]

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")


class FakeDisks:
    """A machine's disks as ``mounts`` reads them, in a temp directory: the
    ``/dev/disk/by-uuid`` and ``by-label`` links, the devices they point at
    (plain files that count as block devices), a mount table, the udisks
    media directory, and ``udisksctl`` on PATH. ``mounts.starter`` is a
    recorder: ``started`` holds every argv, and ``on_start(argv)`` returns
    the ``FakeProcess`` (by default one that succeeds, after ``mounting``
    has put the device into the mount table, if it is set).

    With ``AGENT_SESSIONS_MOUNTS`` set (a ``Sandbox``), the mount table is
    that override and ``mount_table`` lines are not read."""

    def __init__(self, case, root):
        self.root = Path(root)
        self.dev = self.root / "dev"
        self.by_uuid = self.dev / "disk" / "by-uuid"
        self.by_label = self.dev / "disk" / "by-label"
        self.media = self.root / "run" / "media"
        self.mountinfo = self.root / "mountinfo"
        for directory in (self.by_uuid, self.by_label, self.media):
            directory.mkdir(parents=True)
        self.mountinfo.write_text("", encoding="utf-8")
        self.block: set[str] = set()
        self.started: list[list[str]] = []
        self.on_start = None
        self.mounting = None  # (device, mount point): what a successful start mounts
        self.has_tool = True
        self.real_is_block_device = mounts.is_block_device
        for target, name, value in (
            (mounts, "DEV_BY_UUID", str(self.by_uuid)),
            (mounts, "DEV_BY_LABEL", str(self.by_label)),
            (mounts, "MEDIA_ROOT", str(self.media)),
            (mounts, "MOUNTINFO", str(self.mountinfo)),
            (mounts, "is_block_device", lambda path: os.fspath(path) in self.block),
            (mounts, "starter", self._start),
            # What PATH holds, for ``mounts`` alone (``FakePath`` answers for
            # everything else, and says yes to every name).
            (
                mounts,
                "shutil",
                types.SimpleNamespace(
                    which=lambda name, *a, **k: f"/usr/bin/{name}" if self.has_tool else None
                ),
            ),
        ):
            patch = mock.patch.object(target, name, value)
            patch.start()
            case.addCleanup(patch.stop)

    def _start(self, argv):
        self.started.append(list(argv))
        if self.on_start is not None:
            return self.on_start(argv)
        if self.mounting is not None:
            self.mount(*self.mounting)
        return FakeProcess()

    @staticmethod
    def udev_name(name: str) -> str:
        """A label the way udev spells it under ``/dev/disk/by-label``."""
        return "".join(
            chr(byte)
            if byte < 128 and (chr(byte).isalnum() or chr(byte) in "#+-.:=@_")
            # udev: every other byte as \xNN
            else f"\\x{byte:02x}"
            for byte in name.encode("utf-8")
        )

    def device(self, name, uuid=None, label=None, block=True) -> str:
        """Attach a device ``dev/<name>``; returns its path."""
        device = self.dev / name
        device.write_bytes(b"")
        if block:
            self.block.add(str(device))
        if uuid is not None:
            os.symlink(f"../../{name}", self.by_uuid / uuid)
        if label is not None:
            os.symlink(f"../../{name}", self.by_label / self.udev_name(label))
        return str(device)

    def detach(self, name) -> None:
        """Take a device away, its links included."""
        device = self.dev / name
        for directory in (self.by_uuid, self.by_label):
            for link in directory.iterdir():
                if os.path.realpath(link) == str(device):
                    link.unlink()
        self.block.discard(str(device))
        device.unlink()

    def mount(self, device, path, fstype="ntfs3", root="/") -> None:
        """Add a line to the mount table: ``device`` mounted at ``path``."""
        number = 100 + len(self.mountinfo.read_text(encoding="utf-8").splitlines())
        where = str(path).replace(" ", "\\040")
        line = (
            f"{number} 29 259:{number} {root} {where} rw,relatime shared:1 - {fstype} {device} rw"
        )
        with open(self.mountinfo, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


LOG_NAME = "agent-sessions.log"


def state_names() -> list[str]:
    """What the state directory holds besides the log, sorted; nothing when
    it does not exist. Scans, imports and launches write the log, so a test
    that says "the state directory is untouched" means everything else."""
    try:
        names = os.listdir(os.environ["AGENT_SESSIONS_STATE"])
    except FileNotFoundError:
        return []
    return sorted(name for name in names if not name.startswith(LOG_NAME))


def drop_state() -> None:
    """Remove the sandbox's state directory, log included, for a test that
    has to see whether something creates it again."""
    logs.close()
    shutil.rmtree(os.environ["AGENT_SESSIONS_STATE"], ignore_errors=True)


REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "bin" / "agent-sessions"
CHILD_TIMEOUT = 60  # seconds a child process of a test may take


def python_child(code, *args, **options):
    """Start a Python that can import the package and runs ``code`` with
    ``args``; returns the ``Popen``. It inherits the environment, the
    overrides of a ``Sandbox`` included. Output is captured as text unless
    ``options`` say otherwise."""
    options.setdefault("stdout", subprocess.PIPE)
    options.setdefault("stderr", subprocess.PIPE)
    options.setdefault("stdin", subprocess.DEVNULL)
    options.setdefault("text", True)
    script = f"import sys\nsys.path.insert(0, {_SRC!r})\n{code}"
    return subprocess.Popen([sys.executable, "-c", script, *map(str, args)], **options)


def launcher_child(*argv, **options):
    """Start our own launcher (``bin/agent-sessions``) with ``argv``."""
    options.setdefault("stdout", subprocess.PIPE)
    options.setdefault("stderr", subprocess.PIPE)
    options.setdefault("stdin", subprocess.DEVNULL)
    options.setdefault("text", True)
    return subprocess.Popen([sys.executable, str(LAUNCHER), *map(str, argv)], **options)


def finish(case, children, timeout=CHILD_TIMEOUT) -> list[tuple[int, str, str]]:
    """Wait for child processes; ``(exit status, stdout, stderr)`` of each.
    One that does not finish is killed and fails the test. A child's stdin,
    when it is a pipe, is closed here (``communicate`` does it): do not close
    it before, Python 3.11 then fails on the closed file."""
    results = []
    try:
        for child in children:
            out, err = child.communicate(timeout=timeout)
            results.append((child.returncode, out, err))
    except subprocess.TimeoutExpired:
        case.fail(f"a child process did not finish in {timeout} s")
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate()
    return results


# A child process that imports slowly, for the tests that have to catch an
# import in the middle of its copy: ``python_child(SLOW_IMPORT, mark, gate,
# "import", key)``. It runs the CLI with ``importer.CHUNK`` at 16 bytes and a
# source reader that, once the first piece is read, creates the file ``mark``
# and then waits for the file ``gate`` before it hands anything on. The hook
# is this script: the package has no switch for it.
SLOW_IMPORT = """
import os, time
from agent_sessions import cli, importer

mark, gate = sys.argv[1], sys.argv[2]
real_open = importer.open_ro
importer.CHUNK = 16


class Slow:
    def __init__(self, reader):
        self.reader = reader

    def read(self, size=-1):
        data = self.reader.read(size)
        if data:
            open(mark, "a").close()
            while not os.path.exists(gate):
                time.sleep(0.01)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.reader.close()

    def __getattr__(self, name):
        return getattr(self.reader, name)


importer.open_ro = lambda path: Slow(real_open(path))
sys.exit(cli.main(sys.argv[3:]))
"""


def wait_for(case, path, timeout=CHILD_TIMEOUT) -> None:
    """Wait until ``path`` exists; fail the test when it does not appear."""
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            case.fail(f"{path} did not appear in {timeout} s")
        time.sleep(0.01)
