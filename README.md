# Melbourne Clinic Contact Scraper

A small, no-API-key tool that collects **publicly published** phone numbers and
email addresses for medical clinics in Melbourne.

It works in two stages:

1. **OpenStreetMap (Overpass API)** — finds clinics, doctors and healthcare
   facilities in a Melbourne bounding box, reading their name, address, phone
   and website from OSM tags. Free, structured, no key required.
2. **Website crawl (optional)** — for clinics that list a website but no email
   in OSM, it politely visits a few likely pages (`/`, `/contact`, `/about`,
   …) and extracts the email the business has published. It obeys
   `robots.txt` and rate-limits every request.

## Setup

```bash
pip install -r requirements.txt
```

Requires Python 3.10+.

## Usage

```bash
# Default: greater Melbourne metro, crawl up to 150 websites for emails
python clinic_scraper.py -o clinics.csv

# Fastest: OpenStreetMap data only, no website visits
python clinic_scraper.py --no-website-crawl

# Just the inner CBD
python clinic_scraper.py --area melbourne-cbd

# A custom bounding box (south west north east)
python clinic_scraper.py --bbox -38.05 144.55 -37.55 145.55

# Be gentler / crawl more sites
python clinic_scraper.py --delay 2 --max-website-crawl 400
```

### Options

| Option | Description |
| --- | --- |
| `--area {melbourne-metro, melbourne-cbd}` | Named search area (default `melbourne-metro`). |
| `--bbox S W N E` | Custom bounding box instead of a named area. |
| `-o, --output` | Output CSV path (default `clinics.csv`). |
| `--no-website-crawl` | Use OSM data only; don't visit websites. |
| `--max-website-crawl N` | Cap how many websites are visited (default 150). |
| `--delay SECONDS` | Pause between website requests (default 1.0). |

## Output

A CSV with one row per clinic:

```
name, phone, email, website, street, suburb, postcode, lat, lon, email_source, osm_id
```

`email_source` is `osm` (email came straight from OpenStreetMap),
`website` (found by crawling the site), or blank (no email found).

## How it stays polite & legal

- Identifies itself with a descriptive `User-Agent`.
- Sends one Overpass query, then rate-limits website requests (`--delay`).
- Reads and respects each site's `robots.txt` before fetching.
- Only collects contact details a business has chosen to publish publicly.
- Filters out obvious non-contacts (image filenames, tracking IDs,
  `example.com`, etc.).

You are responsible for how you use the collected data. Australian
[Spam Act 2003](https://www.legislation.gov.au/Details/C2016C00614) and
[Privacy Act 1988](https://www.oaic.gov.au/) rules apply to commercial
messaging and handling of personal information — obtain consent where required
and honour unsubscribe requests. Use this for legitimate purposes only.

## Data source & attribution

Clinic locations and contacts come from
[OpenStreetMap](https://www.openstreetmap.org/copyright), © OpenStreetMap
contributors, available under the Open Database License (ODbL). If you
republish derived data, attribute OpenStreetMap accordingly.

## Notes

- OSM coverage varies; not every clinic is listed, and some listings are
  incomplete. The website crawl fills in many missing emails but not all.
- The two public Overpass mirrors used here are shared community resources.
  Don't hammer them — run the tool occasionally, not in a tight loop.
