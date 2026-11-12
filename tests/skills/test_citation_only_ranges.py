"""The render CLI must select ledger IDs without expanding numeric ranges."""

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "skills/research/grounded-citations/scripts/sources.py"


def run_render(tmp_path, selection):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"version": 1, "sources": [
        {"id": 1, "url": "https://one.invalid"},
        {"id": 7, "url": "https://seven.invalid"},
        {"id": 9999, "url": "https://last.invalid"},
    ]}), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--ledger", str(ledger), "render", "--only", selection],
        text=True, capture_output=True, timeout=5,
    )


def test_very_large_range_renders_existing_sources_promptly(tmp_path):
    result = run_render(tmp_path, "1-1000000000000000000")
    assert result.returncode == 0, result.stderr
    assert "[1] https://one.invalid" in result.stdout
    assert "[7] https://seven.invalid" in result.stdout
    assert "[9999] https://last.invalid" in result.stdout


def test_overlapping_ranges_and_missing_ids_select_existing_sources_once(tmp_path):
    result = run_render(tmp_path, "3-8,7,20-5,100-101")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("[7]") == 1
    assert "[1]" not in result.stdout and "[9999]" not in result.stdout


def test_bad_selection_has_concise_error(tmp_path):
    result = run_render(tmp_path, "1-not-a-number")
    assert result.returncode != 0
    assert "invalid --only selection" in result.stderr
    assert "Traceback" not in result.stderr
