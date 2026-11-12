# PR 301 review — commit 27c0f9c

**Recommendation: not ready to present for approval.** Resolve the document-link navigation regression and the oversized-response cancellation gap, then rerun relevant checks. CI was not fully green at review time.

## Findings

1. **P2 — Document-relative and fragment links target the chat page rather than the document.** `ergonaut/frontend/src/components/Markdown.tsx:47-60,84-85` permits relative URLs and renders them as ordinary same-tab anchors; headings have no generated IDs. In Chromium against the synthetic harness, clicking `[fragment](#section)` changed the host page URL to `.../md-reader-harness/#section` while the rendered `Section` heading had no ID; clicking `[relative](../guide)` navigated the current tab to `/guide` and destroyed the dialog. On a real chat route these URLs resolve against the chat URL, not the attachment; `/s/...` links likewise navigate the current tab rather than remaining isolated in the reader. This can discard the mounted chat and unsent state. A fragment matching `#m-<line>` may also interact with the chat message-link handling (`ergonaut/frontend/src/pages/Chat.tsx:353-379`, `ergonaut/frontend/src/messageLink.ts:12-15`); this specific full-Chat interaction was not exercised. Fix fragment navigation within the reader and define safe relative-link behavior without replacing chat; add browser interaction tests, not only href assertions (`Markdown.test.tsx:29-38`).

2. **P2 — Early oversized-response rejection does not cancel the fetch.** `ergonaut/frontend/src/components/MarkdownAttachment.tsx:9-14,68-83` throws immediately when `Content-Length` exceeds 500,000, before acquiring/canceling the response body; the controller is aborted only when the dialog unmounts. Consequently the UI can show an error while the network response remains active until the dialog closes or the browser finishes/stops it; the advertised bound is not a bound on the transfer. Cancel the response body or abort the controller on this path and test a delayed oversized response with a Content-Length header. The existing size test (`MarkdownAttachment.test.tsx:116-122`) covers a body without that header and only checks the error, not cancellation.

## Security and behavior observations

- Document mode makes raw HTML text, suppresses Markdown images, rejects unsafe and protocol-relative schemes, and uses `DocumentLink` rather than `ThreadLink` (`Markdown.tsx:9-15,45-65,81-85`). No new authorization endpoint or server access-control change appears in the diff. This is code review plus existing tests, not a complete security proof.
- Download points to the existing non-inline URL; copy uses the decoded source, with UTF-8 replacement semantics, preserved BOM and original line endings. Therefore copy is exact as decoded text, not arbitrary invalid UTF-8 bytes; download retains original bytes. Close/unmount aborts the fetch and an active flag blocks late state updates (`MarkdownAttachment.tsx:15,27-29,68-83`). The reopened-response test exercises stale result suppression, not actual network abort.
- Existing API test separately covers own and same-user shared-visible files, other-user/missing files, logout, and original bytes (`ergonaut/ergonaut/apps/bots/tests/test_api.py:344-381`). A share from another user's inaccessible session does not grant access; this test does not assert that case explicitly. The browser harness stubs the response and does not exercise backend authorization.
- Media classification precedes the Markdown card, and ordinary chat renderer mode remains the default (`MediaGallery.tsx:232-303`, `Markdown.tsx:72-85`). Existing component tests cover selected cases, not every sent/reply/shared integration path.

## Evidence and limits

- Independently ran `bun test src/components/MarkdownAttachment.test.tsx src/components/Markdown.test.tsx src/components/AttachmentView.test.tsx`: **13 passed, 0 failed**.
- Independently exercised a synthetic Chromium harness with intercepted Markdown response: fragment updated the host URL without a matching heading ID; relative link navigated the same tab and removed the dialog. Inspected the supplied synthetic mobile screenshot. This was not a full Chat interaction or authenticated end-to-end exercise.
- The supplied `md-reader-harness/check.py` tests keyboard/focus/Escape, scroll under a synthetic update, image interception, copy and desktop/mobile sizes, but uses simplified chrome/scroll containers and a stubbed download. Its link checks do not activate fragment or relative links. Mobile is a viewport-sized Chromium emulation, not a physical mobile browser or device keyboard. No independent full Chat, actual browser download, screen-reader, cross-browser, or mobile-device verification was performed in this review.
- The proposal records a previous frontend suite/build, Django authorization test, and synthetic desktop/mobile harness run; these are prior worker claims, not independently rerun here.

## GitHub checks at review time

- **Pending:** test — https://github.com/boundcorp/django-ergo/actions/runs/43330509470/job/131573726604
- **Passed:** frontend — https://github.com/boundcorp/django-ergo/actions/runs/43330509463/job/131573726220
- **Passed:** test — https://github.com/boundcorp/django-ergo/actions/runs/43330509463/job/131573726408
- **Skipped:** image — https://github.com/boundcorp/django-ergo/actions/runs/43330509463/job/131573728549
- **Skipped:** minimal-wheel — https://github.com/boundcorp/django-ergo/actions/runs/43330509470/job/131573728677

No merge, deployment, restart, production write, or code edit was performed. This review file is local and uncommitted.
