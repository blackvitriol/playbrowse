"""Self-contained config for PlayBrowse (Instagram + Playwright).

All paths (.env, browser_data, logs, .cdp_url) live in the project root.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
HERE = ROOT


def load_env() -> None:
    """Load .env from this folder as UTF-8 (Windows-safe)."""
    env_path = ROOT / ".env"
    if env_path.is_file():
        load_dotenv(env_path, encoding="utf-8", override=True)
    else:
        load_dotenv(encoding="utf-8")


load_env()

BASE_URL = os.getenv("LM_STUDIO_BASE_URL")
API_KEY = os.getenv("LM_STUDIO_API_KEY")
MODEL = os.getenv("LM_STUDIO_MODEL") or "local-model"
HEADLESS = os.getenv("HEADLESS", "false").lower() in ("1", "true", "yes")

INSTA_ID = (os.getenv("INSTA_ID") or os.getenv("insta_id") or "").strip().strip('"').strip("'")
INSTA_PASS = (os.getenv("INSTA_PASS") or os.getenv("insta_pass") or "").strip().strip('"').strip("'")

_raw_browser = os.getenv("BROWSER_DATA_DIR", "browser_data")
BROWSER_DATA_DIR = Path(_raw_browser)
if not BROWSER_DATA_DIR.is_absolute():
    BROWSER_DATA_DIR = (ROOT / BROWSER_DATA_DIR).resolve()
else:
    BROWSER_DATA_DIR = BROWSER_DATA_DIR.resolve()

CDP_URL = (os.getenv("CDP_URL") or "").strip() or None
CDP_PORT = int(os.getenv("CDP_PORT", "9222"))
_cdp_state = os.getenv("CDP_STATE_FILE", ".cdp_url")
CDP_STATE_FILE = Path(_cdp_state)
if not CDP_STATE_FILE.is_absolute():
    CDP_STATE_FILE = (ROOT / CDP_STATE_FILE).resolve()
else:
    CDP_STATE_FILE = CDP_STATE_FILE.resolve()

CHROME_PATH = (os.getenv("CHROME_PATH") or "").strip() or None
WINDOW_WIDTH = int(os.getenv("WINDOW_WIDTH", "1280"))
WINDOW_HEIGHT = int(os.getenv("WINDOW_HEIGHT", "900"))

LOG_PATH = HERE / "comment_log.json"
CSV_PATH = HERE / "comment_log.csv"


def _models_url() -> str:
    if not BASE_URL:
        raise RuntimeError("LM_STUDIO_BASE_URL is not set in .env")
    return f"{BASE_URL.rstrip('/')}/models"


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY or 'lm-studio'}"}


def list_loaded_chat_models() -> list[str]:
    try:
        r = httpx.get(_models_url(), headers=_auth_headers(), timeout=10)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"LM Studio is not reachable at {BASE_URL}. "
            "Start LM Studio, load a model, and enable the local server."
        ) from exc

    models = r.json().get("data", [])
    return [m["id"] for m in models if "embed" not in m.get("id", "").lower()]


def get_model() -> str:
    loaded = list_loaded_chat_models()
    if MODEL and MODEL != "local-model":
        if MODEL not in loaded:
            raise RuntimeError(
                f"Configured model '{MODEL}' is not loaded in LM Studio. "
                f"Loaded: {loaded or '(none)'}"
            )
        return MODEL
    if not loaded:
        raise RuntimeError(
            f"No chat model loaded in LM Studio at {BASE_URL}. "
            "Load a model in LM Studio before running."
        )
    return loaded[0]


def ensure_llm_ready(*, ping: bool = True) -> str:
    model = get_model()
    print(f"LM Studio OK: {BASE_URL} | model: {model}")

    if not ping:
        return model

    chat_url = f"{BASE_URL.rstrip('/')}/chat/completions"
    try:
        r = httpx.post(
            chat_url,
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK"}],
                "max_tokens": 8,
                "temperature": 0,
            },
            timeout=60,
        )
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Model '{model}' is listed but did not respond at {chat_url}. "
            "Wait until LM Studio finishes loading, then retry."
        ) from exc

    content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content")
    if not content:
        raise RuntimeError(f"Model '{model}' returned an empty response. Is it fully loaded?")

    print(f"Model ping OK: {content.strip()[:80]}")
    return model


def probe_cdp(url: str, timeout: float = 1.5) -> bool:
    try:
        r = httpx.get(f"{url.rstrip('/')}/json/version", timeout=timeout)
        return r.status_code == 200 and "webSocketDebuggerUrl" in r.text
    except Exception:
        return False


def find_open_cdp_url() -> str | None:
    candidates: list[str] = []
    if CDP_URL:
        candidates.append(CDP_URL)
    if CDP_STATE_FILE.is_file():
        saved = CDP_STATE_FILE.read_text(encoding="utf-8").strip()
        if saved:
            candidates.append(saved)
    candidates.append(f"http://127.0.0.1:{CDP_PORT}")
    candidates.append("http://127.0.0.1:9242")
    for port in range(9222, 9232):
        candidates.append(f"http://127.0.0.1:{port}")

    seen: set[str] = set()
    for url in candidates:
        key = url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        if probe_cdp(key):
            return key
    return None


def find_chrome_executable() -> str:
    if CHROME_PATH and Path(CHROME_PATH).is_file():
        return CHROME_PATH

    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Chromium/Application/chrome.exe",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)

    pw_root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if pw_root.is_dir():
        matches = sorted(pw_root.glob("chromium-*/chrome-win64/chrome.exe")) + sorted(
            pw_root.glob("chromium-*/chrome-win/chrome.exe")
        )
        if matches:
            return str(matches[-1])

    raise RuntimeError(
        "Chrome executable not found. Install Google Chrome or set CHROME_PATH in .env"
    )


def launch_chrome_with_cdp() -> str:
    import subprocess
    import time

    BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    cdp_url = f"http://127.0.0.1:{CDP_PORT}"

    if probe_cdp(cdp_url):
        CDP_STATE_FILE.write_text(cdp_url, encoding="utf-8")
        return cdp_url

    chrome = find_chrome_executable()
    args = [
        chrome,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={BROWSER_DATA_DIR}",
        f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-popup-blocking",
    ]
    if HEADLESS:
        args.append("--headless=new")

    print(f"Launching Chrome with CDP on port {CDP_PORT}...")
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )

    for _ in range(40):
        if probe_cdp(cdp_url):
            CDP_STATE_FILE.write_text(cdp_url, encoding="utf-8")
            print(f"Chrome CDP ready: {cdp_url}")
            return cdp_url
        time.sleep(0.25)

    raise RuntimeError(
        f"Chrome did not open CDP on {cdp_url}. "
        "Close other Chrome windows using this profile, then retry."
    )


def ensure_browser_cdp() -> str:
    existing = find_open_cdp_url()
    if existing:
        print(f"Reusing open browser session: {existing}")
        CDP_STATE_FILE.write_text(existing, encoding="utf-8")
        return existing
    return launch_chrome_with_cdp()
