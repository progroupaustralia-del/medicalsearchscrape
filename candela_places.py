#!/usr/bin/env python3
"""
NSW Candela prospector -- Google Places edition.

Companion to candela_nsw.py (must be in the same folder). It finds clinics via
Google's official Places API instead of OpenStreetMap -- far better coverage --
then reuses candela_nsw.py's website/social Candela detection.

Needs:
  * candela_nsw.py in the same folder
  * a Google Maps API key (Places API enabled, billing on)
      export GOOGLE_MAPS_API_KEY=...      # or pass --api-key
  * pip install requests

Run:
  python3 candela_places.py --eligible-only -o leads.csv
  python3 candela_places.py --max-places 500 --eligible-only -o leads.csv

Cost note: every search/details request is billable. Google's free monthly
allowance comfortably covers a few hundred clinics, but you must enable billing.
Use --max-places to cap how many clinics (and therefore API calls) it makes.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("The 'requests' package is required. Run: python3 -m pip install requests")

try:
    from candela_nsw import (Lead, RobotsCache, assess_website, assess_social,
                             dedupe, write_csv, USER_AGENT, normalize_phone,
                             normalize_url)
except ImportError:
    sys.exit("candela_nsw.py must be in the same folder as this file.")

TEXTSEARCH = "https://maps.googleapis.com/maps/api/place/textsearch/json"
DETAILS = "https://maps.googleapis.com/maps/api/place/details/json"

# Where to search (NSW population centres). More locations = more coverage AND
# more API calls. Override with --locations "Sydney NSW,Newcastle NSW,...".
DEFAULT_LOCATIONS = [
    "Sydney CBD NSW", "Parramatta NSW", "Bondi Junction NSW", "Chatswood NSW",
    "Liverpool NSW", "Penrith NSW", "Bankstown NSW", "Hornsby NSW",
    "Cronulla NSW", "Newcastle NSW", "Wollongong NSW", "Central Coast NSW",
]
# What to search for. Override with --keywords "skin clinic,laser clinic,...".
DEFAULT_KEYWORDS = [
    "laser skin clinic", "cosmetic clinic", "laser hair removal",
    "skin clinic", "cosmetic injectables", "dermatology clinic", "medical spa",
]


def text_search(session, key, query, region="au", max_pages=3):
    """Run a Places Text Search, following up to max_pages of results."""
    results = []
    params = {"query": query, "region": region, "key": key}
    for _ in range(max_pages):
        try:
            r = session.get(TEXTSEARCH, params=params, timeout=30)
            data = r.json()
        except (requests.RequestException, ValueError):
            break
        status = data.get("status")
        if status == "REQUEST_DENIED":
            raise RuntimeError("Google Places denied the request: "
                               + str(data.get("error_message",
                                     "check the API key / billing / enabled APIs")))
        if status == "OVER_QUERY_LIMIT":
            raise RuntimeError("Google Places quota exceeded (OVER_QUERY_LIMIT).")
        results += data.get("results", []) or []
        token = data.get("next_page_token")
        if not token:
            break
        time.sleep(2)  # next_page_token needs a moment before it's valid
        params = {"pagetoken": token, "key": key}
    return results


def get_details(session, key, place_id):
    """Fetch website / phone / suburb for one place."""
    params = {"place_id": place_id, "key": key,
              "fields": "name,website,formatted_phone_number,address_components"}
    try:
        r = session.get(DETAILS, params=params, timeout=30)
        data = r.json()
    except (requests.RequestException, ValueError):
        return {}
    if data.get("status") == "OVER_QUERY_LIMIT":
        raise RuntimeError("Google Places quota exceeded (OVER_QUERY_LIMIT).")
    res = data.get("result", {}) or {}
    suburb = ""
    for comp in res.get("address_components", []) or []:
        if "locality" in comp.get("types", []):
            suburb = comp.get("long_name", "")
            break
    return {"name": res.get("name", ""), "website": res.get("website", ""),
            "phone": res.get("formatted_phone_number", ""), "suburb": suburb}


def discover(session, key, locations, keywords, max_places, delay):
    """Discover clinics with a website across locations x keywords."""
    seen = set()
    leads = []
    for loc in locations:
        for kw in keywords:
            if len(leads) >= max_places:
                return leads
            query = kw + " in " + loc
            print("  searching: " + query, file=sys.stderr)
            for r in text_search(session, key, query):
                pid = r.get("place_id")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                time.sleep(delay)
                d = get_details(session, key, pid)
                web = normalize_url(d.get("website", ""))
                if not web:
                    continue
                leads.append(Lead(
                    name=d.get("name") or r.get("name", ""),
                    suburb=d.get("suburb", ""),
                    phone=normalize_phone(d.get("phone", "")),
                    website=web,
                    osm_id="places/" + pid))
                if len(leads) >= max_places:
                    return leads
    return leads


def run(args):
    key = args.api_key or os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not key:
        raise RuntimeError("No API key. Set GOOGLE_MAPS_API_KEY or pass --api-key.")
    locations = ([s.strip() for s in args.locations.split(",") if s.strip()]
                 if args.locations else DEFAULT_LOCATIONS)
    keywords = ([s.strip() for s in args.keywords.split(",") if s.strip()]
                if args.keywords else DEFAULT_KEYWORDS)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    print("Discovering clinics via Google Places (cap " + str(args.max_places)
          + ") ...", file=sys.stderr)
    leads = dedupe(discover(session, key, locations, keywords,
                            args.max_places, args.delay))
    print(str(len(leads)) + " clinics with a website found. Checking for Candela...",
          file=sys.stderr)

    robots = RobotsCache(session)
    for i, lead in enumerate(leads, 1):
        assess_website(session, robots, lead, args.delay, args.max_pages)
        if not args.no_social and not lead.eligible:
            assess_social(session, robots, lead, args.delay)
        flag = ("CANDELA: " + ", ".join(lead.matched_devices)) if lead.eligible else "-"
        print("  [" + str(i) + "/" + str(len(leads)) + "] "
              + lead.name[:38].ljust(38) + " " + flag, file=sys.stderr)

    if args.eligible_only:
        leads = [l for l in leads if l.eligible]
    write_csv(leads, args.output)
    print("\nDone. Wrote " + str(len(leads)) + " rows to " + args.output, file=sys.stderr)
    print("  Candela-eligible clinics: "
          + str(sum(1 for l in leads if l.eligible)), file=sys.stderr)


def main():
    p = argparse.ArgumentParser(
        description="Find NSW clinics that advertise a Candela device, using "
                    "Google Places to discover clinics.")
    p.add_argument("--api-key", default="",
                   help="Google Maps API key (else GOOGLE_MAPS_API_KEY env var).")
    p.add_argument("-o", "--output", default="candela_clinics.csv")
    p.add_argument("--locations", default="",
                   help="Comma-separated places to search (default: NSW centres).")
    p.add_argument("--keywords", default="",
                   help="Comma-separated search terms (default: clinic types).")
    p.add_argument("--max-places", type=int, default=300,
                   help="Cap on clinics to assess / API calls (default: 300).")
    p.add_argument("--max-pages", type=int, default=8,
                   help="Max pages to crawl per clinic website (default: 8).")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Seconds between requests (default: 1.0).")
    p.add_argument("--no-social", action="store_true")
    p.add_argument("--eligible-only", action="store_true")
    args = p.parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except RuntimeError as e:
        print("Error: " + str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
