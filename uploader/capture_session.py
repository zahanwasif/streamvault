"""Save a signed-in YouTube session for the Studio uploader. Run on the PC once, and again
whenever uploads fail with "Signed out of YouTube".

Opens Chrome normally (its own profile, so your everyday Chrome is untouched) on YouTube Studio.
You sign in by hand; nothing is automated during sign-in. Then press Enter here: the script reads
the youtube.com/google.com cookies over Chrome's debugging port and writes them gzip+base64, the
format uploader/studio_upload.py expects. With --gh-repo it stores them as the YT_SESSION secret
(gh CLI signed in), so they never land in a file you might commit.

The cookies are as good as your password for this Google account: keep the file private.

If uploads say "signed out" right after a fresh capture, Chrome may have bound the session to
this PC; try --browser chromium (Playwright's Chromium, installed with `playwright install chromium`).

Usage:
    uv run --with playwright==1.63.0 python uploader/capture_session.py --gh-repo zahanwasif/streamvault
    uv run --with playwright==1.63.0 python uploader/capture_session.py --out yt-session.b64
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from studio_upload import dump_session  # noqa: E402

PORT = 9333
SECRET_LIMIT = 48 * 1024
SIGNED_IN = {"SAPISID", "__Secure-3PSID", "SID"}


def chrome_path(kind: str) -> str:
    if kind == "chromium":
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            return pw.chromium.executable_path
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome") or "", shutil.which("google-chrome-stable") or "",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    sys.exit("Chrome not found. Install Google Chrome, or use --browser chromium")


def main() -> None:
    ap = argparse.ArgumentParser(description="Capture a YouTube session for the Studio uploader")
    ap.add_argument("--browser", choices=["chrome", "chromium"], default="chrome")
    ap.add_argument("--profile", type=Path,
                    default=Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "StreamVault" / "yt-session",
                    help="browser profile folder used only for this")
    ap.add_argument("--gh-repo", help="store as the YT_SESSION secret of this repo (owner/name)")
    ap.add_argument("--out", type=Path, help="write the session to this file instead")
    args = ap.parse_args()
    if not args.gh_repo and not args.out:
        ap.error("give --gh-repo or --out")

    args.profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([chrome_path(args.browser), f"--user-data-dir={args.profile}",
                             f"--remote-debugging-port={PORT}", "--no-first-run",
                             "https://studio.youtube.com"])
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
            break
        except OSError:
            time.sleep(0.5)
    else:
        proc.terminate()
        sys.exit("Chrome did not open its debugging port; close other Chrome windows using this profile")

    input("\nSign in to YouTube Studio in the Chrome window (the channel's Google account).\n"
          "When Studio shows your channel dashboard, come back here and press Enter… ")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        cookies = browser.contexts[0].cookies()
        names = {c["name"] for c in cookies if c.get("domain", "").endswith("youtube.com")}
        browser.close()
    proc.terminate()

    if not names & SIGNED_IN:
        sys.exit("No YouTube sign-in cookies found. Sign in fully (Studio dashboard visible), then run again.")
    session = dump_session(cookies)
    if len(session) > SECRET_LIMIT:
        sys.exit(f"session is {len(session)} bytes, over GitHub's 48 KB secret limit")

    if args.out:
        args.out.write_text(session, encoding="utf-8")
        print(f"saved {args.out} ({len(session)} bytes). Keep it private.")
    if args.gh_repo:
        gh = shutil.which("gh")
        if not gh:
            sys.exit("gh CLI not found: install it and run `gh auth login`, or use --out")
        subprocess.run([gh, "secret", "set", "YT_SESSION", "--repo", args.gh_repo], input=session, text=True, check=True)
        print(f"stored YT_SESSION on {args.gh_repo}")


if __name__ == "__main__":
    main()
