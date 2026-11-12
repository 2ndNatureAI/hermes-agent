#!/usr/bin/env python3
"""Compare staged Python test definitions with HEAD without executing source."""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

MAX_FILES = 200
MAX_BYTES = 1_000_000
MAX_LIST_BYTES = 1_000_000


def git(repo: Path, *args: str, deadline: float) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("Git inspection exceeded its total time limit")
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    process = subprocess.Popen(["git", "-C", str(repo), "-c", "core.fsmonitor=false", *args],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    chunks = []
    reader = threading.Thread(target=lambda: chunks.append(process.stdout.read(MAX_BYTES + 1)), daemon=True)
    reader.start()
    reader.join(min(10, remaining))
    if reader.is_alive():
        if process.poll() is None:
            process.kill()
        process.wait()
        reader.join(1)
        raise ValueError("Git inspection timed out")
    output = chunks[0]
    if len(output) > MAX_BYTES:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise ValueError("Git output exceeds the size limit")
    try:
        return_code = process.wait(timeout=max(0.01, min(10, deadline - time.monotonic())))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise ValueError("Git inspection timed out")
    if return_code:
        raise ValueError("Git could not inspect the staged changes")
    return output


def definitions(data: bytes) -> set[str]:
    tree = ast.parse(data)
    found = set()
    for item in tree.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_"):
            found.add(item.name)
        elif isinstance(item, ast.ClassDef):
            for member in item.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name.startswith("test_"):
                    found.add(f"{item.name}.{member.name}")
    return found


def blob(repo: Path, reference: str, deadline: float) -> bytes:
    size = int(git(repo, "cat-file", "-s", reference, deadline=deadline))
    if size > MAX_BYTES:
        raise ValueError("test file exceeds the size limit")
    return git(repo, "cat-file", "blob", reference, deadline=deadline)


def inspect(repo: Path) -> dict:
    repo = repo.resolve(strict=True)
    if not repo.is_dir():
        raise ValueError("repository must be a directory")
    deadline = time.monotonic() + 45
    raw = git(repo, "diff", "--cached", "--name-status", "-z", "--no-renames",
              "--no-ext-diff", "--no-textconv", "HEAD", "--", deadline=deadline)
    if len(raw) > MAX_LIST_BYTES:
        raise ValueError("staged file listing exceeds the size limit")
    fields = raw.split(b"\0")
    if not fields or fields[-1] != b"" or len(fields) % 2 != 1:
        raise ValueError("unexpected Git file listing")
    files = []
    for i in range(0, len(fields) - 1, 2):
        status, path = fields[i].decode("ascii"), os.fsdecode(fields[i + 1])
        name = Path(path).name
        if path.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
            files.append((status, path))
    if len(files) > MAX_FILES:
        raise ValueError("staged test file count exceeds the limit")
    missing, unknown = [], []
    for status, path in files:
        if time.monotonic() >= deadline:
            raise ValueError("Git inspection exceeded its total time limit")
        if status == "A":
            continue
        if status not in {"M", "D"}:
            unknown.append({"path": path, "reason": "unsupported staged status"})
            continue
        try:
            previous = definitions(blob(repo, "HEAD:" + path, deadline))
            current = set() if status == "D" else definitions(blob(repo, ":" + path, deadline))
        except (ValueError, SyntaxError, UnicodeError, RecursionError, subprocess.TimeoutExpired):
            unknown.append({"path": path, "reason": "could not safely parse both versions"})
            continue
        removed = sorted(previous - current)
        if len(missing) + len(removed) > 5000:
            raise ValueError("removed test definition count exceeds the limit")
        missing.extend({"path": path, "definition": name} for name in removed)
    return {"complete": not unknown, "removed_definitions": missing, "unknown": unknown,
            "scope": "Top-level Python test_ functions and class methods in staged test_*.py or *_test.py files. A definition may have moved or been renamed; behavior and skipped tests are not checked. Index changes during the scan are not atomic."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path, nargs="?", default=Path.cwd())
    args = parser.parse_args()
    try:
        report = inspect(args.repository)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
