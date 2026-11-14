"""Smoke the actual App and Chat with synthetic local API responses.

Run from the repository root: python ergonaut/frontend/md-reader-harness/check_app.py
Requires frontend dependencies, Python Playwright and Chromium. Optionally set
ERGONAUT_TEST_CHROMIUM to override the browser path.
"""

import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from check import FRONTEND, PORT, ROOT, SOURCE
from playwright.sync_api import sync_playwright

ORIGIN = f"http://127.0.0.1:{PORT}"
OUTPUT = os.environ.get("MD_READER_APP_OUTPUT")
OUTPUT = ROOT / "docs" / "md-reader-desktop" / "after" if OUTPUT is None else Path(OUTPUT)
COLLISION = {
    "existing": "preserved",
    "__ergonautMarkdownReader": {
        "kind": "ergonaut-markdown-reader",
        "owner": "another-feature",
        "token": "not-registered",
        "id": "keep",
        "label": "keep",
        "original": {"must": "not replace the rest of the state"},
    },
}
VISUAL_SOURCE = SOURCE.split("\n[Project website]")[0]
SESSION = {
    "id": "fixture",
    "bot": "release-helper",
    "title": "Release planning",
    "role": "thread",
    "status": "active",
    "parent_id": None,
    "username": "demo",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}
MESSAGES = [
    {
        "line": i + 1,
        "role": "assistant" if i % 2 else "user",
        "timestamp": None,
        "blocks": (
            [
                {"type": "text", "text": "Please review the attached release plan."},
                {
                    "type": "attachment",
                    "id": "fixture-markdown",
                    "label": "release-plan.md",
                    "media_type": "text/markdown",
                },
                {
                    "type": "attachment",
                    "id": "fixture-markdown",
                    "label": "release-plan.md",
                    "media_type": "text/markdown",
                },
            ]
            if i == 8
            else [{"type": "text", "text": f"Conversation message {i + 1}: discussing the release plan."}]
        ),
    }
    for i in range(30)
]


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    server = subprocess.Popen(
        ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(PORT), "--strictPort"],
        cwd=FRONTEND,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        for _ in range(100):
            if server.poll() is not None:
                raise RuntimeError("Vite exited before becoming ready")
            try:
                with urllib.request.urlopen(ORIGIN, timeout=1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Vite did not start")
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, executable_path=os.environ.get("ERGONAUT_TEST_CHROMIUM"), args=["--no-sandbox"]
            )
            try:
                for name, width, height in [("desktop", 1440, 900), ("wide", 1920, 1080), ("mobile", 390, 844)]:
                    context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
                    page = context.new_page()
                    unexpected = []
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    responses = {
                        "/api/auth/csrf": {"csrftoken": "fixture"},
                        "/api/auth/me": {
                            "id": "demo",
                            "username": "demo",
                            "email": "",
                            "first_name": "",
                            "last_name": "",
                        },
                        "/api/bots": [],
                        "/api/sessions": [SESSION],
                        "/api/pins": {},
                        "/api/bot-errors": [],
                        "/api/workers": [],
                        "/api/realtime-voice/config": {"enabled": False},
                        "/api/realtime-voice/sessions": [],
                        "/api/sessions/fixture": {
                            "session": SESSION,
                            "messages": MESSAGES,
                            "calls": [],
                            "debug_shortcut": False,
                        },
                        "/api/sessions/fixture/attachments": [],
                        "/api/sessions/fixture/pins": [],
                        "/api/sessions/fixture/voice": {"enabled": False, "max_duration": 0},
                        "/api/sessions/fixture/usage": {
                            "until": "",
                            "hours": [],
                            "thread_tokens": 0,
                            "agent_tokens": 0,
                        },
                        "/api/bots/release-helper/models": {"models": [], "default": "", "default_engine_type": ""},
                        "/api/version": {"available": False, "latest": None},
                        "/api/routing": {"capacity": {"accounts": []}, "providers": [], "tiers": [], "agents": []},
                    }

                    def route_request(route):
                        path = urlsplit(route.request.url).path
                        if path == "/api/attachments/fixture-markdown/download":
                            route.fulfill(status=200, body=VISUAL_SOURCE, content_type="text/markdown; charset=utf-8")
                        elif path in responses:
                            route.fulfill(status=200, body=json.dumps(responses[path]), content_type="application/json")
                        elif path.endswith("/events"):
                            route.abort()
                        elif path.startswith("/api/") or not route.request.url.startswith(ORIGIN + "/"):
                            unexpected.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    page.route("**/*", route_request)
                    page.goto(ORIGIN + "/", wait_until="networkidle")
                    page.goto(ORIGIN + "/s/fixture?fixture=1#host-fragment", wait_until="networkidle")
                    page.evaluate("(state) => history.replaceState({...history.state, usr: state}, '')", COLLISION)
                    # Reload so BrowserRouter reads the fixture location state.
                    page.reload(wait_until="networkidle")
                    assert page.evaluate("history.state.usr") == COLLISION
                    opener = page.get_by_role("button", name="Read release-plan.md").first
                    opener.wait_for()
                    composer = page.get_by_role("textbox", name="Message")
                    composer.fill("Unsent draft: verify the release.")
                    opener.scroll_into_view_if_needed()
                    scroll = page.locator(".chat-transcript")
                    before = scroll.evaluate("el => el.scrollTop")
                    opener.focus()
                    page.keyboard.press("Enter")
                    dialog = page.get_by_role("dialog", name="release-plan.md")
                    dialog.get_by_role("heading", name="Release plan", exact=True).wait_for()
                    geometry = dialog.evaluate("""el => {
                      const rect = el.getBoundingClientRect(); const prose = el.querySelector('.markdown').getBoundingClientRect();
                      const content = el.querySelector('.md-reader-content'); const style = getComputedStyle(content);
                      return {x: rect.x, y: rect.y, width: rect.width, height: rect.height, proseWidth: prose.width,
                        proseLeft: prose.left, padding: style.padding, radius: getComputedStyle(el).borderRadius,
                        scrollable: content.scrollHeight > content.clientHeight};
                    }""")
                    if width > 600:
                        assert page.evaluate("history.state.usr.existing") == "preserved"
                        assert (
                            page.evaluate("history.state.usr.__ergonautMarkdownReader")
                            == COLLISION["__ergonautMarkdownReader"]
                        )
                    if width > 600:
                        assert (geometry["x"], geometry["y"], geometry["width"], geometry["height"]) == (
                            0,
                            0,
                            width,
                            height,
                        ), geometry
                        assert (
                            geometry["proseWidth"] <= 832
                            and abs(geometry["proseLeft"] - (width - geometry["proseWidth"]) / 2) < 1
                        ), geometry
                    else:
                        assert geometry["width"] < width and geometry["height"] < height, geometry
                    assert geometry["scrollable"], geometry
                    page.screenshot(path=str(OUTPUT / f"{name}.png"))
                    assert dialog.locator("pre code").count() == 1
                    assert dialog.locator(".md-reader-content").evaluate(
                        "el => { el.scrollTop = el.scrollHeight; return el.scrollTop > 0 }"
                    )
                    page.screenshot(path=str(OUTPUT / f"{name}-scrolled.png"))
                    page.keyboard.press("Escape")
                    dialog.wait_for(state="detached")
                    assert opener.evaluate("el => el === document.activeElement")
                    assert scroll.evaluate("el => el.scrollTop") == before
                    assert composer.input_value() == "Unsent draft: verify the release."
                    assert page.url.endswith("/s/fixture?fixture=1#host-fragment")
                    if width > 600:
                        assert page.evaluate("history.state.usr") == COLLISION
                        page.go_forward()
                        dialog.wait_for()
                        page.go_back()
                        dialog.wait_for(state="detached")
                        assert composer.input_value() == "Unsent draft: verify the release."
                        second = page.get_by_role("button", name="Read release-plan.md").nth(1)
                        second.click()
                        dialog.wait_for()
                        dialog.get_by_role("button", name="Close reader").click()
                        dialog.wait_for(state="detached")
                        assert second.evaluate("el => el === document.activeElement")
                        page.go_back()
                        page.wait_for_url(ORIGIN + "/")
                        page.go_forward()
                        page.wait_for_url("**/s/fixture?fixture=1#host-fragment")
                        second = page.get_by_role("button", name="Read release-plan.md").nth(1)
                        second.click()
                        dialog.wait_for()
                        # Navigate through an actual React Router link while the dialog is open.
                        page.locator("a.topbar-brand").evaluate("el => el.click()")
                        page.wait_for_url(ORIGIN + "/")
                        dialog.wait_for(state="detached")
                        page.go_back()
                        dialog.wait_for()
                        dialog.get_by_role("button", name="Close reader").click()
                        dialog.wait_for(state="detached")
                        page.wait_for_function("document.activeElement === document.querySelector('main')")
                        # Voice route remains an ordinary Router navigation.
                        page.get_by_role("button", name="Options").click()
                        page.get_by_role("link", name="Voice call").click()
                        page.wait_for_url("**/s/fixture/voice")
                        page.go_back()
                        page.wait_for_url("**/s/fixture?fixture=1#host-fragment")
                        second = page.get_by_role("button", name="Read release-plan.md").nth(1)
                        second.click()
                        dialog.wait_for()
                        page.reload(wait_until="networkidle")
                        dialog.wait_for(state="detached")
                        assert page.evaluate("history.state.usr") == COLLISION
                        assert page.url.endswith("/s/fixture?fixture=1#host-fragment")
                        # A non-object state must survive both opening and closing.
                        page.evaluate("history.replaceState({...history.state, usr: 'opaque state'}, '')")
                        page.reload(wait_until="networkidle")
                        page.get_by_role("button", name="Read release-plan.md").first.click()
                        dialog.wait_for()
                        dialog.get_by_role("button", name="Close reader").click()
                        dialog.wait_for(state="detached")
                        assert page.evaluate("history.state.usr") == "opaque state"
                    else:
                        page.go_back()
                        assert page.url == ORIGIN + "/"
                    assert not unexpected, unexpected
                    assert not errors, errors
                    print(
                        f"{name}: actual App+Chat, geometry={geometry}; scrolling, Escape, focus, draft and host URL passed"
                    )
                    context.close()
            finally:
                browser.close()
    finally:
        os.killpg(server.pid, signal.SIGTERM)
        server.wait(timeout=10)


if __name__ == "__main__":
    main()
