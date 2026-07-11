"""
Run the Instagram comment flow until N comments are posted.

Usage:
  .venv\\Scripts\\python.exe main_test.py
  .venv\\Scripts\\python.exe main_test.py --target 10
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

from comment_llm import generate_comment
from config import (
    BROWSER_DATA_DIR,
    CSV_PATH,
    INSTA_ID,
    INSTA_PASS,
    LOG_PATH,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
    ensure_browser_cdp,
    ensure_llm_ready,
    load_env,
)

load_env()

pw = browser = page = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def load_comment_log() -> list:
    if LOG_PATH.exists():
        try:
            return json.loads(LOG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def already_seen(post_id: str = "", username: str = "", caption: str = "") -> dict | None:
    for row in load_comment_log():
        if post_id and row.get("post_id") and row["post_id"] == post_id:
            return row
        if (
            not post_id
            and username
            and row.get("username") == username
            and (row.get("caption") or "") == (caption or "")
        ):
            return row
    return None


def log_post(
    *,
    post_id: str,
    username: str,
    caption: str,
    comment: str,
    relevant: bool | None = None,
) -> dict:
    now_local = datetime.now().astimezone()
    now_utc = datetime.now(timezone.utc)
    rows = load_comment_log()
    entry = {
        "datetime": now_local.strftime("%Y-%m-%d %H:%M:%S %z"),
        "datetime_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "ts": now_utc.isoformat(),
        "post_id": post_id or "",
        "username": username or "",
        "caption": caption or "",
        "comment": comment or "",
        "relevant": relevant,
    }
    replaced = False
    for i, row in enumerate(rows):
        same_id = entry["post_id"] and row.get("post_id") == entry["post_id"]
        same_text = (
            not entry["post_id"]
            and row.get("username") == entry["username"]
            and (row.get("caption") or "") == entry["caption"]
        )
        if same_id or same_text:
            rows[i] = {**row, **entry}
            replaced = True
            break
    if not replaced:
        rows.append(entry)

    LOG_PATH.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    fields = [
        "datetime",
        "datetime_utc",
        "ts",
        "post_id",
        "username",
        "caption",
        "comment",
        "relevant",
    ]
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})
    return entry


def count_logged_comments() -> int:
    return sum(1 for r in load_comment_log() if (r.get("comment") or "").strip())


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------


async def connect():
    global pw, browser, page
    cdp = ensure_browser_cdp()
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(cdp)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.set_viewport_size({"width": WINDOW_WIDTH, "height": WINDOW_HEIGHT})
    return cdp, page.url


async def disconnect():
    global pw, browser, page
    if browser:
        await browser.close()
    if pw:
        await pw.stop()
    browser = page = pw = None


async def goto_home():
    await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)
    return page.url


async def dismiss():
    clicked = []
    for label in (
        "Not Now",
        "Not now",
        "Decline optional cookies",
        "Allow all cookies",
        "Allow all cookies",
    ):
        btn = page.get_by_role("button", name=label)
        if await btn.count():
            try:
                await btn.first.click(timeout=1500)
                await page.wait_for_timeout(400)
                clicked.append(label)
            except Exception:
                pass
    return clicked


async def is_logged_in() -> bool:
    """True if the session looks logged in (no login form / has Home)."""
    url = (page.url or "").lower()
    if "accounts/login" in url or "/login" in url:
        return False

    user_box = page.locator('input[name="username"]')
    if await user_box.count() and await user_box.first.is_visible():
        return False

    for sel in (
        'svg[aria-label="Home"]',
        'a[href="/"] svg[aria-label="Home"]',
        'svg[aria-label="Search"]',
        'svg[aria-label="New post"]',
    ):
        loc = page.locator(sel)
        if await loc.count():
            return True

    # Feed articles usually mean home
    if await page.locator("article").count():
        return True
    return False


async def login_if_needed() -> dict:
    """
    Go to Instagram and sign in with INSTA_ID / INSTA_PASS from .env if needed.
    Reuses browser_data session when already logged in.
    """
    await goto_home()
    await dismiss()

    if await is_logged_in():
        return {"status": "already_logged_in", "url": page.url, "user": INSTA_ID or ""}

    if not INSTA_ID or not INSTA_PASS:
        raise RuntimeError(
            "Not logged in and INSTA_ID/INSTA_PASS missing from playwright/.env"
        )

    # Prefer dedicated login URL if form not on current page
    user_box = page.locator('input[name="username"]')
    if not (await user_box.count() and await user_box.first.is_visible()):
        await page.goto(
            "https://www.instagram.com/accounts/login/",
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(1500)
        await dismiss()
        user_box = page.locator('input[name="username"]')

    if not await user_box.count():
        raise RuntimeError("Login form not found — Instagram UI may have changed")

    await user_box.first.click()
    await user_box.first.fill(INSTA_ID)
    pass_box = page.locator('input[name="password"]')
    await pass_box.first.click()
    await pass_box.first.fill(INSTA_PASS)

    submit = page.locator('button[type="submit"]')
    if await submit.count():
        await submit.first.click()
    else:
        await page.get_by_role("button", name="Log in", exact=True).first.click()

    await page.wait_for_timeout(4500)
    await dismiss()

    # Post-login prompts: Save info / Turn on Notifications
    for label in ("Not Now", "Not now", "Save info"):
        btn = page.get_by_role("button", name=label)
        if await btn.count():
            try:
                # Prefer dismissing save/notifications with Not Now
                if label.startswith("Not"):
                    await btn.first.click(timeout=2000)
                    await page.wait_for_timeout(500)
            except Exception:
                pass

    await page.wait_for_timeout(1000)
    if not await is_logged_in():
        raise RuntimeError(
            "Login submitted but still on login page — check credentials or 2FA"
        )

    return {"status": "logged_in", "url": page.url, "user": INSTA_ID}


async def _comment_btn(root):
    btn = root.locator('svg[aria-label="Comment"]').first
    if await btn.count():
        return btn
    btn = root.get_by_role("button", name="Comment").first
    if await btn.count():
        return btn
    return None


async def _post_id_from(el) -> str:
    if el is None:
        return ""
    return await el.evaluate(
        """(node) => {
          const hrefs = [...node.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]')]
            .map(a => a.getAttribute('href') || '');
          for (const href of hrefs) {
            const m = href.match(/\\/(p|reel)\\/([^\\/?#]+)/);
            if (m) return m[2];
          }
          return '';
        }"""
    )


async def _dialog_post_id() -> str:
    dialog = page.locator('div[role="dialog"]').first
    if await dialog.count() == 0:
        return ""
    handle = await dialog.element_handle()
    pid = await _post_id_from(handle)
    if pid:
        return pid
    return await page.evaluate(
        """() => {
          const m = (location.pathname + ' ' + location.href).match(/\\/(p|reel)\\/([^\\/?#]+)/);
          return m ? m[2] : '';
        }"""
    )


async def dismiss_more_menu():
    cancel = page.get_by_role("button", name="Cancel", exact=True)
    if await cancel.count():
        try:
            await cancel.last.click(timeout=2000)
            await page.wait_for_timeout(400)
            return "closed via Cancel"
        except Exception:
            pass

    menus = page.locator('div[role="dialog"]')
    n = await menus.count()
    if n >= 2:
        top = menus.nth(n - 1)
        for label in ("Cancel", "Close", "Not Now"):
            btn = top.get_by_role("button", name=label, exact=True)
            if await btn.count():
                try:
                    await btn.first.click(timeout=2000)
                    await page.wait_for_timeout(400)
                    return f"closed via {label}"
                except Exception:
                    pass
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)
        return "closed via Escape"

    for label in ("Report", "About this account", "Copy link", "Embed"):
        if await page.get_by_role("button", name=label, exact=True).count():
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)
            return "closed via Escape (menu items)"
    return "ok"


async def close_post():
    await dismiss_more_menu()
    dialog = page.locator('div[role="dialog"]').first
    if await dialog.count() == 0:
        return "no dialog"

    close = dialog.locator('svg[aria-label="Close"]').first
    if await close.count() == 0:
        close = page.locator('svg[aria-label="Close"]').first
    if await close.count() == 0:
        close = dialog.get_by_role("button", name="Close").first
    if await close.count() == 0:
        close = page.get_by_role("button", name="Close").first

    if await close.count():
        try:
            await close.click(timeout=3000)
        except Exception:
            await close.locator("xpath=..").click(timeout=3000)
    else:
        await page.keyboard.press("Escape")

    await page.wait_for_timeout(600)
    try:
        await dialog.wait_for(state="hidden", timeout=5000)
    except Exception:
        pass
    return "closed"


async def open_next_unseen(max_scrolls: int = 24):
    seen_ids = {r.get("post_id") for r in load_comment_log() if r.get("post_id")}
    dialog = page.locator('div[role="dialog"]')
    if await dialog.count():
        await close_post()

    tried: set[str] = set()

    for _ in range(max_scrolls + 1):
        articles = page.locator("article")
        n = await articles.count()
        for i in range(n):
            target = articles.nth(i)
            pid = ""
            try:
                handle = await target.element_handle()
                if handle:
                    pid = await _post_id_from(handle) or ""
            except Exception:
                pid = ""

            if pid and (pid in seen_ids or pid in tried):
                continue

            comment = await _comment_btn(target)
            if comment is None:
                continue

            try:
                await comment.scroll_into_view_if_needed(timeout=2000)
                await page.wait_for_timeout(250)
                await comment.click(timeout=3000)
                await page.wait_for_timeout(1000)
                await dialog.first.wait_for(state="visible", timeout=12000)
            except Exception:
                continue

            opened_id = await _dialog_post_id() or pid
            if opened_id and opened_id in seen_ids:
                tried.add(opened_id)
                await close_post()
                continue

            if opened_id:
                tried.add(opened_id)
            await dismiss_more_menu()
            return {"status": "opened", "post_id": opened_id}

        await page.mouse.wheel(0, 500)
        await page.wait_for_timeout(550)

    raise RuntimeError("No unseen posts found (by post_id) after scrolling")


async def _expand_caption():
    await dismiss_more_menu()
    root = page.locator('div[role="dialog"]').first
    if await root.count() == 0:
        root = page.locator("article").first

    for pattern in (r"^more$", r"^… more$", r"^\.\.\. more$", r"^See more$"):
        more = root.get_by_role("button", name=re.compile(pattern, re.I))
        if await more.count() == 0:
            more = root.locator("span").filter(has_text=re.compile(pattern, re.I))
        if await more.count() == 0:
            continue
        for i in range(await more.count()):
            el = more.nth(i)
            label = (await el.get_attribute("aria-label") or "").lower()
            if "option" in label or "more options" in label:
                continue
            try:
                await el.click(timeout=1500)
                await page.wait_for_timeout(400)
                await dismiss_more_menu()
                return
            except Exception:
                continue


async def extract():
    await dismiss_more_menu()
    await _expand_caption()
    await dismiss_more_menu()

    data = await page.evaluate(
        """() => {
          const dialog = document.querySelector('div[role="dialog"]') || document.querySelector('article');
          if (!dialog) return { error: 'No post dialog/article found. Open a post first.' };

          const userEl = dialog.querySelector('header a[href^="/"]') || dialog.querySelector('a[href^="/"][role="link"]');
          let username = (userEl?.getAttribute('href') || '').replaceAll('/', '').trim();
          if (!username) username = (userEl?.innerText || '').trim();

          let caption = '';
          const h1 = dialog.querySelector('h1');
          if (h1) caption = (h1.innerText || '').trim();
          const candidates = dialog.querySelectorAll('ul li span[dir="auto"], span[dir="auto"], h1');
          for (const s of candidates) {
            const t = (s.innerText || '').trim();
            if (!t) continue;
            if (t.toLowerCase() === username.toLowerCase()) continue;
            if (t === 'more' || t === '… more' || t === '... more') continue;
            if (t.length > caption.length) caption = t;
          }
          caption = caption.replace(/\\s*…?\\s*more$/i, '').trim();

          let image_url = '';
          let bestArea = 0;
          for (const img of dialog.querySelectorAll('img')) {
            const src = img.currentSrc || img.src || '';
            if (!src || src.startsWith('data:')) continue;
            const w = img.naturalWidth || img.width || 0;
            const h = img.naturalHeight || img.height || 0;
            const area = w * h;
            if (area > bestArea && w >= 100) {
              bestArea = area;
              image_url = src;
            }
          }
          if (!image_url) {
            const img = dialog.querySelector('article img') || dialog.querySelector('img[src*="cdninstagram"]');
            image_url = img?.currentSrc || img?.src || '';
          }

          let post_id = '';
          const hrefs = [
            ...[...dialog.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]')].map(a => a.getAttribute('href') || ''),
            location.pathname,
            location.href,
          ];
          for (const href of hrefs) {
            const m = href.match(/\\/(p|reel)\\/([^\\/?#]+)/);
            if (m) { post_id = m[2]; break; }
          }

          return { username, caption, image_url, post_id };
        }"""
    )
    if data.get("error"):
        raise RuntimeError(data["error"])

    if data.get("image_url"):
        raw = await page.evaluate(
            """async (url) => {
              const res = await fetch(url);
              const buf = await res.arrayBuffer();
              return Array.from(new Uint8Array(buf));
            }""",
            data["image_url"],
        )
        image = bytes(raw)
    else:
        loc = page.locator('div[role="dialog"]').first
        image = (
            await loc.screenshot(type="jpeg", quality=85)
            if await loc.count()
            else await page.screenshot(type="jpeg", quality=85)
        )
    return {
        "username": data.get("username") or "",
        "caption": data.get("caption") or "",
        "image": image,
        "post_id": data.get("post_id") or "",
    }


async def submit(text: str):
    await dismiss_more_menu()

    dialog = page.locator('div[role="dialog"]').first
    scope = dialog if await dialog.count() else page

    box = scope.locator(
        'textarea[aria-label*="Add a comment"], '
        'textarea[placeholder*="Add a comment"], '
        'div[role="textbox"][contenteditable="true"][aria-label*="Add a comment"]'
    )
    if await box.count() == 0:
        box = scope.get_by_role("textbox", name=re.compile(r"add a comment", re.I))
    if await box.count() == 0:
        raise RuntimeError("Comment textbox not found")

    target = box.first
    await target.click()
    await page.wait_for_timeout(200)
    await target.fill("")
    try:
        await target.press_sequentially(text, delay=12)
    except Exception:
        await target.fill(text)
        await target.dispatch_event("input")
    await page.wait_for_timeout(700)

    await dismiss_more_menu()

    post_btn = scope.locator("form").get_by_role("button", name="Post", exact=True)
    if await post_btn.count() == 0:
        post_btn = scope.get_by_role("button", name="Post", exact=True)
    if await post_btn.count() == 0:
        post_btn = scope.locator('[role="button"]', has_text=re.compile(r"^Post$"))

    if await post_btn.count() == 0:
        raise RuntimeError("Blue Post button not found")

    clicked = False
    for i in range(await post_btn.count() - 1, -1, -1):
        btn = post_btn.nth(i)
        try:
            if not await btn.is_visible():
                continue
            await btn.click(timeout=4000, force=False)
            clicked = True
            break
        except Exception:
            try:
                await btn.click(timeout=4000, force=True)
                clicked = True
                break
            except Exception:
                continue
    if not clicked:
        raise RuntimeError("Could not click blue Post button")

    await page.wait_for_timeout(1500)
    return "submitted"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def process_one_post() -> str:
    """
    Open next unseen post → LLM → submit if relevant.
    Returns: 'commented' | 'skipped' | 'already'
    """
    opened = await open_next_unseen()
    print(f"  opened: {opened}")

    extracted = await extract()
    post_id = extracted.get("post_id") or opened.get("post_id") or ""
    username = extracted.get("username") or ""
    caption = extracted.get("caption") or ""
    image = extracted.get("image")

    print(f"  post_id=@{post_id} user=@{username}")
    print(f"  caption: {(caption[:120] + '…') if len(caption) > 120 else caption}")

    seen = already_seen(post_id, username, caption)
    if seen and (seen.get("comment") or "").strip():
        print(f"  already commented at {seen.get('datetime') or seen.get('ts')}")
        await close_post()
        return "already"

    result = generate_comment(username=username, caption=caption, image=image)
    comment = (result.get("comment") or "").strip()
    relevant = bool(result.get("relevant"))
    print(f"  LLM relevant={relevant} reason={result.get('reason')!r}")
    print(f"  comment={comment!r}")

    if not comment:
        log_post(
            post_id=post_id,
            username=username,
            caption=caption,
            comment="",
            relevant=relevant,
        )
        await close_post()
        return "skipped"

    await submit(comment)
    log_post(
        post_id=post_id,
        username=username,
        caption=caption,
        comment=comment,
        relevant=relevant,
    )
    await close_post()
    return "commented"


async def main(target: int = 10, max_attempts: int = 40) -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    print(f"Profile: {BROWSER_DATA_DIR}")
    print(f"Log:     {LOG_PATH.resolve()} ({count_logged_comments()} comments so far)")
    ensure_llm_ready(ping=False)

    cdp, url = await connect()
    print(f"Connected: {cdp} | {url}")
    login = await login_if_needed()
    print(f"Login: {login}")
    await dismiss()

    posted = 0
    attempts = 0
    while posted < target and attempts < max_attempts:
        attempts += 1
        print(f"\n[{attempts}] seeking post ({posted}/{target} comments this run)…")
        try:
            status = await process_one_post()
        except Exception as exc:
            print(f"  ERROR: {exc}")
            try:
                await close_post()
            except Exception:
                pass
            continue

        if status == "commented":
            posted += 1
            print(f"  OK — {posted}/{target} comments this run")
        else:
            print(f"  → {status}")

    print(f"\nDone. Posted {posted}/{target} this run. Total logged comments: {count_logged_comments()}")
    await disconnect()
    print("Disconnected (Chrome CDP may stay open).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Loop Instagram comment flow until N posts are commented.")
    parser.add_argument("--target", type=int, default=10, help="Number of comments to post (default 10)")
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=40,
        help="Stop after this many post attempts even if target not reached",
    )
    args = parser.parse_args()
    asyncio.run(main(target=args.target, max_attempts=args.max_attempts))
