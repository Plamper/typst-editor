"""Dragging one fitting sidebar file into the editor writes its Typst line where it is dropped."""
import re

from playwright.sync_api import expect

from e2e.helpers import file_paths, put_file, register


def _project(page):
    api = page.request
    pid = api.post("/api/projects", data={"title": "Drops"}).json()["id"]
    assert api.post(f"/api/projects/{pid}/docs", data={"title": "Main", "content": "First line\nSecond line"}).ok
    for name, data in (("pic.png", b"\x89PNG\r\n\x1a\n"), ("data.csv", "a,b\n1,2\n"), ("notes.zip", b"PK")):
        assert put_file(api, pid, name, data).ok
    page.reload()
    page.wait_for_function("window.__booted === true")
    page.locator("aside .doc.sub", has_text="Main").first.click()
    expect(page.locator("#cm .cm-content")).to_contain_text("Second line")
    return pid


def _row(page, name):
    return page.locator(f'aside [data-package-key="file::{name}"]')


def _drag(page, source, target, at):
    box = target.bounding_box()
    source.hover()
    page.mouse.down()
    page.mouse.move(box["x"] + at[0], box["y"] + at[1], steps=8)
    page.mouse.move(box["x"] + at[0] + 1, box["y"] + at[1], steps=2)


def _text(page):
    return page.locator("#cm .cm-content").inner_text()


def test_image_lands_at_the_pointer_and_data_on_its_own_line(page):
    register(page, "editor_drop")
    _project(page)
    line = page.locator("#cm .cm-line").first
    _drag(page, _row(page, "pic.png"), line, (40, 8))
    mark = page.locator(".editorDrop")
    expect(mark).to_be_visible()  # the drop spot and the line it writes show while dragging
    expect(mark).to_contain_text('#image("pic.png", width: 80%)')
    expect(page.locator("#editWrap")).to_have_class("editor-drop")
    page.mouse.up()
    expect(mark).to_have_count(0)
    text = _text(page)
    assert '#image("pic.png", width: 80%)' in text.splitlines()[0] and text.splitlines()[0] != '#image("pic.png", width: 80%)'

    second = page.locator("#cm .cm-line").nth(1)
    _drag(page, _row(page, "data.csv"), second, (60, 2))
    page.mouse.up()
    assert _text(page).splitlines()[1] == '#csv("data.csv")'  # a statement goes on a line of its own


def test_unfit_files_and_selections_are_refused(page):
    register(page, "editor_drop_refuse")
    _project(page)
    before = _text(page)
    line = page.locator("#cm .cm-line").first
    _drag(page, _row(page, "notes.zip"), line, (40, 8))
    expect(page.locator(".editorDrop")).to_have_count(0)
    page.mouse.up()
    _row(page, "pic.png").click(modifiers=["ControlOrMeta"])
    _row(page, "data.csv").click(modifiers=["ControlOrMeta"])
    expect(_row(page, "pic.png")).to_have_class(re.compile(r"\bsel\b"))
    _drag(page, _row(page, "pic.png"), line, (40, 8))
    expect(page.locator(".editorDrop")).to_have_count(0)  # several entries never go in at once
    page.mouse.up()
    assert _text(page) == before


def test_one_uploaded_image_lands_where_it_is_dropped(page):
    register(page, "editor_drop_upload")
    pid = _project(page)
    page.locator("#cm .cm-line").nth(1).evaluate("""line => {
        const r = line.getBoundingClientRect(), at = { clientX: r.left + 1, clientY: r.top + r.height / 2, bubbles: true, cancelable: true }
        const dataTransfer = new DataTransfer()
        dataTransfer.items.add(new File([new Uint8Array([137, 80, 78, 71])], 'shot.png', { type: 'image/png' }))
        line.dispatchEvent(new DragEvent('dragover', { ...at, dataTransfer }))
        line.dispatchEvent(new DragEvent('drop', { ...at, dataTransfer }))
    }""")
    expect(page.locator("#cm .cm-line").nth(1)).to_have_text('#image("shot.png", width: 80%)Second line')
    assert "shot.png" in file_paths(page.request, pid)
