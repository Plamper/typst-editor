"""Conversion in the sidebar menu: a document becomes a .typ file and a .typ file a document; nothing else converts."""
from helpers import file_paths, put_file, register
from playwright.sync_api import expect


def _menu(page, key, text):
    row = page.locator(f'[data-package-key^="{key}"]', has_text=text).first
    row.hover()
    row.locator("button").last.click()


def test_only_typ_files_offer_the_document_conversion(page, base_url):
    register(page, "e2e_convert")
    api = page.context.request
    pid = api.post(f"{base_url}/api/projects", data={"title": "Convert"}).json()["id"]
    api.post(f"{base_url}/api/projects/{pid}/docs", data={"title": "Main", "content": '#include "Chapters/part.typ"'})
    assert put_file(api, pid, "Chapters/part.typ", "Part").ok
    assert put_file(api, pid, "data.csv", "a,b").ok
    page.reload()
    page.wait_for_function("window.__booted === true")
    _menu(page, "file::", "data.csv")
    expect(page.locator("#menu")).not_to_contain_text("Convert")
    page.keyboard.press("Escape")
    folder = page.locator("aside .fhd.folder", has_text="Chapters").locator("b")
    if folder.get_attribute("aria-expanded") == "false":
        folder.click()
    _menu(page, "file::", "part.typ")
    menu = page.locator("#menu")
    expect(menu.locator("button", has_text="Convert to document")).to_have_count(1)
    expect(menu).not_to_contain_text("module")
    menu.locator("button", has_text="Convert to document").click()
    expect(page.locator("#mCard")).to_contain_text("Main")  # Main still includes it: documents cannot be included
    page.click("#mYes")
    expect(page.locator("#title")).to_have_text("part")
    assert "Chapters/part.typ" not in file_paths(api, pid)
    docs = api.get(f"{base_url}/api/projects/{pid}").json()["documents"]
    assert {(d["title"], d["folder"]) for d in docs} == {("Main", ""), ("part", "Chapters")}


def test_a_document_converts_to_a_typ_file_in_its_folder(page, base_url):
    register(page, "e2e_convert2")
    api = page.context.request
    pid = api.post(f"{base_url}/api/projects", data={"title": "Convert2"}).json()["id"]
    api.post(f"{base_url}/api/projects/{pid}/docs", data={"title": "Main", "content": '#import "Style.typ": x\n#x'})
    api.post(f"{base_url}/api/projects/{pid}/docs", data={"title": "Style", "content": "#let x = 1"})
    page.reload()
    page.wait_for_function("window.__booted === true")
    _menu(page, "document:", "Style")
    menu = page.locator("#menu")
    expect(menu.locator("button", has_text="Convert to .typ file")).to_have_count(1)
    expect(menu).not_to_contain_text("module")
    menu.locator("button", has_text="Convert to .typ file").click()
    expect(page.locator('[data-package-key="file::Style.typ"]')).to_be_visible()
    assert "Style.typ" in file_paths(api, pid)
    assert [d["title"] for d in api.get(f"{base_url}/api/projects/{pid}").json()["documents"]] == ["Main"]
    _menu(page, "file::", "Style.typ")
    expect(page.locator("#menu button", has_text="Convert to document")).to_have_count(1)
