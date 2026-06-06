#!/usr/bin/env python3
"""
NSW Candela-device clinic prospector -- SELF-CONTAINED single-file version.

Finds aesthetic / skin / laser clinics in New South Wales (via the free
OpenStreetMap Overpass API) and flags the ones that publicly advertise a
Candela laser/energy device on their website or linked social page.

Only needs Python 3.10+ and the `requests` package. No API key.

  pip install requests
  python candela_nsw_standalone.py -o leads.csv
  python candela_nsw_standalone.py --area nsw --eligible-only -o leads.csv
  python candela_nsw_standalone.py --no-social        # websites only, faster
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
except ImportError:
    sys.exit("The 'requests' package is required. Run: pip install requests")


USER_AGENT = ("MelbourneClinicScraper/1.0 "
              "(+https://example.com; contact: you@example.com)")

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Named bounding boxes: (south, west, north, east)
AREAS = {
    "nsw": (-37.55, 140.95, -28.05, 153.70),          # whole state (long run)
    "sydney-metro": (-34.20, 150.50, -33.50, 151.40),  # greater Sydney
    "newcastle": (-33.10, 151.40, -32.70, 152.00),
}

# Candela devices. "strong" terms are unambiguous; "weak" terms are real
# Candela products that are also common words, so they only count when the
# "Candela" brand also appears on the site (avoids false positives).
STRONG_DEVICES = {
    "candela": "Candela (brand)", "syneron candela": "Syneron Candela",
    "gentlelase": "GentleLase", "gentlemax": "GentleMax",
    "gentlemax pro": "GentleMax Pro", "gentleyag": "GentleYAG",
    "gentle pro": "Gentle Pro", "vbeam": "Vbeam", "v-beam": "Vbeam",
    "vbeam perfecta": "Vbeam Perfecta", "vbeam prima": "Vbeam Prima",
    "picoway": "PicoWay", "alextrivantage": "AlexTriVantage", "co2re": "CO2RE",
    "nordlys": "Nordlys", "profound matrix": "Profound Matrix",
    "frax 1550": "Frax 1550", "frax pro": "Frax Pro",
}
WEAK_DEVICES = {
    "matrix": "Matrix (Candela)", "exion": "Exion", "profound": "Profound RF",
    "ellipse": "Ellipse", "frax": "Frax", "serenity": "Serenity",
}


def _kw_regex(term: str) -> re.Pattern:
    pat = re.escape(term).replace(r"\ ", r"[\s\-]+")
    return re.compile(rf"(?<![a-z0-9]){pat}(?![a-z0-9])", re.IGNORECASE)

STRONG_RE = {t: _kw_regex(t) for t in STRONG_DEVICES}
WEAK_RE = {t: _kw_regex(t) for t in WEAK_DEVICES}

OSM_SELECTORS = [
    '["amenity"="clinic"]', '["amenity"="doctors"]', '["healthcare"="clinic"]',
    '["healthcare"="cosmetic"]', '["healthcare"~"dermatolog"]',
    '["shop"="beauty"]', '["beauty"~"skin|laser|cosmetic"]',
]

NAME_HINTS = re.compile(
    r"skin|laser|cosmetic|aesthet|derma|beauty|medispa|med ?spa|medi ?spa|"
    r"rejuven|clinique|glow|radiance|contour|hair removal|injectable|"
    r"anti[- ]?age|complexion|appearance|plastic surgery", re.IGNORECASE)

LINK_HINTS = re.compile(
    r"laser|treatment|service|technolog|device|machine|equipment|hair[-_ ]?removal|"
    r"skin|pigment|vascular|rejuven|tattoo|pico|candela|gentle|vbeam|"
    r"about|cosmetic|injectable|aesthetic", re.IGNORECASE)

SEED_PATHS = ["", "/treatments", "/services", "/technology", "/our-technology",
              "/laser", "/laser-treatments", "/about", "/about-us"]

SOCIAL_RE = {
    "instagram": re.compile(r'https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.]+', re.I),
    "facebook": re.compile(r'https?://(?:www\.)?facebook\.com/[A-Za-z0-9_.\-/]+', re.I),
}


@dataclass
class Lead:
    name: str = ""
    suburb: str = ""
    phone: str = ""
    website: str = ""
    eligible: bool = False
    matched_devices: list[str] = field(default_factory=list)
    evidence_url: str = ""
    evidence_source: str = ""
    instagram: str = ""
    facebook: str = ""
    pages_checked: int = 0
    osm_id: str = ""


def normalize_phone(raw: str) -> str:
    if not raw:
        return ""
    return re.sub(r"\s{2,}", " ", raw.split(";")[0].strip())


def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def build_overpass_query(bbox) -> str:
    s, w, n, e = bbox
    box = f"{s},{w},{n},{e}"
    parts = [f"  {kind}{sel}({box});" for sel in OSM_SELECTORS
             for kind in ("node", "way")]
    return f"[out:json][timeout:180];\n(\n" + "\n".join(parts) + "\n);\nout center tags;"


def fetch_osm(session, bbox, retries: int = 3) -> list[dict]:
    query = build_overpass_query(bbox)
    last = None
    for attempt in range(retries):
        endpoint = OVERPASS_ENDPOINTS[attempt % len(OVERPASS_ENDPOINTS)]
        try:
            r = session.post(endpoint, data={"data": query}, timeout=300)
            if r.status_code == 200:
                return r.json().get("elements", [])
            last = f"HTTP {r.status_code} from {endpoint}"
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
        wait = 2 ** attempt
        print(f"  Overpass attempt {attempt+1} failed ({last}); retry in {wait}s...",
              file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError(f"Overpass API unavailable: {last}")


def osm_to_lead(el: dict) -> Lead | None:
    tags = el.get("tags", {})
    name = tags.get("name") or tags.get("operator") or ""
    website = (tags.get("website") or tags.get("contact:website")
               or tags.get("url") or "")
    if not name or not website:
        return None
    explicit = (tags.get("shop") == "beauty" or tags.get("beauty")
                or tags.get("healthcare") == "cosmetic"
                or "dermatolog" in tags.get("healthcare", ""))
    if not explicit and not NAME_HINTS.search(name):
        return None
    return Lead(
        name=name.strip(),
        suburb=(tags.get("addr:suburb") or tags.get("addr:city") or "").strip(),
        phone=normalize_phone(tags.get("phone") or tags.get("contact:phone") or ""),
        website=normalize_url(website),
        instagram=tags.get("contact:instagram", ""),
        facebook=tags.get("contact:facebook", ""),
        osm_id=f"{el.get('type')}/{el.get('id')}",
    )


def detect_devices(text: str) -> tuple[list[str], bool]:
    matched, brand_present = [], False
    for term, regex in STRONG_RE.items():
        if regex.search(text):
            canon = STRONG_DEVICES[term]
            if canon not in matched:
                matched.append(canon)
            brand_present = True
    for term, regex in WEAK_RE.items():
        if regex.search(text) and brand_present:
            canon = WEAK_DEVICES[term]
            if canon not in matched:
                matched.append(canon)
    return matched, brand_present


class RobotsCache:
    def __init__(self, session):
        self.session = session
        self._cache = {}

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
        return True if rp is None else rp.can_fetch(USER_AGENT, url)


def visible_text(html_content: str) -> str:
    txt = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html_content)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    return html.unescape(txt)


def discover_links(base_url, html_content, limit):
    parsed = urllib.parse.urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    out, seen = [], set()
    for m in re.finditer(r'href=["\']([^"\'#]+)["\']', html_content, re.I):
        href = m.group(1).strip()
        if href.startswith(("mailto:", "tel:")):
            continue
        full = urllib.parse.urljoin(root + "/", href)
        p = urllib.parse.urlparse(full)
        if p.netloc != parsed.netloc or not LINK_HINTS.search(p.path):
            continue
        full = full.split("?")[0]
        if full not in seen:
            seen.add(full)
            out.append(full)
        if len(out) >= limit:
            break
    return out


def find_social_links(html_content):
    found = {}
    for platform, regex in SOCIAL_RE.items():
        m = regex.search(html_content)
        if m and not re.search(r"/(sharer|share|intent|plugins|tr\?|login)",
                               m.group(0), re.I):
            found[platform] = m.group(0)
    return found


def get(session, robots, url, delay):
    if not robots.allowed(url):
        return None
    try:
        time.sleep(delay)
        r = session.get(url, timeout=20, allow_redirects=True)
    except requests.RequestException:
        return None
    if r.status_code != 200 or "text/html" not in r.headers.get("Content-Type", ""):
        return None
    return r.text


def _check_text(lead, html_content, url):
    matched, _ = detect_devices(visible_text(html_content))
    if matched:
        lead.eligible = True
        lead.matched_devices = matched
        lead.evidence_url = url
        lead.evidence_source = "website"
        return True
    return False


def assess_website(session, robots, lead, delay, max_pages):
    parsed = urllib.parse.urlparse(lead.website)
    root = f"{parsed.scheme}://{parsed.netloc}"
    home = get(session, robots, lead.website, delay)
    pages = []
    if home:
        lead.pages_checked += 1
        s = find_social_links(home)
        lead.instagram = lead.instagram or s.get("instagram", "")
        lead.facebook = lead.facebook or s.get("facebook", "")
        if _check_text(lead, home, lead.website):
            return
        pages.extend(discover_links(lead.website, home, max_pages))
    for path in SEED_PATHS[1:]:
        u = root + path
        if u not in pages:
            pages.append(u)
    for url in pages[:max_pages]:
        page = get(session, robots, url, delay)
        if not page:
            continue
        lead.pages_checked += 1
        if not lead.instagram or not lead.facebook:
            s = find_social_links(page)
            lead.instagram = lead.instagram or s.get("instagram", "")
            lead.facebook = lead.facebook or s.get("facebook", "")
        if _check_text(lead, page, url):
            return


def assess_social(session, robots, lead, delay):
    for url in (lead.instagram, lead.facebook):
        if not url or lead.eligible:
            continue
        page = get(session, robots, url, delay)
        if not page:
            continue
        matched, _ = detect_devices(visible_text(page))
        if matched:
            lead.eligible = True
            lead.matched_devices = matched
            lead.evidence_url = url
            lead.evidence_source = "social"
            return


def dedupe(leads):
    out, seen = [], set()
    for lead in leads:
        dom = urllib.parse.urlparse(lead.website).netloc.replace("www.", "")
        key = dom or lead.name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(lead)
    return out


def write_csv(leads, path):
    fields = ["name", "suburb", "phone", "website", "eligible",
              "matched_devices", "evidence_url", "evidence_source",
              "instagram", "facebook", "pages_checked", "osm_id"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for lead in leads:
            row = asdict(lead)
            row["matched_devices"] = "; ".join(lead.matched_devices)
            row["eligible"] = "yes" if lead.eligible else "no"
            w.writerow({k: row[k] for k in fields})


def run(args):
    bbox = tuple(args.bbox) if args.bbox else AREAS[args.area]
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    print(f"Querying OpenStreetMap for clinics in {args.area or 'custom bbox'} "
          f"{bbox} ...", file=sys.stderr)
    leads = dedupe([l for el in fetch_osm(session, bbox) if (l := osm_to_lead(el))])
    leads = leads[: args.max_sites]
    print(f"{len(leads)} candidate aesthetic/skin/laser clinics with a website.",
          file=sys.stderr)

    robots = RobotsCache(session)
    for i, lead in enumerate(leads, 1):
        assess_website(session, robots, lead, args.delay, args.max_pages)
        if not args.no_social and not lead.eligible:
            assess_social(session, robots, lead, args.delay)
        flag = ("CANDELA: " + ", ".join(lead.matched_devices)) if lead.eligible else "-"
        print(f"  [{i}/{len(leads)}] {lead.name[:38]:38s} {flag}", file=sys.stderr)

    if args.eligible_only:
        leads = [l for l in leads if l.eligible]
    write_csv(leads, args.output)
    eligible = sum(1 for l in leads if l.eligible)
    print(f"\nDone. Wrote {len(leads)} rows to {args.output}", file=sys.stderr)
    print(f"  Candela-eligible clinics: {eligible}", file=sys.stderr)
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Find NSW aesthetic/skin/laser clinics that advertise a "
                    "Candela device, via their websites and linked social pages.")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--area", choices=sorted(AREAS), default="sydney-metro",
                     help="Named area (default: sydney-metro).")
    src.add_argument("--bbox", nargs=4, type=float, metavar=("S", "W", "N", "E"),
                     help="Custom bounding box.")
    p.add_argument("-o", "--output", default="candela_clinics.csv",
                   help="Output CSV path (default: candela_clinics.csv).")
    p.add_argument("--max-sites", type=int, default=400,
                   help="Max candidate clinics to assess (default: 400).")
    p.add_argument("--max-pages", type=int, default=8,
                   help="Max pages to crawl per clinic website (default: 8).")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Seconds between HTTP requests (default: 1.0).")
    p.add_argument("--no-social", action="store_true",
                   help="Skip social-media checks; assess websites only.")
    p.add_argument("--eligible-only", action="store_true",
                   help="Only write clinics confirmed to use a Candela device.")
    return p.parse_args(argv)


def main(argv=None):
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
