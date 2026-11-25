"""Ctrl/Shift select several sidebar entries across projects and act on all of them."""
from helpers import file_paths, put_file, register
from playwright.sync_api import expect


def _projects(page):
    api = page.request
    a = api.post("/api/projects", data={"title": "Alpha"}).json()["id"]
    b = api.post("/api/projects", data={"title": "Beta"}).json()["id"]
    for pid in (a, b):  # a file opens next to a document of its project
        assert api.post(f"/api/projects/{pid}/docs", data={"title": "Main", "content": ""}).ok
    for pid, name in ((a, "a1.typ"), (a, "a2.typ"), (a, "a3.typ"), (b, "b1.typ")):
        assert put_file(api, pid, name, "= " + name).ok
    page.reload()
    page.wait_for_function("window.__booted === true")
    return a, b


def _row(page, title, name):
    group = page.locator("aside .project-group").filter(has=page.locator(".fhd.project b", has_text=title)).first
    return group.locator(f'[data-package-key="file::{name}"]')


def test_ctrl_and_shift_click_select_across_projects_and_trash_together(page):
    register(page, "e2e_select_trash")
    a, b = _projects(page)
    _row(page, "Alpha", "a1.typ").click()
    _row(page, "Alpha", "a3.typ").click(modifiers=["Shift"])
    _row(page, "Beta", "b1.typ").click(modifiers=["ControlOrMeta"])
    _row(page, "Alpha", "a2.typ").click(modifiers=["ControlOrMeta"])
    expect(page.locator("aside .sel")).to_have_count(3)
    expect(_row(page, "Alpha", "a2.typ")).not_to_have_class("sel")

    _row(page, "Beta", "b1.typ").click(button="right")
    menu = page.locator("#menu")
    expect(menu).to_contain_text("3 selected")
    menu.locator("button.danger").click()
    expect(page.locator("#mHead")).to_contain_text("3 entries")
    page.click("#mYes")
    expect(_row(page, "Beta", "b1.typ")).to_have_count(0)
    assert file_paths(page.request, a) == ["a2.typ"]
    assert file_paths(page.request, b) == []


def test_shift_arrows_extend_and_a_drag_moves_the_whole_selection(page):
    register(page, "e2e_select_drag")
    a, b = _projects(page)
    first = _row(page, "Alpha", "a1.typ")
    first.focus()
    page.keyboard.press("Shift+ArrowDown")
    page.keyboard.press("Shift+ArrowDown")
    expect(page.locator("aside .sel")).to_have_count(3)
    page.keyboard.press("Shift+ArrowUp")
    expect(page.locator("aside .sel")).to_have_count(2)
    page.keyboard.press("ControlOrMeta+Space")
    expect(page.locator("aside .sel")).to_have_count(1)
    page.keyboard.press("Escape")
    expect(page.locator("aside .sel")).to_have_count(0)

    first.click(modifiers=["ControlOrMeta"])
    _row(page, "Alpha", "a2.typ").click(modifiers=["ControlOrMeta"])
    expect(page.locator("aside .sel")).to_have_count(2)
    beta = page.locator("aside .project-group .fhd.project").filter(has_text="Beta").first
    _row(page, "Alpha", "a2.typ").drag_to(beta)
    expect(_row(page, "Beta", "a1.typ")).to_have_count(1)
    expect(_row(page, "Beta", "a2.typ")).to_have_count(1)
    assert file_paths(page.request, a) == ["a3.typ"]
    assert sorted(file_paths(page.request, b)) == ["a1.typ", "a2.typ", "b1.typ"]
