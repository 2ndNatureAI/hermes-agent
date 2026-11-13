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


def _table_cells(line):
    # GFM tables use unescaped pipes as cell boundaries. Pipes escaped in a
    # wikilink alias or a code span belong to the cell, not the table layout.
    pipes = []
    for match in re.finditer(r'\|', line):
        pos = match.start()
        before = pos - 1
        while before >= 0 and line[before] == '\\':
            before -= 1
        if (pos - before - 1) % 2 == 0:
            pipes.append(pos)
    if not pipes:
        return [], []
    cells = []
    start = 0
    for end in pipes:
        cells.append(line[start:end].strip())
        start = end + 1
    cells.append(line[start:].strip())
    if cells[0] == '':
        cells.pop(0)
    if cells and cells[-1] == '':
        cells.pop()
    return cells, pipes


def _mask_code_and_comments(text, blank):
    # Inline spans can cross ordinary paragraph lines, but not block boundaries.
    # This deliberately recognizes a conservative subset of Markdown blocks.
    block_start = re.compile(
        r'^(?:[ \t]*(?:$|#{1,6}(?:[ \t]|$)|>|[-+*][ \t]+|'
        r'\d{1,9}[.)][ \t]+|`{3,}|~{3,}|%%|<!--|<[/!?A-Za-z]|'
        r'(?:[-*_][ \t]*){3,}$)| {4,}|\t)'
    )
    lines = text.splitlines(keepends=True)
    table_rows = set()
    divider_line = re.compile(r'^[ \t]*\|?[ \t]*:?-+:?(?:[ \t]*\|[ \t]*:?-+:?)*[ \t]*\|?[ \t]*$')
    i = 1
    while i < len(lines):
        line = lines[i].rstrip('\r\n')
        if not divider_line.fullmatch(line):
            i += 1
            continue
        divider, _ = _table_cells(line)
        if not divider and re.fullmatch(r'[ \t]*:?-+:?[ \t]*', line):
            # A single-column table may omit pipes in its delimiter, provided
            # the preceding header still has an unescaped table pipe.
            divider = [line.strip()]
        if not divider:
            i += 1
            continue
        header, _ = _table_cells(lines[i - 1].rstrip('\r\n'))
        if (header and len(header) == len(divider)
                and all(re.fullmatch(r':?-+:?', cell) for cell in divider)):
            table_rows.update((i - 1, i))
            i += 1
            while i < len(lines) and not block_start.match(lines[i].rstrip('\r\n')):
                table_rows.add(i)
                i += 1
        else:
            i += 1
    boundaries, table_pipes = set(), set()
    offset, previous_block, previous_plain_quote = 0, True, False
    for line_number, line in enumerate(lines):
        # Unknown pipe layouts may be table rows. Keep old line-local behavior
        # across them without treating their pipes as cell separators.
        block = line_number in table_rows or '|' in line or bool(block_start.match(line.rstrip('\r\n')))
        quote = re.match(r'^ {0,3}>[ \t]?(.*)$', line.rstrip('\r\n'))
        plain_quote = bool(quote and not block_start.match(quote.group(1)) and '|' not in line)
        if line_number in table_rows:
            _, pipes = _table_cells(line.rstrip('\r\n'))
            table_pipes.update(offset + pos for pos in pipes)
        if offset and (previous_block or block) and not (previous_plain_quote and plain_quote):
            boundaries.add(offset - 1)
        offset += len(line)
        previous_block = block
        previous_plain_quote = plain_quote

    # Fences, comments and inline code are processed in source order. A
    # backslash can escape an opener, but never escapes a closer inside code.
    runs = list(re.finditer(r"`+|\n", text))
    starts = {}
    next_run = {}
    table_pipes = sorted(table_pipes)
    pipe_index = len(table_pipes) - 1
    for i in range(len(runs) - 1, -1, -1):
        while pipe_index >= 0 and table_pipes[pipe_index] > runs[i].start():
            next_run.clear()
            pipe_index -= 1
        if runs[i].group() == "\n":
            if runs[i].start() in boundaries:
                next_run.clear()
            continue
        start = runs[i].start()
        width = len(runs[i].group())
        before = start - 1
        while before >= 0 and text[before] == '\\':
            before -= 1
        escaped = (start - before - 1) % 2 == 1
        if not escaped:
            starts[start] = (i, next_run.get(width))
        elif width > 1:
            # The escaped first tick is literal; the rest can open a span.
            starts[start + 1] = (i, next_run.get(width - 1))
        next_run[width] = i
    pos = 0
    fence = None
    while pos < len(text):
        if pos == 0 or text[pos - 1] == "\n":
            newline = text.find("\n", pos)
            line_end = len(text) if newline < 0 else newline + 1
            line = text[pos:line_end]
            m = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
            if fence is not None:
                if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not line[m.end():].strip():
                    fence = None
                blank(pos, line_end)
                pos = line_end
                continue
            if m:
                fence = (m.group(1)[0], len(m.group(1)))
                blank(pos, line_end)
                pos = line_end
                continue
        if text.startswith("%%", pos):
            close = text.find("%%", pos + 2)
            end = len(text) if close < 0 else close + 2
        elif text.startswith("<!--", pos):
            close = text.find("-->", pos + 4)
            end = len(text) if close < 0 else close + 3
        elif pos in starts:
            i, close = starts[pos]
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
    # Frontmatter is masked before tokenization; fence markers in properties
    # are data and cannot hide the body.
    _mask_code_and_comments("".join(chars), blank)
    return "".join(chars)


def _heading_paths(text):
    # Keep the heading hierarchy so [[Note#Parent#Child]] is not mistaken
    # for one literal heading, and a child elsewhere does not resolve it.
    stack = []
    paths = []
    for match in re.finditer(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+)$", text, re.M):
        level = len(match.group(1))
        title = match.group(2).strip().rstrip("#").strip().casefold()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        paths.append(tuple(item[1] for item in stack))
    return paths


def _has_heading(note, anchor):
    parts = tuple(part.strip().casefold() for part in anchor.split("#"))
    if not all(parts):
        return False
    if len(parts) == 1:
        return parts[0] in note["headings"]
    return any(path[-len(parts):] == parts for path in note["heading_paths"])


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
                "heading_paths": _heading_paths(clean),
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
                    and not _has_heading(target_note, anchor)
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
