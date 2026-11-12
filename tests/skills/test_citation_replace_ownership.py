"""Replacement must preserve user text outside the explicitly managed block."""

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "skills/research/grounded-citations/scripts/sources.py"
SPEC = importlib.util.spec_from_file_location("citation_replace_sources", SCRIPT)
sources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sources)


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "ledger.json"
    sources.add_sources(path, ["https://one.example", "https://two.example"])
    return path


@pytest.mark.parametrize("tail", [
    "\n## Appendix\nDo not delete this.\n",
    '\n    > "An unrelated user quote."\n',
    "\nNotes that are not generated citations.\n",
])
def test_ambiguous_legacy_block_is_never_rewritten(tmp_path, ledger, tail):
    draft = tmp_path / "draft.md"
    original = ("Claim [1].\n\n## Sources\n\n[1] https://old.example\n" + tail).encode()
    draft.write_bytes(original)
    with pytest.raises(SystemExit, match="ambiguous"):
        sources.main(["--ledger", str(ledger), "render", "--replace-in", str(draft)])
    assert draft.read_bytes() == original


@pytest.mark.parametrize("style", ["markdown", "plain", "evidence", "footnotes", "bibtex"])
def test_owned_block_is_idempotent_and_preserves_appendix(tmp_path, ledger, style):
    draft = tmp_path / "draft.md"
    draft.write_bytes(b"Before [1].\r\n")
    args = ["--ledger", str(ledger), "render", "--style", style, "--replace-in", str(draft)]
    assert sources.main(args) == 0
    original = draft.read_bytes()
    draft.write_bytes(original + b"\r\n## Appendix\r\nAdditional claim [2].\r\n")
    assert sources.main(args) == 0
    updated = draft.read_bytes()
    assert updated.startswith(b"Before [1].\r\n")
    assert updated.endswith(b"\r\n## Appendix\r\nAdditional claim [2].\r\n")
    assert b"https://two.example" in updated
    assert sources.main(args) == 0
    assert draft.read_bytes() == updated


def test_malformed_owned_markers_cannot_destroy_document(tmp_path, ledger):
    draft = tmp_path / "draft.md"
    original = b"Claim [1].\n<!-- grounded-citations:start -->\nkeep this\n"
    draft.write_bytes(original)
    with pytest.raises(SystemExit, match="marker"):
        sources.main(["--ledger", str(ledger), "render", "--replace-in", str(draft)])
    assert draft.read_bytes() == original


def test_multiline_evidence_replaces_only_the_managed_block(tmp_path, ledger):
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data["sources"][0]["quotes"] = [{"text": "First line\nsecond line"}]
    ledger.write_text(json.dumps(data), encoding="utf-8")
    draft = tmp_path / "draft.md"
    draft.write_text("Claim [1].\n", encoding="utf-8")
    args = ["--ledger", str(ledger), "render", "--style", "evidence", "--replace-in", str(draft)]
    assert sources.main(args) == 0
    draft.write_text(draft.read_text(encoding="utf-8") + "\nAppendix claim [3].\n", encoding="utf-8")
    assert sources.main(args) == 0
    result = draft.read_text(encoding="utf-8")
    assert "First line\nsecond line" in result
    assert result.endswith("\nAppendix claim [3].\n")
    code, errors, _ = sources.verify_draft(draft, data["sources"])
    assert code == 1
    assert any("[3]" in error for error in errors)


def test_marker_in_rendered_evidence_is_refused(tmp_path, ledger):
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data["sources"][0]["quotes"] = [{"text": "<!-- grounded-citations:end -->"}]
    ledger.write_text(json.dumps(data), encoding="utf-8")
    draft = tmp_path / "draft.md"
    original = b"Claim [1].\n"
    draft.write_bytes(original)
    with pytest.raises(SystemExit, match="reserved marker"):
        sources.main(["--ledger", str(ledger), "render", "--style", "evidence", "--replace-in", str(draft)])
    assert draft.read_bytes() == original
