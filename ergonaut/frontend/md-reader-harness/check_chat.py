"""Browser smoke for the real Chat component with synthetic session and EventSource.

Run from the repository root: python ergonaut/frontend/md-reader-harness/check_chat.py
Requires frontend dependencies, Python Playwright and Chromium; optionally set
ERGONAUT_TEST_CHROMIUM. No backend or external services are used.
"""

import os
import subprocess
import time
import urllib.request

from check import FRONTEND, OUTPUT, SOURCE
from playwright.sync_api import sync_playwright

PORT = 41740
ORIGIN = f"http://127.0.0.1:{PORT}"
SOURCE += f"""

[Go to milestones](#milestones) · [Message fragment](#m-123) · [Second duplicate](#duplicate-1)
· [Relative guide](../guide) · [Chat path](/s/12345678-1234-1234-1234-123456789abc)
· [Unsafe encoded](java&#x73;cript:alert) · [Unsafe network](//example.invalid/path)
· [Unsafe mixed](JaVaScRiPt:alert) · [Absolute]({ORIGIN}/md-reader-harness/chat.html)

## m-123

## Duplicate

## Duplicate
"""


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    server = subprocess.Popen(
        ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(PORT), "--strictPort"],
        cwd=FRONTEND,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if server.poll() is not None:
                raise RuntimeError("Vite exited before becoming ready")
            try:
                with urllib.request.urlopen(ORIGIN + "/md-reader-harness/chat.html", timeout=1):
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
                for name, width, height in [("desktop", 1440, 900), ("mobile", 390, 844)]:
                    context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
                    page = context.new_page()
                    unexpected = []

                    def route_request(route):
                        url = route.request.url
                        if url == ORIGIN + "/api/attachments/fixture-markdown/download":
                            route.fulfill(status=200, body=SOURCE, content_type="text/markdown; charset=utf-8")
                        elif url.startswith(ORIGIN + "/api/") or not url.startswith(ORIGIN + "/"):
                            unexpected.append(url)
                            route.abort()
                        else:
                            route.continue_()

                    page.route("**/*", route_request)
                    page.goto(ORIGIN + "/md-reader-harness/chat.html", wait_until="networkidle")
                    composer = page.get_by_role("textbox", name="Message")
                    composer.fill("Unsent draft: review the release plan.")
                    opener = page.get_by_role("button", name="Read release-plan.md")
                    opener.scroll_into_view_if_needed()
                    scroll = page.locator(".chat-transcript")
                    page.wait_for_timeout(200)
                    before = scroll.evaluate(
                        "el => ({top: el.scrollTop, remaining: el.scrollHeight - el.scrollTop - el.clientHeight})"
                    )
                    assert before["top"] > 0 and before["remaining"] > 80, before
                    opener.focus()
                    page.screenshot(path=str(OUTPUT / f"{name}-chat-card.png"))
                    page.keyboard.press("Enter")
                    dialog = page.get_by_role("dialog", name="release-plan.md")
                    dialog.get_by_role("heading", name="Release plan", exact=True).wait_for()
                    assert composer.input_value() == "Unsent draft: review the release plan."
                    assert scroll.evaluate("el => el.scrollTop") == before["top"]
                    page.screenshot(path=str(OUTPUT / f"{name}-chat-reader.png"))
                    url = page.url
                    dialog.get_by_role("link", name="Go to milestones").click()
                    assert page.url == url and dialog.is_visible()
                    assert dialog.get_by_role("heading", name="Milestones").evaluate(
                        "el => { const box = el.closest('.md-reader-content').getBoundingClientRect(); const heading = el.getBoundingClientRect(); return heading.top >= box.top - 2 && heading.bottom <= box.bottom + 2 }"
                    )
                    for link, heading_id in [("Message fragment", "m-123"), ("Second duplicate", "duplicate-1")]:
                        dialog.get_by_role("link", name=link).click()
                        assert page.url == url and dialog.is_visible()
                        assert dialog.locator(f"#{heading_id}").evaluate(
                            "el => { const box = el.closest('.md-reader-content').getBoundingClientRect(); const heading = el.getBoundingClientRect(); return heading.top >= box.top - 2 && heading.bottom <= box.bottom + 2 }"
                        )
                    for label in [
                        "Relative guide (unsupported relative link)",
                        "Chat path (unsupported relative link)",
                        "Unsafe encoded (unsupported link)",
                        "Unsafe network (unsupported link)",
                        "Unsafe mixed (unsupported link)",
                    ]:
                        inert = dialog.get_by_text(label, exact=True)
                        assert inert.evaluate("el => !el.closest('a[href]')")
                        inert.click()
                        assert page.url == url and dialog.is_visible()
                    with page.expect_popup() as popup:
                        dialog.get_by_role("link", name="Absolute", exact=True).click()
                    popup.value.close()
                    assert page.url == url and dialog.is_visible()
                    assert composer.input_value() == "Unsent draft: review the release plan."
                    page.evaluate("window.dispatchEvent(new Event('synthetic-chat-update'))")
                    page.get_by_text("Synthetic live update arrived while reading.").wait_for(state="attached")
                    assert scroll.evaluate("el => el.scrollTop") == before["top"]
                    page.keyboard.press("Escape")
                    dialog.wait_for(state="detached")
                    assert opener.evaluate("el => el === document.activeElement")
                    assert scroll.evaluate("el => el.scrollTop") == before["top"]
                    assert composer.input_value() == "Unsent draft: review the release plan."
                    assert not unexpected, unexpected
                    print(
                        f"{name}: real Chat, scrolled-up reader, fragment/relative links, live event, focus/scroll and draft passed"
                    )
                    context.close()
            finally:
                browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    main()
