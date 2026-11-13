import errno, hashlib, importlib.util, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "skills/note-taking/obsidian/scripts/note_check.py"
)
SPEC = importlib.util.spec_from_file_location("obsidian_note_check", SCRIPT)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)
inspect = checker.inspect


def test_cli_reports_partial_scope_without_modifying_notes(tmp_path):
    note = tmp_path / "Example.md"
    raw = b"Private fixture body\n[[Absent]]\n"
    note.write_bytes(raw)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["scope_mode"] == "partial"
    assert report["findings"][0]["status"] == "unresolved_in_scope"
    assert "Private fixture body" not in result.stdout
    assert note.read_bytes() == raw


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, p, t):
        q = self.root / p
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text(t, encoding="utf8")
        return q

    def states(self, r=None):
        return [x["status"] for x in (r or inspect(self.root))["findings"]]

    def test_link(self):
        self.put("A.md", "[[B]]")
        self.put("B.md", "# B")
        self.assertEqual(self.states(), [])

    def test_dotted_note_names_and_file_embeds(self):
        self.put("A.md", "[[Project v1.2]] ![[manual.pdf]] [[Project v1.2#Milestone]]")
        self.put("Project v1.2.md", "# Milestone")
        self.put("manual.pdf", "fixture")
        self.assertEqual(self.states(), [])

    def test_dotted_note_and_file_collision_is_ambiguous(self):
        self.put("A.md", "[[manual.pdf]]")
        self.put("manual.pdf", "fixture")
        self.put("manual.pdf.md", "# Note")
        self.assertEqual(self.states(), ["ambiguous"])

    def test_heading_block_alias(self):
        self.put("A.md", "[[B#Heading|display]] [[B#^a]]")
        self.put("B.md", "## Heading\nhello ^a")
        self.assertEqual(self.states(), [])

    def test_missing_heading(self):
        self.put("A.md", "[[B#Gone]]")
        self.put("B.md", "# B")
        self.assertEqual(self.states(), ["missing_heading"])

    def test_missing_block(self):
        self.put("A.md", "[[B#^gone]]")
        self.put("B.md", "# B")
        self.assertEqual(self.states(), ["missing_block"])

    def test_partial(self):
        self.put("A.md", "[[Outside]]")
        self.assertEqual(self.states(), ["unresolved_in_scope"])

    def test_complete(self):
        self.put("A.md", "[[Outside]]")
        self.assertEqual(self.states(inspect(self.root, True)), ["missing"])

    def test_same_note(self):
        self.put("A.md", "# Present\n[[#Present]]")
        self.assertEqual(self.states(), [])

    def test_embed(self):
        self.put("A.md", "![[photo.png|300]]")
        self.put("photo.png", "not an actual image")
        self.assertEqual(self.states(), [])

    def test_pdf(self):
        self.put("A.md", "![[file.pdf#page=3]]")
        self.put("file.pdf", "fixture")
        self.assertEqual(self.states(), ["anchor_not_checked"])

    def test_ignore(self):
        self.put(
            "A.md",
            "---\naliases: [[ignore]]\n---\n```md\n[[ignore]]\n```\n`[[ignore]]`\n%% [[ignore]] %%\n<!-- [[ignore]] -->\n",
        )
        self.assertEqual(inspect(self.root)["link_count"], 0)

    def test_tilde_fence(self):
        self.put("A.md", "~~~\n[[ignore]]\n~~~\n[[real]]")
        self.assertEqual(inspect(self.root)["link_count"], 1)

    def symlink_or_skip(self, target, link, *, target_is_directory=False):
        try:
            os.symlink(target, link, target_is_directory=target_is_directory)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314 or exc.errno in {
                errno.EPERM,
                errno.EACCES,
                errno.ENOTSUP,
            }:
                self.skipTest("Symlink creation is unavailable to this account")
            raise

    def test_symlinks(self):
        self.put("A.md", "[[Escape]]")
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "outside.md"
            target.write_text("synthetic outside", encoding="utf8")
            self.symlink_or_skip(target, self.root / "Escape.md")
            r = inspect(self.root)
            self.assertEqual(r["note_count"], 1)
            self.assertEqual(r["skipped"][0]["reason"], "symlink")

    def test_no_walk_symlink(self):
        with tempfile.TemporaryDirectory() as outside:
            (Path(outside) / "outside.md").write_text(
                "synthetic outside", encoding="utf8"
            )
            self.symlink_or_skip(
                outside, self.root / "external", target_is_directory=True
            )
            r = inspect(self.root)
            self.assertEqual(r["note_count"], 0)
            self.assertEqual(r["skipped"][0]["reason"], "symlink")

    def test_escape(self):
        self.put(
            "A.md",
            "[[../secret]] [[/etc/passwd]] [[C:\\private]] [[https://example.com]]",
        )
        self.assertEqual(self.states(), ["outside_scope"] * 4)

    def test_ambiguous(self):
        self.put("A.md", "[[Shared]]")
        self.put("x/Shared.md", "a")
        self.put("y/Shared.md", "b")
        self.assertEqual(self.states(), ["ambiguous"])

    def test_exact_folder(self):
        self.put("A.md", "[[x/Shared]]")
        self.put("x/Shared.md", "a")
        self.put("y/Shared.md", "b")
        self.assertEqual(self.states(), [])

    def test_unicode(self):
        self.put("A.md", "[[Evolve²#Mémoire]]")
        self.put("Evolve².md", "# Mémoire")
        self.assertEqual(self.states(), [])

    def test_immutable(self):
        p = self.put("A.md", "[[Nope]]")
        before = p.read_bytes()
        inspect(self.root)
        self.assertEqual(p.read_bytes(), before)

    def test_report_no_body(self):
        self.put("A.md", "very-private-body\n[[B]]")
        self.assertNotIn("very-private-body", json.dumps(inspect(self.root)))

    def test_deterministic(self):
        self.put("B.md", "[[A]]")
        self.put("A.md", "A")
        self.assertEqual(inspect(self.root), inspect(self.root))

    def test_hash_changes(self):
        self.put("A.md", "before")
        a = inspect(self.root)["input_manifest_sha256"]
        self.put("A.md", "after")
        self.assertNotEqual(inspect(self.root)["input_manifest_sha256"], a)

    def test_large(self):
        self.put("A.md", "x" * 2_000_001)
        self.assertEqual(inspect(self.root)["skipped"][0]["reason"], "oversized")

    def test_invalid_utf8(self):
        (self.root / "A.md").write_bytes(b"\xff")
        self.assertEqual(inspect(self.root)["skipped"][0]["reason"], "unreadable_utf8")


if __name__ == "__main__":
    unittest.main()


def test_backslash_escaped_wikilinks_are_not_reported(tmp_path):
    note = tmp_path / 'Examples.md'
    note.write_text(
        chr(92) + '[[Escaped]]\n'
        + chr(92) * 3 + '[[AlsoEscaped]]\n'
        + chr(92) * 2 + '[[Live]]\n'
        + chr(92) + '![[StillALink]]\n',
        encoding='utf-8',
    )
    report = inspect(tmp_path)
    assert report['link_count'] == 2
    assert {item['target'] for item in report['findings']} == {'Live', 'StillALink'}
