"""Project file create/rename flows use server names and list the references that follow."""
from helpers import create_doc, register
from playwright.sync_api import expect


def test_file_server_name_and_rename_preview(page):
    register(page, 'project_module_ui')
    create_doc(page, 'Workspace')
    head = page.locator('aside .project-group').filter(has_text='Workspace').locator('.fhd.project')
    head.hover()
    head.locator('button.mini').first.click()
    page.locator('#menu button').filter(has_text='New file').click()
    page.locator('#submenu button').filter(has_text='.typ').first.click()
    page.fill('#mInp', 'Änderungen')
    page.click('#mYes')
    # File names use the server's ASCII normalization; the UI must open that returned name.
    tab = page.locator('#tabs button.tab').filter(has_text='_nderungen.typ')
    expect(tab).to_have_attribute('aria-selected', 'true')
    tab.dblclick()
    page.fill('#mInp', 'renamed.typ')
    page.click('#mYes')
    expect(page.locator('#modal')).to_contain_text('No static references found')
    expect(page.locator('#modal')).to_contain_text('updated to the new path')
    page.click('#mYes')
    expect(page.locator('#tabs button.tab').filter(has_text='renamed.typ')).to_be_visible()
    expect(page.locator('#tabs button.tab').filter(has_text='_nderungen.typ')).to_have_count(0)
