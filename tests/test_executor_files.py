import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from bolos.executor import files


@pytest.fixture
def folders(tmp_path, monkeypatch):
    roots = {name: tmp_path / f"redirected {name}" for name in ("Downloads", "Documents", "Desktop")}
    for root in roots.values():
        root.mkdir()
    monkeypatch.setattr(files, "_known_folder", roots.get)
    return roots


def make_file(folder: Path, name: str) -> Path:
    path = folder / name
    path.write_text("sample", encoding="utf-8")
    return path.resolve()


def entry(path, *, created, modified=0, attributes=0):
    return path, SimpleNamespace(st_mode=stat.S_IFREG, st_birthtime_ns=created, st_mtime_ns=modified,
                                 st_file_attributes=attributes)


def test_named_lookup_finds_spoken_extension_and_unicode_name(folders):
    subfolder = folders["Documents"] / "Semester 1"
    subfolder.mkdir()
    report = make_file(subfolder, "परीक्षा-रिपोर्ट final.pdf")
    make_file(subfolder, "परीक्षा-रिपोर्ट final.docx")
    assert files.find_file("परीक्षा रिपोर्ट pdf") == files.FileResult("found", report)


def test_same_filename_in_different_folders_does_not_open_either(folders):
    for folder in ("Downloads", "Desktop"):
        make_file(folders[folder], "Resume.pdf")
    assert files.find_file("resume pdf") == files.FileResult("ambiguous")


def test_explicit_extension_filters_partial_matches(folders):
    make_file(folders["Downloads"], "Budget report pdf.txt")
    report = make_file(folders["Documents"], "Budget report 2026.pdf")
    assert files.find_file("budget report.pdf") == files.FileResult("found", report)
    exact = make_file(folders["Desktop"], "Budget report.pdf")
    assert files.find_file("budget report.pdf") == files.FileResult("found", exact)


@pytest.mark.parametrize("query", [r"C:\Windows\system.ini", "../secret.pdf", "::", "   "])
def test_path_like_or_empty_query_is_rejected(query, monkeypatch):
    def unexpected_roots():
        pytest.fail("An invalid query must never reach the filesystem")

    monkeypatch.setattr(files, "_roots", unexpected_roots)
    assert files.find_file(query).status == "invalid"


def test_latest_download_uses_creation_time_and_does_not_skip_a_newer_partial(folders, monkeypatch):
    older = folders["Downloads"] / "older-edited.pdf"
    newest = folders["Downloads"] / "newest.pdf"
    partial = folders["Downloads"] / "current.pdf.crdownload"
    candidates = [entry(older, created=10, modified=40), entry(newest, created=20, modified=20)]

    def downloads(roots, depth):
        assert roots == (folders["Downloads"].resolve(),) and depth == 0
        return iter(candidates)

    monkeypatch.setattr(files, "_entries", downloads)
    assert files.latest_download() == files.FileResult("found", newest)
    candidates.append(entry(partial, created=20, modified=50))
    assert files.latest_download() == files.FileResult("pending")
    candidates[-1] = entry(partial.with_suffix(""), created=20, modified=50)
    assert files.latest_download().status == "ambiguous"


def test_reparse_match_cannot_be_returned_as_a_launchable_path(folders, monkeypatch):
    alias = folders["Downloads"] / "Report.pdf"
    monkeypatch.setattr(files, "_entries", lambda *args: iter([
        entry(alias, created=10, attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT),
    ]))
    assert files.find_file("report") == files.FileResult("unsafe")
    assert files.latest_download() == files.FileResult("unsafe")


def test_incomplete_search_does_not_pick_a_potential_duplicate(folders, monkeypatch):
    make_file(folders["Downloads"], "letter.pdf")
    make_file(folders["Documents"], "letter.pdf")
    monkeypatch.setattr(files, "_ENTRY_LIMIT", 1)
    assert files.find_file("letter.pdf") == files.FileResult("unavailable")


def test_open_only_sends_a_supported_file_inside_a_known_folder(folders, tmp_path, monkeypatch):
    document = make_file(folders["Downloads"], "Report, Q4.PDF")
    script = make_file(folders["Downloads"], "Report.pdf.cmd")
    outside = make_file(tmp_path, "outside.pdf")
    calls = []
    monkeypatch.setattr(files.os, "startfile", lambda path, verb: calls.append((path, verb)))
    assert files.find_file(script.name) == files.FileResult("found", script)
    assert not files.open_file(script)
    assert not files.open_file(outside)
    assert not files.open_file(folders["Downloads"] / ".." / outside.name)
    assert files.open_file(document)
    assert calls == [(str(document), "open")]


def test_explorer_can_select_an_unsupported_file_without_opening_it(folders, monkeypatch):
    from win32com.shell import shell

    archive = make_file(folders["Downloads"], "Archive, July.zip")
    events = []

    def parse(path, _attributes):
        assert path == str(archive)
        return [b"folder", b"item"], 0

    monkeypatch.setattr(shell, "SHParseDisplayName", parse)
    monkeypatch.setattr(shell, "SHOpenFolderAndSelectItems", lambda *args: events.append(args))
    assert files.reveal_file(archive)
    assert events == [([b"folder"], [[b"item"]])]
