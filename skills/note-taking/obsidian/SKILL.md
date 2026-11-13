---
name: obsidian
description: Read, search, create, and edit notes in the Obsidian vault.
version: 1.0.0
author: Teknium (teknium1), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Obsidian, Notes, Markdown, Vault]
    related_skills: []
---

# Obsidian Vault

Use this skill for filesystem-first Obsidian vault work: reading notes, listing notes, searching note files, creating notes, appending content, and adding wikilinks.

## Vault path

Use a known or resolved vault path before calling file tools.

The documented vault-path convention is the `OBSIDIAN_VAULT_PATH` environment variable, for example from `${HERMES_HOME:-~/.hermes}/.env`. If it is unset, use `~/Documents/Obsidian Vault`.

File tools do not expand shell variables. Do not pass paths containing `$OBSIDIAN_VAULT_PATH` to `read_file`, `write_file`, `patch`, or `search_files`; resolve the vault path first and pass a concrete absolute path. Vault paths may contain spaces, which is another reason to prefer file tools over shell commands.

If the vault path is unknown, `terminal` is acceptable for resolving `OBSIDIAN_VAULT_PATH` or checking whether the fallback path exists. Once the path is known, switch back to file tools.

## Read a note

Use `read_file` with the resolved absolute path to the note. Prefer this over `cat` because it provides line numbers and pagination.

## List notes

Use `search_files` with `target: "files"` and the resolved vault path. Prefer this over `find` or `ls`.

- To list markdown notes, use `pattern: "*.md"` under the vault path. Search returns up to 50 results by default; when the result is truncated, follow its offset guidance or narrow the scope. Do not describe a bounded, interrupted, or filtered result as a complete vault inventory.
- To list a subfolder, search under that subfolder's absolute path.

## Search

Use `search_files` for both filename and content searches. Prefer this over `grep`, `find`, or `ls`.

- For filenames, use `search_files` with `target: "files"` and a filename `pattern`.
- For note contents, use `search_files` with `target: "content"`, the content regex as `pattern`, and `file_glob: "*.md"` when you want to restrict matches to markdown notes.

## Check note references

When asked to check links, the bundled `scripts/note_check.py` can inspect a user-selected, trusted, stable copy of notes without modifying them or using the network. Resolve `skill_dir` from `skill_view`, then run with `terminal`:

```text
python "<skill_dir>/scripts/note_check.py" "<selected-copy>"
```

The JSON report includes relative paths, link targets and an input digest; treat those names as private. The default partial scope reports absent targets as `unresolved_in_scope`, not broken. Use `--complete-vault` only for a verified entire vault copy, and review skipped paths and findings manually. This is a limited Markdown/Obsidian parser, not a safety boundary for a hostile or concurrently changing tree; multiline inline code in lists, blockquotes or indented lines, and some aliases or Markdown formatting, may not be resolved. An exit status of zero means the check ran, not that every link is correct.

## Check a Canvas file

For a user-selected, trusted and stable `.canvas` file, run `python "<skill_dir>/scripts/canvas_check.py" "<canvas-file>"` after resolving `skill_dir` from `skill_view`. The read-only command checks JSON structure, node and edge IDs, and whether edge endpoints exist. It reads at most 5 MiB, does not open file or URL references, and reports at most 100 findings. A passing result is not full JSON Canvas schema validation; positions, node contents, external links, and referenced files are not checked. Exit status is 0 when these checks pass, 1 for findings, and 2 for an unreadable, oversized, invalid, or ambiguous JSON file (including duplicate object keys).

## Create a note

Use `write_file` with the resolved absolute path and the full markdown content. Prefer this over shell heredocs or `echo` because it avoids shell quoting issues and returns structured results.

## Append to a note

Prefer a native file-tool workflow when it is not awkward:

- Read the target note with `read_file`.
- Use `patch` for an anchored append when there is stable context, such as adding a section after an existing heading or appending before a known trailing block.
- Use `write_file` when rewriting the whole note is clearer than constructing a fragile patch.

For an anchored append with `patch`, replace the anchor with the anchor plus the new content.

For a simple append with no stable context, `terminal` is acceptable if it is the clearest safe option.

## Targeted edits

Use `patch` for focused note changes when the current content gives you stable context. Prefer this over shell text rewriting.

## Wikilinks

Obsidian links notes with `[[Note Name]]` syntax. When creating notes, use these to link related content.
