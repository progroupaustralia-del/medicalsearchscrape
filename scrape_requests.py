#!/usr/bin/env python3
"""
Scrape contacts (Name / Email / Phone / Address) from the MedicalSearch
supplier "requests" area and write them to contacts.csv.

WHY THIS RUNS LOCALLY (not in CI / not on a server you don't control):
  - The target page (https://sma.medicalsearch.com.au/requests) sits behind a
    login and behind bot/WAF protection. The reliable way past both is to drive
    a *real* browser that YOU log into. Your credentials stay on your machine —
    they are never typed into this script and never stored by it.

HOW IT WORKS:
  1. Opens a real Chromium window using a *persistent* profile (so you only have
     to log in once; the session is reused on later runs).
  2. On the first run it pauses and waits for you to log in by hand, then press
     Enter in the terminal.
  3. Navigates to the requests listing, walks every page of results, and pulls
     out Name / Email / Phone / Address from each row.
  4. De-duplicates and writes contacts.csv.

SETUP (one time):
    python3 -m venv .venv
    source .venv/bin/activate            # Windows: .venv\\Scripts\\activate
    pip install -r requirements.txt
    python -m playwright install chromium

RUN:
    python scrape_requests.py

TUNING THE SELECTORS:
  The CONFIG block below is the only thing you should normally need to touch.
  After your first run, open the page in your browser, right-click a listing,
  choose "Inspect", and copy the CSS selector / class names into CONFIG. If you
  paste that HTML back to me I'll set these precisely for you.
"""

from __future__ import annotations

import csv
import re
import sys
import time
from dataclasses import dataclass, asdict, fields

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# --------------------------------------------------------------------------- #
# CONFIG — adjust these to match the real page once you can see its HTML.
# --------------------------------------------------------------------------- #
CONFIG = {
    # Where the listings live.
    "start_url": "https://sma.medicalsearch.com.au/requests",

    # CSS selector that matches ONE listing/row. The defaults below are common
    # patterns; replace with the real one after inspecting the page. Multiple
    # candidates are tried in order until one matches something.
    "item_selectors": [
        "[class*='request']",
        "article",
        "li[class*='card']",
        "div[class*='card']",
        "tr",
    ],

    # Within a single item, where to read the name from. First match wins.
    "name_selectors": ["h1", "h2", "h3", "h4", "a[class*='title']", "[class*='name']", "[class*='title']"],

    # Within a single item, where to read the address from (optional; falls back
    # to a heuristic over the item's text if none match).
    "address_selectors": ["[class*='address']", "[class*='location']", "address"],

    # How to advance to the next page. The script tries, in order:
    #   1) clicking a "next" control matching next_selector
    #   2) appending ?page=N to start_url (set use_page_param=True)
    "next_selector": "a[rel='next'], a[class*='next'], button[class*='next'], [aria-label*='Next']",
    "use_page_param": False,
    "page_param": "page",

    "max_pages": 200,          # hard safety cap
    "wait_after_load_ms": 1500,  # let JS-rendered content settle
    "headless": False,          # keep visible so you can log in / solve any challenge
    "profile_dir": ".ms_profile",  # persistent browser profile (keeps you logged in)
    "output_csv": "contacts.csv",
    "debug_dump": True,         # write page_dump.html + sample_item.html on page 1
}

# --------------------------------------------------------------------------- #
# Extraction helpers
# --------------------------------------------------------------------------- #
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Australian-friendly phone matcher: mobiles (04xx), landlines with optional
# area code, +61 international, and common separators / brackets.
PHONE_RE = re.compile(
    r"(?:\+?61[\s\-]?|\(?0\)?[\s\-]?)?(?:\(?0?[1-9]\)?[\s\-]?)?\d(?:[\s\-]?\d){7,9}"
)

# A loose address heuristic: a line containing a street-type word or an
# Australian state + 4-digit postcode.
ADDRESS_HINT_RE = re.compile(
    r"(?i)\b(?:unit|suite|level|floor|p\.?o\.?\s*box|"
    r"st(?:reet)?|rd|road|ave|avenue|dr(?:ive)?|hwy|highway|"
    r"ln|lane|ct|court|pl(?:ace)?|tce|terrace|cres(?:cent)?|blvd|parade|pde)\b"
    r"|\b(?:NSW|VIC|QLD|SA|WA|TAS|NT|ACT)\b\s*\d{4}"
)


@dataclass
class Contact:
    name: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""

    def is_empty(self) -> bool:
        return not any((self.name, self.email, self.phone, self.address))

    def key(self):
        # De-dup key: prefer email, else name+phone.
        return (self.email or "").lower() or f"{self.name}|{self.phone}".lower()


def _first_text(item, selectors) -> str:
    for sel in selectors:
        try:
            el = item.query_selector(sel)
            if el:
                txt = (el.inner_text() or "").strip()
                if txt:
                    return " ".join(txt.split())
        except Exception:
            continue
    return ""


def _clean_phone(raw: str) -> str:
    digits = re.sub(r"[^\d+]", "", raw)
    # Require a plausible AU length (8-12 incl. country code) to avoid matching
    # random numbers like prices or IDs.
    only_digits = re.sub(r"\D", "", digits)
    if 8 <= len(only_digits) <= 12:
        return raw.strip()
    return ""


def extract_contact(item) -> Contact:
    """Pull a Contact out of a single listing element."""
    full_text = ""
    try:
        full_text = item.inner_text() or ""
    except Exception:
        pass

    c = Contact()

    # Name
    c.name = _first_text(item, CONFIG["name_selectors"])

    # Email — prefer mailto links, then regex over text.
    try:
        mailto = item.query_selector("a[href^='mailto:']")
        if mailto:
            href = mailto.get_attribute("href") or ""
            c.email = href.split("mailto:", 1)[-1].split("?")[0].strip()
    except Exception:
        pass
    if not c.email:
        m = EMAIL_RE.search(full_text)
        if m:
            c.email = m.group(0)

    # Phone — prefer tel: links, then regex.
    try:
        tel = item.query_selector("a[href^='tel:']")
        if tel:
            href = tel.get_attribute("href") or ""
            c.phone = href.split("tel:", 1)[-1].strip()
    except Exception:
        pass
    if not c.phone:
        for m in PHONE_RE.finditer(full_text):
            cleaned = _clean_phone(m.group(0))
            if cleaned:
                c.phone = cleaned
                break

    # Address — selector first, then heuristic line scan.
    c.address = _first_text(item, CONFIG["address_selectors"])
    if not c.address:
        for line in (l.strip() for l in full_text.splitlines()):
            if line and ADDRESS_HINT_RE.search(line):
                c.address = " ".join(line.split())
                break

    return c


# --------------------------------------------------------------------------- #
# Page walking
# --------------------------------------------------------------------------- #
def find_items(page):
    """Return the list of listing elements using the first selector that hits."""
    for sel in CONFIG["item_selectors"]:
        items = page.query_selector_all(sel)
        if items and len(items) >= 1:
            # Avoid catching the whole document with an overly broad selector:
            # require at least 2 matches OR a selector that's clearly specific.
            if len(items) >= 2 or "request" in sel or "card" in sel:
                print(f"  using item selector: {sel!r}  ({len(items)} matches)")
                return items
    return []


def goto_next(page, page_index) -> bool:
    """Advance to the next page. Returns True if it navigated, False if done."""
    if CONFIG["use_page_param"]:
        sep = "&" if "?" in CONFIG["start_url"] else "?"
        url = f"{CONFIG['start_url']}{sep}{CONFIG['page_param']}={page_index + 1}"
        page.goto(url, wait_until="domcontentloaded")
        return True

    try:
        nxt = page.query_selector(CONFIG["next_selector"])
        if nxt:
            disabled = (nxt.get_attribute("disabled") is not None) or (
                "disabled" in (nxt.get_attribute("class") or "")
            )
            if disabled:
                return False
            nxt.click()
            page.wait_for_load_state("domcontentloaded")
            return True
    except Exception as e:
        print(f"  next-page click failed: {e}")
    return False


def main():
    out_path = CONFIG["output_csv"]
    seen = set()
    contacts: list[Contact] = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=CONFIG["profile_dir"],
            headless=CONFIG["headless"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print(f"Opening {CONFIG['start_url']} ...")
        page.goto(CONFIG["start_url"], wait_until="domcontentloaded")
        time.sleep(CONFIG["wait_after_load_ms"] / 1000)

        # If we got bounced to a login page, wait for the human.
        if "login" in page.url.lower() or "signin" in page.url.lower():
            input(
                "\n>> Please LOG IN in the browser window, navigate to the "
                "requests page if needed,\n   then come back here and press "
                "Enter to start scraping... "
            )
            page.goto(CONFIG["start_url"], wait_until="domcontentloaded")
            time.sleep(CONFIG["wait_after_load_ms"] / 1000)

        for page_index in range(1, CONFIG["max_pages"] + 1):
            print(f"\nPage {page_index}: {page.url}")
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PWTimeout:
                pass
            time.sleep(CONFIG["wait_after_load_ms"] / 1000)

            items = find_items(page)
            if not items:
                print("  no listings found on this page — stopping.")
                break

            # DEBUG: on the first page, dump HTML so the selectors can be tuned
            # against the real structure. These files are git-ignored.
            if page_index == 1 and CONFIG.get("debug_dump"):
                try:
                    with open("page_dump.html", "w", encoding="utf-8") as fh:
                        fh.write(page.content())
                    sample_html = items[0].evaluate("el => el.outerHTML")
                    with open("sample_item.html", "w", encoding="utf-8") as fh:
                        fh.write(sample_html)
                    print(
                        "  [debug] wrote page_dump.html (full page) and "
                        "sample_item.html (first listing). Send sample_item.html "
                        "to tune selectors."
                    )
                except Exception as e:
                    print(f"  [debug] dump failed: {e}")

            new_on_page = 0
            for it in items:
                c = extract_contact(it)
                if c.is_empty():
                    continue
                k = c.key()
                if k in seen:
                    continue
                seen.add(k)
                contacts.append(c)
                new_on_page += 1
            print(f"  +{new_on_page} new contacts (total {len(contacts)})")

            if not goto_next(page, page_index):
                print("  no next page — done.")
                break
            time.sleep(0.8)  # be polite

        ctx.close()

    # Write CSV
    cols = [f.name for f in fields(Contact)]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for c in contacts:
            w.writerow(asdict(c))

    print(f"\nDone. Wrote {len(contacts)} contacts to {out_path}")
    if not contacts:
        print(
            "No contacts captured. The selectors in CONFIG almost certainly need\n"
            "to match the real page — inspect a listing element and update\n"
            "CONFIG['item_selectors'] / ['name_selectors'] etc. (or paste the\n"
            "page HTML and I'll set them for you)."
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
