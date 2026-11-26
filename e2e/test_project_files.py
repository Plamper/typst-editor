"""One file tree per project: nested files in the sidebar, folder-relative compile in the preview, ZIP import dialog."""
import io
import struct
import zipfile
import zlib

from playwright.sync_api import expect

from e2e.helpers import file_paths, put_file, put_source, register


def _png() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")


def _project(page):
    api = page.request
    pid = api.post("/api/projects", data={"title": "Tree"}).json()["id"]
    did = api.post(f"/api/projects/{pid}/docs", data={"title": "Intro", "folder": "Chapters",
                   "content": '#image("../img/dot.png", width: 2cm)\n#include "part.typ"\nIntro text'}).json()["id"]
    assert put_file(api, pid, "img/dot.png", _png()).ok
    assert put_file(api, pid, "Chapters/part.typ", "Part text").ok
    assert put_file(api, pid, "refs.bib", "@book{k, title={T}}").ok
    page.reload()
    page.wait_for_function("window.__booted === true")
    return pid, did


def _unfold(page, name):
    label = page.locator("aside .fhd.folder", has_text=name).first.locator("b")
    if label.get_attribute("aria-expanded") == "false":
        label.click()


def test_tree_lists_documents_and_nested_files_and_opens_them_as_tabs(page):
    register(page, "tree_files")
    pid, did = _project(page)
    group = page.locator("aside .project-group").filter(has_text="Tree").first
    for name in ("Chapters", "img"):
        _unfold(page, name)
    expect(group.locator('[data-package-key="file::refs.bib"]')).to_have_count(1)
    expect(group.locator('[data-package-key="file::Chapters/part.typ"]')).to_have_count(1)
    expect(group.locator('[data-package-key="file::img/dot.png"]')).to_have_count(1)
    expect(group.locator(f'[data-package-key="document:{did}:"]')).to_have_count(1)
    expect(group.locator(".package-files, .doc-files")).to_have_count(0)  # no attachment sub-lists under the document
    group.locator(f'[data-package-key="document:{did}:"]').click()
    expect(page.locator("#title")).to_have_text("Intro")
    group.locator('[data-package-key="file::Chapters/part.typ"]').click()
    expect(page.locator("#tabs button.tab[aria-selected=true]")).to_have_attribute("title", "/Chapters/part.typ")
    expect(page.locator("#cm .cm-content")).to_have_text("Part text")
    expect(page.locator("#pvFor")).to_have_text("Preview: Intro.typ")
    page.click("#cm .cm-content")
    page.keyboard.press("End")
    page.keyboard.type(" edited")
    expect(page.locator("#save")).to_contain_text("saved", timeout=20_000)
    assert page.request.get(f"/api/projects/{pid}/files/Chapters/part.typ").text() == "Part text edited"
    group.locator('[data-package-key="file::img/dot.png"]').click()
    expect(page.locator("#imageFile img")).to_be_visible()
    expect(page.locator("#tabs button.tab")).to_have_count(3)


def test_a_document_in_a_folder_reaches_files_with_relative_paths(page):
    register(page, "tree_relative")
    pid, did = _project(page)
    _unfold(page, "Chapters")
    page.locator(f'[data-package-key="document:{did}:"]').click()
    expect(page.locator("#title")).to_have_text("Intro")
    expect(page.locator("#preview .pvPage[data-done]").first).to_be_visible(timeout=30_000)
    expect(page.locator("#eBadge")).to_be_hidden()
    # the same path without ../ starts in Chapters/ and misses the image: the compiler says so
    put_source(page, '#image("img/dot.png", width: 2cm)\nIntro text')
    expect(page.locator("#eBadge")).to_be_visible(timeout=30_000)
    put_source(page, '#image("/img/dot.png", width: 2cm)\n#include "/Chapters/part.typ"')  # project-root absolute works too
    expect(page.locator("#eBadge")).to_be_hidden(timeout=30_000)
    expect(page.locator("#preview .pvPage[data-done]").first).to_be_visible()


def _zip(entries) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buffer.getvalue()


def _import(page, name, data):
    head = page.locator("#mHead")
    for _ in range(10):  # a pick while the previous import still refreshes the sidebar is ignored (upBusy): pick again
        page.set_input_files("#docPick", files=[{"name": name, "mimeType": "application/zip", "buffer": data}])
        try:
            expect(head).to_have_text(f"New project “{name.removesuffix('.zip')}”", timeout=1500)
            break
        except AssertionError:
            pass
    expect(head).to_have_text(f"New project “{name.removesuffix('.zip')}”")
    expect(page.locator("#mInp")).to_be_hidden()  # no title prompt: the project is named after the file


def _project_named(page, title):
    return next(p for p in page.request.get("/api/projects").json()["projects"] if p["title"] == title)


def test_zip_import_ticks_suggested_documents_and_allows_any_choice(page):
    register(page, "zip_dialog")
    source = _zip({"main.typ": '#include "chap/a.typ"\nHello', "chap/a.typ": "A", "notes.typ": "N", "img.png": _png()})
    _import(page, "Thesis.zip", source)
    checks = page.locator("#mCard .mCheck input")
    expect(checks).to_have_count(3)  # every .typ entry, nothing else
    ticked = {checks.nth(i).get_attribute("value"): checks.nth(i).is_checked() for i in range(3)}
    assert ticked == {"main.typ": True, "chap/a.typ": False, "notes.typ": True}  # suggested = roots nobody includes
    expect(page.locator("#mCard .mCheck", has_text="chap/a.typ")).to_contain_text("included by main.typ")
    expect(page.locator("#mCard .mNote")).to_have_text("1 other file is kept as a project file: img.png")
    page.locator("#mCard .mCheck", has_text="chap/a.typ").locator("input").check()
    page.click("#mYes")
    expect(page.locator("#title")).not_to_have_text("Welcome")
    project = _project_named(page, "Thesis")
    assert sorted((d["folder"], d["title"]) for d in project["documents"]) == [("", "main"), ("", "notes"), ("chap", "a")]
    assert sorted(f["path"] for f in project["files"]) == ["img.png"]

    _import(page, "Assets.zip", source)
    for i in range(3):
        checks.nth(i).uncheck()
    page.click("#mYes")
    expect(page.locator("aside .project-group").filter(has_text="Assets")).to_have_count(1)
    project = _project_named(page, "Assets")
    assert project["documents"] == []
    assert sorted(file_paths(page.request, project["id"])) == ["chap/a.typ", "img.png", "main.typ", "notes.typ"]


def test_a_zip_with_one_typ_file_still_asks_and_keeps_the_file_name(page):
    register(page, "zip_single")
    _import(page, "Letter.zip", _zip({"letter.typ": "Dear reader", "logo.png": _png()}))
    checks = page.locator("#mCard .mCheck input")
    expect(checks).to_have_count(1)
    expect(checks.first).to_be_checked()
    page.click("#mYes")
    expect(page.locator("#title")).to_have_text("letter")
    project = _project_named(page, "Letter")
    assert [d["title"] for d in project["documents"]] == ["letter"]
