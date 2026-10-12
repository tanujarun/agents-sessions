"""Shared test scaffolding: a sandbox (fake home, cache, mounts) and a fake
provider. Nothing here touches a real store."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from agent_sessions import providers  # noqa: E402
from agent_sessions.fsutil import home  # noqa: E402
from agent_sessions.model import Candidate, Session  # noqa: E402
from agent_sessions.providers.base import (  # noqa: E402
    Provider,
    clean_title,
    parse_ts,
    warn,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


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
        for base, dirs, _files in os.walk(self.root):
            for name in dirs:
                try:
                    os.chmod(os.path.join(base, name), 0o700)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)

    def set_mounts(self, *mounts):
        """``mounts``: ``(path, label)`` pairs; the fake home's parent is
        always mounted as ``Linux``."""
        self.mounts = [{"path": str(self.root / "home"), "label": "Linux"}]
        self.mounts += [{"path": str(path), "label": label} for path, label in mounts]
        os.environ["AGENT_SESSIONS_MOUNTS"] = json.dumps(self.mounts)

    def mount(self, name, label=None) -> Path:
        """Create a directory under the sandbox and add it to the mounts."""
        path = self.root / "media" / name
        path.mkdir(parents=True, exist_ok=True)
        pairs = [(m["path"], m["label"]) for m in self.mounts[1:]]
        self.set_mounts(*pairs, (path, label or name))
        return path

    def unmount(self, path):
        pairs = [(m["path"], m["label"]) for m in self.mounts[1:] if m["path"] != str(path)]
        self.set_mounts(*pairs)

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
    One that does not finish is killed and fails the test."""
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
