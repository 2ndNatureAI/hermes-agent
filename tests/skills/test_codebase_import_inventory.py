"""The offline inventory reads source without executing it or following links."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "skills/software-development/codebase-inspection/scripts/import_inventory.py"


def run(root, *args):
    result = subprocess.run([sys.executable, str(SCRIPT), str(root), *args], capture_output=True,
                            text=True, encoding="utf-8", timeout=20)
    return result, json.loads(result.stdout) if result.stdout else None


def test_inventories_python_and_javascript_without_execution(tmp_path):
    (tmp_path / "server.py").write_text("import os\nfrom pkg import route\nraise RuntimeError('not run')\n", encoding="utf-8")
    (tmp_path / "client.js").write_text("import Widget from './widget.js';\nconst x = require('example');\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "excluded.js").write_text("import 'hidden'", encoding="utf-8")
    result, report = run(tmp_path)
    assert result.returncode == 0, result.stderr
    files = {entry["path"]: entry for entry in report["files"]}
    assert set(files) == {"client.js", "server.py"}
    assert {item["module"] for item in files["server.py"]["imports"]} >= {"os", "pkg"}
    assert {item["module"] for item in files["client.js"]["imports"]} >= {"./widget.js", "example"}
    assert report["complete"] is True


def test_file_cap_reports_omitted_entries(tmp_path):
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("import os\n", encoding="utf-8")
    result, report = run(tmp_path, "--max-files", "1")
    assert result.returncode == 0 and len(report["files"]) == 1
    assert report["complete"] is False and report["file_limit_reached"] is True


def test_unparseable_or_oversize_files_are_honestly_marked(tmp_path):
    (tmp_path / "broken.py").write_text("def ( invalid", encoding="utf-8")
    (tmp_path / "large.js").write_text("x" * 100, encoding="utf-8")
    result, report = run(tmp_path, "--max-file-bytes", "40")
    assert result.returncode == 0 and report["complete"] is False
    statuses = {entry["path"]: entry["status"] for entry in report["files"]}
    assert statuses == {"broken.py": "parse_error", "large.js": "too_large"}


def test_invalid_limit_fails_without_scanning(tmp_path):
    result, report = run(tmp_path, "--max-files", "0")
    assert result.returncode != 0 and report is None


def test_very_long_javascript_line_is_bounded_and_reported(tmp_path):
    (tmp_path / "long.js").write_text("import " + " " * 100000 + "x", encoding="utf-8")
    result, report = run(tmp_path)
    assert result.returncode == 0 and not report["complete"]
    assert report["files"][0]["status"] == "line_too_long"


def test_import_count_is_bounded_and_reported(tmp_path):
    (tmp_path / "many.py").write_text("import os\n" * 501, encoding="utf-8")
    result, report = run(tmp_path)
    assert result.returncode == 0 and not report["complete"]
    assert report["files"][0]["imports_truncated"] is True
    assert len(report["files"][0]["imports"]) == 500


def test_symbolic_link_outside_root_is_not_read(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("import outside_secret\n", encoding="utf-8")
    try:
        (root / "link.py").symlink_to(outside)
    except OSError:
        pytest.skip("creating symbolic links requires platform privileges")
    result, report = run(root)
    assert result.returncode == 0 and report["complete"] is False
    assert report["excluded_links"] == 1
    assert "outside_secret" not in result.stdout
