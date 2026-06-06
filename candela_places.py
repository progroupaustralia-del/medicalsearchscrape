#!/usr/bin/env python3
"""NSW Candela prospector -- Places API (New) edition (companion to candela_nsw.py)."""
from __future__ import annotations
import argparse, os, sys, time
try:
    import requests
except ImportError:
    sys.exit("The 'requests' package is required. Run: python3 -m pip install requests")
try:
    from candela_nsw import (Lead, RobotsCache, assess_website, assess_social,
                             dedupe, write_csv, USER_AGENT, normalize_phone, normalize_url)
except ImportError:
    sys.exit("candela_nsw.py must be in the same folder as this file.")

API_KEY = ""  # optional: set GOOGLE_MAPS_API_KEY env or pass --api-key

SEARCHTEXT = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ("places.id,places.displayName,places.websiteUri,"
              "places.nationalPhoneNumber,places.addressComponents,nextPageToken")
DEFAULT_LOCATIONS = ["Sydney CBD NSW","Parramatta NSW","Bondi Junction NSW","Chatswood NSW",
    "Liverpool NSW","Penrith NSW","Bankstown NSW","Hornsby NSW","Cronulla NSW",
    "Newcastle NSW","Wollongong NSW","Central Coast NSW"]
DEFAULT_KEYWORDS = ["laser skin clinic","cosmetic clinic","laser hair removal",
    "skin clinic","cosmetic injectables","dermatology clinic","medical spa"]
def search_text(session,key,query,max_pages=3):
    results=[]
    headers={"Content-Type":"application/json","X-Goog-Api-Key":key,"X-Goog-FieldMask":FIELD_MASK}
    body={"textQuery":query,"regionCode":"AU","pageSize":20}
    for _ in range(max_pages):
        try:
            r=session.post(SEARCHTEXT,headers=headers,json=body,timeout=30); data=r.json()
        except (requests.RequestException,ValueError): break
        if isinstance(data,dict) and data.get("error"):
            raise RuntimeError("Google Places (New) error: "+str(data["error"].get("message","")))
        results+=data.get("places",[]) or []
        token=data.get("nextPageToken")
        if not token: break
        time.sleep(2); body={"textQuery":query,"regionCode":"AU","pageSize":20,"pageToken":token}
    return results
def place_to_lead(p):
    web=normalize_url(p.get("websiteUri",""))
    if not web: return None
    name=(p.get("displayName") or {}).get("text","")
    phone=normalize_phone(p.get("nationalPhoneNumber",""))
    suburb=""
    for comp in p.get("addressComponents",[]) or []:
        if "locality" in comp.get("types",[]): suburb=comp.get("longText",""); break
    return Lead(name=name,suburb=suburb,phone=phone,website=web,osm_id="places/"+str(p.get("id","")))
def discover(session,key,locations,keywords,max_places,delay):
    seen=set(); leads=[]
    for loc in locations:
        for kw in keywords:
            if len(leads)>=max_places: return leads
            query=kw+" in "+loc; print("  searching: "+query,file=sys.stderr)
            time.sleep(delay)
            for p in search_text(session,key,query):
                pid=p.get("id")
                if not pid or pid in seen: continue
                seen.add(pid); lead=place_to_lead(p)
                if lead is None: continue
                leads.append(lead)
                if len(leads)>=max_places: return leads
    return leads
def run(args):
    key=args.api_key or os.environ.get("GOOGLE_MAPS_API_KEY","") or API_KEY
    if not key: raise RuntimeError("No API key. Set GOOGLE_MAPS_API_KEY or pass --api-key.")
    locations=[s.strip() for s in args.locations.split(",") if s.strip()] if args.locations else DEFAULT_LOCATIONS
    keywords=[s.strip() for s in args.keywords.split(",") if s.strip()] if args.keywords else DEFAULT_KEYWORDS
    session=requests.Session(); session.headers.update({"User-Agent":USER_AGENT})
    print("Discovering clinics via Google Places (New) (cap "+str(args.max_places)+") ...",file=sys.stderr)
    leads=dedupe(discover(session,key,locations,keywords,args.max_places,args.delay))
    print(str(len(leads))+" clinics with a website found. Checking for Candela...",file=sys.stderr)
    robots=RobotsCache(session)
    for i,lead in enumerate(leads,1):
        assess_website(session,robots,lead,args.delay,args.max_pages)
        if not args.no_social and not lead.eligible: assess_social(session,robots,lead,args.delay)
        flag=("CANDELA: "+", ".join(lead.matched_devices)) if lead.eligible else "-"
        print("  ["+str(i)+"/"+str(len(leads))+"] "+lead.name[:38].ljust(38)+" "+flag,file=sys.stderr)
    if args.eligible_only: leads=[l for l in leads if l.eligible]
    write_csv(leads,args.output)
    print("\nDone. Wrote "+str(len(leads))+" rows to "+args.output,file=sys.stderr)
    print("  Candela-eligible clinics: "+str(sum(1 for l in leads if l.eligible)),file=sys.stderr)
def main():
    p=argparse.ArgumentParser(description="Find NSW clinics that advertise a Candela device, via Places API (New).")
    p.add_argument("--api-key",default="")
    p.add_argument("-o","--output",default="candela_clinics.csv")
    p.add_argument("--locations",default="")
    p.add_argument("--keywords",default="")
    p.add_argument("--max-places",type=int,default=300)
    p.add_argument("--max-pages",type=int,default=8)
    p.add_argument("--delay",type=float,default=1.0)
    p.add_argument("--no-social",action="store_true")
    p.add_argument("--eligible-only",action="store_true")
    args=p.parse_args()
    try: run(args)
    except KeyboardInterrupt: print("\nInterrupted.",file=sys.stderr); return 130
    except RuntimeError as e: print("Error: "+str(e),file=sys.stderr); return 1
if __name__=="__main__": raise SystemExit(main())
