"""The offline inventory reads source without executing it or following links."""

import json
import stat
import subprocess
import sys
import zipfile
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


def test_zip_inventories_without_extracting_and_rejects_unsafe_members(tmp_path):
    archive = tmp_path / "bank.zip"
    link = zipfile.ZipInfo("repo/link.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("repo/a.py", "import os\nraise RuntimeError('not run')\n")
        z.writestr("repo/client.js", "import x from 'example';\n")
        z.writestr("repo/node_modules/pkg.js", "import 'excluded';\n")
        z.writestr("../outside.py", "import unsafe\n")
        z.writestr(link, "outside.py")
    result, report = run(archive)
    assert result.returncode == 0, result.stderr
    assert not report["complete"] and report["excluded_members"] == 2
    assert {row["path"] for row in report["files"]} == {"repo/a.py", "repo/client.js"}
    assert not (tmp_path / "repo").exists()
    assert "unsafe" not in result.stdout and "excluded" not in [i["module"] for row in report["files"] for i in row["imports"]]


def test_zip_member_limits_and_unsupported_compression(tmp_path):
    archive = tmp_path / "bank.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("large.py", "x" * 100)
        z.writestr("unsupported.py", "import os", compress_type=zipfile.ZIP_BZIP2)
    result, report = run(archive, "--max-file-bytes", "40")
    assert result.returncode == 0 and not report["complete"]
    assert [item["status"] for item in report["files"]] == ["too_large", "unsupported_or_encrypted"]
    result, report = run(archive, "--max-files", "1")
    assert result.returncode == 0 and report["file_limit_reached"] and not report["complete"]


def test_zip_rejects_too_many_members_before_reading(tmp_path):
    archive = tmp_path / "bank.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for n in range(5001):
            z.writestr(f"{n}.py", "")
    result, report = run(archive)
    assert result.returncode != 0 and report is None


def test_zip_stops_at_total_uncompressed_source_limit(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    import zip_import_inventory

    monkeypatch.setattr(zip_import_inventory, "MAX_TOTAL_SOURCE", 15)
    archive = tmp_path / "bank.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("a.py", "import os\n")
        z.writestr("b.py", "import os\n")
    report = zip_import_inventory.inventory_zip(archive, 10, 100)
    assert not report["complete"] and report["total_source_limit_reached"]
    assert [row["path"] for row in report["files"]] == ["a.py"]


def test_find_module_filters_matches_but_keeps_incomplete_files(tmp_path):
    (tmp_path / "a.py").write_text("import os\nimport json\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import json\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("def (", encoding="utf-8")
    result, report = run(tmp_path, "--find-module", "os")
    assert result.returncode == 0 and not report["complete"]
    assert report["find_module"] == {"query": "os", "examined_files": 3, "matching_files": 1}
    files = {row["path"]: row for row in report["files"]}
    assert set(files) == {"a.py", "broken.py"}
    assert [i["module"] for i in files["a.py"]["imports"]] == ["os"]
    assert files["broken.py"]["status"] == "parse_error"


def test_find_module_works_on_zip_and_is_case_sensitive(tmp_path):
    archive = tmp_path / "bank.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("a.py", "import os\n")
        z.writestr("b.js", "import 'os';\n")
    result, report = run(archive, "--find-module", "OS")
    assert result.returncode == 0 and report["complete"] and not report["files"]
    result, report = run(archive, "--find-module", "os")
    assert result.returncode == 0 and report["find_module"]["matching_files"] == 2
