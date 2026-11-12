#!/usr/bin/env python3
"""Create a bounded, offline inventory of Python and JavaScript imports."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import stat
import sys
from pathlib import Path


_SKIP = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
         ".cache", "dist", "build", "coverage", ".next"}
_SUFFIXES = {".py": "python", ".js": "javascript", ".jsx": "javascript",
             ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript"}
_JS_FROM = re.compile(r'''\bfrom[ \t]+["']([^"'\n]+)["']''')
_JS_SIDE_EFFECT = re.compile(r'''^[ \t]*import[ \t]+["']([^"'\n]+)["']''')
_JS_CALL = re.compile(r'''\b(?:require|import)[ \t]*\([ \t]*["']([^"'\n]+)["'][ \t]*\)''')
_MAX_JS_LINE = 4096
_MAX_IMPORTS_PER_FILE = 500
_MAX_IMPORTS_TOTAL = 20000


def _reparse_or_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _imports(text: str, language: str) -> tuple[list[dict], bool]:
    if language == "python":
        tree = ast.parse(text)
        entries = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                entries.extend({"module": name.name, "line": node.lineno} for name in node.names)
            elif isinstance(node, ast.ImportFrom):
                entries.append({"module": "." * node.level + (node.module or ""), "line": node.lineno})
        return sorted(entries, key=lambda item: (item["line"], item["module"])), False
    entries = []
    long_line = False
    for line_number, line in enumerate(text.splitlines(), 1):
        if len(line) > _MAX_JS_LINE:
            long_line = True
            line = line[:_MAX_JS_LINE]
        stripped = line.lstrip()
        if stripped.startswith(("import ", "import\t", "export ", "export\t")):
            match = _JS_SIDE_EFFECT.search(line) or _JS_FROM.search(line)
            if match:
                entries.append({"module": match.group(1), "line": line_number})
        entries.extend({"module": match.group(1), "line": line_number} for match in _JS_CALL.finditer(line))
    return entries, long_line


def _inventory_file(path: Path, root: Path, language: str, max_file_bytes: int, remaining: int):
    entry = {"path": path.relative_to(root).as_posix(), "language": language, "imports": [], "status": "ok"}
    try:
        if _reparse_or_link(path):
            return None
        if not stat.S_ISREG(path.lstat().st_mode):
            entry["status"] = "not_regular"
        else:
            with path.open("rb") as source:
                raw = source.read(max_file_bytes + 1)
            if len(raw) > max_file_bytes:
                entry["status"] = "too_large"
            else:
                found, long_line = _imports(raw.decode("utf-8-sig"), language)
                available = min(_MAX_IMPORTS_PER_FILE, remaining)
                entry["imports"] = found[:available]
                if len(found) > available:
                    entry["imports_truncated"] = True
                if long_line:
                    entry["status"] = "line_too_long"
    except (SyntaxError, RecursionError):
        entry["status"] = "parse_error"
    except UnicodeDecodeError:
        entry["status"] = "non_utf8"
    except OSError:
        entry["status"] = "unreadable"
    return entry


def inventory(root: Path, max_files: int, max_file_bytes: int) -> dict:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("repository path must be a directory")
    entries = []
    errors = []
    excluded_links = 0
    limit_reached = False
    directory_limit_reached = False
    visited_directories = 0
    import_count = 0

    def on_error(error):
        errors.append({"status": "directory_unreadable", "path": str(error.filename or "unknown")})

    for parent, directories, files in os.walk(root, followlinks=False, onerror=on_error):
        visited_directories += 1
        if visited_directories > 10000:
            directory_limit_reached = True
            break
        base = Path(parent)
        keep = []
        for name in sorted(directories):
            if name.casefold() in _SKIP:
                continue
            try:
                if _reparse_or_link(base / name):
                    excluded_links += 1
                else:
                    keep.append(name)
            except OSError:
                errors.append({"status": "directory_unreadable", "path": str((base / name).relative_to(root))})
        directories[:] = keep
        for name in sorted(files):
            path = base / name
            language = _SUFFIXES.get(path.suffix.lower())
            if language is None:
                continue
            if len(entries) >= max_files:
                limit_reached = True
                break
            entry = _inventory_file(path, root, language, max_file_bytes, _MAX_IMPORTS_TOTAL - import_count)
            if entry is None:
                excluded_links += 1
            else:
                import_count += len(entry["imports"])
                entries.append(entry)
        if limit_reached:
            break
    complete = not (errors or excluded_links or limit_reached or directory_limit_reached
                    or any(e["status"] != "ok" or e.get("imports_truncated") for e in entries))
    return {"complete": complete, "file_limit_reached": limit_reached, "excluded_links": excluded_links,
            "directory_limit_reached": directory_limit_reached, "errors": errors, "files": entries,
            "scope": "Python AST; JavaScript/TypeScript import-pattern heuristic; dependency resolution not performed"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("--max-files", type=int, default=2000)
    parser.add_argument("--max-file-bytes", type=int, default=262144)
    args = parser.parse_args()
    if not 1 <= args.max_files <= 10000 or not 1 <= args.max_file_bytes <= 1048576:
        parser.error("limits must be within 1-10000 files and 1-1048576 bytes per file")
    try:
        result = inventory(args.repository, args.max_files, args.max_file_bytes)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
