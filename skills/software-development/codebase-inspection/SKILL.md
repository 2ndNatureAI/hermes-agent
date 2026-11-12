---
name: codebase-inspection
description: "Inspect codebases for imports and size."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [LOC, Code Analysis, pygount, Codebase, Metrics, Repository]
    related_skills: [github]
---

# Codebase Inspection with pygount

Inventory local Python and JavaScript/TypeScript imports without executing the repository. When available, `pygount` can also provide line counts and language breakdowns.

## When to Use

- User asks for LOC (lines of code) count
- User wants a language breakdown of a repo
- User asks about codebase size or composition
- User wants code-vs-comment ratios
- General "how big is this repo" questions
- User wants a first-pass map of local Python or JavaScript/TypeScript imports

## Prerequisites

The offline import inventory requires Python 3 only. The line-count commands below
also require `pygount` to be installed in an approved environment.

## Offline import inventory

From this skill directory, run:

```bash
python scripts/import_inventory.py /path/to/repo
python scripts/import_inventory.py /path/to/repo.zip
```

The JSON output lists files and their import names without executing source or
contacting the network. Python imports use the standard-library AST parser;
JavaScript/TypeScript imports use a text heuristic and may miss or misidentify
imports. The output does not resolve dependency versions or establish runtime
behavior. `complete` means the eligible files encountered within the stated
limits were read and parsed; it does not guarantee every import was found.

By default the tool reads at most 2,000 source files, 10,000 directories, and 262,144 bytes per file,
and excludes common dependency, build, and cache directories. Adjust the bounds
with `--max-files` and `--max-file-bytes`. It records at most 500 imports per file
and 20,000 overall. Skipped links, unreadable files,
parse failures, JavaScript/TypeScript lines over 4,096 characters, and exceeded
limits are reported as incomplete. Link checks are
not atomic against concurrent filesystem changes; inspect untrusted repositories
in an isolated, stable workspace.

ZIP support reads eligible source members directly without extraction or code execution.
It rejects ZIP64 and multidisk archives, more than 5,000 members, a central directory
larger than 4 MiB, and archives larger than 512 MiB. It reads at most 32 MiB of
eligible uncompressed source in total; unsafe paths, links, encrypted or unsupported
compression, and limits are reported as incomplete. Treat archived source and paths as
untrusted, and keep the input archive stable while scanning. An import inventory is not
an evaluation, security review, dependency resolution or implementation.

## 1. Basic Summary (Most Common)

Get a full language breakdown with file counts, code lines, and comment lines:

```bash
cd /path/to/repo
pygount --format=summary \
  --folders-to-skip=".git,node_modules,venv,.venv,__pycache__,.cache,dist,build,.next,.tox,.eggs,*.egg-info" \
  .
```

**IMPORTANT:** Always use `--folders-to-skip` to exclude dependency/build directories, otherwise pygount will crawl them and take a very long time or hang.

## 2. Common Folder Exclusions

Adjust based on the project type:

```bash
# Python projects
--folders-to-skip=".git,venv,.venv,__pycache__,.cache,dist,build,.tox,.eggs,.mypy_cache"

# JavaScript/TypeScript projects
--folders-to-skip=".git,node_modules,dist,build,.next,.cache,.turbo,coverage"

# General catch-all
--folders-to-skip=".git,node_modules,venv,.venv,__pycache__,.cache,dist,build,.next,.tox,vendor,third_party"
```

## 3. Filter by Specific Language

```bash
# Only count Python files
pygount --suffix=py --format=summary .

# Only count Python and YAML
pygount --suffix=py,yaml,yml --format=summary .
```

## 4. Detailed File-by-File Output

```bash
# Default format shows per-file breakdown
pygount --folders-to-skip=".git,node_modules,venv" .

# Sort by code lines (pipe through sort)
pygount --folders-to-skip=".git,node_modules,venv" . | sort -t$'\t' -k1 -nr | head -20
```

## 5. Output Formats

```bash
# Summary table (default recommendation)
pygount --format=summary .

# JSON output for programmatic use
pygount --format=json .

# Pipe-friendly: Language, file count, code, docs, empty, string
pygount --format=summary . 2>/dev/null
```

## 6. Interpreting Results

The summary table columns:
- **Language** — detected programming language
- **Files** — number of files of that language
- **Code** — lines of actual code (executable/declarative)
- **Comment** — lines that are comments or documentation
- **%** — percentage of total

Special pseudo-languages:
- `__empty__` — empty files
- `__binary__` — binary files (images, compiled, etc.)
- `__generated__` — auto-generated files (detected heuristically)
- `__duplicate__` — files with identical content
- `__unknown__` — unrecognized file types

## Pitfalls

1. **Always exclude .git, node_modules, venv** — without `--folders-to-skip`, pygount will crawl everything and may take minutes or hang on large dependency trees.
2. **Markdown shows 0 code lines** — pygount classifies all Markdown content as comments, not code. This is expected behavior.
3. **JSON files show low code counts** — pygount may count JSON lines conservatively. For accurate JSON line counts, use `wc -l` directly.
4. **Large monorepos** — for very large repos, consider using `--suffix` to target specific languages rather than scanning everything.
