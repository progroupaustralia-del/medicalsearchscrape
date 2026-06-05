#!/usr/bin/env python3
"""
Scrape contacts (Name / Email / Phone / Address) from the MedicalSearch
supplier requests / accepted-leads area and write them to contacts.csv.

WHY THIS RUNS LOCALLY:
  The page is behind a login and behind bot protection. This script drives a
  real Chromium browser that YOU log into, so your credentials never leave your
  machine and are never stored by the script.

HOW IT FINDS CONTACTS (class-name independent):
  Each lead card on the page shows a name, a company + location line, a phone,
  and an email. Rather than depend on the site's HTML class names (which change
  and are easy to get wrong), this script locates every email on the page, then
  for each email picks the smallest surrounding block that also contains a phone
  number and a "(SUBURB, POSTCODE STATE)" location line, and reads the name from
  the line directly above that location. That makes it robust to layout changes.

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

    # Pagination: the script first tries clicking a "next" control. If the page
    # instead uses ?page=N in the URL, set use_page_param=True.
    "next_selector": "a[rel='next'], a[class*='next'], button[class*='next'], [aria-label*='Next']",
    "use_page_param": False,
    "page_param": "page",

    "max_pages": 200,            # hard safety cap
    "wait_after_load_ms": 1500,  # let JS-rendered content settle
    "headless": False,           # keep visible so you can log in
    "profile_dir": ".ms_profile",  # persistent browser profile (keeps you logged in)
    "output_csv": "contacts.csv",
    "debug_dump": True,          # write page_dump.html on page 1 for troubleshooting
}

# Python-side regexes (mirror the JS ones used in the browser).
PHONE_RE = re.compile(
    r"(?:\+?61[\s\-]?|\(?0\)?[\s\-]?)?(?:\(?0?[1-9]\)?[\s\-]?)?\d(?:[\s\-]?\d){7,9}"
)
LOCATION_RE = re.compile(r"\([^)]*\b\d{3,4}\b[^)]*\)")

# --------------------------------------------------------------------------- #
# In-browser extraction. Runs inside the page and returns one record per email.
# --------------------------------------------------------------------------- #
EXTRACT_JS = r"""
() => {
  const EMAIL_G = /[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}/g;
  const PHONE = /(?:\+?61[\s\-]?|\(?0\)?[\s\-]?)?(?:\(?0?[1-9]\)?[\s\-]?)?\d(?:[\s\-]?\d){7,9}/;
  const LOC = /\([^)]*\b\d{3,4}\b[^)]*\)/;
  const LABEL = /^(accepted\b|buyer|use:|type needed|procedures|wavelength|cooling|budget|send\s*quote|archive|view|reject|decline|message)/i;

  function nameAbove(text){
    const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
    let li = -1;
    for (let i = 0; i < lines.length; i++){ if (LOC.test(lines[i])){ li = i; break; } }
    if (li <= 0) return null;
    for (let i = li - 1; i >= 0; i--){
      const l = lines[i];
      if (/[0-9@]/.test(l)) continue;       // skip dates / emails
      if (LABEL.test(l)) continue;          // skip known labels / buttons
      if (l.length < 2 || l.length > 50) continue;
      return l;
    }
    return null;
  }

  const all = Array.from(document.querySelectorAll('body *'));
  const byEmail = {};
  for (const el of all){
    const t = el.innerText || '';
    if (t.indexOf('@') < 0) continue;
    const found = t.match(EMAIL_G);
    if (!found) continue;
    const uniq = [...new Set(found.map(e => e.toLowerCase()))];
    if (uniq.length !== 1) continue;        // skip containers holding many cards
    (byEmail[uniq[0]] = byEmail[uniq[0]] || []).push(el);
  }

  const out = [];
  for (const email in byEmail){
    // Smallest element first: walk from the tight email block outward.
    const chain = byEmail[email].sort((a, b) => a.innerText.length - b.innerText.length);
    let chosen = null, chosenName = null;
    for (const el of chain){
      const t = el.innerText;
      if (!PHONE.test(t)) continue;
      if (!LOC.test(t)) continue;
      const nm = nameAbove(t);
      if (nm){ chosen = el; chosenName = nm; break; }
    }
    if (!chosen){                            // fallback: smallest block with a phone
      for (const el of chain){ if (PHONE.test(el.innerText)){ chosen = el; break; } }
    }
    if (!chosen) chosen = chain[chain.length - 1];
    out.push({ email: email, name: chosenName || '', text: chosen.innerText });
  }
  return out;
}
"""


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


def parse_cards(page) -> list[Contact]:
    """Run the in-browser extractor and turn each record into a Contact."""
    records = page.evaluate(EXTRACT_JS)
    out: list[Contact] = []
    for r in records:
        text = r.get("text", "")
        c = Contact(email=(r.get("email", "") or "").strip(), name=(r.get("name", "") or "").strip())

        # Phone: first regex match in the card text that passes a length check.
        for m in PHONE_RE.finditer(text):
            cleaned = _clean_phone(m.group(0))
            if cleaned:
                c.phone = cleaned
                break

        # Address: the company + "(SUBURB, POSTCODE STATE)" line.
        for line in text.splitlines():
            line = line.strip()
            if line and LOCATION_RE.search(line):
                c.address = " ".join(line.split())
                break

        out.append(c)
    return out


def auto_scroll(page):
    """Scroll to the bottom a few times so lazy-loaded cards render."""
    last = 0
    for _ in range(20):
        height = page.evaluate("document.body.scrollHeight")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.4)
        if height == last:
            break
        last = height
    page.evaluate("window.scrollTo(0, 0)")


def goto_next(page, page_index) -> bool:
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

        if "login" in page.url.lower() or "signin" in page.url.lower():
            input(
                "\n>> Please LOG IN in the browser window, navigate to the page "
                "with the leads\n   you want, then come back here and press Enter "
                "to start scraping... "
            )
            time.sleep(CONFIG["wait_after_load_ms"] / 1000)

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
                    with open("page_dump.html", "w", encoding="utf-8") as fh:
                        fh.write(page.content())
                    # Plain readable text of the page — small and easy to share.
                    txt = page.evaluate("document.body.innerText") or ""
                    with open("page_text.txt", "w", encoding="utf-8") as fh:
                        fh.write(txt[:60000])
                    print("  [debug] wrote page_text.txt — paste it to tune extraction.")
                except Exception as e:
                    print(f"  [debug] dump failed: {e}")

            cards = parse_cards(page)
            new_on_page = 0
            for c in cards:
                key = (c.email or f"{c.name}|{c.phone}").lower()
                if not key.strip("|") or key in seen:
                    continue
                seen.add(key)
                contacts.append(c)
                new_on_page += 1
            print(f"  found {len(cards)} cards, +{new_on_page} new (total {len(contacts)})")

            if new_on_page == 0 and page_index > 1:
                print("  no new contacts — stopping.")
                break
            if not goto_next(page, page_index):
                print("  no next page — done.")
                break
            time.sleep(0.8)

        ctx.close()

    cols = [f.name for f in fields(Contact)]
    with open(CONFIG["output_csv"], "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for c in contacts:
            w.writerow(asdict(c))

    print(f"\nDone. Wrote {len(contacts)} contacts to {CONFIG['output_csv']}")
    if not contacts:
        print(
            "No contacts captured. If you can see leads on the page, the layout may\n"
            "differ from what's expected — paste me the contents of page_dump.html\n"
            "(run:  cat page_dump.html | pbcopy  then paste here) and I'll adjust."
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
