"""Find local files by name and hand them to Windows without passing text to a shell."""

from __future__ import annotations

import os
import stat
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

FileStatus = Literal["found", "not_found", "ambiguous", "invalid", "unsafe", "pending", "unavailable"]


@dataclass(frozen=True)
class FileResult:
    status: FileStatus
    path: Path | None = None


_FOLDERS = ("Downloads", "Documents", "Desktop")
_SEARCH_DEPTH = 3
_ENTRY_LIMIT = 15_000
_PARTIAL = {".crdownload", ".part", ".partial", ".download"}
_OPENABLE = {
    ".pdf", ".txt", ".md", ".csv", ".tsv", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif",
    ".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus", ".mp4", ".mov", ".mkv", ".webm", ".avi",
}
_HIDDEN = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM


class _ScanLimit(Exception):
    """An incomplete scan cannot prove a result is unique."""


def _known_folder(name: str) -> Path | None:
    from pywintypes import com_error, error
    from win32com.shell import shell, shellcon

    folder_id = getattr(shellcon, f"FOLDERID_{name}")
    try:
        return Path(shell.SHGetKnownFolderPath(folder_id, shellcon.KF_FLAG_DONT_VERIFY))
    except (OSError, com_error, error):
        return None


def _local_absolute(path: Path) -> bool:
    return path.is_absolute() and not str(path).startswith(("\\\\", "//"))


def _roots(names: tuple[str, ...] = _FOLDERS) -> tuple[Path, ...]:
    roots = []
    for name in names:
        folder = _known_folder(name)
        if folder is None or not _local_absolute(folder):
            continue
        try:
            # The user's known folder itself may be redirected. Scan its real location,
            # while skipping aliases and junctions encountered underneath it.
            folder = folder.resolve(strict=True)
            if _local_absolute(folder) and folder.is_dir() and folder not in roots:
                roots.append(folder)
        except (OSError, ValueError):
            continue
    return tuple(roots)


def _reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _entries(roots: tuple[Path, ...], depth_limit: int) -> Iterator[tuple[Path, os.stat_result]]:
    scanned = 0
    pending = [(root, 0) for root in reversed(roots)]
    visited: set[Path] = set()
    while pending:
        folder, depth = pending.pop()
        if folder in visited:
            continue
        visited.add(folder)
        if depth and _reparse(folder.lstat()):
            continue  # A queued directory can be replaced by a junction.
        with os.scandir(folder) as entries:
            for entry in entries:
                scanned += 1
                if scanned > _ENTRY_LIMIT:
                    raise _ScanLimit
                path = Path(entry.path)
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # A download can be renamed while it completes.
                if getattr(info, "st_file_attributes", 0) & _HIDDEN:
                    continue
                if stat.S_ISDIR(info.st_mode) and not _reparse(info):
                    if depth < depth_limit:
                        pending.append((path, depth + 1))
                elif stat.S_ISREG(info.st_mode) or _reparse(info):
                    yield path, info


def _key(name: str) -> str:
    normal = unicodedata.normalize("NFKC", name).casefold()
    # Preserve Indic vowel marks as well as letters when normalizing separators.
    return " ".join("".join(c if c.isalnum() or unicodedata.category(c).startswith("M") else " "
                            for c in normal).split())


def _score(query: str, path: Path) -> int:
    full, stem = _key(path.name), _key(path.stem)
    if query == full:
        return 4
    if query == stem:
        return 3
    short_name, separator, extension = query.rpartition(" ")
    if separator and extension == _key(path.suffix):
        if f" {short_name} " in f" {stem} ":
            return 2
        if len(short_name) >= 3 and short_name in stem:
            return 1
    if f" {query} " in f" {full} ":
        return 2
    if len(query) >= 3 and query in stem:
        return 1
    return 0


def _safe_name(path: Path) -> bool:
    return bool(path.name) and path.name.rstrip(" .") == path.name and ":" not in path.name


def _result(path: Path, info: os.stat_result) -> FileResult:
    if _reparse(info) or not _safe_name(path):
        return FileResult("unsafe")
    if path.suffix.casefold() in _PARTIAL:
        return FileResult("pending")
    return FileResult("found", path)


def find_file(query: str) -> FileResult:
    """Find a unique name in the user's Downloads, Documents, or Desktop folders."""
    query = unicodedata.normalize("NFKC", query).strip().rstrip(".!?।")
    needle = _key(query)
    if not needle or any(c in query for c in ("/", "\\", ":", "\x00")):
        return FileResult("invalid")
    roots = _roots()
    if not roots:
        return FileResult("unavailable")
    extension = Path(query).suffix.casefold()
    if not any(c.isalpha() for c in extension):
        extension = ""
    best = 0
    matches: dict[Path, os.stat_result] = {}
    try:
        for path, info in _entries(roots, _SEARCH_DEPTH):
            if extension and path.suffix.casefold() != extension:
                continue
            score = _score(needle, path)
            if not score or score < best:
                continue
            if score > best:
                best, matches = score, {}
            matches[path] = info
    except (OSError, _ScanLimit):
        return FileResult("unavailable")
    if len(matches) > 1:
        return FileResult("ambiguous")
    if not matches:
        return FileResult("not_found")
    path, info = next(iter(matches.items()))
    return _result(path, info)


def latest_download() -> FileResult:
    """Find the newest file directly in Downloads, using creation time when available."""
    roots = _roots(("Downloads",))
    if not roots:
        return FileResult("unavailable")
    newest = None
    matches: dict[Path, os.stat_result] = {}
    try:
        for path, info in _entries(roots, 0):
            stamp = getattr(info, "st_birthtime_ns", info.st_mtime_ns)
            if newest is not None and stamp < newest:
                continue
            if newest is None or stamp > newest:
                newest, matches = stamp, {}
            matches[path] = info
    except (OSError, _ScanLimit):
        return FileResult("unavailable")
    if not matches:
        return FileResult("not_found")
    if any(path.suffix.casefold() in _PARTIAL for path in matches):
        return FileResult("pending")
    if len(matches) > 1:
        return FileResult("ambiguous")
    path, info = next(iter(matches.items()))
    return _result(path, info)


def _still_safe(path: Path) -> bool:
    if not _local_absolute(path) or not _safe_name(path) or path.suffix.casefold() in _PARTIAL:
        return False
    root = next((root for root in _roots() if path.is_relative_to(root)), None)
    if root is None:
        return False
    try:
        # Recheck when used so a replaced file or folder cannot redirect an earlier match.
        folder = root
        for part in path.relative_to(root).parts[:-1]:
            folder /= part
            if not _safe_name(folder) or _reparse(folder.lstat()):
                return False
        info = path.lstat()
        return stat.S_ISREG(info.st_mode) and not _reparse(info)
    except (OSError, ValueError):
        return False


def open_file(path: Path) -> bool:
    """Open a supported document, image, or media file with its Windows default app."""
    if path.suffix.casefold() not in _OPENABLE or not _still_safe(path):
        return False
    try:
        os.startfile(str(path), "open")
    except OSError:
        return False
    return True


def reveal_file(path: Path) -> bool:
    """Select the file in Explorer without opening it."""
    import pythoncom
    from win32com.shell import shell
    from winerror import RPC_E_CHANGED_MODE

    if not _still_safe(path):
        return False
    initialized = False
    try:
        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            initialized = True
        except pythoncom.com_error as error:
            if error.hresult != RPC_E_CHANGED_MODE:
                raise
        item, _ = shell.SHParseDisplayName(str(path), 0)
        shell.SHOpenFolderAndSelectItems(item[:-1], [item[-1:]])
    except (OSError, pythoncom.com_error):
        return False
    finally:
        if initialized:
            pythoncom.CoUninitialize()
    return True
