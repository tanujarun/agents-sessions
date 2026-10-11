"""pathmap.py: normalisation, drive letters by evidence, overrides."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import Sandbox  # noqa: E402

from agent_sessions.mounts import Mount  # noqa: E402
from agent_sessions.pathmap import PathMap, Recorded, normalise  # noqa: E402


class NormaliseTest(unittest.TestCase):
    def test_every_form(self):
        site = Recorded("F:", ("AI Projects", "Website Works"))
        cases = {
            r"F:\AI Projects\Website Works": site,
            "F:/AI Projects/Website Works": site,
            r"f:\AI Projects\Website Works": site,                 # lower-case letter
            "\\\\?\\F:\\AI Projects\\Website Works": site,         # extended-length prefix
            "//?/F:/AI Projects/Website Works": site,
            "file:///F:/AI%20Projects/Website%20Works": site,      # URI
            "file:///f%3A/AI%20Projects/Website%20Works": site,    # encoded colon
            "file://localhost/F:/AI%20Projects/Website%20Works": site,
            "F:\\AI Projects/Website Works\\": site,               # mixed, trailing
            "F:\\\\AI Projects\\.\\Website Works": site,
            "F:\\AI Projects\\Website Works\r\n": site,
            "C:": Recorded("C:", ()),
            "c:\\": Recorded("C:", ()),
            "/home/user/My Proj": Recorded("/", ("home", "user", "My Proj")),
            "/home/user/proj/": Recorded("/", ("home", "user", "proj")),
            "file:///home/user/My%20Proj": Recorded("/", ("home", "user", "My Proj")),
            "/": Recorded("/", ()),
            "/back\\slash/kept": Recorded("/", ("back\\slash", "kept")),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(normalise(text), expected)

    def test_unmappable(self):
        for text in (None, 3, "", "relative/path", "proj", "C:relative", r"\\server\share\x",
                     "//server/share", r"\\?\UNC\server\share", "file://server/share/x",
                     "file://", "https://example.com/x"):
            with self.subTest(text=text):
                self.assertIsNone(normalise(text))


class PathMapTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)
        self.sys = self.box.mount("SYS")        # a Windows system volume
        self.data = self.box.mount("Data Disk")
        self.empty = self.box.mount("Empty")
        for d in ("Windows/System32", "Users/arun/Downloads/pdf2png"):
            (self.sys / d).mkdir(parents=True)
        (self.data / "AI Projects" / "Website Works" / "Pill Content").mkdir(parents=True)
        (self.data / "Hypermedia" / "Ep.2").mkdir(parents=True)
        self.mounts = [Mount(str(p), p.name) for p in (self.sys, self.data, self.empty)]

    def build(self, evidence, overrides=None, mounts=None):
        pairs = [(cwd, str(source)) for cwd, source in evidence]
        return PathMap.build(mounts or self.mounts, pairs, overrides)

    def standard(self):
        return self.build([
            (r"C:\Users\arun", self.sys),
            (r"C:\Users\arun\Downloads\pdf2png", self.sys),
            (r"C:\Users\arun\AppData\Local\Temp\gone", self.sys),
            (r"F:\AI Projects\Website Works", self.sys),
            (r"f:\ai projects\website works", self.sys),
            ("file:///F:/AI%20Projects/Website%20Works/Pill%20Content", self.sys),
            (r"F:\AI Projects\Website Works\UWW Sites", self.sys),
            (r"E:\Nowhere\At All", self.sys),
            ("/some/posix/path", self.sys),
            (None, self.sys), ("", self.sys), ("relative", self.sys),
        ])

    def test_letters_resolve_by_evidence(self):
        pm = self.standard()
        self.assertEqual(
            [(r["letter"], r["mount"], r["how"], r["paths"], r["source_mount"])
             for r in pm.letters()],
            [("C:", str(self.sys), "evidence", 2, str(self.sys)),
             ("F:", str(self.data), "evidence", 2, str(self.sys))])
        self.assertEqual(pm.letter_for_mount(str(self.sys)), "C:")
        self.assertEqual(pm.letter_for_mount(self.mounts[1]), "F:")
        self.assertIsNone(pm.letter_for_mount(str(self.empty)))
        self.assertEqual(pm.letter_for_mount(str(self.data), str(self.sys)), "F:")
        self.assertIsNone(pm.letter_for_mount(str(self.data), str(self.empty)))

    def test_to_local_every_form(self):
        pm = self.standard()
        site = str(self.data / "AI Projects" / "Website Works")
        for recorded in (r"F:\AI Projects\Website Works", "F:/AI Projects/Website Works/",
                         r"f:\ai projects\website works",                  # on-disk spelling
                         "\\\\?\\F:\\AI Projects\\Website Works",
                         "file:///F:/AI%20Projects/Website%20Works",
                         "F:\\AI PROJECTS\\.\\WEBSITE WORKS"):
            with self.subTest(recorded=recorded):
                self.assertEqual(pm.to_local(recorded, str(self.sys)), (site, True))
        self.assertEqual(pm.to_local(r"c:\users\ARUN\downloads\PDF2PNG", str(self.sys)),
                         (str(self.sys / "Users/arun/Downloads/pdf2png"), True))
        self.assertEqual(pm.to_local("C:\\", str(self.sys)), (str(self.sys), True))
        self.assertEqual(pm.to_local("F:", self.mounts[0]), (str(self.data), True))

    def test_vanished_folder_maps_to_nothing(self):
        pm = self.standard()
        for recorded in (r"F:\AI Projects\Website Works\UWW Sites",       # parent exists
                         r"F:\AI Projects\Website Works\UWW Sites\Landing Page",
                         r"C:\Users\arun\AppData\Local\Temp\gone",
                         r"F:\Hypermedia\Ep2",                             # renamed to Ep.2
                         r"F:\AI Projects\..\AI Projects"):
            with self.subTest(recorded=recorded):
                self.assertEqual(pm.to_local(recorded, str(self.sys)), (None, False))

    def test_a_file_is_not_a_folder(self):
        (self.data / "AI Projects" / "notes.txt").write_text("x")
        pm = self.standard()
        self.assertEqual(pm.to_local(r"F:\AI Projects\notes.txt", str(self.sys)), (None, False))

    def test_unresolved_letter(self):
        pm = self.standard()
        self.assertEqual(pm.unresolved(), [{
            "letter": "E:", "source_mount": str(self.sys), "reason": "no evidence",
            "candidates": [], "paths": 1}])
        self.assertEqual(pm.to_local(r"E:\Nowhere\At All", str(self.sys)), (None, False))
        self.assertEqual(pm.to_local(r"Z:\never seen", str(self.sys)), (None, False))
        self.assertEqual(pm.to_local(None, str(self.sys)), (None, False))
        self.assertEqual(pm.to_local("relative", str(self.sys)), (None, False))

    def test_ambiguous_letter_stays_unmapped(self):
        (self.empty / "Shared" / "Proj").mkdir(parents=True)
        (self.data / "Shared" / "Proj").mkdir(parents=True)
        pm = self.build([(r"G:\Shared\Proj", self.sys)])
        self.assertEqual(pm.unresolved(), [{
            "letter": "G:", "source_mount": str(self.sys), "reason": "ambiguous",
            "candidates": sorted([str(self.data), str(self.empty)]), "paths": 1}])
        self.assertEqual(pm.to_local(r"G:\Shared\Proj", str(self.sys)), (None, False))
        self.assertEqual(pm.letters(), [])

    def test_conflicting_evidence_is_ambiguous(self):
        (self.empty / "Only Here").mkdir()
        pm = self.build([(r"F:\AI Projects", self.sys), (r"F:\Only Here", self.sys)])
        self.assertEqual([(u["letter"], u["reason"], u["candidates"]) for u in pm.unresolved()],
                         [("F:", "ambiguous", sorted([str(self.data), str(self.empty)]))])
        self.assertEqual(pm.to_local(r"F:\AI Projects", str(self.sys)), (None, False))

    def test_one_unique_path_settles_a_letter_shared_paths_follow(self):
        (self.empty / "AI Projects").mkdir()
        pm = self.build([(r"F:\AI Projects", self.sys),
                         (r"F:\AI Projects\Website Works", self.sys)])
        self.assertEqual(pm.to_local(r"F:\AI Projects", str(self.sys)),
                         (str(self.data / "AI Projects"), True))

    def test_bare_root_is_no_evidence(self):
        pm = self.build([("D:\\", self.sys), ("D:", self.sys)])
        self.assertEqual([(u["letter"], u["reason"]) for u in pm.unresolved()],
                         [("D:", "no evidence")])
        self.assertEqual(pm.to_local("D:\\", str(self.sys)), (None, False))

    def test_letters_are_per_source_mount(self):
        """Two Windows installs that call different disks D:."""
        other = self.box.mount("SYS2")
        archive = self.box.mount("Archive")
        (archive / "Old Work").mkdir()
        mounts = self.mounts + [Mount(str(other), "SYS2"), Mount(str(archive), "Archive")]
        pm = self.build([(r"D:\AI Projects", self.sys), (r"D:\Old Work", other)], mounts=mounts)
        self.assertEqual(pm.to_local(r"D:\AI Projects", str(self.sys)),
                         (str(self.data / "AI Projects"), True))
        self.assertEqual(pm.to_local(r"D:\Old Work", str(other)), (str(archive / "Old Work"), True))
        self.assertEqual(pm.to_local(r"D:\Old Work", str(self.sys)), (None, False))
        self.assertEqual(pm.to_local(r"D:\AI Projects", str(other)), (None, False))
        self.assertEqual(pm.letter_for_mount(str(self.data)), "D:")
        self.assertEqual(pm.letter_for_mount(str(archive)), "D:")
        # A source that recorded nothing on D: gets no guess while the others disagree,
        self.assertEqual(pm.to_local(r"D:\AI Projects", str(self.empty)), (None, False))
        # but follows them when they agree.
        agreed = self.build([(r"D:\AI Projects", self.sys)], mounts=mounts)
        self.assertEqual(agreed.to_local(r"D:\AI Projects\Website Works", str(self.empty)),
                         (str(self.data / "AI Projects" / "Website Works"), True))
        self.assertEqual(agreed.to_local(r"D:\AI Projects", None),
                         (str(self.data / "AI Projects"), True))

    def test_system_volume_rule(self):
        """Two installs with the same profile folders: each store means the
        volume it is on."""
        other = self.box.mount("SYS2")
        for d in ("Windows", "Users/arun"):
            (other / d).mkdir(parents=True)
        mounts = self.mounts + [Mount(str(other), "SYS2")]
        pm = self.build([(r"C:\Users\arun", self.sys), (r"C:\Users\arun", other)], mounts=mounts)
        self.assertEqual([(r["source_mount"], r["mount"], r["how"]) for r in pm.letters()],
                         [(str(self.sys), str(self.sys), "system volume"),
                          (str(other), str(other), "system volume")])
        self.assertEqual(pm.to_local(r"C:\Users\arun", str(other)),
                         (str(other / "Users" / "arun"), True))
        self.assertEqual(pm.letter_for_mount(str(other)), "C:")
        # a source that is not a system volume gets no such help
        pm = self.build([(r"C:\Users\arun", self.data)], mounts=mounts)
        self.assertEqual([u["reason"] for u in pm.unresolved()], ["ambiguous"])

    def test_case_clash_on_disk(self):
        (self.data / "Docs").mkdir()
        (self.data / "docs").mkdir()
        (self.data / "Docs" / "A").mkdir()
        pm = self.build([(r"F:\AI Projects", self.sys)])
        self.assertEqual(pm.to_local(r"F:\Docs\A", str(self.sys)),
                         (str(self.data / "Docs" / "A"), True))      # exact spelling wins
        self.assertEqual(pm.to_local(r"F:\DOCS", str(self.sys)), (None, False))

    def test_msix_redirect(self):
        packaged = (self.sys / "Users/arun/AppData/Local/Packages/Claude_pzs8sxrjxfjjc"
                    / "LocalCache/Roaming/Claude/scratch-workspaces/s1")
        packaged.mkdir(parents=True)
        recorded = r"C:\Users\arun\AppData\Roaming\Claude\scratch-workspaces\s1"
        pm = self.build([(recorded, self.sys)])
        self.assertEqual(pm.to_local(recorded, str(self.sys)), (str(packaged), True))
        self.assertEqual(pm.to_local(recorded.lower(), str(self.sys)), (str(packaged), True))
        self.assertEqual(pm.letter_for_mount(str(self.sys)), "C:")   # counts as evidence
        self.assertEqual(pm.to_local(recorded + r"\missing", str(self.sys)), (None, False))
        # the plain path wins when it exists
        plain = self.sys / "Users/arun/AppData/Roaming/Claude/scratch-workspaces/s1"
        plain.mkdir(parents=True)
        pm = self.build([(recorded, self.sys)])
        self.assertEqual(pm.to_local(recorded, str(self.sys)), (str(plain), True))

    def test_override_file_wins_over_evidence(self):
        config = self.box.home / ".config" / "agent-sessions"
        config.mkdir(parents=True)
        (self.empty / "AI Projects").mkdir()
        (config / "pathmap.json").write_text(json.dumps(
            {"f:": str(self.empty) + "/", "E:": str(self.data)}))
        pm = self.standard()
        self.assertEqual(pm.to_local(r"F:\AI Projects", str(self.sys)),
                         (str(self.empty / "AI Projects"), True))
        self.assertEqual(pm.to_local(r"F:\AI Projects\Website Works", str(self.sys)), (None, False))
        self.assertEqual(pm.to_local(r"E:\Hypermedia", str(self.sys)),
                         (str(self.data / "Hypermedia"), True))
        self.assertEqual(pm.unresolved(), [])
        self.assertEqual({(r["letter"], r["how"]) for r in pm.letters()},
                         {("C:", "evidence"), ("E:", "override"), ("F:", "override")})
        self.assertEqual(pm.letter_for_mount(str(self.empty)), "F:")
        self.assertEqual(pm.warnings, [])

    def test_override_without_evidence_and_for_a_posix_prefix(self):
        old_home = self.data / "old-home"
        (old_home / "proj").mkdir(parents=True)
        pm = self.build([], overrides={"Q:": str(self.data), "/home/gone": str(old_home)})
        self.assertEqual(pm.to_local(r"Q:\Hypermedia", None), (str(self.data / "Hypermedia"), True))
        self.assertEqual(pm.letter_for_mount(str(self.data)), "Q:")
        self.assertEqual(pm.to_local("/home/gone/proj", None), (str(old_home / "proj"), True))
        self.assertEqual(pm.to_local("/home/gone", None), (str(old_home), True))
        self.assertEqual(pm.to_local("/home/gone/none", None), (None, False))

    def test_bad_override_file_is_a_warning(self):
        config = self.box.home / ".config" / "agent-sessions"
        config.mkdir(parents=True)
        for content in ("{not json", "[1, 2]", '"text"'):
            (config / "pathmap.json").write_text(content)
            pm = self.standard()
            self.assertEqual(len(pm.warnings), 1, content)
            self.assertEqual(pm.letter_for_mount(str(self.data)), "F:")
        (config / "pathmap.json").write_text(json.dumps(
            {"F:": "relative", "nonsense": "/x", "G:\\sub": "/x", "H:": 5, "E:": str(self.data)}))
        pm = self.standard()
        self.assertEqual(len(pm.warnings), 4)
        self.assertEqual(pm.to_local(r"E:\Hypermedia", None), (str(self.data / "Hypermedia"), True))

    def test_posix_paths_map_to_themselves_when_they_exist(self):
        pm = self.standard()
        here = self.box.home / "My Proj"
        here.mkdir()
        self.assertEqual(pm.to_local(str(here), str(self.sys)), (str(here), True))
        self.assertEqual(pm.to_local(str(here) + "/", None), (str(here), True))
        self.assertEqual(pm.to_local(f"file://{here}".replace(" ", "%20"), None), (str(here), True))
        self.assertEqual(pm.to_local(str(here / "gone"), str(self.sys)), (None, False))
        self.assertEqual(pm.to_local(str(here).upper(), None), (None, False))   # case matters
        self.assertEqual(pm.to_local("/", None), ("/", True))

    def test_unreadable_or_missing_mount(self):
        ghost = Mount(str(self.box.root / "media" / "ghost"), "ghost")
        pm = self.build([(r"F:\AI Projects", self.sys), (r"K:\x", ghost.path)],
                        mounts=self.mounts + [ghost])
        self.assertEqual(pm.to_local(r"F:\AI Projects", str(self.sys)),
                         (str(self.data / "AI Projects"), True))
        self.assertEqual(pm.to_local(r"K:\x", ghost.path), (None, False))

    def test_deterministic(self):
        first, second = self.standard(), self.standard()
        self.assertEqual(first.letters(), second.letters())
        self.assertEqual(first.unresolved(), second.unresolved())


if __name__ == "__main__":
    unittest.main()
