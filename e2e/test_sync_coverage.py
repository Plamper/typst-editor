"""Klick-Sync deckt die häufigen Typst-Konstrukte ab: Fußnoten, Inline-Markup,
Mathe, Listen, Spalten, Blöcke, Kopf/Fuß und rotierter Text.

Die Brüche sind gegen das gepinnte Typst-0.15.x-Layout gemessen. Ein Wechsel
der Toolchain soll hier laut scheitern, nicht still falsch klicken.
"""
import re

from helpers import create_doc, register
from playwright.sync_api import expect

PREVIEW_TIMEOUT = 60_000

NOTES = """= Notes

Body line with a note.#footnote[Footnote text lives here.]

Body line after the note.
"""


def _fill(page, source):
    page.click("#cm .cm-content")
    page.keyboard.press("ControlOrMeta+A")
    assert page.evaluate("(t) => document.execCommand('insertText', false, t)", source)


def _ready(page):
    expect(page.locator("#preview .pvPage[data-done] canvas").first).to_be_visible(timeout=PREVIEW_TIMEOUT)
    expect(page.locator("#preview .pvPage")).to_have_count(1, timeout=PREVIEW_TIMEOUT)
    page.wait_for_timeout(700)


def _open(page, slug, title, source):
    register(page, slug)
    create_doc(page, title)
    _fill(page, source)
    _ready(page)


def _settle(page):
    page.wait_for_function(
        """() => new Promise(done => {
          const pv = document.getElementById('preview')
          let last = pv.scrollTop, calm = 0
          const tick = () => {
            if (Math.abs(pv.scrollTop - last) < 0.5) calm++
            else calm = 0
            last = pv.scrollTop
            if (calm >= 3) done(true)
            else setTimeout(tick, 80)
          }
          tick()
        })""",
        timeout=10_000,
    )


def _click(page, fx, fy):
    _settle(page)  # a link jump or follow scroll must stop before the box is measured
    slot = page.locator('#preview .pvPage[data-page="1"]')
    slot.scroll_into_view_if_needed()
    box = slot.locator("canvas").bounding_box()  # re-read: an earlier jump may have scrolled the preview
    assert box
    page.mouse.click(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy)
    page.wait_for_timeout(450)


def _pos(page):
    m = re.match(r"Ln (\d+):(\d+)", page.locator("#pos").inner_text())
    assert m, "status line has no line:column"
    return int(m.group(1)), int(m.group(2))


def test_footnote_reference_text_and_marker_all_point_at_the_note(page):
    _open(page, "e2e_cover_notes", "Cover notes", NOTES)

    _click(page, 0.2021, 0.1078)
    line, col = _pos(page)
    assert line == 3 and 10 <= col <= 30, f"body before the note: Ln {line}:{col}"

    _click(page, 0.2880, 0.1078)  # the generated superscript
    line, col = _pos(page)
    assert line == 3, f"superscript reference: Ln {line}:{col}"

    _click(page, 0.2099, 0.1321)
    line, col = _pos(page)
    assert line == 5, f"line after the note: Ln {line}:{col}"

    _click(page, 0.2174, 0.9103)  # the note body at the page bottom
    line, col = _pos(page)
    assert line == 3 and 33 <= col <= 58, f"footnote text: Ln {line}:{col}"

    _click(page, 0.1372, 0.9103)  # the generated marker before the note body
    line, col = _pos(page)
    assert line == 3, f"footnote marker: Ln {line}:{col}"
