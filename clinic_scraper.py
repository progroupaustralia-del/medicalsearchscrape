#!/usr/bin/env python3
"""
Melbourne clinic contact scraper.

Pulls medical clinics / doctors / healthcare facilities from OpenStreetMap
(via the public Overpass API) and extracts their phone numbers. For clinics
that publish a website but no email in OSM, it politely visits the site
(homepage + a few likely contact pages) to find published email addresses.

Design goals:
  * No API key required.
  * Respect robots.txt and rate-limit every request.
  * Only collect contact details that the business has published publicly.

Usage:
    python clinic_scraper.py --output clinics.csv
    python clinic_scraper.py --area melbourne-metro --max-website-crawl 200
    python clinic_scraper.py --bbox -38.05 144.55 -37.55 145.55
    python clinic_scraper.py --no-website-crawl   # OSM data only, fastest
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field, asdict

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("The 'requests' package is required. Run: pip install -r requirements.txt")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

USER_AGENT = (
    "MelbourneClinicScraper/1.0 "
    "(+https://github.com/progroupaustralia-del/medicalsearchscrape; "
    "contact: progroupaustralia@gmail.com)"
)

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Named bounding boxes: (south, west, north, east)
AREAS = {
    # Greater Melbourne metropolitan area (roughly)
    "melbourne-metro": (-38.20, 144.45, -37.50, 145.55),
    # Inner Melbourne / CBD and surrounds
    "melbourne-cbd": (-37.86, 144.93, -37.77, 145.02),
}

# OSM tags that identify a medical clinic-like facility.
OSM_SELECTORS = [
    '["amenity"="clinic"]',
    '["amenity"="doctors"]',
    '["healthcare"="clinic"]',
    '["healthcare"="doctor"]',
    '["healthcare"="centre"]',
]

# Pages we'll check on a clinic website when hunting for an email.
CONTACT_PATHS = ["", "/contact", "/contact-us", "/contacts", "/about", "/about-us"]

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+"
)

# Email addresses that are almost never a real clinic contact.
EMAIL_BLOCKLIST_SUBSTR = (
    "example.com",
    "sentry.io",
    "wixpress.com",
    "@2x",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    "your@email",
    "email@",
    "@domain",
)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Clinic:
    name: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    street: str = ""
    suburb: str = ""
    postcode: str = ""
    lat: float | None = None
    lon: float | None = None
    osm_id: str = ""
    email_source: str = ""  # "osm" or "website" or ""

    @property
    def address(self) -> str:
        parts = [self.street, self.suburb, self.postcode]
        return ", ".join(p for p in parts if p)


# --------------------------------------------------------------------------- #
# OpenStreetMap / Overpass
# --------------------------------------------------------------------------- #

def build_overpass_query(bbox: tuple[float, float, float, float]) -> str:
    """Build an Overpass QL query for clinic-like nodes/ways in the bbox."""
    s, w, n, e = bbox
    bbox_str = f"{s},{w},{n},{e}"
    parts = []
    for sel in OSM_SELECTORS:
        for kind in ("node", "way", "relation"):
            parts.append(f"  {kind}{sel}({bbox_str});")
    body = "\n".join(parts)
    return f"[out:json][timeout:120];\n(\n{body}\n);\nout center tags;"


def fetch_osm(session: requests.Session, bbox, retries: int = 3) -> list[dict]:
    """Run the Overpass query, trying mirrors and backing off on failure."""
    query = build_overpass_query(bbox)
    last_err = None
    for attempt in range(retries):
        endpoint = OVERPASS_ENDPOINTS[attempt % len(OVERPASS_ENDPOINTS)]
        try:
            resp = session.post(endpoint, data={"data": query}, timeout=180)
            if resp.status_code == 200:
                return resp.json().get("elements", [])
            last_err = f"HTTP {resp.status_code} from {endpoint}"
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        wait = 2 ** attempt
        print(f"  Overpass attempt {attempt + 1} failed ({last_err}); retrying in {wait}s...",
              file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError(f"Overpass API unavailable: {last_err}")


def parse_osm_element(el: dict) -> Clinic | None:
    tags = el.get("tags", {})
    name = tags.get("name") or tags.get("operator") or ""
    if not name:
        return None  # unnamed point of care is rarely actionable

    phone = (tags.get("phone") or tags.get("contact:phone")
             or tags.get("phone:mobile") or "")
    email = tags.get("email") or tags.get("contact:email") or ""
    website = tags.get("website") or tags.get("contact:website") or tags.get("url") or ""

    lat = el.get("lat") or el.get("center", {}).get("lat")
    lon = el.get("lon") or el.get("center", {}).get("lon")

    street_parts = [tags.get("addr:housenumber", ""), tags.get("addr:street", "")]
    street = " ".join(p for p in street_parts if p).strip()

    return Clinic(
        name=name.strip(),
        phone=normalize_phone(phone),
        email=email.strip().lower(),
        website=normalize_url(website),
        street=street,
        suburb=(tags.get("addr:suburb") or tags.get("addr:city") or "").strip(),
        postcode=tags.get("addr:postcode", "").strip(),
        lat=lat,
        lon=lon,
        osm_id=f"{el.get('type')}/{el.get('id')}",
        email_source="osm" if email else "",
    )


# --------------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------------- #

def normalize_phone(raw: str) -> str:
    if not raw:
        return ""
    # OSM sometimes packs multiple numbers separated by ; — keep the first.
    raw = raw.split(";")[0].strip()
    return re.sub(r"\s{2,}", " ", raw)


def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def is_plausible_email(addr: str) -> bool:
    addr = addr.lower()
    if any(bad in addr for bad in EMAIL_BLOCKLIST_SUBSTR):
        return False
    # reject things that are clearly file hashes / tracking ids
    local = addr.split("@")[0]
    return len(local) <= 64 and not local.isdigit()


# --------------------------------------------------------------------------- #
# Website email extraction (polite)
# --------------------------------------------------------------------------- #

class RobotsCache:
    """Caches robots.txt parsers per host so we ask each site only once."""

    def __init__(self, session: requests.Session):
        self.session = session
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        if host not in self._cache:
            rp = urllib.robotparser.RobotFileParser()
            try:
                resp = self.session.get(host + "/robots.txt", timeout=15)
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    rp = None  # no robots.txt => allowed
            except requests.RequestException:
                rp = None
            self._cache[host] = rp
        rp = self._cache[host]
        if rp is None:
            return True
        return rp.can_fetch(USER_AGENT, url)


def extract_emails_from_html(content: str) -> list[str]:
    content = html.unescape(content)
    found: list[str] = []
    seen = set()

    # mailto: links are the most reliable signal
    for m in re.findall(r'mailto:([^"\'>\s?]+)', content, flags=re.IGNORECASE):
        m = urllib.parse.unquote(m).strip().lower()
        if m and m not in seen and is_plausible_email(m):
            seen.add(m)
            found.append(m)

    for m in EMAIL_RE.findall(content):
        m = m.strip().lower()
        if m not in seen and is_plausible_email(m):
            seen.add(m)
            found.append(m)
    return found


def find_email_on_website(session, robots: RobotsCache, base_url: str,
                          delay: float) -> str:
    """Visit a few likely pages on a clinic site and return the first email."""
    parsed = urllib.parse.urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    for path in CONTACT_PATHS:
        url = root + path if path else base_url
        if not robots.allowed(url):
            continue
        try:
            time.sleep(delay)
            resp = session.get(url, timeout=20, allow_redirects=True)
        except requests.RequestException:
            continue
        if resp.status_code != 200 or "text/html" not in resp.headers.get("Content-Type", ""):
            continue
        emails = extract_emails_from_html(resp.text)
        if emails:
            # Prefer an address on the clinic's own domain when available.
            domain = parsed.netloc.replace("www.", "")
            for e in emails:
                if e.endswith("@" + domain) or domain in e.split("@")[-1]:
                    return e
            return emails[0]
    return ""


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def dedupe(clinics: list[Clinic]) -> list[Clinic]:
    """Merge duplicates that share a name+suburb or a phone number."""
    out: list[Clinic] = []
    seen_key: dict[str, Clinic] = {}
    for c in clinics:
        keys = []
        if c.name:
            keys.append(("ns", c.name.lower(), c.suburb.lower()))
        if c.phone:
            keys.append(("ph", re.sub(r"\D", "", c.phone)))
        match = next((seen_key[str(k)] for k in keys if str(k) in seen_key), None)
        if match:
            # fill in any blanks on the existing record
            for f in ("phone", "email", "website", "street", "suburb", "postcode"):
                if not getattr(match, f) and getattr(c, f):
                    setattr(match, f, getattr(c, f))
            continue
        out.append(c)
        for k in keys:
            seen_key[str(k)] = c
    return out


def write_csv(clinics: list[Clinic], path: str) -> None:
    fields = ["name", "phone", "email", "website", "street", "suburb",
              "postcode", "lat", "lon", "email_source", "osm_id"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for c in clinics:
            row = {k: v for k, v in asdict(c).items() if k in fields}
            writer.writerow(row)


def run(args) -> int:
    if args.bbox:
        bbox = tuple(args.bbox)
    else:
        bbox = AREAS[args.area]

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    print(f"Querying OpenStreetMap for clinics in bbox {bbox} ...", file=sys.stderr)
    elements = fetch_osm(session, bbox)
    clinics = [c for el in elements if (c := parse_osm_element(el))]
    clinics = dedupe(clinics)
    print(f"Found {len(clinics)} named clinics in OSM.", file=sys.stderr)

    if not args.no_website_crawl:
        robots = RobotsCache(session)
        targets = [c for c in clinics if c.website and not c.email]
        targets = targets[: args.max_website_crawl]
        print(f"Crawling {len(targets)} clinic websites for emails "
              f"(delay {args.delay}s)...", file=sys.stderr)
        for i, c in enumerate(targets, 1):
            email = find_email_on_website(session, robots, c.website, args.delay)
            if email:
                c.email = email
                c.email_source = "website"
            print(f"  [{i}/{len(targets)}] {c.name[:40]:40s} "
                  f"{'-> ' + email if email else '(no email)'}", file=sys.stderr)

    write_csv(clinics, args.output)

    with_phone = sum(1 for c in clinics if c.phone)
    with_email = sum(1 for c in clinics if c.email)
    print(f"\nDone. Wrote {len(clinics)} clinics to {args.output}", file=sys.stderr)
    print(f"  with phone: {with_phone}   with email: {with_email}", file=sys.stderr)
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Scrape Melbourne clinic phone numbers and emails from "
                    "OpenStreetMap + clinic websites.")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--area", choices=sorted(AREAS), default="melbourne-metro",
                     help="Named area to search (default: melbourne-metro).")
    src.add_argument("--bbox", nargs=4, type=float,
                     metavar=("SOUTH", "WEST", "NORTH", "EAST"),
                     help="Custom bounding box, e.g. --bbox -38.05 144.55 -37.55 145.55")
    p.add_argument("-o", "--output", default="clinics.csv",
                   help="Output CSV path (default: clinics.csv).")
    p.add_argument("--no-website-crawl", action="store_true",
                   help="Skip visiting websites; use OSM data only.")
    p.add_argument("--max-website-crawl", type=int, default=150,
                   help="Max number of clinic websites to visit (default: 150).")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Seconds to wait between website requests (default: 1.0).")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
