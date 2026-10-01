#!/usr/bin/env python3
"""Opt-in real browser/backend walkthrough; requires a disposable repository branch.

SBX_UI_URL defaults to localhost:5174; SBX_BASE_URL and SBX_API_KEY select an
isolated deployment. SBX_E2E_REPO and SBX_E2E_REF are required. The branch must
start sbx-ui-; this script creates one Markdown file and a draft PR on that
branch, records a revision approval, and NEVER merges. Credentials are not logged.
Requires the installed agent-browser CLI; no Python packages are needed.
"""
import json
import os
import re
import subprocess
import time
import uuid
import urllib.request

UI = os.environ.get("SBX_UI_URL", "http://localhost:5174").rstrip("/")
BASE = os.environ["SBX_BASE_URL"].rstrip("/")
KEY = os.environ["SBX_API_KEY"]
REPO = os.environ["SBX_E2E_REPO"]
REF = os.environ["SBX_E2E_REF"]
if not REF.startswith("sbx-ui-") or "sbx-ui" not in BASE:
    raise SystemExit("Use an isolated sbx-ui deployment and a disposable sbx-ui- branch.")
SESSION = "sbx-live-walkthrough"


def browser(*args):
    r = subprocess.run(["agent-browser", "--session", SESSION, *args],
                       capture_output=True, text=True, timeout=45)
    if r.returncode:
        # Tool diagnostics can include evaluated arguments; never echo them.
        raise RuntimeError("Browser action failed; inspect the dedicated walkthrough tab.")
    return r.stdout.strip()


def evaluate(js):
    return json.loads(browser("eval", js))


def wait(predicate, seconds=180):
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        if evaluate(predicate):
            return
        time.sleep(1)
    raise RuntimeError("Browser state did not converge; inspect the dedicated walkthrough tab.")


def click(text):
    wait(f'Array.from(document.querySelectorAll("button")).some(b=>b.textContent.trim()==={json.dumps(text)}&&!b.disabled)')
    evaluate(f'Array.from(document.querySelectorAll("button")).find(b=>b.textContent.trim()==={json.dumps(text)}).click(); true')


def api(path):
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + KEY})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def checkpoint(label, started):
    print(f"{label}: {time.monotonic() - started:.1f}s", flush=True)


browser("open", UI)
evaluate(f'localStorage.setItem("sbx.console.token",{json.dumps(KEY)}); true')
browser("reload")
wait('!!document.querySelector("summary[title=\\\"Select repository\\\"]")')
filename = "sbx-ui-walkthrough-" + uuid.uuid4().hex[:8] + ".md"
prompt = (f"Create only {filename} with heading SBX integration walkthrough and one sentence "
          "This disposable file validates live Session delivery. Then run git diff --check. "
          "Do not modify other files, commit, push, or open a PR.")
evaluate('document.querySelector("summary[title=\\\"Select repository\\\"]").click();true')
browser("fill", 'input[aria-label="Repository"]', REPO)
click("Done")
evaluate('document.querySelector("summary[title=\\\"Session configuration\\\"]").click();document.querySelector(".advanced-config summary").click();true')
browser("fill", 'input[aria-label="Base branch"]', REF)
evaluate('document.querySelector("summary[title=\\\"Session configuration\\\"]").click();true')
browser("fill", 'textarea[aria-label="Session task"]', prompt)
wait('!document.querySelector("button[aria-label=\\\"Start session\\\"]").disabled')
started = time.monotonic()
evaluate('document.querySelector("button[aria-label=\\\"Start session\\\"]").click();true')
wait('location.pathname.startsWith("/sessions/sess_")')
session_id = evaluate('location.pathname.split("/").pop()')
print("Created", session_id, flush=True)
wait('document.querySelector(".session-header .status")?.textContent.includes("Idle")', 360)
checkpoint("Initial work completed", started)
assert api("/v2/sessions/" + session_id)["session"]["repository"]["ref"] == REF
# Force reload to exercise persisted history + real SSE replay, before a follow-up.
browser("reload")
wait('!!document.querySelector("textarea[aria-label=\\\"Follow-up message\\\"]")')
browser("fill", 'textarea[aria-label="Follow-up message"]', "Read-only: confirm the new Markdown filename. Do not modify files.")
started = time.monotonic()
evaluate('document.querySelector("button[aria-label=\\\"Send follow-up\\\"]").click();true')
wait('document.querySelectorAll(".user-message").length===2 && document.querySelector(".session-header .status")?.textContent.includes("Idle")', 240)
checkpoint("Follow-up completed", started)
assert api("/v2/sessions/" + session_id)["session"]["turns"] == 2
click("Changes •") if evaluate('Array.from(document.querySelectorAll("button")).some(b=>b.textContent.trim()==="Changes •")') else evaluate('Array.from(document.querySelectorAll(".pane-tabs button")).find(b=>b.textContent.includes("Changes")).click();true')
wait('!!document.querySelector("[aria-label^=\\\"Diff for\\\"]")')
summary = api("/v2/sessions/" + session_id + "/changes/diff")
assert len(summary["files"]) == 1 and summary["files"][0]["path"] == filename
click("Create draft PR") if evaluate('Array.from(document.querySelectorAll("button")).some(b=>b.textContent.trim()==="Create draft PR")') else click("Create pull request")
wait('!!document.querySelector("dialog[open]") && !document.querySelector("dialog button.primary").disabled')
assert evaluate('Array.from(document.querySelectorAll("dialog input")).find(i=>i.type!=="checkbox"&&i.value===' + json.dumps(REF) + ')!==undefined')
started = time.monotonic()
evaluate('document.querySelector("dialog form").requestSubmit();true')
wait('!document.querySelector("dialog[open]") && !!document.querySelector(".pr-outcome")', 180)
checkpoint("Draft PR delivered", started)
delivery = api("/v2/sessions/" + session_id)["session"]["delivery"]["pull_request"]
assert delivery["base"] == REF and delivery["draft"] is True
print("Disposable draft PR", delivery["number"], flush=True)
click("Record approval")
wait('document.querySelector(".review-panel")?.textContent.includes("An approval is recorded")')
browser("open", UI + "/integrations")
wait('!!document.querySelector(".account-row") && !!document.querySelector(".github-card")')
print("Connections loaded; walkthrough passed (no merge).", flush=True)
