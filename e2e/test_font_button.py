"""The toolbar font button is one source-only slider, not a pair of size steps."""
import base64

from playwright.sync_api import expect

from e2e.helpers import create_doc, doc_project, put_file, register

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lY8AAAAASUVORK5CYII=')


def test_font_button_opens_slider(page):
    register(page, 'font_button_slider')
    create_doc(page, 'Font button')
    expect(page.locator('#fMinus')).to_have_count(0)
    expect(page.locator('#fPlus')).to_have_count(0)
    expect(page.locator('#fontBtn')).to_be_visible()
    expect(page.locator('#fontBtn')).to_have_attribute('aria-label', 'Editor font size')
    page.locator('#fontBtn').click()
    button = page.locator('#fontBtn').bounding_box()
    popup = page.locator('#menu').bounding_box()
    assert abs((button['x'] + button['width'] / 2) - (popup['x'] + popup['width'] / 2)) <= 2
    assert popup['y'] >= button['y'] + button['height']
    slider = page.locator('#menu .mRange input[type=range]')
    expect(slider).to_have_attribute('aria-label', 'Editor font size')
    expect(slider).to_have_value('14')
    slider.focus()
    page.keyboard.press('ArrowRight')
    expect(slider).to_have_value('15')
    assert page.evaluate("getComputedStyle(document.getElementById('cmWrap')).getPropertyValue('--ed').trim()") == '15px'
    assert page.evaluate("localStorage.getItem('typst_ed')") == '15'
    page.keyboard.press('Escape')
    expect(page.locator('#menu')).to_be_hidden()


def test_font_button_shows_only_in_source_views(page):
    register(page, 'font_button_sources')
    create_doc(page, 'Font modes')
    project = doc_project(page.request, next(d for d in page.request.get('/api/docs').json()['own'] if d['title'] == 'Font modes')['id'])
    assert put_file(page.request, project, 'sources.bib', '@book{key, title={Writing}, year={2026}}\n').ok
    assert put_file(page.request, project, 'data.csv', 'a,b\n1,2\n').ok
    assert put_file(page.request, project, 'blob.bin', 'binary\n').ok
    assert put_file(page.request, project, 'picture.png', PNG).ok

    def open_file(name):
        page.locator('#own .doc .t').filter(has_text=name).click()
        expect(page.locator('#title')).to_have_text(name)

    open_file('sources.bib')
    expect(page.locator('#fontBtn')).to_be_hidden()
    page.locator('#tools button[aria-label="Source"]:visible').click()
    expect(page.locator('#fontBtn')).to_be_visible()
    page.locator('#tools button[aria-label="Entries"]').click()
    expect(page.locator('#fontBtn')).to_be_hidden()

    open_file('data.csv')
    expect(page.locator('#fontBtn')).to_be_hidden()
    page.locator('#tools button[aria-label="Source"]:visible').click()
    expect(page.locator('#fontBtn')).to_be_visible()
    page.locator('#tools button[aria-label="Cells"]').click()
    expect(page.locator('#fontBtn')).to_be_hidden()

    open_file('blob.bin')
    expect(page.locator('#fontBtn')).to_be_visible()

    open_file('picture.png')
    expect(page.locator('#fontBtn')).to_be_hidden()
