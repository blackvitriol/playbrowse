# playbrowse

Instagram feed commenting with Playwright + LM Studio.

## Setup

```cmd
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
playwright install chromium
```

Copy `.env` and set:

- `LM_STUDIO_BASE_URL` / `LM_STUDIO_API_KEY` / `LM_STUDIO_MODEL`
- `INSTA_ID` / `INSTA_PASS`
- optional: `WINDOW_WIDTH`, `WINDOW_HEIGHT`, `CDP_PORT`

## Run

```cmd
python main_test.py --target 10
```

Or open `manual_instagram.ipynb` (kernel = this `.venv`).

Chrome opens via CDP using `browser_data/` (gitignored). Comments are logged to `comment_log.json` / `.csv` (gitignored).
