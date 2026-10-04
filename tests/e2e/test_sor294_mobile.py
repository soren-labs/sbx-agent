"""REV-006/007 real layout and touch/keyboard regressions without credentials."""

import pytest
from tests.e2e.hosted_fixture import hosted_browser
from tests.unit.test_hosted_workflow import workflow_app as workflow_app


@pytest.mark.parametrize("width,interaction", [(390, "keyboard"), (320, "touch")])
def test_auth_redirect_and_mobile_api_key_form(workflow_app, width, interaction):
    app, user, _, _ = workflow_app
    token = app.state.auth_store.create_session(user.id)[1]
    with hosted_browser(app, token) as (initial, base, playwright):
        context = initial.context.browser.new_context(
            viewport={"width": width, "height": 844},
            has_touch=True,
            storage_state=initial.context.storage_state(),
        )
        page = context.new_page()
        page.goto(base + "/auth")
        playwright.expect(page.get_by_role("textbox", name="Session task")).to_be_visible()
        assert page.url == base + "/"
        page.goto(base + "/settings")
        name = page.get_by_label("Key name")
        expiry = page.get_by_label("Key expiry")
        submit = page.get_by_role("button", name="Create API key")
        for control in (name, expiry, submit):
            playwright.expect(control).to_be_visible()
            box = control.bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= width
            assert box["height"] >= 44
        assert page.locator(".api-key-form").evaluate("e=>e.scrollWidth<=e.clientWidth")
        name.fill(f"mobile-{width}")
        if interaction == "keyboard":
            name.press("Tab")
            playwright.expect(expiry).to_be_focused()
            expiry.select_option("30")
            expiry.press("Tab")
            playwright.expect(submit).to_be_focused()
            submit.press("Enter")
        else:
            expiry.select_option("30")
            submit.tap()
        playwright.expect(page.get_by_label("New API key")).to_be_visible()
        page.get_by_role("button", name="Dismiss key").click()
        page.get_by_role("button", name=f"Revoke mobile-{width}").click()
        playwright.expect(
            page.get_by_role("button", name=f"Revoke mobile-{width}")
        ).to_be_disabled()
        context.close()
