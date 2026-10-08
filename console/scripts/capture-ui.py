"""Real Chromium evidence against Vite; /api fixtures exist only in this browser.

Run: uv run --with playwright==1.63.0 python console/scripts/capture-ui.py
Start the current Console separately with `make console-dev`.
No product mock mode, cloud account, or backend credentials are used.
"""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse

os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH", "/home/zheng/.local/state/sbx-review-163/browsers"
)

from playwright.sync_api import expect, sync_playwright

OUTPUT = Path(__file__).resolve().parents[1] / "docs/ui-restoration"
BASE = "http://127.0.0.1:5174"
ME = {
    "user": {"id": "u1", "email": "alex@example.com", "email_verified": True},
    "workspaces": [{"id": "w1", "name": "Personal workspace", "kind": "personal"}],
    "auth": {"via": "cookie"},
}
SESSION = {
    "id": "s1",
    "lifecycle": "open",
    "activity": "idle",
    "role": "developer",
    "title": "Improve the onboarding experience",
    "harness": {"provider_id": "opencode", "model": "big-pickle"},
    "executor": {"backend": "modal", "lease_id": "l1", "lease_state": "ready"},
    "worktree": {"availability": "live", "generation": 1, "base_sha": "abc"},
    "parent_session_id": None,
    "actions": ["send", "archive", "close"],
    "version": 1,
}
CONNECTIONS = [
    {
        "id": f"c_{kind}",
        "kind": kind,
        "label": label,
        "state": "configured",
        "health": "ready",
        "health_reason": None,
        "external_identity": "example-workspace" if kind == "github" else None,
        "version": 1,
        "credential": None,
        "validation": None,
        "catalog": None,
    }
    for kind, label in [
        ("modal", "My Modal workspace"),
        ("github", "GitHub repositories"),
        ("opencode_zen", "OpenCode Zen"),
    ]
]
PROJECT = {
    "id": "p1",
    "workspace_id": "w1",
    "slug": "workspace",
    "name": "Workspace application",
    "version": 1,
    "created_at": "2026-10-08T10:00:00Z",
    "updated_at": "2026-10-08T10:00:00Z",
    "current_version": {
        "id": "pv1",
        "project_id": "p1",
        "ordinal": 1,
        "spec": {"repository": {"full_name": "example/workspace", "base_ref": "main"}},
        "spec_digest": "example",
        "created_at": "2026-10-08T10:00:00Z",
    },
}


def message(mid, ordinal, role, text):
    return {
        "id": mid,
        "ordinal": ordinal,
        "role": role,
        "content": [{"kind": "text", "text": text}],
        "turn_id": "t1",
        "state": "accepted" if role == "user" else "completed",
        "parts": [],
    }


def run():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    unexpected = []
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1540, "height": 960},
            color_scheme="dark",
            locale="en-US",
            record_video_dir=str(OUTPUT / "video"),
            record_video_size={"width": 1540, "height": 960},
        )
        authenticated = False

        def fixture(route):
            nonlocal authenticated
            request = route.request
            path = urlparse(request.url).path
            status = 200
            if path == "/api/me":
                status = 200 if authenticated else 401
                data = ME if authenticated else {"error": {"code": "unauthenticated"}}
            elif path == "/api/auth/login":
                assert request.post_data_json == {
                    "email": "alex@example.com",
                    "password": "REDACTED",
                }
                authenticated = True
                data = {**ME, "csrf_token": "REDACTED", "expires_at": "2099-01-01"}
            elif path == "/api/auth/register":
                data = {"status": "pending"}
            elif path == "/api/auth/email-verifications":
                data = {"status": "verified"}
            elif path == "/api/workspaces/w1/connections":
                data = {"items": CONNECTIONS}
            elif path == "/api/workspaces/w1/projects":
                data = {"items": [PROJECT]}
            elif path == "/api/models":
                data = {
                    "provider_id": "opencode",
                    "preferred_model": "big-pickle",
                    "connections": [
                        {
                            **CONNECTIONS[2],
                            "connection_id": "c_opencode_zen",
                            "models": [{"id": "big-pickle", "free": True}],
                        }
                    ],
                }
            elif path == "/api/workspaces/w1/sessions":
                data = {
                    "items": [
                        SESSION,
                        {
                            **SESSION,
                            "id": "s2",
                            "title": "Add keyboard shortcuts",
                            "activity": "running",
                        },
                    ],
                    "next_cursor": None,
                }
            elif path == "/api/sessions/s1":
                data = {"session": SESSION, "event_watermark": 0}
            elif path == "/api/sessions/s1/messages":
                data = {
                    "event_watermark": 0,
                    "items": [
                        message(
                            "m1",
                            1,
                            "user",
                            "Make the onboarding clearer on mobile. Keep the existing authentication and workspace flows.",
                        ),
                        message(
                            "m2",
                            2,
                            "assistant",
                            "I've updated the onboarding layout and checked the mobile breakpoints.\n\n### Ready for review\n- Simplified the page hierarchy and improved input spacing.\n- Preserved the existing sign-in and verification flow.\n- Checked keyboard navigation and responsive layouts.\n\nThe changes are ready for your review.",
                        ),
                    ],
                }
            elif path == "/api/sessions/s1/turns":
                data = {
                    "items": [
                        {
                            "id": "t1",
                            "ordinal": 1,
                            "state": "succeeded",
                            "reason": None,
                            "retry_of_turn_id": None,
                            "error": None,
                            "outcome": None,
                            "actions": [],
                            "version": 1,
                        }
                    ]
                }
            elif path == "/api/sessions/s1/executor":
                data = {
                    "backend": "modal",
                    "leases": [],
                    "worktree": SESSION["worktree"],
                    "recovery_point": {
                        "snapshot_id": "snap1",
                        "generation": 1,
                        "created_at": "2026-10-08T10:00:00Z",
                    },
                }
            elif path == "/api/sessions/s1/events":
                if request.headers.get("accept") == "text/event-stream":
                    route.fulfill(content_type="text/event-stream", body=": evidence heartbeat\n\n")
                    return
                data = {"items": [], "next_after": 0, "event_watermark": 0}
            elif path == "/api/api-keys":
                data = {"items": []}
            else:
                unexpected.append(f"{request.method} {path}")
                status, data = 404, {"error": {"code": "not_found"}}
            route.fulfill(status=status, content_type="application/json", body=json.dumps(data))

        context.route(BASE + "/api/**", fixture)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def capture(name):
            page.evaluate("document.fonts.ready")
            page.evaluate("window.scrollTo(0, 0)")
            page.evaluate("document.querySelector('#workspace-main')?.scrollTo(0, 0)")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
            page.screenshot(
                path=str(OUTPUT / name),
                full_page=page.viewport_size["width"] > 760,
                animations="disabled",
            )

        page.goto(BASE + "/login")
        expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()
        capture("01-login-desktop.png")
        page.set_viewport_size({"width": 390, "height": 844})
        capture("02-login-mobile.png")
        page.get_by_role("link", name="Create account").click()
        expect(page.get_by_role("heading", name="Create account", exact=True)).to_be_visible()
        capture("03-register-mobile.png")
        page.goto(BASE + "/verify-email?token=REDACTED")
        expect(page.get_by_role("status")).to_contain_text("verified")
        capture("04-verify-mobile.png")
        page.goto(BASE + "/login")
        page.set_viewport_size({"width": 1540, "height": 960})
        page.get_by_label("Email", exact=True).fill("alex@example.com")
        page.get_by_label("Password", exact=True).fill("REDACTED")
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.locator(".home-hero h1")).to_be_visible()
        expect(page.get_by_role("link", name=SESSION["title"]).first).to_be_visible()
        capture("05-home-desktop.png")
        page.locator(".side-nav").get_by_role("link", name="Sessions", exact=True).click()
        capture("06-sessions-desktop.png")
        page.get_by_role("link", name=SESSION["title"]).first.click()
        expect(page.get_by_role("log")).to_contain_text("Ready for review")
        capture("07-session-desktop.png")
        page.get_by_role("button", name="Activity", exact=True).click()
        expect(page).to_have_url(BASE + "/sessions/s1/activity")
        page.get_by_role("button", name="Conversation", exact=True).click()
        page.set_viewport_size({"width": 390, "height": 844})
        capture("08-session-mobile.png")
        page.locator(".bottom-nav").get_by_role("link", name="New Session", exact=True).click()
        capture("09-home-mobile.png")
        page.locator(".session-options > summary").click()
        expect(page.get_by_label("Model", exact=True)).to_be_visible()
        page.get_by_label("Repository", exact=True).fill("example/workspace")
        page.locator(".session-options > summary").click()
        page.locator(".bottom-nav").get_by_role("link", name="Connections", exact=True).click()
        expect(page.get_by_role("group", name="My Modal workspace")).to_be_visible()
        capture("10-connections-mobile.png")
        page.set_viewport_size({"width": 1540, "height": 960})
        capture("11-connections-desktop.png")
        page.goto(BASE + "/projects")
        expect(page.get_by_text("Workspace application", exact=True)).to_be_visible()
        capture("12-projects-desktop.png")
        page.locator(".side-nav").get_by_role("link", name="Settings", exact=True).click()
        capture("13-settings-desktop.png")
        page.get_by_label("Theme", exact=True).select_option("light")
        expect(page.locator("html")).to_have_attribute("data-theme", "light")
        page.get_by_label("Theme", exact=True).select_option("dark")
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        page.reload()
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        capture("14-settings-dark.png")
        page.locator(".new-session-button").click()
        capture("15-home-dark.png")
        for width in [320, 768, 899, 900, 1024]:
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
        page.set_viewport_size({"width": 320, "height": 844})
        for route in [
            "/login",
            "/register",
            "/verify-email",
            "/projects",
            "/connections",
            "/settings",
        ]:
            page.goto(BASE + route)
            expect(page.locator("h1")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), route
        assert not unexpected, unexpected
        assert not errors, errors
        video = page.video
        context.close()
        video.save_as(str(OUTPUT / "walkthrough.webm"))
        video.delete()
        browser.close()
    print("Browser walkthrough passed; screenshots and video saved to", OUTPUT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=BASE, help="URL of this checkout’s Vite or preview server")
    BASE = parser.parse_args().url.rstrip("/")
    run()
