"""Upload one Short through YouTube Studio with Playwright, then report to n8n.

Stand-in for the YouTube Data API while the API project's audit is pending (unaudited API
uploads are locked to private). Runs on GitHub Actions (.github/workflows/youtube-upload.yml),
which n8n dispatches every 4 hours with the chosen Short.

Automating Studio is against YouTube's terms and can get the channel flagged; Studio changes can
break it. Every selector is in SELECTORS below, so a Studio change is a one-place fix.

The Google session is a cookie snapshot made by uploader/capture_session.py (a real, human
sign-in on the PC), stored as the YT_SESSION secret. After a successful run the refreshed cookies
are written to --session-out, so the workflow can store them for next time.

Result, printed and POSTed to --resume-url (the n8n Wait node) when given:
    {"ok": true,  "video_id", "url", "privacy", "posted_at", "uploader": "studio"}
    {"ok": false, "kind": "session"|"limit"|"post"|"studio", "error": "...", "screenshot_b64"}
kind: session = signed out (re-run capture_session.py); limit = daily upload limit;
      post = this Short (e.g. a copyright claim); studio = the page didn't behave (selectors?).

Usage:
    python uploader/studio_upload.py --video v.mp4 --post post.json --session yt-session.b64 \
        [--resume-url URL] [--session-out new.b64] [--dry-run] [--headed]
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright

UPLOAD_URL = "https://www.youtube.com/upload"   # redirects into Studio's upload dialog
STUDIO_URL = "https://studio.youtube.com"
OUT = Path(__file__).resolve().parent / "out"

SELECTORS = {
    "file_input": 'input[type="file"]',
    "title": "#title-textarea #textbox",
    "description": "#description-textarea #textbox",
    "not_for_kids": 'tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]',
    "show_more": "#toggle-button",
    "altered_no": 'tp-yt-paper-radio-button[name="VIDEO_HAS_ALTERED_CONTENT_NO"]',
    "tags": "#tags-container input",
    "next": "#next-button",
    "public": 'tp-yt-paper-radio-button[name="PUBLIC"]',
    "private": 'tp-yt-paper-radio-button[name="PRIVATE"]',
    "done": "#done-button",
    "video_link": "ytcp-video-info a, .video-url-fadeable a",
    "progress": "ytcp-video-upload-progress",
    "published_close": "ytcp-video-share-dialog #close-button, #close-button",
    "dialog_close": "ytcp-uploads-dialog #close-button",
    # Studio's "Improve your experience: unsupported browser" page
    "skip_unsupported": 'a:has-text("Skip to YouTube Studio"), button:has-text("Skip to YouTube Studio"), '
                        'ytcp-button:has-text("Skip to YouTube Studio"), :text-is("SKIP TO YOUTUBE STUDIO")',
    # welcome / what's-new dialogs that can cover the dashboard
    "dismiss": '#dismiss-button, ytcp-button#close-button, tp-yt-paper-dialog button:has-text("Got it"), '
               'tp-yt-paper-dialog button:has-text("Continue")',
    "create": "#create-icon",
    "upload_menu": '#text-item-0, tp-yt-paper-item:has-text("Upload videos")',
}
# Text Studio shows (English UI; the context forces en-US).
UNSUPPORTED_TEXT = re.compile(r"unsupported browser|old browser version", re.I)
LIMIT_TEXT = re.compile(r"daily upload limit", re.I)
COPYRIGHT_TEXT = re.compile(r"copyright[- ]protected content found|copyright claim", re.I)
CHECKS_DONE = re.compile(r"checks complete|no issues found", re.I)
UPLOAD_DONE = re.compile(r"upload complete|processing (hd|sd|will begin)|checks complete|video uploaded", re.I)


class UploadFailed(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def load_session(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8").strip()
    return json.loads(gzip.decompress(base64.b64decode(raw)))


def dump_session(cookies: list[dict]) -> str:
    keep = [c for c in cookies if c.get("domain", "").endswith(("youtube.com", "google.com"))]
    return base64.b64encode(gzip.compress(json.dumps(keep).encode())).decode()


def report(result: dict, resume_url: str | None) -> None:
    shown = {k: v for k, v in result.items() if k != "screenshot_b64"}
    print("RESULT " + json.dumps(shown, ensure_ascii=False), flush=True)
    if not resume_url:
        return
    body = json.dumps(result).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(resume_url, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                print(f"reported to n8n: HTTP {resp.status}")
                return
        except Exception as e:  # noqa: BLE001
            print(f"report attempt {attempt + 1} failed: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    print("could not report to n8n; it will mark this Short unconfirmed", file=sys.stderr)


def page_text(page: Page) -> str:
    try:
        return page.inner_text("body", timeout=5000)
    except PlaywrightError:
        return ""


def where(page: Page) -> str:
    """Which page we're on, for error messages: title and path only (the Actions log is public)."""
    try:
        url = page.url.split("?")[0].split("#")[0]
        return f"page \"{page.title()[:80]}\" at {url}"
    except PlaywrightError:
        return "page unknown"


def skip_unsupported(page: Page) -> bool:
    """Click through Studio's "unsupported browser" page if it's showing. True if it was."""
    if not UNSUPPORTED_TEXT.search(page_text(page)):
        return False
    try:
        page.locator(SELECTORS["skip_unsupported"]).first.click(timeout=10_000)
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
        page.wait_for_timeout(3000)
        print("note: skipped Studio's unsupported-browser page", file=sys.stderr)
    except PlaywrightTimeout:
        raise UploadFailed("studio", f"Studio says this browser is unsupported and its Skip link wasn't found ({where(page)})") from None
    return True


def dismiss_dialogs(page: Page) -> None:
    for _ in range(3):
        try:
            page.locator(SELECTORS["dismiss"]).first.click(timeout=2000)
            page.wait_for_timeout(800)
        except PlaywrightError:
            return


def open_upload_dialog(page: Page, start_url: str) -> None:
    """Get to a page with the upload file input: the upload URL first, then Create → Upload videos."""
    page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3000)
    check_blockers(page)
    if skip_unsupported(page):
        check_blockers(page)
    file_input = page.locator(SELECTORS["file_input"]).first
    try:
        file_input.wait_for(state="attached", timeout=15_000)
        return
    except PlaywrightTimeout:
        pass
    # Fallback: open Studio and use its Create menu.
    studio = STUDIO_URL if start_url == UPLOAD_URL else start_url.split("/upload")[0] + "/studio"
    if not page.url.startswith(studio):
        page.goto(studio, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3000)
        check_blockers(page)
        skip_unsupported(page)
    dismiss_dialogs(page)
    try:
        page.locator(SELECTORS["create"]).first.click(timeout=30_000)
        page.locator(SELECTORS["upload_menu"]).first.click(timeout=10_000)
        file_input.wait_for(state="attached", timeout=30_000)
    except PlaywrightTimeout:
        check_blockers(page)
        raise UploadFailed("studio", f"Studio's upload dialog did not open ({where(page)})") from None


def check_blockers(page: Page) -> None:
    if "accounts.google.com" in page.url or "ServiceLogin" in page.url:
        raise UploadFailed("session", "Signed out of YouTube: run uploader/capture_session.py again")
    if LIMIT_TEXT.search(page_text(page)):
        raise UploadFailed("limit", "Daily upload limit reached (verify the channel at youtube.com/verify)")


def click(page: Page, key: str, timeout: float = 30_000) -> None:
    try:
        page.locator(SELECTORS[key]).first.click(timeout=timeout)
    except PlaywrightTimeout:
        check_blockers(page)
        raise UploadFailed("studio", f"Studio element not found: {key} ({where(page)})") from None


def wait_for_upload(page: Page, timeout_s: int, checks_grace_s: int = 120) -> str:
    """Wait until the file is uploaded and the copyright check has run (or had checks_grace_s to
    run after the upload finished); return Studio's progress text."""
    deadline = time.time() + timeout_s
    uploaded_at = None
    last = ""
    while time.time() < deadline:
        check_blockers(page)
        try:
            last = page.locator(SELECTORS["progress"]).first.inner_text(timeout=3000).strip()
        except PlaywrightError:
            pass
        if COPYRIGHT_TEXT.search(page_text(page)):
            raise UploadFailed("post", "copyright")
        if CHECKS_DONE.search(last):
            return last
        if UPLOAD_DONE.search(last):
            uploaded_at = uploaded_at or time.time()
            if time.time() - uploaded_at >= checks_grace_s:
                return last
        time.sleep(3)
    if uploaded_at:
        return last  # uploaded; checks still running is fine to publish
    raise UploadFailed("studio", f"upload did not finish in {timeout_s}s (last status: {last[:120]!r})")


def upload(page: Page, video: Path, post: dict, dry_run: bool, upload_timeout: int,
           checks_grace: int = 120, start_url: str = UPLOAD_URL) -> dict:
    open_upload_dialog(page, start_url)
    try:
        page.locator(SELECTORS["file_input"]).first.set_input_files(str(video), timeout=45_000)
    except PlaywrightTimeout:
        check_blockers(page)
        raise UploadFailed("studio", f"Studio's upload dialog did not open ({where(page)})") from None

    # Details
    title = page.locator(SELECTORS["title"]).first
    try:
        title.wait_for(timeout=60_000)
    except PlaywrightTimeout:
        check_blockers(page)
        raise UploadFailed("studio", f"details form did not appear after choosing the file ({where(page)})") from None
    title.fill(post["title"][:100])
    page.locator(SELECTORS["description"]).first.fill(post.get("description", "")[:5000])
    click(page, "not_for_kids")
    click(page, "show_more")
    try:
        page.locator(SELECTORS["altered_no"]).first.click(timeout=10_000)
    except PlaywrightTimeout:
        print("note: no altered-content question on this account", file=sys.stderr)
    tags = [t.lstrip("#") for t in post.get("tags", []) if t]
    if tags:
        try:
            box = page.locator(SELECTORS["tags"]).first
            box.fill(",".join(tags)[:480] + ",", timeout=10_000)
        except PlaywrightTimeout:
            print("note: tags box not found, posting without tags", file=sys.stderr)

    # Video elements, Checks, Visibility
    for _ in range(3):
        click(page, "next")
        page.wait_for_timeout(1500)
    status = wait_for_upload(page, upload_timeout, checks_grace)
    click(page, "private" if dry_run else "public")

    link = ""
    try:
        link = page.locator(SELECTORS["video_link"]).first.get_attribute("href", timeout=15_000) or ""
    except PlaywrightTimeout:
        pass
    m = re.search(r"(?:youtu\.be/|shorts/|v=)([\w-]{11})", link)
    video_id = m.group(1) if m else None

    if dry_run:
        print(f"dry run: filled everything, left as a private draft ({link or 'no link'})")
        return {"ok": True, "dry_run": True, "video_id": video_id, "url": link, "privacy": "private",
                "status": status}

    click(page, "done")
    page.wait_for_timeout(4000)
    check_blockers(page)
    try:
        page.locator(SELECTORS["published_close"]).first.click(timeout=15_000)
    except PlaywrightTimeout:
        pass
    if not video_id:
        raise UploadFailed("studio", "published, but the video link was not found; check Studio")
    return {"ok": True, "video_id": video_id, "url": f"https://youtube.com/shorts/{video_id}",
            "privacy": "public", "status": status}


def main() -> int:
    ap = argparse.ArgumentParser(description="Upload one Short through YouTube Studio")
    ap.add_argument("--video", type=Path, required=True)
    ap.add_argument("--post", type=Path, required=True, help="post.json (title, description, tags)")
    ap.add_argument("--session", type=Path, required=True, help="gzip+base64 cookie file from capture_session.py")
    ap.add_argument("--resume-url", help="n8n Wait-node URL to POST the result to")
    ap.add_argument("--session-out", type=Path, help="write refreshed cookies here after a successful run")
    ap.add_argument("--dry-run", action="store_true", help="fill everything but leave a private draft")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--upload-timeout", type=int, default=900)
    ap.add_argument("--checks-grace", type=int, default=120,
                    help="after the upload, how long to wait for the copyright check before publishing")
    ap.add_argument("--start-url", default=UPLOAD_URL, help=argparse.SUPPRESS)  # tests point this at a fake Studio
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    post = json.loads(args.post.read_text(encoding="utf-8"))
    result: dict
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context(locale="en-US", viewport={"width": 1400, "height": 1000})
        try:
            context.add_cookies(load_session(args.session))
        except (ValueError, OSError) as e:
            report({"ok": False, "kind": "session", "error": f"YT_SESSION is unreadable: {e}"}, args.resume_url)
            return 1
        page = context.new_page()
        try:
            result = upload(page, args.video, post, args.dry_run, args.upload_timeout,
                            args.checks_grace, args.start_url)
            if args.session_out:
                args.session_out.write_text(dump_session(context.cookies()), encoding="utf-8")
        except UploadFailed as e:
            result = {"ok": False, "kind": e.kind, "error": str(e)}
        except Exception as e:  # noqa: BLE001  - always tell n8n something
            result = {"ok": False, "kind": "studio", "error": f"{type(e).__name__}: {e}"[:500]}
        if not result.get("ok"):
            # The screenshot goes to n8n (saved privately in OneDrive): the repo's run logs are public.
            try:
                shot = page.screenshot(type="jpeg", quality=55)
                (OUT / "failure.jpg").write_bytes(shot)
                (OUT / "failure.html").write_text(page.content(), encoding="utf-8")
                result["screenshot_b64"] = base64.b64encode(shot).decode()
            except PlaywrightError:
                pass
        browser.close()

    result.update(uploader="studio", post_id=post.get("id"),
                  posted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    report(result, None if result.get("dry_run") else args.resume_url)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
