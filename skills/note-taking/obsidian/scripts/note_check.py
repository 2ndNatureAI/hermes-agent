#!/usr/bin/env python3
"""Read-only, scoped Obsidian link diagnostics. No plugins, network or writes."""

from __future__ import annotations
import argparse, hashlib, json, os, re, stat
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

VERSION = "0.1.3-candidate"
MAX_BYTES = 2_000_000
LINK = re.compile(r"(?<!!)\[\[([^\]\n]+)\]\]|!\[\[([^\]\n]+)\]\]")


def linklike(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def fail_walk(error):
    raise error


def _mask_inline_and_comments(text, blank):
    # Match same-width backtick runs on each line, then respect source order:
    # comment markers inside a code span are literal, and vice versa.
    runs = list(re.finditer(r"`+|\n", text))
    starts = {}
    next_run = {}
    following = {}
    for i in range(len(runs) - 1, -1, -1):
        if runs[i].group() == "\n":
            next_run.clear()
            continue
        starts[runs[i].start()] = i
        width = len(runs[i].group())
        following[i] = next_run.get(width)
        next_run[width] = i
    pos = 0
    while pos < len(text):
        if text.startswith("%%", pos):
            close = text.find("%%", pos + 2)
            end = len(text) if close < 0 else close + 2
        elif text.startswith("<!--", pos):
            close = text.find("-->", pos + 4)
            end = len(text) if close < 0 else close + 3
        elif pos in starts:
            i = starts[pos]
            close = following[i]
            if close is None:
                pos = runs[i].end()
                continue
            end = runs[close].end()
        else:
            pos += 1
            continue
        blank(pos, end)
        pos = end


def visible(text):
    # Preserve offsets/line numbers while masking code, comments and properties.
    chars = list(text)

    def blank(a, b):
        for i in range(a, b):
            if chars[i] != "\n":
                chars[i] = " "

    opening = re.match(r"\A---\r?\n", text)
    if opening:
        end = re.search(r"^---[ \t]*\r?$", text[opening.end() :], re.M)
        if end:
            blank(0, opening.end() + end.end())
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        m = re.match(r"^\s*(`{3,}|~{3,})", line)
        if m:
            token = m.group(1)
            if fence is None:
                fence = (token[0], len(token))
                blank(offset, offset + len(line))
            elif token[0] == fence[0] and len(token) >= fence[1]:
                blank(offset, offset + len(line))
                fence = None
            elif fence:
                blank(offset, offset + len(line))
        elif fence:
            blank(offset, offset + len(line))
        offset += len(line)
    _mask_inline_and_comments("".join(chars), blank)
    return "".join(chars)


def _collect(root: Path):
    files = {}
    notes = {}
    skipped = []
    digest = hashlib.sha256()
    for base, dirs, names in os.walk(root, followlinks=False, onerror=fail_walk):
        kept = []
        for d in sorted(dirs):
            p = Path(base) / d
            if d.startswith("."):
                skipped.append({
                    "path": p.relative_to(root).as_posix(),
                    "reason": "hidden_directory",
                })
                continue
            if linklike(p):
                skipped.append({
                    "path": p.relative_to(root).as_posix(),
                    "reason": "symlink" if p.is_symlink() else "reparse_point",
                })
                continue
            kept.append(d)
        dirs[:] = kept
        for name in sorted(names):
            p = Path(base) / name
            rel = p.relative_to(root).as_posix()
            if linklike(p):
                skipped.append({
                    "path": rel,
                    "reason": "symlink" if p.is_symlink() else "reparse_point",
                })
                continue
            if not p.is_file():
                continue
            files[rel] = p
            if p.suffix.lower() != ".md":
                continue
            if p.stat().st_size > MAX_BYTES:
                skipped.append({"path": rel, "reason": "oversized"})
                continue
            try:
                data = p.read_bytes()
                text = data.decode("utf-8-sig")
            except (OSError, UnicodeError):
                skipped.append({"path": rel, "reason": "unreadable_utf8"})
                continue
            clean = visible(text)
            notes[rel] = {
                "text": clean,
                "headings": set(
                    m.group(1).strip().rstrip("#").strip().casefold()
                    for m in re.finditer(r"^\s{0,3}#{1,6}\s+(.+)$", clean, re.M)
                ),
                "blocks": set(re.findall(r"\^([\w-]+)\s*$", clean, re.M)),
            }
            digest.update(rel.encode() + b"\0" + hashlib.sha256(data).digest())
    return files, notes, skipped, digest.hexdigest()


def _find_links(notes, files, complete):
    findings = []
    checked = 0
    for src, note in sorted(notes.items()):
        for m in LINK.finditer(note["text"]):
            # An odd run of backslashes escapes the first bracket. An escaped
            # embed marker alone does not escape the following wikilink.
            opening = m.start() + (1 if m.group(2) is not None else 0)
            backslash = opening - 1
            while backslash >= 0 and note["text"][backslash] == "\\":
                backslash -= 1
            if (opening - backslash - 1) % 2:
                continue
            raw = m.group(1) or m.group(2)
            before_alias, separator, _ = raw.partition("|")
            if separator:
                # Obsidian tables escape the alias separator as \|.
                trailing_slashes = len(before_alias) - len(before_alias.rstrip("\\"))
                if trailing_slashes % 2:
                    before_alias = before_alias[:-1]
            target = unquote(before_alias.strip())
            dest, sep, anchor = target.partition("#")
            checked += 1
            status = "resolved"
            matches = []
            # Never resolve traversal or absolute paths outside the explicitly selected root.
            if (
                re.match(r"^(?:/|[A-Za-z]:|\\|[a-zA-Z][\w+.-]*:)", dest)
                or "\\" in dest
                or ".." in PurePosixPath(dest).parts
            ):
                status = "outside_scope"
            elif not dest:
                matches = [src]
            else:
                # A dot is legal in an extensionless note title (e.g. v1.2).
                # Only an explicit Markdown extension rules out appending it.
                variants = [dest] if dest.lower().endswith(".md") else [dest + ".md", dest]
                local = [str(PurePosixPath(src).parent / v) for v in variants]
                exact = [v for v in variants + local if v in files]
                if exact:
                    matches = list(dict.fromkeys(exact))
                elif "/" not in dest:
                    matches = [p for p in files if PurePosixPath(p).name in variants]
                if not matches:
                    status = "missing" if complete else "unresolved_in_scope"
                elif len(matches) > 1:
                    status = "ambiguous"
            if status == "resolved" and matches and sep:
                target_note = notes.get(matches[0])
                if not target_note:
                    status = "anchor_not_checked"
                elif anchor.startswith("^") and anchor[1:] not in target_note["blocks"]:
                    status = "missing_block"
                elif (
                    not anchor.startswith("^")
                    and anchor.casefold() not in target_note["headings"]
                ):
                    status = "missing_heading"
            if status != "resolved":
                findings.append({
                    "source": src,
                    "line": note["text"].count("\n", 0, m.start()) + 1,
                    "target": target,
                    "status": status,
                    "candidates": sorted(matches),
                })
    return findings, checked


def inspect(root: Path, complete=False):
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("scope must be a directory")
    files, notes, skipped, digest = _collect(root)
    complete = complete and not skipped
    findings, checked = _find_links(notes, files, complete)
    return {
        "version": VERSION,
        "scope_mode": "complete" if complete else "partial",
        "note_count": len(notes),
        "link_count": checked,
        "resolved_count": checked - len(findings),
        "findings": findings,
        "skipped": skipped,
        "input_manifest_sha256": digest,
        "limitations": [
            "Not a full Markdown/YAML parser. HTML, aliases, advanced heading formatting and Markdown links are not resolved.",
            "Partial scope does not prove absent links are broken. No source note text is copied into this report.",
            "No files were modified.",
        ],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("scope", type=Path)
    p.add_argument(
        "--complete-vault",
        action="store_true",
        help="Only use when scope contains the entire vault; otherwise absent links remain unknown.",
    )
    args = p.parse_args()
    print(
        json.dumps(
            inspect(args.scope, args.complete_vault), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
