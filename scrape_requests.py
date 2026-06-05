#!/usr/bin/env python3
"""
Scrape your ACCEPTED leads (Name / Email / Phone / Address) from MedicalSearch
and write them to contacts.csv.

IMPORTANT — which tab to use:
  MedicalSearch HIDES the buyer's phone and email on the "Invited" tab
  (you'll see things like "0402 77..." and "name@gm..." with "Accept to view
  & quote"). The FULL details only show on the "Accepted" tab — the leads you
  have already accepted. This script switches to the Accepted tab for you and
  pauses so you can confirm full details are visible before it scrapes.

WHY IT RUNS LOCALLY:
  The page is behind your login. This drives a real browser that YOU log into;
  your credentials never leave your machine and are never stored by the script.

SETUP (one time):
    pip3 install -r requirements.txt
    python3 -m playwright install chromium

RUN:
    python3 scrape_requests.py
"""

from __future__ import annotations

import csv
import re
import sys
import time
from dataclasses import dataclass, asdict, fields

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #
CONFIG = {
    "start_url": "https://sma.medicalsearch.com.au/requests",
    "tab": "Accepted",           # which sub-tab to scrape: Accepted / Invited / Archived
    "max_pages": 100,            # hard safety cap
    "wait_after_load_ms": 1500,  # let JS-rendered content settle
    "headless": False,           # keep visible so you can log in / confirm the tab
    "profile_dir": ".ms_profile",  # persistent browser profile (keeps you logged in)
    "output_csv": "contacts.csv",
    "debug_dump": True,          # write page_text.txt on page 1 for troubleshooting
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(
    r"(?:\+?61[\s\-]?|\(?0\)?[\s\-]?)?(?:\(?0?[1-9]\)?[\s\-]?)?\d(?:[\s\-]?\d){7,9}"
)
# A lead's location line, e.g. "Clinic Name (SUBURB, 3123 VIC)" or "(6053 WA)".
# Requires a postcode followed by an Australian state (or "United States"), which
# avoids matching parenthetical numbers inside the buyer's message / specs.
LOCATION_RE = re.compile(
    r"\([^)]*\b\d{3,5}\s+(?:NSW|VIC|QLD|SA|WA|TAS|NT|ACT|United States)\s*\)"
)


@dataclass
class Contact:
    name: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""


def _clean_phone(raw: str) -> str:
    only_digits = re.sub(r"\D", "", raw)
    if 8 <= len(only_digits) <= 12:
        return " ".join(raw.split())
    return ""


def parse_cards(page):
    """
    Parse the page's visible text into leads.

    Each lead card renders as a run of lines like:
        <title>
        <date>
        <name>
        <company> (<suburb>, <postcode> <state>)   <- located by LOCATION_RE
        <phone>
        <email>
        Buyer's Message: ...
    We anchor on the location line: the name is the line directly above it, and
    the phone/email are the next lines below it (before "Buyer's Message").

    Returns (contacts, masked_count) where masked_count counts cards whose
    details are hidden ("Accept to view & quote") — a sign of the wrong tab.
    """
    text = page.evaluate("document.body.innerText") or ""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    n = len(lines)

    contacts = []
    masked = 0
    for i in range(n):
        if not LOCATION_RE.search(lines[i]):
            continue
        address = lines[i]
        name = lines[i - 1] if i >= 1 else ""

        phone = ""
        email = ""
        is_masked = False
        for j in range(i + 1, min(i + 10, n)):
            lj = lines[j]
            if LOCATION_RE.search(lj) or lj.lower().startswith("buyer's message"):
                break
            if "accept to view" in lj.lower():
                is_masked = True
            if not phone:
                m = PHONE_RE.search(lj)
                if m:
                    cleaned = _clean_phone(m.group(0))
                    if cleaned:
                        phone = cleaned
            if not email and "@" in lj:
                em = EMAIL_RE.search(lj)
                if em:
                    email = em.group(0)
                else:
                    is_masked = True  # truncated like "name@gm..."
            if phone and email:
                break

        if is_masked:
            masked += 1
        # Keep the card if we got at least a name + a location.
        if name and address:
            contacts.append(Contact(name=name, email=email, phone=phone, address=address))

    return contacts, masked


def auto_scroll(page):
    """Scroll down repeatedly so lazy-loaded cards render, then back to top."""
    last = 0
    for _ in range(25):
        height = page.evaluate("document.body.scrollHeight")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.4)
        if height == last:
            break
        last = height
    page.evaluate("window.scrollTo(0, 0)")


def click_exact(page, label) -> bool:
    """Click the first link/button/tab whose trimmed text equals `label`."""
    xpath = (
        "xpath=//*[self::a or self::button or self::li or self::span or self::div]"
        f"[normalize-space(text())='{label}']"
    )
    try:
        el = page.query_selector(xpath)
        if el:
            el.click()
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PWTimeout:
                pass
            time.sleep(1.2)
            return True
    except Exception as e:
        print(f"  click '{label}' failed: {e}")
    return False


def goto_next(page) -> bool:
    """Click the pagination 'Next' control if it exists and is enabled."""
    try:
        el = page.query_selector(
            "xpath=//a[normalize-space()='Next'] | //button[normalize-space()='Next']"
        )
        if not el:
            return False
        cls = (el.get_attribute("class") or "").lower()
        parent_cls = ""
        try:
            parent_cls = (el.evaluate("e => e.parentElement ? e.parentElement.className : ''") or "").lower()
        except Exception:
            pass
        if "disabled" in cls or "disabled" in parent_cls:
            return False
        el.click()
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            pass
        time.sleep(1.2)
        return True
    except Exception as e:
        print(f"  next-page click failed: {e}")
        return False


def main():
    seen = set()
    contacts: list[Contact] = []
    total_masked = 0

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

        if "login" in page.url.lower() or "signin" in page.url.lower():
            input("\n>> Please LOG IN in the browser window, then press Enter... ")
            time.sleep(CONFIG["wait_after_load_ms"] / 1000)

        # Switch to the desired tab (Accepted) so full details are visible.
        if CONFIG.get("tab"):
            print(f"Selecting the '{CONFIG['tab']}' tab ...")
            click_exact(page, CONFIG["tab"])

        input(
            f"\n>> The page should now show your {CONFIG.get('tab','')} leads with FULL\n"
            "   emails and phone numbers (not '...' / 'Accept to view'). If not, click\n"
            f"   the '{CONFIG.get('tab','Accepted')}' tab yourself now.\n"
            "   Press Enter to start scraping... "
        )

        for page_index in range(1, CONFIG["max_pages"] + 1):
            print(f"\nPage {page_index}: {page.url}")
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PWTimeout:
                pass
            time.sleep(CONFIG["wait_after_load_ms"] / 1000)
            auto_scroll(page)

            if page_index == 1 and CONFIG.get("debug_dump"):
                try:
                    txt = page.evaluate("document.body.innerText") or ""
                    with open("page_text.txt", "w", encoding="utf-8") as fh:
                        fh.write(txt[:60000])
                except Exception as e:
                    print(f"  [debug] dump failed: {e}")

            cards, masked = parse_cards(page)
            total_masked += masked
            new_on_page = 0
            for c in cards:
                key = (c.email or f"{c.name}|{c.phone}|{c.address}").lower()
                if key in seen:
                    continue
                seen.add(key)
                contacts.append(c)
                new_on_page += 1
            print(
                f"  found {len(cards)} cards, +{new_on_page} new "
                f"(total {len(contacts)}){', some HIDDEN' if masked else ''}"
            )

            if new_on_page == 0 and page_index > 1:
                print("  no new contacts — stopping.")
                break
            if not goto_next(page):
                print("  no next page — done.")
                break

        ctx.close()

    cols = [f.name for f in fields(Contact)]
    with open(CONFIG["output_csv"], "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for c in contacts:
            w.writerow(asdict(c))

    print(f"\nDone. Wrote {len(contacts)} contacts to {CONFIG['output_csv']}")

    with_email = sum(1 for c in contacts if c.email)
    if total_masked and with_email < len(contacts) / 2:
        print(
            "\n!! WARNING: many leads had HIDDEN details ('Accept to view & quote').\n"
            "   You're probably on the 'Invited' tab. Re-run and make sure the\n"
            "   'Accepted' tab is selected — that's where full emails/phones show."
        )
    elif contacts:
        print(f"   ({with_email} of {len(contacts)} have an email address.)")
    if not contacts:
        print(
            "No leads found. If you can see leads on the page, run\n"
            "   cat page_text.txt | pbcopy\n"
            "and paste the result so the parser can be adjusted."
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
