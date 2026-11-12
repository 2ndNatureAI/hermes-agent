"""Bounded ZIP import inventory; archive entries never become filesystem paths."""

import re
import stat
import struct
import zipfile
import zlib
from pathlib import PurePosixPath

from import_inventory import _imports, _MAX_IMPORTS_PER_FILE, _MAX_IMPORTS_TOTAL, _SKIP, _SUFFIXES

MAX_ARCHIVE = 512 * 1024 * 1024
MAX_DIRECTORY = 4 * 1024 * 1024
MAX_MEMBERS = 5000
MAX_TOTAL_SOURCE = 32 * 1024 * 1024


def _check_directory(path):
    size = path.stat().st_size
    if size > MAX_ARCHIVE:
        raise ValueError("ZIP exceeds the archive size limit")
    with path.open("rb") as source:
        source.seek(max(0, size - 65557))
        tail = source.read(65557)
    pos = tail.rfind(b"PK\x05\x06")
    if pos < 0 or len(tail) - pos < 22:
        raise ValueError("ZIP end record missing")
    _, disk, cd_disk, disk_count, count, cd_size, cd_offset, comment = struct.unpack_from("<4s4H2LH", tail, pos)
    if pos + 22 + comment != len(tail):
        raise ValueError("Invalid ZIP end record")
    if disk or cd_disk or disk_count != count or count == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        raise ValueError("ZIP64 and multidisk archives are not supported")
    if count > MAX_MEMBERS or cd_size > MAX_DIRECTORY or cd_offset + cd_size > size - len(tail) + pos:
        raise ValueError("ZIP directory exceeds limits or has an invalid offset")
    return count


def _safe_member(info):
    name = info.filename
    parts = name.split("/")
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    return (bool(name) and not name.startswith("/") and "\\" not in name and "\x00" not in name
            and not re.match(r"^[A-Za-z]:", name) and all(part not in (".", "..") for part in parts)
            and kind in (0, stat.S_IFREG, stat.S_IFDIR) and not stat.S_ISLNK(mode))


def _read_member(archive, info, language, max_bytes, remaining_imports):
    entry = {"path": info.filename, "language": language, "imports": [], "status": "ok"}
    if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        entry["status"] = "unsupported_or_encrypted"
    elif info.file_size > max_bytes:
        entry["status"] = "too_large"
    else:
        try:
            with archive.open(info) as source:
                raw = source.read(max_bytes + 1)
            if len(raw) > max_bytes:
                entry["status"] = "too_large"
            else:
                found, long_line = _imports(raw.decode("utf-8-sig"), language)
                allowed = min(_MAX_IMPORTS_PER_FILE, remaining_imports)
                entry["imports"] = found[:allowed]
                if len(found) > allowed:
                    entry["imports_truncated"] = True
                if long_line:
                    entry["status"] = "line_too_long"
        except (SyntaxError, RecursionError):
            entry["status"] = "parse_error"
        except UnicodeDecodeError:
            entry["status"] = "non_utf8"
        except (OSError, RuntimeError, zipfile.BadZipFile, EOFError, zlib.error):
            entry["status"] = "unreadable"
    return entry


def inventory_zip(path, max_files, max_file_bytes):
    expected_count = _check_directory(path)
    entries, errors = [], []
    seen = set()
    source_bytes = import_count = excluded = 0
    file_limit = total_limit = False
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) != expected_count or len(infos) > MAX_MEMBERS:
                raise ValueError("ZIP member count does not match its directory")
            for info in infos:
                if not _safe_member(info) or info.filename in seen:
                    excluded += 1
                    continue
                seen.add(info.filename)
                if info.is_dir() or any(part.casefold() in _SKIP for part in info.filename.split("/")[:-1]):
                    continue
                language = _SUFFIXES.get(PurePosixPath(info.filename).suffix.lower())
                if not language:
                    continue
                if len(entries) >= max_files:
                    file_limit = True
                    break
                if info.file_size <= max_file_bytes and source_bytes + info.file_size > MAX_TOTAL_SOURCE:
                    total_limit = True
                    break
                if info.file_size <= max_file_bytes:
                    source_bytes += info.file_size
                entry = _read_member(archive, info, language, max_file_bytes, _MAX_IMPORTS_TOTAL - import_count)
                import_count += len(entry["imports"])
                entries.append(entry)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("Invalid or unreadable ZIP archive") from exc
    complete = not (excluded or file_limit or total_limit or errors or
                    any(e["status"] != "ok" or e.get("imports_truncated") for e in entries))
    return {"complete": complete, "file_limit_reached": file_limit, "excluded_members": excluded,
            "total_source_limit_reached": total_limit, "errors": errors, "files": entries,
            "scope": "ZIP source members only; Python AST; JavaScript/TypeScript import-pattern heuristic; dependency resolution not performed"}
