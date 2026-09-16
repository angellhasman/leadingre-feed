#!/usr/bin/env python3
"""
Angell Hasman -> LeadingRE XML feed generator

This version automatically:
1. Opens the hand-selected MyRealPage listing-set page.
2. Follows pagination (Page 1, Page 2, etc.).
3. Finds up to 20 listing-detail URLs.
4. Opens each listing and extracts current public listing data and photo URLs.
5. Writes feed-audit.txt so missing/uncertain fields are easy to review.
6. Rebuilds leadingre.xml only when required fields and key safety checks pass.

The listing-set page remains the "control panel":
add/remove a listing there, and the nightly feed follows it automatically.

IMPORTANT:
- A listing with "Address on Request" is allowed to stay in the feed, but the audit
  will flag it so the true address can later be added as a private override if desired.
- Individual agent attribution is intentionally deferred. LeadingRE permits a general
  Member record representing the company/listing team. We can switch to individual
  agents after the automated 20-listing feed is proven.
"""

from __future__ import annotations

import base64
import html as html_lib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from xml.etree import ElementTree as ET


START_URL = "https://maxhasman.ca/leading-real-estate.html"
OUTPUT_FILE = Path("leadingre.xml")
AUDIT_FILE = Path("feed-audit.txt")
OVERRIDES_FILE = Path("listing_overrides.json")

MAX_LISTINGS = 20
MAX_INDEX_PAGES = 8
MIN_PHOTOS = 6

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (compatible; AngellHasman-LeadingREFeed/5.1; "
            "+https://angellhasman.github.io/leadingre-feed/)"
        ),
        "Accept-Language": "en-CA,en;q=0.9",
    }
)

OFFICE = {
    "OfficeKey": "ANGELL-HASMAN-WV",
    "OfficeStatus": "Active",
    "OfficeName": "Angell, Hasman & Associates (Malcolm Hasman) Realty Ltd.",
    "OfficeAddress1": "1544 Marine Drive",
    "OfficeAddress2": "Suite 203",
    "OfficeCity": "West Vancouver",
    "OfficeStateOrProvince": "BC",
    "OfficePostalCode": "V7V 1H8",
    "OfficeCountry": "CAN",
    "OfficePhone": "604-921-1188",
    "OfficeEmail": "info@angellhasman.ca",
    "OfficeMlsId": "V002321",
}

# LeadingRE permits one general Member record representing the company/listing team.
GENERAL_MEMBER = {
    "MemberKey": "ANGELL-HASMAN-LISTINGS",
    "OfficeKey": OFFICE["OfficeKey"],
    "MemberMlsId": "ANGELL-HASMAN-LISTINGS",
    "MemberLastName": "Listings",
    "MemberFirstName": "Angell Hasman",
    "MemberStatus": "Active",
    "MemberMobilePhone": "",
    "MemberEmail": "info@angellhasman.ca",
}

KNOWN_BC_CITIES = [
    "West Vancouver",
    "North Vancouver",
    "Vancouver",
    "Whistler",
    "Pemberton",
    "Squamish",
    "Burnaby",
    "Richmond",
    "Surrey",
    "White Rock",
    "Delta",
    "Langley",
    "Coquitlam",
    "Port Moody",
    "Port Coquitlam",
    "New Westminster",
    "Kelowna",
    "West Kelowna",
    "Lake Country",
    "Peachland",
    "Naramata",
    "Penticton",
]


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = html_lib.unescape(str(value))
    return re.sub(r"\s+", " ", value).strip()


def strip_fragment(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))


def fetch(url: str) -> tuple[BeautifulSoup, str, str]:
    """Fetch a page with retries. Returns soup, raw html, final URL."""
    last_error = None
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=35, allow_redirects=True)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml"), r.text, r.url
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def page_lines(soup: BeautifulSoup) -> list[str]:
    return [
        clean_text(x)
        for x in soup.get_text("\n", strip=True).splitlines()
        if clean_text(x)
    ]


def page_text(soup: BeautifulSoup) -> str:
    return "\n".join(page_lines(soup))


def load_overrides() -> dict:
    if not OVERRIDES_FILE.exists():
        return {}
    try:
        data = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        raise RuntimeError(f"Could not read {OVERRIDES_FILE}: {exc}")


def discover_listing_urls() -> tuple[list[str], list[str]]:
    """
    Crawl the selected MyRealPage page and its pagination pages.
    Only detail URLs under /leading-real-estate.html/listing. are accepted.
    """
    queue = [START_URL]
    visited: set[str] = set()
    listing_urls: list[str] = []
    seen_listings: set[str] = set()
    audit_notes: list[str] = []

    start = urlparse(START_URL)
    start_path = start.path.rstrip("/")

    while queue and len(visited) < MAX_INDEX_PAGES:
        page_url = strip_fragment(queue.pop(0))
        if page_url in visited:
            continue
        visited.add(page_url)

        soup, _raw, final_url = fetch(page_url)
        audit_notes.append(f"INDEX PAGE: {final_url}")

        for a in soup.find_all("a", href=True):
            href = clean_text(a.get("href"))
            if not href:
                continue
            absolute = strip_fragment(urljoin(final_url, href))
            p = urlparse(absolute)

            if p.netloc and p.netloc.lower() != start.netloc.lower():
                continue

            # Listing-detail links from this exact hand-selected listing set.
            if "/leading-real-estate.html/listing." in p.path.lower():
                if absolute not in seen_listings:
                    seen_listings.add(absolute)
                    listing_urls.append(absolute)
                continue

            # Pagination: keep links on the same listing-set path when the anchor
            # looks like a page number/next arrow or the query looks page-related.
            same_path = p.path.rstrip("/").lower() == start_path.lower()
            if not same_path:
                continue

            anchor_text = clean_text(a.get_text(" ", strip=True)).lower()
            query_l = p.query.lower()
            paginationish = (
                anchor_text.isdigit()
                or anchor_text in {"next", ">", "›", "»", "→"}
                or "next" in anchor_text
                or any(token in query_l for token in ["_pg=", "page=", "start=", "offset=", "ipp="])
            )

            if paginationish and absolute not in visited and absolute not in queue:
                queue.append(absolute)

    if len(listing_urls) > MAX_LISTINGS:
        raise RuntimeError(
            f"Discovered {len(listing_urls)} listings, which is more than the configured "
            f"maximum of {MAX_LISTINGS}. Refusing to continue in case the wrong page was read."
        )

    return listing_urls, audit_notes


def find_label_value(lines: list[str], labels: list[str]) -> str:
    normalized = [re.sub(r"[:\s]+$", "", x).lower() for x in lines]
    targets = {re.sub(r"[:\s]+$", "", x).lower() for x in labels}
    for i, label in enumerate(normalized):
        if label in targets and i + 1 < len(lines):
            return lines[i + 1]
    return ""


def walk_json(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_json(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_json(v)


def parse_jsonld(soup: BeautifulSoup) -> list[dict]:
    objects: list[dict] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
            objects.extend(x for x in walk_json(parsed) if isinstance(x, dict))
        except Exception:
            # Some publishers output imperfect JSON-LD. It is optional for us.
            pass
    return objects


def first_json_value(objects: list[dict], keys: list[str]) -> str:
    for obj in objects:
        for key in keys:
            value = obj.get(key)
            if isinstance(value, (str, int, float)) and clean_text(value):
                return clean_text(value)
    return ""


def listing_title(soup: BeautifulSoup) -> str:
    candidates = []
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        candidates.append(clean_text(og.get("content")))
    if soup.title:
        candidates.append(clean_text(soup.title.get_text(" ", strip=True)))
    for tag_name in ["h1", "h2"]:
        tag = soup.find(tag_name)
        if tag:
            candidates.append(clean_text(tag.get_text(" ", strip=True)))
    return " | ".join(x for x in candidates if x)


CITY_SLUGS = [
    ("west-vancouver", "West Vancouver"),
    ("north-vancouver", "North Vancouver"),
    ("vancouver-west", "Vancouver"),
    ("new-westminster", "New Westminster"),
    ("port-coquitlam", "Port Coquitlam"),
    ("port-moody", "Port Moody"),
    ("west-kelowna", "West Kelowna"),
    ("sunshine-coast", "Sunshine Coast"),
    ("white-rock", "White Rock"),
    ("lake-country", "Lake Country"),
    ("vancouver", "Vancouver"),
    ("whistler", "Whistler"),
    ("pemberton", "Pemberton"),
    ("squamish", "Squamish"),
    ("burnaby", "Burnaby"),
    ("richmond", "Richmond"),
    ("surrey", "Surrey"),
    ("delta", "Delta"),
    ("langley", "Langley"),
    ("coquitlam", "Coquitlam"),
    ("kelowna", "Kelowna"),
    ("peachland", "Peachland"),
    ("naramata", "Naramata"),
    ("penticton", "Penticton"),
]


STREET_SUFFIXES = {
    "street": "Street", "st": "Street",
    "drive": "Drive", "dr": "Drive",
    "road": "Road", "rd": "Road",
    "avenue": "Avenue", "ave": "Avenue",
    "court": "Court", "ct": "Court",
    "lane": "Lane", "ln": "Lane",
    "place": "Place", "pl": "Place",
    "crescent": "Crescent", "cr": "Crescent", "cres": "Crescent",
    "boulevard": "Boulevard", "blvd": "Boulevard",
    "way": "Way",
    "terrace": "Terrace",
    "highway": "Highway", "hwy": "Highway",
    "close": "Close",
    "circle": "Circle",
}


def _parse_street_text(value: str) -> dict:
    result = {
        "StreetNumber": "",
        "StreetName": "",
        "StreetSuffix": "",
        "UnitNumber": "",
    }
    value = clean_text(value).replace(",", " ").strip()
    value = re.sub(r"^#\s*", "", value)
    value = re.sub(r"\s+", " ", value)

    # Unit + street number, e.g. PH2 1568 Alberni Street,
    # 2901 1408 Robson Street, or 19 180 Sheerwater Court.
    m = re.match(
        r"^([A-Za-z]*\d+[A-Za-z-]*|\d+)\s+(\d+[A-Za-z-]?)\s+(.+)$",
        value,
        flags=re.I,
    )
    if m:
        first, second, rest = m.groups()
        # Two leading numeric tokens almost always mean Unit + StreetNumber
        # on the selected MyRealPage inventory. An alphanumeric first token
        # such as PH2 is also a unit.
        if (first.isdigit() and second[0].isdigit()) or re.search(r"[A-Za-z]", first):
            result["UnitNumber"] = first
            result["StreetNumber"] = second
        else:
            result["StreetNumber"] = first
            rest = f"{second} {rest}"
    else:
        m = re.match(r"^(\d+[A-Za-z-]?)\s+(.+)$", value)
        if not m:
            return result
        result["StreetNumber"], rest = m.groups()

    bits = rest.split()
    if bits:
        last = bits[-1].strip(".").lower()
        if last in STREET_SUFFIXES:
            result["StreetSuffix"] = STREET_SUFFIXES[last]
            bits = bits[:-1]
        result["StreetName"] = " ".join(bits).strip()
    return result


def _postal_from_slug(slug: str) -> tuple[str, str]:
    """Return slug without trailing Canadian postal code, plus formatted postal code."""
    m = re.search(
        r"-(?P<a>[abceghj-nprstvxy]\d[abceghj-nprstvwxyz])-(?P<b>\d[abceghj-nprstvwxyz]\d)$",
        slug,
        flags=re.I,
    )
    if not m:
        return slug, ""
    postal = f"{m.group('a')} {m.group('b')}".upper()
    return slug[:m.start()], postal


def _address_from_url(url: str) -> dict:
    result = {
        "StreetNumber": "",
        "StreetName": "",
        "StreetSuffix": "",
        "UnitNumber": "",
        "City": "",
        "PostalCode": "",
    }

    path = urlparse(url).path.rstrip("/")
    m = re.search(r"/listing\.[^/]+?-(.+)\.(\d{6,})$", path, flags=re.I)
    if not m:
        return result

    slug = m.group(1).lower()
    slug, postal = _postal_from_slug(slug)
    result["PostalCode"] = postal

    # Hidden-address records deliberately contain no street address.
    if slug.startswith("address-on-request") or slug.startswith("address-request"):
        for city_slug, city in CITY_SLUGS:
            if city_slug in slug:
                result["City"] = city
                break
        return result

    address_slug = slug
    city_match_pos = None
    for city_slug, city in CITY_SLUGS:
        if slug == city_slug or slug.startswith(city_slug + "-"):
            pos = 0
        else:
            token = f"-{city_slug}"
            pos = slug.find(token)
        if pos != -1 and (city_match_pos is None or pos < city_match_pos):
            city_match_pos = pos
            result["City"] = city
            address_slug = slug[:pos]

    # If the slug is only the city (e.g. PH2 Kengo Kuma URL), let title parsing fill address.
    if result["City"] and not address_slug:
        return result

    street_text = address_slug.replace("-", " ").strip()
    parsed = _parse_street_text(street_text)
    result.update(parsed)
    return result


def _address_from_title(soup: BeautifulSoup, city_hint: str = "") -> dict:
    result = {
        "StreetNumber": "",
        "StreetName": "",
        "StreetSuffix": "",
        "UnitNumber": "",
        "City": city_hint,
        "PostalCode": "",
    }
    title = listing_title(soup)
    if not title or "address on request" in title.lower():
        return result

    # Prefer the part before " in <city>", ": ... for sale", or "|".
    candidate = title.split("|", 1)[0].strip()
    candidate = re.split(r"\s+in\s+[A-Za-z ]+(?::|$)", candidate, maxsplit=1, flags=re.I)[0]
    candidate = re.split(r"\s*:\s*", candidate, maxsplit=1)[0].strip()

    # Find a plausible address beginning with optional unit + street number.
    m = re.search(
        r"\b((?:[A-Za-z]*\d+[A-Za-z-]*\s+)?\d+[A-Za-z-]?\s+[A-Za-z0-9' .-]+"
        r"(?:Street|St|Drive|Dr|Road|Rd|Avenue|Ave|Court|Ct|Lane|Ln|Place|Pl|"
        r"Crescent|Cres|Boulevard|Blvd|Way|Terrace|Highway|Hwy))\b",
        candidate,
        flags=re.I,
    )
    if m:
        result.update(_parse_street_text(m.group(1)))

    if not result["City"]:
        for _slug, city in CITY_SLUGS:
            if re.search(rf"\b{re.escape(city)}\b", title, flags=re.I):
                result["City"] = city
                break

    pm = re.search(
        r"\b([ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTVWXYZ])\s?(\d[ABCEGHJ-NPRSTVWXYZ]\d)\b",
        title,
        flags=re.I,
    )
    if pm:
        result["PostalCode"] = f"{pm.group(1)} {pm.group(2)}".upper()

    return result


def extract_address(url: str, soup: BeautifulSoup, text: str) -> dict:
    """
    Property address extraction intentionally ignores generic JSON-LD address blocks.
    MyRealPage pages contain the brokerage office address in structured metadata, which
    previously caused every property to be incorrectly mapped to 1544 Marine Drive.
    """
    result = _address_from_url(url)

    # If the URL omits the street address (for example, a short custom URL),
    # use the listing title/heading rather than sitewide contact information.
    if not result["StreetNumber"] or not result["StreetName"]:
        title_result = _address_from_title(soup, result["City"])
        for key, value in title_result.items():
            if value and not result.get(key):
                result[key] = value

    # Safe city/postal fallbacks from visible listing text.
    if not result["City"]:
        for city in KNOWN_BC_CITIES + ["Sunshine Coast"]:
            if re.search(rf"\b{re.escape(city)}\b", text, flags=re.I):
                result["City"] = city
                break

    if not result["PostalCode"]:
        lines = page_lines(soup)
        labelled_postal = find_label_value(
            lines,
            ["Postal Code", "PostalCode", "Postal / Zip Code", "Zip Code"],
        )
        pm = re.search(
            r"\b([ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTVWXYZ])\s?(\d[ABCEGHJ-NPRSTVWXYZ]\d)\b",
            labelled_postal,
            flags=re.I,
        )
        if pm:
            result["PostalCode"] = f"{pm.group(1)} {pm.group(2)}".upper()

    # Cosmetic normalization for public presentation.
    if result["StreetName"]:
        result["StreetName"] = result["StreetName"].title()

    return result



def extract_price(lines: list[str], text: str, objects: list[dict]) -> str:
    value = first_json_value(objects, ["price", "lowPrice", "highPrice"])
    if value:
        digits = re.sub(r"[^\d.]", "", value)
        if digits:
            return str(int(float(digits)))

    labelled = find_label_value(lines, ["Price", "List Price"])
    for source in [labelled, text]:
        m = re.search(r"\$\s*([0-9][0-9,]*)", source)
        if m:
            return m.group(1).replace(",", "")
    return ""


def extract_status(lines: list[str], text: str) -> str:
    raw = find_label_value(lines, ["Status"])
    if not raw:
        m = re.search(r"\bStatus\s*:?\s*(Active Under Contract|Pending|Active)\b", text, re.I)
        raw = m.group(1) if m else ""
    low = raw.lower()
    if "active under contract" in low:
        return "Active Under Contract"
    if "pending" in low:
        return "Pending"
    if "active" in low:
        return "Active"
    return ""


def extract_remarks(soup: BeautifulSoup, objects: list[dict]) -> str:
    candidates: list[str] = []

    for obj in objects:
        for key in ["description", "disambiguatingDescription"]:
            value = clean_text(obj.get(key))
            if 100 <= len(value) <= 8000:
                candidates.append(value)

    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if meta and meta.get("content"):
        value = clean_text(meta.get("content"))
        if 100 <= len(value) <= 8000:
            candidates.append(value)

    selectors = [
        '[class*="remarks"]',
        '[class*="description"]',
        '[class*="listing-description"]',
        '[id*="remarks"]',
        '[id*="description"]',
    ]
    for selector in selectors:
        for node in soup.select(selector):
            value = clean_text(node.get_text(" ", strip=True))
            if 100 <= len(value) <= 8000:
                candidates.append(value)

    if not candidates:
        return ""

    # De-duplicate and prefer rich, substantial copy.
    unique = list(dict.fromkeys(candidates))
    return max(unique, key=len)


def extract_mls(url: str, lines: list[str]) -> str:
    labelled = find_label_value(lines, ["MLS® Num", "MLS Num", "MLS® Number", "MLS Number"])
    m = re.search(r"\b(?:R\d{6,8}|\d{8})\b", labelled, re.I)
    if m:
        return m.group(0).upper()

    # MyRealPage URL identifier immediately after "listing." is the source listing ID.
    m = re.search(r"/listing\.((?:r\d{6,8})|(?:\d{8}))-", url, re.I)
    if m:
        return m.group(1).upper()

    return ""



def extract_internal_id(url: str) -> str:
    # MyRealPage detail URLs end with a stable numeric record id.
    path = urlparse(url).path.rstrip("/")
    m = re.search(r"\.(\d{6,})$", path)
    if m:
        return m.group(1)
    # Stable fallback: normalized path.
    return re.sub(r"[^A-Za-z0-9]+", "-", path).strip("-").upper()


def map_property_type(lines: list[str], soup: BeautifulSoup, url: str) -> tuple[str, str]:
    raw = find_label_value(
        lines,
        [
            "Property Type",
            "Dwelling Type",
            "Type",
            "Building Type",
        ],
    )
    title = listing_title(soup)
    hay = f"{raw} {title} {url}".lower()

    # Specific classifications first.
    if re.search(r"\bland\b|\bvacant land\b", hay):
        return "Land", "Land"
    if re.search(r"\bapartment/condo\b|\bapartment\b|\bcondo\b|\bpenthouse\b", hay):
        return "Residential", "Condominium"
    if re.search(r"\btownhouse\b|\btownhome\b", hay):
        return "Residential", "Townhouse"
    if re.search(r"\bduplex\b", hay):
        return "Residential", "Duplex"
    if re.search(r"\bhouse\b|\bsingle family\b|\bsingle-family\b|\bdetached\b|\bchalet\b", hay):
        return "Residential", "Single Family Residence"

    # "Residential" alone is valid PropertyType but doesn't reliably identify subtype.
    if "residential" in hay:
        return "Residential", ""

    return "Residential", ""



def numeric_label(lines: list[str], labels: list[str]) -> str:
    raw = find_label_value(lines, labels)
    m = re.search(r"[\d,]+(?:\.\d+)?", raw)
    return m.group(0).replace(",", "") if m else ""


def _append_srcset(value: str, base_url: str, output: list[str]) -> None:
    renditions = []
    for item in value.split(","):
        bits = item.strip().split()
        if not bits:
            continue
        url = urljoin(base_url, bits[0])
        width = 0
        if len(bits) > 1 and bits[1].lower().endswith("w"):
            try:
                width = int(bits[1][:-1])
            except ValueError:
                pass
        renditions.append((width, url))
    for _width, url in sorted(renditions, reverse=True):
        output.append(url)


def _decoded_myrealpage_source(url: str) -> str:
    """Decode the original source embedded in MyRealPage CDN URLs when possible."""
    try:
        p = urlparse(url)
        if "myrealpage.com" not in p.netloc.lower():
            return ""
        parts = [part for part in p.path.split("/") if part]
        if not parts:
            return ""
        token = parts[-1]
        token += "=" * ((4 - len(token) % 4) % 4)
        return base64.urlsafe_b64decode(token).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _is_non_listing_site_asset(url: str) -> bool:
    """Reject site-wide branding/headshot assets that are not property photography."""
    low = url.lower()

    # Direct assets from the brokerage website (for example Max's headshot).
    if "maxhasman.ca/_media/" in low or "maxhasman.ca/__media/" in low:
        return True

    # MyRealPage's image CDN can hide the original site asset inside a base64 path.
    decoded = _decoded_myrealpage_source(url).lower()
    if decoded:
        if "max-hasman.myrealpagewebsite.com/_media/" in decoded or "max-hasman.myrealpagewebsite.com/__media/" in decoded:
            return True
        if any(term in decoded for term in ("logo", "headshot", "avatar", "agent", "profile")):
            return True

    return False



def _prefer_original_listing_image(url: str) -> str:
    """
    Prefer the original public listing image behind MyRealPage's resized CDN URL.

    MyRealPage listing pages often expose 320px thumbnail renditions in the HTML.
    The CDN URL embeds the original S3 listing-photo URL as its final base64 path
    segment. LeadingRE recommends substantially larger listing images, so when the
    embedded source is a genuine mrp-listings image we send that original instead.

    Non-MyRealPage URLs (for example direct CloudFront listing photos) are left
    unchanged.
    """
    source = _decoded_myrealpage_source(url)
    if not source:
        return url

    try:
        p = urlparse(source)
    except Exception:
        return url

    host = p.netloc.lower()
    path = p.path.lower()
    if host == "s3.amazonaws.com" and path.startswith("/mrp-listings/"):
        if re.search(r"\.(?:jpe?g|webp|png)$", p.path, re.I):
            # Use HTTPS for the public original image.
            return urlunparse(("https", p.netloc, p.path, p.params, p.query, ""))

    return url

def extract_photo_urls(soup: BeautifulSoup, raw_html: str, base_url: str, listing_token: str) -> list[str]:
    candidates: list[str] = []

    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        candidates.append(urljoin(base_url, og["content"]))

    for tag in soup.find_all(["img", "source"]):
        for attr in ["src", "data-src", "data-original", "data-lazy-src", "data-image"]:
            value = tag.get(attr)
            if value:
                candidates.append(urljoin(base_url, value))
        for attr in ["srcset", "data-srcset"]:
            value = tag.get(attr)
            if value:
                _append_srcset(value, base_url, candidates)

    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if href and re.search(r"\.(?:jpe?g|webp|png)(?:\?|$)", href, re.I):
            candidates.append(urljoin(base_url, href))

    unescaped = raw_html.replace("\\/", "/")
    candidates.extend(
        re.findall(
            r'https?://[^"\'\s<>]+?\.(?:jpe?g|webp|png)(?:\?[^"\'\s<>]*)?',
            unescaped,
            flags=re.I,
        )
    )

    bad_terms = (
        "logo", "favicon", "icon", "avatar", "agent", "profile", "headshot",
        "reciprocity", "facebook", "instagram", "linkedin", "youtube",
        "map", "marker", "captcha", "spinner",
    )
    good_terms = (
        "photo", "image", "listing", "property", "media",
        "cdn", "mls", "ddf", listing_token.lower(),
    )

    results: list[str] = []
    seen_path: set[str] = set()

    for url in candidates:
        url = clean_text(url).strip("'\"")
        if not url.startswith(("http://", "https://")):
            continue
        low = url.lower()
        if any(term in low for term in bad_terms):
            continue
        if _is_non_listing_site_asset(url):
            continue

        p = urlparse(url)
        if not p.netloc:
            continue

        image_ext = bool(re.search(r"\.(?:jpe?g|webp|png)$", p.path, re.I))
        if not image_ext and not any(term in low for term in good_terms):
            continue

        # De-duplicate alternate MyRealPage renditions of the same source photo.
        # MyRealPage CDN variants generally preserve the same encoded original
        # source as the final path segment while changing transformation segments.
        if "myrealpage.com" in p.netloc.lower():
            path_parts = [part for part in p.path.split("/") if part]
            if path_parts:
                key = f"{p.netloc.lower()}::{path_parts[-1].lower()}"
            else:
                key = (p.netloc.lower() + p.path).lower()
        else:
            key = (p.netloc.lower() + p.path).lower()

        if key in seen_path:
            continue
        seen_path.add(key)
        results.append(_prefer_original_listing_image(url))

    # Some MyRealPage listing pages expose the same gallery twice:
    # once as the original S3 mrp-listings images and once through CloudFront.
    # When the two sets have exactly the same photo count, keep the S3 originals
    # and discard the duplicate CloudFront rendition set.
    s3_urls = []
    cloudfront_urls = []
    for image_url in results:
        parsed = urlparse(image_url)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if host == "s3.amazonaws.com" and path.startswith("/mrp-listings/"):
            s3_urls.append(image_url)
        elif host.endswith("cloudfront.net"):
            cloudfront_urls.append(image_url)

    if s3_urls and len(s3_urls) == len(cloudfront_urls):
        cloudfront_set = set(cloudfront_urls)
        results = [image_url for image_url in results if image_url not in cloudfront_set]

    return results[:150]


def parse_listing(url: str, overrides: dict) -> tuple[dict, list[str], list[str], list[str]]:
    soup, raw_html, final_url = fetch(url)
    lines = page_lines(soup)
    text = "\n".join(lines)
    objects = parse_jsonld(soup)

    internal_id = extract_internal_id(final_url)
    listing_key = f"MRP-{internal_id}"
    mls = extract_mls(final_url, lines)

    address = extract_address(final_url, soup, text)
    property_type, subtype = map_property_type(lines, soup, final_url)

    record = {
        "ListingKey": listing_key,
        "ListAgentKey": GENERAL_MEMBER["MemberKey"],
        "ListOfficeKey": OFFICE["OfficeKey"],
        "ListingId": mls or listing_key,
        "PropertyType": property_type,
        "PropertySubType": subtype,
        "StreetNumber": address["StreetNumber"],
        "StreetName": address["StreetName"],
        "StreetSuffix": address["StreetSuffix"],
        "UnitNumber": address["UnitNumber"],
        "City": address["City"],
        "StateOrProvince": "BC",
        "PostalCode": address["PostalCode"],
        "Country": "CAN",
        "ListPrice": extract_price(lines, text, objects),
        "ListingURL": final_url,
        "PublicRemarks": extract_remarks(soup, objects),
        "BedroomsTotal": numeric_label(lines, ["Bedrooms", "Bedrooms Total"]),
        "BathroomsFull": "",
        "BathroomsHalf": "",
        "StandardStatus": extract_status(lines, text),
        "LivingArea": numeric_label(lines, ["Floor Area", "Floor Area Total", "Living Area"]),
        "LivingAreaUnits": "Square Feet",
        "InternetAddressDisplayYN": "0" if "address on request" in text.lower() else "1",
        "InternetPriceDisplayYN": "0" if "price on request" in text.lower() else "1",
        "Currency": "CAD",
    }

    # Apply manual overrides by MLS number, ListingKey, or internal MRP id.
    for key in [mls, listing_key, internal_id]:
        if key and isinstance(overrides.get(key), dict):
            for field, value in overrides[key].items():
                if field in record:
                    record[field] = "" if value is None else str(value)

    photos = extract_photo_urls(
        soup,
        raw_html,
        final_url,
        mls or internal_id,
    )

    fatal: list[str] = []
    warnings: list[str] = []

    # LeadingRE Required fields / conditions needed for Canadian Residential inventory.
    required = [
        "ListingKey",
        "PropertyType",
        "City",
        "StateOrProvince",
        "Country",
        "ListPrice",
        "PublicRemarks",
        "StandardStatus",
        "InternetAddressDisplayYN",
        "InternetPriceDisplayYN",
        "Currency",
    ]
    for field in required:
        if not clean_text(record.get(field)):
            fatal.append(f"missing required {field}")

    if record["StandardStatus"] not in {"Active", "Active Under Contract", "Pending"}:
        fatal.append(f"unsupported/non-active StandardStatus={record['StandardStatus']!r}")

    if record["ListPrice"]:
        try:
            if float(record["ListPrice"]) <= 0:
                fatal.append("ListPrice is zero or negative")
        except ValueError:
            fatal.append(f"invalid ListPrice={record['ListPrice']!r}")

    if len(photos) < MIN_PHOTOS:
        fatal.append(f"only {len(photos)} usable photo URLs found; need at least {MIN_PHOTOS}")

    # Safety checks for the two parser failures most likely to create misleading
    # public data: accidentally using the brokerage office as the property address,
    # or classifying a substantial residence as vacant land.
    office_address_match = (
        record.get("StreetNumber") == "1544"
        and "Marine" in record.get("StreetName", "")
        and record.get("City") == "West Vancouver"
    )
    if office_address_match:
        fatal.append("property address incorrectly matches brokerage office address")

    if record["InternetAddressDisplayYN"] == "1" and (
        not record.get("StreetNumber") or not record.get("StreetName")
    ):
        fatal.append("public-address listing is missing a usable street address")

    if record.get("PropertyType") == "Land" and (
        record.get("BedroomsTotal") or record.get("LivingArea")
    ):
        fatal.append(
            "suspicious Land classification on a listing that has bedrooms/living area"
        )

    if not mls:
        warnings.append("no MLS number visible; using stable MyRealPage record ID as ListingId")
    if record["InternetAddressDisplayYN"] == "0":
        warnings.append("address is hidden publicly; feed will suppress it, but LeadingRE recommends supplying the true address privately for geocoding")
    if not record["StreetNumber"] or not record["StreetName"]:
        warnings.append("street address not fully available")
    if not record["PostalCode"]:
        warnings.append("postal code unavailable")
    if record["PostalCode"] == OFFICE["OfficePostalCode"] and not (
        record.get("StreetNumber") == "1544" and "Marine" in record.get("StreetName", "")
    ):
        fatal.append(
            "property postal code incorrectly matches brokerage office postal code; "
            "use listing_overrides.json if this property genuinely shares that postal code"
        )
    if not record["PropertySubType"]:
        warnings.append("PropertySubType could not be confidently mapped")
    if not record["BedroomsTotal"] and record["PropertyType"] == "Residential":
        warnings.append("bedroom count unavailable")
    if not record["LivingArea"] and record["PropertyType"] == "Residential":
        warnings.append("living area unavailable")
    warnings.append("individual listing-agent attribution not yet enabled; using general Angell Hasman Member record")
    warnings.append("full/half bathroom split not yet available; both Recommended fields are sent blank")
    if len(photos) >= 150:
        warnings.append("photo extractor reached its 150-photo safety cap; review for duplicates")

    return record, photos, fatal, warnings


def add_text(parent: ET.Element, name: str, value: str | None) -> ET.Element:
    node = ET.SubElement(parent, name)
    if value is not None and value != "":
        node.text = str(value)
    return node


def build_xml(records: list[tuple[dict, list[str]]]) -> ET.ElementTree:
    root = ET.Element("Data")

    offices = ET.SubElement(root, "Offices")
    office = ET.SubElement(offices, "Office")
    for key in [
        "OfficeKey", "OfficeStatus", "OfficeName", "OfficeAddress1",
        "OfficeAddress2", "OfficeCity", "OfficeStateOrProvince",
        "OfficePostalCode", "OfficeCountry", "OfficePhone",
        "OfficeEmail", "OfficeMlsId",
    ]:
        add_text(office, key, OFFICE.get(key, ""))

    members = ET.SubElement(root, "Members")
    member = ET.SubElement(members, "Member")
    for key in [
        "MemberKey", "OfficeKey", "MemberMlsId", "MemberLastName",
        "MemberFirstName", "MemberStatus", "MemberMobilePhone", "MemberEmail",
    ]:
        add_text(member, key, GENERAL_MEMBER.get(key, ""))

    properties = ET.SubElement(root, "Properties")
    for record, _photos in records:
        prop = ET.SubElement(properties, "Property")

        # Order follows LeadingRE XML Feed Specification v1.1.
        for key in [
            "ListingKey",
            "ListAgentKey",
            "ListOfficeKey",
            "ListingId",
            "PropertyType",
            "PropertySubType",
            "StreetNumber",
            "StreetName",
            "StreetSuffix",
            "UnitNumber",
            "City",
            "StateOrProvince",
            "PostalCode",
            "Country",
            "ListPrice",
            "ListingURL",
            "PublicRemarks",
            "BedroomsTotal",
            "BathroomsFull",
            "BathroomsHalf",
            "StandardStatus",
            "LivingArea",
            "LivingAreaUnits",
            "InternetAddressDisplayYN",
            "InternetPriceDisplayYN",
            "Currency",
        ]:
            add_text(prop, key, record.get(key, ""))

    medias = ET.SubElement(root, "Medias")
    for record, photos in records:
        for order, url in enumerate(photos, start=1):
            media = ET.SubElement(medias, "Media")
            add_text(media, "MediaKey", f"{record['ListingKey']}-PHOTO-{order}")
            add_text(media, "Order", str(order))
            add_text(media, "MediaCategory", "Photo")
            add_text(media, "MediaURL", url)
            add_text(media, "ResourceName", "Property")
            add_text(media, "ResourceRecordID", record["ListingKey"])

    ET.indent(root, space="  ")
    return ET.ElementTree(root)


def write_audit(
    discovered: list[str],
    index_notes: list[str],
    results: list[tuple[dict, list[str], list[str], list[str]]],
    global_fatal: list[str],
    feed_updated: bool,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    total_photos = sum(len(photos) for _record, photos, _fatal, _warnings in results)
    all_fatal = list(global_fatal)
    for record, _photos, fatal, _warnings in results:
        all_fatal.extend(f"{record.get('ListingId')}: {x}" for x in fatal)

    lines = [
        "ANGELL HASMAN -> LEADINGRE FEED AUDIT",
        f"Generated: {now}",
        "",
        f"Listing-set URL: {START_URL}",
        f"Listings discovered: {len(discovered)}",
        f"Listings parsed: {len(results)}",
        f"Total photo URLs: {total_photos}",
        f"Feed updated: {'YES' if feed_updated else 'NO'}",
        f"Fatal issues: {len(all_fatal)}",
        "",
    ]

    if len(discovered) != 20:
        lines.append(
            f"NOTE: The current selected-listing count is {len(discovered)} rather than 20. "
            "This is not automatically fatal as long as the page intentionally contains that number."
        )
        lines.append("")

    if index_notes:
        lines.append("INDEX PAGES READ")
        lines.extend(f"- {x}" for x in index_notes)
        lines.append("")

    if global_fatal:
        lines.append("GLOBAL FATAL ISSUES")
        lines.extend(f"- {x}" for x in global_fatal)
        lines.append("")

    for i, (record, photos, fatal, warnings) in enumerate(results, start=1):
        lines.extend(
            [
                f"{i}. {record.get('ListingId') or record.get('ListingKey')}",
                f"   ListingKey: {record.get('ListingKey')}",
                f"   URL: {record.get('ListingURL')}",
                f"   Address: {record.get('UnitNumber')} {record.get('StreetNumber')} "
                f"{record.get('StreetName')} {record.get('StreetSuffix')}, "
                f"{record.get('City')} {record.get('PostalCode')}".replace("  ", " ").strip(),
                f"   Price: {record.get('ListPrice')}",
                f"   Status: {record.get('StandardStatus')}",
                f"   Type: {record.get('PropertyType')} / {record.get('PropertySubType')}",
                f"   Bedrooms: {record.get('BedroomsTotal') or '[blank]'}",
                f"   Living area: {record.get('LivingArea') or '[blank]'}",
                f"   Photos: {len(photos)}",
                f"   Fatal: {', '.join(fatal) if fatal else 'none'}",
                "   Warnings:",
            ]
        )
        lines.extend(f"     - {w}" for w in warnings)
        lines.append("")

    AUDIT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    feed_updated = False
    global_fatal: list[str] = []
    results: list[tuple[dict, list[str], list[str], list[str]]] = []
    discovered: list[str] = []
    index_notes: list[str] = []

    try:
        overrides = load_overrides()
        discovered, index_notes = discover_listing_urls()

        if not discovered:
            global_fatal.append("No listing-detail URLs were discovered.")
        if len(discovered) > MAX_LISTINGS:
            global_fatal.append(
                f"Discovered {len(discovered)} listings; maximum allowed is {MAX_LISTINGS}."
            )

        for i, url in enumerate(discovered, start=1):
            print(f"[{i}/{len(discovered)}] Reading {url}")
            try:
                record, photos, fatal, warnings = parse_listing(url, overrides)
            except Exception as exc:
                # Create a placeholder audit entry so one bad listing is easy to identify.
                record = {
                    "ListingId": url,
                    "ListingKey": "",
                    "ListingURL": url,
                    "City": "",
                    "ListPrice": "",
                    "StandardStatus": "",
                    "PropertyType": "",
                    "PropertySubType": "",
                    "BedroomsTotal": "",
                    "LivingArea": "",
                    "UnitNumber": "",
                    "StreetNumber": "",
                    "StreetName": "",
                    "StreetSuffix": "",
                    "PostalCode": "",
                }
                photos = []
                fatal = [f"could not parse listing: {exc}"]
                warnings = []
            results.append((record, photos, fatal, warnings))

        any_listing_fatal = any(fatal for _record, _photos, fatal, _warnings in results)

        if not global_fatal and not any_listing_fatal and results:
            active_records = [
                (record, photos)
                for record, photos, _fatal, _warnings in results
                if record.get("StandardStatus") in {"Active", "Active Under Contract", "Pending"}
            ]
            if not active_records:
                global_fatal.append("No active records remained after parsing.")
            else:
                tree = build_xml(active_records)
                temp = OUTPUT_FILE.with_suffix(".xml.tmp")
                tree.write(temp, encoding="utf-8", xml_declaration=True)
                temp.replace(OUTPUT_FILE)
                feed_updated = True

        write_audit(discovered, index_notes, results, global_fatal, feed_updated)

        if feed_updated:
            print(f"SUCCESS: updated {OUTPUT_FILE} with {len(results)} listing(s).")
        else:
            print(
                "AUDIT ONLY: leadingre.xml was NOT overwritten because one or more "
                "items need review. See feed-audit.txt."
            )
        print(f"Wrote {AUDIT_FILE}.")
        return 0

    except Exception as exc:
        global_fatal.append(f"Unexpected generator error: {exc}")
        try:
            write_audit(discovered, index_notes, results, global_fatal, False)
        except Exception:
            pass
        print(f"ERROR: {exc}", file=sys.stderr)
        # Return 0 so GitHub can still commit feed-audit.txt for review.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
