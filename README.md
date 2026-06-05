# MedicalSearch requests scraper

Scrapes contacts (**Name / Email / Phone / Address**) from the MedicalSearch
supplier requests area (`https://sma.medicalsearch.com.au/requests`) and writes
them to `contacts.csv`.

## Why it runs on your machine

The target page is **behind a login** and **behind bot/WAF protection**. The
reliable way past both is to drive a real browser that *you* log into. Because of
that, this scraper is designed to run locally:

- Your credentials stay on your computer — you type them into the browser the
  script opens. The script never asks for, sees, or stores your password.
- A persistent browser profile (`.ms_profile/`) keeps you logged in between runs.

> Only run this against a site you are authorised to scrape, and stay within its
> terms of use.

## Setup (one time)

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Run

```bash
python scrape_requests.py
```

1. A Chromium window opens. If you land on a login page, log in by hand, then
   return to the terminal and press **Enter**.
2. The script walks every page of results and extracts the contacts.
3. Results are written to `contacts.csv`.

## Tuning selectors

Everything you'd normally need to change lives in the `CONFIG` block at the top of
`scrape_requests.py`. After your first run, right-click a listing in the browser →
**Inspect**, copy the class/selector for one listing row, and drop it into
`CONFIG["item_selectors"]` (and the name/address selectors as needed).

If the first run produces an empty or messy CSV, paste the page's HTML (or a single
listing element) back to me and I'll set the selectors precisely.

## Files

- `scrape_requests.py` — the scraper.
- `requirements.txt` — Python dependencies.
- `contacts.csv` — output (created on run; git-ignored).
