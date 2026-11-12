"""Run from the repository root: python ergonaut/frontend/md-reader-harness/check.py.

Requires frontend dependencies (npm ci), Python Playwright, and a Playwright Chromium
installation (python -m playwright install chromium). Alternatively set
ERGONAUT_TEST_CHROMIUM to a Chromium executable. Uses only local synthetic data.
"""

import os
import subprocess
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
FRONTEND = ROOT / "ergonaut" / "frontend"
OUTPUT = ROOT / "docs" / "md-reader" / "after"
PORT = 41739
ORIGIN = f"http://127.0.0.1:{PORT}"
SOURCE = """# Release plan

A synthetic document for a focused reading experience.

## Milestones

- [x] Prepare the release notes
- [ ] Review the rollout plan
- [ ] Publish after approval

## Decision table

| Item | Owner | Status | Phase | Region | Review | Risk | Window | Verification detail |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Documentation | Morgan | Ready | 1 | US | Yes | Low | AM | release_notes_and_migration_guide_with_unbroken_verification_identifier_2026 |
| Rollout | Alex | In review | 2 | EU | Yes | Low | PM | The rollout checklist awaits final approval |

## Implementation notes

```python
def release(approved):
    return "ship" if approved else "wait"
    audit_log = "A deliberately long line to verify that a code block can scroll horizontally on a narrow mobile screen"
```

[Project website](https://example.org/release) · [unsafe](javascript:alert(1))

![Remote image](https://remote-image.example.invalid/track.png)
![Relative image](/md-reader-harness/relative-tracker.png)
![Inline image](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a9sAAAAASUVORK5CYII=)

<img src="/md-reader-harness/html-tracker.png" onerror="window.fixtureScriptRan = true">

<script>window.fixtureScriptRan = true</script>
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
                with urllib.request.urlopen(ORIGIN + "/md-reader-harness/", timeout=1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Vite did not start")
        with sync_playwright() as p:
            executable = os.environ.get("ERGONAUT_TEST_CHROMIUM")
            browser = p.chromium.launch(headless=True, executable_path=executable, args=["--no-sandbox"])
            try:
                for name, width, height in [("desktop", 1440, 900), ("mobile", 390, 844)]:
                    context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
                    page = context.new_page()
                    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=ORIGIN)
                    remote = []
                    image_requests = []
                    failed_response = False
                    attachment_requests = []

                    def route_request(route):
                        url = route.request.url
                        if route.request.resource_type == "image":
                            image_requests.append(url)
                            route.abort()
                        elif url == ORIGIN + "/api/attachments/fixture-markdown/download":
                            attachment_requests.append(url)
                            if failed_response:
                                route.fulfill(status=403, body="Forbidden")
                            else:
                                route.fulfill(status=200, body=SOURCE, content_type="text/markdown; charset=utf-8")
                        elif url.startswith(ORIGIN + "/"):
                            route.continue_()
                        else:
                            remote.append(url)
                            route.abort()

                    page.route("**/*", route_request)
                    page.goto(ORIGIN + "/md-reader-harness/", wait_until="networkidle")
                    opener = page.get_by_role("button", name="Read release-plan.md")
                    opener.scroll_into_view_if_needed()
                    page.screenshot(path=str(OUTPUT / f"{name}-card.png"))
                    scroll = page.get_by_test_id("transcript-scroll")
                    scroll.evaluate("(el) => { el.scrollTop = Math.max(el.scrollTop, 100) }")
                    before = scroll.evaluate("(el) => el.scrollTop")
                    assert before > 0, "The scroll-preservation fixture must start scrolled"
                    opener.focus()
                    page.keyboard.press("Enter")
                    dialog = page.get_by_role("dialog", name="release-plan.md")
                    dialog.wait_for()
                    dialog.get_by_role("heading", name="Release plan", exact=True).wait_for()
                    assert dialog.get_by_role("button", name="Close reader").evaluate(
                        "(el) => el === document.activeElement"
                    )
                    assert scroll.evaluate("(el) => el.scrollTop") == before
                    assert dialog.locator("table").count() == 1
                    assert dialog.locator("pre code").count() == 1
                    assert dialog.locator("img").count() == 0
                    assert dialog.locator("script").count() == 0
                    assert page.evaluate("window.fixtureScriptRan") is None
                    assert dialog.locator('a[href^="javascript:"]').count() == 0
                    external = dialog.get_by_role("link", name="Project website")
                    assert {"noopener", "noreferrer"} <= set((external.get_attribute("rel") or "").split())
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    assert dialog.evaluate("(el) => el.getBoundingClientRect().width <= window.innerWidth")
                    page.screenshot(path=str(OUTPUT / f"{name}-reader.png"))
                    if name == "mobile":
                        for selector in ("table", "pre"):
                            element = dialog.locator(selector)
                            assert element.evaluate("(el) => el.scrollWidth > el.clientWidth"), (
                                f"{selector} should overflow internally"
                            )
                            assert element.evaluate("(el) => { el.scrollLeft = 40; return el.scrollLeft > 0 }"), (
                                f"{selector} should scroll horizontally"
                            )
                    dialog.get_by_role("button", name="Copy raw source").click()
                    dialog.get_by_text("Raw source copied.").wait_for()
                    assert page.evaluate("navigator.clipboard.readText()") == SOURCE
                    assert (
                        dialog.get_by_role("link", name="Download original").get_attribute("href")
                        == "/api/attachments/fixture-markdown/download"
                    )
                    for _ in range(5):
                        page.keyboard.press("Tab")
                        # Native dialogs may move focus to browser chrome between cycles.
                        assert dialog.evaluate(
                            "(el) => el.contains(document.activeElement) || document.activeElement === document.body"
                        )
                    page.evaluate("window.dispatchEvent(new Event('synthetic-update'))")
                    page.get_by_text("Synthetic live update 1:", exact=False).wait_for(state="attached")
                    assert scroll.evaluate("(el) => el.scrollTop") == before
                    page.keyboard.press("Escape")
                    dialog.wait_for(state="detached")
                    assert opener.evaluate("(el) => el === document.activeElement")
                    assert scroll.evaluate("(el) => el.scrollTop") == before
                    failed_response = True
                    opener.click()
                    dialog.get_by_role("alert").wait_for()
                    assert dialog.get_by_role("link", name="Download original").is_visible()
                    page.keyboard.press("Escape")
                    dialog.wait_for(state="detached")
                    assert len(attachment_requests) == 2
                    assert not remote, f"Unexpected remote resource requests: {remote}"
                    assert not image_requests, f"Unexpected image resource requests: {image_requests}"
                    print(
                        f"{name}: screenshots, copy, error, focus, Escape, update/scroll, overflow and remote-request checks passed"
                    )
                    context.close()
            finally:
                browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    main()
