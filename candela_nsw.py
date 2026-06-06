#!/usr/bin/env python3
"""NSW Candela-device clinic prospector -- self-contained single file."""
from __future__ import annotations
import argparse, csv, html, re, sys, time, urllib.parse, urllib.robotparser
from dataclasses import dataclass, field, asdict
try:
    import requests
except ImportError:
    sys.exit("The 'requests' package is required. Run: python3 -m pip install requests")

USER_AGENT = "ClinicResearchBot/1.0 (B2B research; contact: you@example.com)"
OVERPASS_ENDPOINTS = ["https://overpass-api.de/api/interpreter",
                      "https://overpass.kumi.systems/api/interpreter"]
AREAS = {"nsw": (-37.55, 140.95, -28.05, 153.70),
         "sydney-metro": (-34.20, 150.50, -33.50, 151.40),
         "newcastle": (-33.10, 151.40, -32.70, 152.00)}
STRONG_DEVICES = {"candela":"Candela (brand)","syneron candela":"Syneron Candela",
    "gentlelase":"GentleLase","gentlemax":"GentleMax","gentlemax pro":"GentleMax Pro",
    "gentleyag":"GentleYAG","gentle pro":"Gentle Pro","vbeam":"Vbeam","v-beam":"Vbeam",
    "vbeam perfecta":"Vbeam Perfecta","vbeam prima":"Vbeam Prima","picoway":"PicoWay",
    "alextrivantage":"AlexTriVantage","co2re":"CO2RE","nordlys":"Nordlys",
    "profound matrix":"Profound Matrix","frax 1550":"Frax 1550","frax pro":"Frax Pro"}
WEAK_DEVICES = {"matrix":"Matrix (Candela)","exion":"Exion","profound":"Profound RF",
    "ellipse":"Ellipse","frax":"Frax","serenity":"Serenity"}
def _kw(t):
    pat = re.escape(t).replace("\\ ", r"[\s\-]+")
    return re.compile("(?<![a-z0-9])" + pat + "(?![a-z0-9])", re.I)
STRONG_RE = {t: _kw(t) for t in STRONG_DEVICES}
WEAK_RE = {t: _kw(t) for t in WEAK_DEVICES}
OSM_SELECTORS = ['["amenity"="clinic"]','["amenity"="doctors"]','["healthcare"="clinic"]',
    '["healthcare"="cosmetic"]','["healthcare"~"dermatolog"]','["shop"="beauty"]',
    '["beauty"~"skin|laser|cosmetic"]']
NAME_HINTS = re.compile(r"skin|laser|cosmetic|aesthet|derma|beauty|medispa|med ?spa|medi ?spa|rejuven|clinique|glow|radiance|contour|hair removal|injectable|anti[- ]?age|complexion|appearance|plastic surgery", re.I)
LINK_HINTS = re.compile(r"laser|treatment|service|technolog|device|machine|equipment|hair[-_ ]?removal|skin|pigment|vascular|rejuven|tattoo|pico|candela|gentle|vbeam|about|cosmetic|injectable|aesthetic", re.I)
SEED_PATHS = ["", "/treatments","/services","/technology","/our-technology","/laser","/laser-treatments","/about","/about-us"]
SOCIAL_RE = {"instagram": re.compile(r'https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.]+', re.I),
             "facebook": re.compile(r'https?://(?:www\.)?facebook\.com/[A-Za-z0-9_.\-/]+', re.I)}
@dataclass
class Lead:
    name:str=""; suburb:str=""; phone:str=""; website:str=""; eligible:bool=False
    matched_devices:list=field(default_factory=list); evidence_url:str=""; evidence_source:str=""
    instagram:str=""; facebook:str=""; pages_checked:int=0; osm_id:str=""
def normalize_phone(r): return re.sub(r"\s{2,}"," ",r.split(";")[0].strip()) if r else ""
def normalize_url(r):
    r=(r or "").strip()
    return ("https://"+r) if r and not r.startswith(("http://","https://")) else r
def build_query(b):
    s,w,n,e=b; box=str(s)+","+str(w)+","+str(n)+","+str(e)
    parts=["  "+k+sel+"("+box+");" for sel in OSM_SELECTORS for k in ("node","way")]
    return "[out:json][timeout:180];\n(\n"+"\n".join(parts)+"\n);\nout center tags;"
def fetch_osm(sess,b,retries=3):
    q=build_query(b); last=None
    for a in range(retries):
        ep=OVERPASS_ENDPOINTS[a%len(OVERPASS_ENDPOINTS)]
        try:
            r=sess.post(ep,data={"data":q},timeout=300)
            if r.status_code==200: return r.json().get("elements",[])
            last="HTTP "+str(r.status_code)+" from "+ep
        except requests.RequestException as x: last=type(x).__name__+": "+str(x)
        wait=2**a; print("  Overpass attempt "+str(a+1)+" failed ("+str(last)+"); retry in "+str(wait)+"s...",file=sys.stderr); time.sleep(wait)
    raise RuntimeError("Overpass API unavailable: "+str(last))
def osm_to_lead(el):
    t=el.get("tags",{}); name=t.get("name") or t.get("operator") or ""
    web=t.get("website") or t.get("contact:website") or t.get("url") or ""
    if not name or not web: return None
    explicit=(t.get("shop")=="beauty" or t.get("beauty") or t.get("healthcare")=="cosmetic" or "dermatolog" in t.get("healthcare",""))
    if not explicit and not NAME_HINTS.search(name): return None
    return Lead(name=name.strip(), suburb=(t.get("addr:suburb") or t.get("addr:city") or "").strip(),
        phone=normalize_phone(t.get("phone") or t.get("contact:phone") or ""), website=normalize_url(web),
        instagram=t.get("contact:instagram",""), facebook=t.get("contact:facebook",""), osm_id=str(el.get("type"))+"/"+str(el.get("id")))
def detect_devices(text):
    matched=[]; brand=False
    for term,rx in STRONG_RE.items():
        if rx.search(text):
            c=STRONG_DEVICES[term]
            if c not in matched: matched.append(c)
            brand=True
    for term,rx in WEAK_RE.items():
        if rx.search(text) and brand:
            c=WEAK_DEVICES[term]
            if c not in matched: matched.append(c)
    return matched, brand
class RobotsCache:
    def __init__(self,s): self.s=s; self.c={}
    def allowed(self,url):
        p=urllib.parse.urlparse(url); host=p.scheme+"://"+p.netloc
        if host not in self.c:
            rp=urllib.robotparser.RobotFileParser()
            try:
                r=self.s.get(host+"/robots.txt",timeout=15)
                if r.status_code==200: rp.parse(r.text.splitlines())
                else: rp=None
            except requests.RequestException: rp=None
            self.c[host]=rp
        rp=self.c[host]
        return True if rp is None else rp.can_fetch(USER_AGENT,url)
def visible_text(h):
    h=re.sub(r"(?is)<(script|style|noscript).*?</\1>"," ",h)
    return html.unescape(re.sub(r"(?s)<[^>]+>"," ",h))
def discover_links(base,h,limit):
    p=urllib.parse.urlparse(base); root=p.scheme+"://"+p.netloc; out=[]; seen=set()
    for m in re.finditer(r'href=["\']([^"\'#]+)["\']',h,re.I):
        href=m.group(1).strip()
        if href.startswith(("mailto:","tel:")): continue
        full=urllib.parse.urljoin(root+"/",href); pp=urllib.parse.urlparse(full)
        if pp.netloc!=p.netloc or not LINK_HINTS.search(pp.path): continue
        full=full.split("?")[0]
        if full not in seen: seen.add(full); out.append(full)
        if len(out)>=limit: break
    return out
def find_social(h):
    f={}
    for plat,rx in SOCIAL_RE.items():
        m=rx.search(h)
        if m and not re.search(r"/(sharer|share|intent|plugins|tr\?|login)",m.group(0),re.I): f[plat]=m.group(0)
    return f
def get(sess,robots,url,delay):
    if not robots.allowed(url): return None
    try:
        time.sleep(delay); r=sess.get(url,timeout=20,allow_redirects=True)
    except requests.RequestException: return None
    if r.status_code!=200 or "text/html" not in r.headers.get("Content-Type",""): return None
    return r.text
def check(lead,h,url):
    m,_=detect_devices(visible_text(h))
    if m: lead.eligible=True; lead.matched_devices=m; lead.evidence_url=url; lead.evidence_source="website"; return True
    return False
def assess_website(sess,robots,lead,delay,maxp):
    p=urllib.parse.urlparse(lead.website); root=p.scheme+"://"+p.netloc
    home=get(sess,robots,lead.website,delay); pages=[]
    if home:
        lead.pages_checked+=1; s=find_social(home)
        lead.instagram=lead.instagram or s.get("instagram",""); lead.facebook=lead.facebook or s.get("facebook","")
        if check(lead,home,lead.website): return
        pages.extend(discover_links(lead.website,home,maxp))
    for path in SEED_PATHS[1:]:
        u=root+path
        if u not in pages: pages.append(u)
    for url in pages[:maxp]:
        page=get(sess,robots,url,delay)
        if not page: continue
        lead.pages_checked+=1
        if not lead.instagram or not lead.facebook:
            s=find_social(page); lead.instagram=lead.instagram or s.get("instagram",""); lead.facebook=lead.facebook or s.get("facebook","")
        if check(lead,page,url): return
def assess_social(sess,robots,lead,delay):
    for url in (lead.instagram,lead.facebook):
        if not url or lead.eligible: continue
        page=get(sess,robots,url,delay)
        if not page: continue
        m,_=detect_devices(visible_text(page))
        if m: lead.eligible=True; lead.matched_devices=m; lead.evidence_url=url; lead.evidence_source="social"; return
def dedupe(leads):
    out=[]; seen=set()
    for l in leads:
        dom=urllib.parse.urlparse(l.website).netloc.replace("www.",""); key=dom or l.name.lower()
        if key in seen: continue
        seen.add(key); out.append(l)
    return out
def write_csv(leads,path):
    fields=["name","suburb","phone","website","eligible","matched_devices","evidence_url","evidence_source","instagram","facebook","pages_checked","osm_id"]
    with open(path,"w",newline="",encoding="utf-8") as fh:
        w=csv.DictWriter(fh,fieldnames=fields); w.writeheader()
        for l in leads:
            row=asdict(l); row["matched_devices"]="; ".join(l.matched_devices); row["eligible"]="yes" if l.eligible else "no"
            w.writerow({k:row[k] for k in fields})
def run(args):
    bbox=tuple(args.bbox) if args.bbox else AREAS[args.area]
    sess=requests.Session(); sess.headers.update({"User-Agent":USER_AGENT})
    print("Querying OpenStreetMap for clinics in "+str(args.area or "custom bbox")+" "+str(bbox)+" ...",file=sys.stderr)
    leads=dedupe([l for el in fetch_osm(sess,bbox) if (l:=osm_to_lead(el))])[:args.max_sites]
    print(str(len(leads))+" candidate aesthetic/skin/laser clinics with a website.",file=sys.stderr)
    robots=RobotsCache(sess)
    for i,lead in enumerate(leads,1):
        assess_website(sess,robots,lead,args.delay,args.max_pages)
        if not args.no_social and not lead.eligible: assess_social(sess,robots,lead,args.delay)
        flag=("CANDELA: "+", ".join(lead.matched_devices)) if lead.eligible else "-"
        print("  ["+str(i)+"/"+str(len(leads))+"] "+lead.name[:38].ljust(38)+" "+flag,file=sys.stderr)
    if args.eligible_only: leads=[l for l in leads if l.eligible]
    write_csv(leads,args.output)
    print("\nDone. Wrote "+str(len(leads))+" rows to "+args.output,file=sys.stderr)
    print("  Candela-eligible clinics: "+str(sum(1 for l in leads if l.eligible)),file=sys.stderr)
def main():
    p=argparse.ArgumentParser(description="Find NSW clinics that advertise a Candela device.")
    g=p.add_mutually_exclusive_group()
    g.add_argument("--area",choices=sorted(AREAS),default="sydney-metro")
    g.add_argument("--bbox",nargs=4,type=float,metavar=("S","W","N","E"))
    p.add_argument("-o","--output",default="candela_clinics.csv")
    p.add_argument("--max-sites",type=int,default=400)
    p.add_argument("--max-pages",type=int,default=8)
    p.add_argument("--delay",type=float,default=1.0)
    p.add_argument("--no-social",action="store_true")
    p.add_argument("--eligible-only",action="store_true")
    args=p.parse_args()
    try: run(args)
    except KeyboardInterrupt: print("\nInterrupted.",file=sys.stderr); return 130
    except RuntimeError as e: print("Error: "+str(e),file=sys.stderr); return 1
if __name__=="__main__": raise SystemExit(main())
