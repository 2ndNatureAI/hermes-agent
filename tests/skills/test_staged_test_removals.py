"""Behavior checks for the staged Python test comparison helper."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills/software-development/requesting-code-review/scripts/staged_test_removals.py"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def run(repo):
    return subprocess.run([sys.executable, str(SCRIPT), str(repo)], capture_output=True, text=True, timeout=30)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    folder = tmp_path / "tests"
    folder.mkdir()
    (folder / "test_names with space.py").write_text("def test_one(): pass\nclass TestThing:\n    def test_two(self): pass\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "baseline")
    return tmp_path


def test_reports_removed_staged_definitions_without_reading_unstaged(repo):
    target = repo / "tests/test_names with space.py"
    target.write_text("def test_one(): pass\n", encoding="utf-8")
    git(repo, "add", "--", str(target))
    target.write_text("def test_one(): pass\nclass TestThing:\n    def test_two(self): pass\n", encoding="utf-8")
    result = run(repo)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["complete"]
    assert report["removed_definitions"] == [{"path": "tests/test_names with space.py", "definition": "TestThing.test_two"}]


def test_invalid_python_is_unknown_not_clean(repo):
    target = repo / "tests/test_names with space.py"
    target.write_text("def test_broken(:\n", encoding="utf-8")
    git(repo, "add", "--", str(target))
    result = run(repo)
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert not report["complete"] and report["unknown"][0]["path"] == "tests/test_names with space.py"


def test_deleted_file_reports_its_definitions(repo):
    target = repo / "tests/test_names with space.py"
    git(repo, "rm", "--", str(target))
    result = run(repo)
    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert {item["definition"] for item in report["removed_definitions"]} == {"test_one", "TestThing.test_two"}


def test_added_test_file_does_not_invent_removals(repo):
    (repo / "test_added.py").write_text("def test_new(): pass\n", encoding="utf-8")
    git(repo, "add", ".")
    result = run(repo)
    assert result.returncode == 0
    assert json.loads(result.stdout)["removed_definitions"] == []

