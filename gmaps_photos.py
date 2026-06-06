#!/usr/bin/env python3
"""
Google Maps photo signal for Candela-device detection.

For a clinic, this looks at the photos on its Google Business Profile (Maps
listing) and tries to spot a Candela device — clinics frequently photograph
their treatment rooms, and the machine's panel/branding (e.g. "GentleMax Pro",
"Vbeam", "Candela") is sometimes legible.

IMPORTANT — how the photos are obtained:
  * Photos are fetched through Google's OFFICIAL Places API (Place Photos),
    which requires a GOOGLE_MAPS_API_KEY with billing enabled. Each Find
    Place / Place Details / Place Photo request is billable.
  * This does NOT scrape the Google Maps website or map tiles. Doing so would
    violate Google's Terms of Service and is bot-blocked. The official API is
    the only supported path.

Two recognition backends (pick with --image-recognition):
  * "ocr"    — pytesseract reads visible text off the photo (free, weaker).
               Needs the Tesseract binary + `pip install pytesseract pillow`.
  * "vision" — Claude vision identifies device branding (needs ANTHROPIC_API_KEY,
               `pip install anthropic`). More accurate, costs per image.

Recognising a specific laser model from a photo is inherently unreliable
(devices are often in cabinets, unbranded in shot, or simply not photographed),
so treat a hit as a strong lead and a miss as "not found", never "absent".
"""

from __future__ import annotations

import base64
import os
import sys
import time
import urllib.parse

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("The 'requests' package is required. Run: pip install -r requirements.txt")

# Canonicalise/validate any recognised text against the known Candela catalogue.
from candela_scraper import detect_devices

PLACES_FIND_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
PLACES_PHOTO_URL = "https://maps.googleapis.com/maps/api/place/photo"

VISION_SYSTEM = (
    "You identify aesthetic / medical laser and energy-based devices in photos of "
    "clinics. Look for brand or model names printed on equipment housings, screens, "
    "handpieces, or signage. Respond with ONLY the device or brand names you can "
    "actually read or clearly recognise, comma-separated. If you cannot identify any "
    "specific device, respond with exactly NONE. Never guess a brand you cannot see."
)


# --------------------------------------------------------------------------- #
# Google Places API (official)
# --------------------------------------------------------------------------- #

def find_place_id(session, api_key: str, name: str, suburb: str = "",
                  lat: float | None = None, lon: float | None = None) -> str | None:
    """Resolve a clinic to a Google place_id via the Find Place endpoint."""
    query = name if not suburb else f"{name}, {suburb}"
    query = f"{query}, NSW, Australia"
    params = {
        "input": query,
        "inputtype": "textquery",
        "fields": "place_id",
        "key": api_key,
    }
    if lat is not None and lon is not None:
        params["locationbias"] = f"circle:3000@{lat},{lon}"
    try:
        r = session.get(PLACES_FIND_URL, params=params, timeout=20)
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    status = data.get("status")
    if status == "OVER_QUERY_LIMIT":
        raise RuntimeError("Google Places API quota exceeded (OVER_QUERY_LIMIT).")
    if status == "REQUEST_DENIED":
        raise RuntimeError(f"Google Places API denied the request: "
                           f"{data.get('error_message', 'check the API key/billing')}.")
    candidates = data.get("candidates") or []
    return candidates[0].get("place_id") if candidates else None


def get_photo_references(session, api_key: str, place_id: str,
                         max_photos: int = 5) -> list[str]:
    """Return up to `max_photos` photo references for a place."""
    params = {"place_id": place_id, "fields": "photos", "key": api_key}
    try:
        r = session.get(PLACES_DETAILS_URL, params=params, timeout=20)
        data = r.json()
    except (requests.RequestException, ValueError):
        return []
    if data.get("status") == "OVER_QUERY_LIMIT":
        raise RuntimeError("Google Places API quota exceeded (OVER_QUERY_LIMIT).")
    photos = (data.get("result") or {}).get("photos") or []
    return [p["photo_reference"] for p in photos[:max_photos] if p.get("photo_reference")]


def fetch_photo(session, api_key: str, photo_reference: str,
                maxwidth: int = 1600) -> tuple[bytes, str] | None:
    """Download one photo. Returns (image_bytes, media_type) or None."""
    params = {"maxwidth": maxwidth, "photo_reference": photo_reference, "key": api_key}
    try:
        r = session.get(PLACES_PHOTO_URL, params=params, timeout=30)
    except requests.RequestException:
        return None
    ctype = r.headers.get("Content-Type", "")
    if r.status_code != 200 or not ctype.startswith("image/"):
        return None
    return r.content, ctype.split(";")[0]


def place_url(place_id: str) -> str:
    """A human-friendly Maps URL for the listing (for manual review)."""
    return ("https://www.google.com/maps/search/?api=1&query=Google&query_place_id="
            + urllib.parse.quote(place_id))


# --------------------------------------------------------------------------- #
# Recognition backends
# --------------------------------------------------------------------------- #

class OCRBackend:
    """Reads visible text from an image with Tesseract."""

    def __init__(self):
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "OCR backend needs pytesseract + pillow and the Tesseract binary.\n"
                "  pip install pytesseract pillow   # and install tesseract-ocr")
        self._pytesseract = __import__("pytesseract")
        self._Image = __import__("PIL.Image", fromlist=["Image"])

    def text(self, image_bytes: bytes, media_type: str) -> str:
        import io
        try:
            img = self._Image.open(io.BytesIO(image_bytes))
            return self._pytesseract.image_to_string(img)
        except Exception:
            return ""


class VisionBackend:
    """Uses Claude vision to name any visible device branding."""

    def __init__(self, model: str = "claude-opus-4-8"):
        try:
            import anthropic
        except ImportError:
            raise RuntimeError(
                "Vision backend needs the anthropic SDK: pip install anthropic")
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise RuntimeError("Set ANTHROPIC_API_KEY to use the vision backend.")
        self._client = anthropic.Anthropic()
        self._model = model

    def text(self, image_bytes: bytes, media_type: str) -> str:
        b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=200,
                system=VISION_SYSTEM,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": media_type, "data": b64}},
                        {"type": "text",
                         "text": "What laser/energy device brands or models are visible "
                                 "in this clinic photo?"},
                    ],
                }],
            )
        except Exception as exc:  # network/auth/rate-limit — skip this image
            print(f"      vision call failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return ""
        out = "".join(b.text for b in resp.content if b.type == "text").strip()
        return "" if out.upper().startswith("NONE") else out


def make_backend(kind: str, vision_model: str = "claude-opus-4-8"):
    if kind == "ocr":
        return [OCRBackend()]
    if kind == "vision":
        return [VisionBackend(vision_model)]
    if kind == "both":
        return [VisionBackend(vision_model), OCRBackend()]
    raise ValueError(f"unknown image-recognition backend: {kind}")


# --------------------------------------------------------------------------- #
# High-level assessment
# --------------------------------------------------------------------------- #

def find_candela_in_photos(session, api_key: str, backends, name: str,
                           suburb: str = "", lat: float | None = None,
                           lon: float | None = None, max_photos: int = 5,
                           delay: float = 0.2) -> dict:
    """
    Look up a clinic on Google Maps and scan its photos for Candela devices.

    Returns: {matched: [..], evidence_url: str, source: str, photos_checked: int}
    """
    result = {"matched": [], "evidence_url": "", "source": "", "photos_checked": 0}

    place_id = find_place_id(session, api_key, name, suburb, lat, lon)
    if not place_id:
        return result
    result["evidence_url"] = place_url(place_id)

    refs = get_photo_references(session, api_key, place_id, max_photos)
    for ref in refs:
        time.sleep(delay)
        photo = fetch_photo(session, api_key, ref)
        if not photo:
            continue
        image_bytes, media_type = photo
        result["photos_checked"] += 1
        for backend in backends:
            text = backend.text(image_bytes, media_type)
            if not text:
                continue
            matched, _ = detect_devices(text)
            if matched:
                result["matched"] = matched
                result["source"] = ("gmaps-photo:" +
                                     ("vision" if isinstance(backend, VisionBackend) else "ocr"))
                return result
    return result
