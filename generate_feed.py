#!/usr/bin/env python3
"""
Angell Hasman -> LeadingRE XML feed generator

This version automatically:
1. Opens the hand-selected MyRealPage listing-set page.
2. Follows pagination (Page 1, Page 2, etc.).
3. Finds up to 20 listing-detail URLs.
4. Opens each listing and extracts current public listing data and photo URLs.
5. Writes feed-audit.txt so missing/uncertain fields are easy to review.
6. Rebuilds leadingre.xml only when all LeadingRE-required fields are present.

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
            "Mozilla/5.0 (compatible; AngellHasman-LeadingREFeed/2.0; "
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
    "OfficeEmail": "",
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
    "MemberEmail": "",
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


def extract_address(objects: list[dict], soup: BeautifulSoup, text: str) -> dict:
    result = {
        "StreetNumber": "",
        "StreetName": "",
        "StreetSuffix": "",
        "UnitNumber": "",
        "City": "",
        "PostalCode": "",
    }

    address_dict = None
    for obj in objects:
        candidate = obj.get("address")
        if isinstance(candidate, dict):
            address_dict = candidate
            break
        if obj.get("@type") == "PostalAddress":
            address_dict = obj
            break

    street_address = ""
    if address_dict:
        street_address = clean_text(address_dict.get("streetAddress"))
        result["City"] = clean_text(address_dict.get("addressLocality"))
        result["PostalCode"] = clean_text(address_dict.get("postalCode"))

    # Prefer a visible heading when JSON-LD has no street address.
    if not street_address:
        for tag_name in ["h1", "h2"]:
            tag = soup.find(tag_name)
            if tag:
                candidate = clean_text(tag.get_text(" ", strip=True))
                if candidate and "address on request" not in candidate.lower():
                    street_address = candidate
                    break

    # Remove common listing-title tail text if present.
    street_address = re.split(
        r"\s+(?:in|for sale|home for sale|condo for sale|land for sale)\b",
        street_address,
        maxsplit=1,
        flags=re.I,
    )[0].strip()

    # Pull unit prefix if present: PH2 1568 Alberni Street, 2403 125 E 14th Street, etc.
    unit = ""
    street = street_address
    m = re.match(r"^([A-Za-z]*\d+[A-Za-z-]*)\s+(\d+[A-Za-z-]?)\s+(.+)$", street_address)
    if m and not re.match(r"^\d+$", m.group(1)):
        unit, number, rest = m.groups()
        result["UnitNumber"] = unit
        result["StreetNumber"] = number
        street = f"{number} {rest}"

    if not result["StreetNumber"]:
        m = re.match(r"^(\d+[A-Za-z-]?)\s+(.+)$", street)
        if m:
            result["StreetNumber"] = m.group(1)
            rest = m.group(2)
        else:
            rest = street
    else:
        rest = street.split(" ", 1)[1] if " " in street else ""

    suffixes = {
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
    }
    if rest:
        bits = rest.replace(",", " ").split()
        if bits:
            last = bits[-1].strip(".").lower()
            if last in suffixes:
                result["StreetSuffix"] = suffixes[last]
                bits = bits[:-1]
            result["StreetName"] = " ".join(bits).strip()

    # City fallback from page text.
    if not result["City"]:
        for city in KNOWN_BC_CITIES:
            if re.search(rf"\b{re.escape(city)}\b", text, flags=re.I):
                result["City"] = city
                break

    # Postal code fallback from page text.
    if not result["PostalCode"]:
        m = re.search(
            r"\b([ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTVWXYZ])\s?(\d[ABCEGHJ-NPRSTVWXYZ]\d)\b",
            text,
            flags=re.I,
        )
        if m:
            result["PostalCode"] = (m.group(1) + " " + m.group(2)).upper()

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
    m = re.search(r"\bR\d{6,8}\b", labelled, re.I)
    if m:
        return m.group(0).upper()

    m = re.search(r"/listing\.(r\d{6,8})-", url, re.I)
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


def map_property_type(lines: list[str], text: str) -> tuple[str, str]:
    raw = find_label_value(
        lines,
        [
            "Property Type",
            "Dwelling Type",
            "Type",
        ],
    )
    hay = f"{raw} {text[:3000]}".lower()

    if re.search(r"\bland\b|\bvacant\b", hay):
        return "Land", "Land"
    if re.search(r"\bcondo\b|\bapartment\b|\bpenthouse\b", hay):
        return "Residential", "Condominium"
    if re.search(r"\btownhouse\b|\btownhome\b", hay):
        return "Residential", "Townhouse"
    if re.search(r"\bduplex\b", hay):
        return "Residential", "Duplex"
    if re.search(r"\bhouse\b|\bdetached\b|\bsingle family\b|\bchalet\b|\bestate\b", hay):
        return "Residential", "Single Family Residence"

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
        "logo", "favicon", "icon", "avatar", "agent", "profile",
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

        p = urlparse(url)
        if not p.netloc:
            continue

        image_ext = bool(re.search(r"\.(?:jpe?g|webp|png)$", p.path, re.I))
        if not image_ext and not any(term in low for term in good_terms):
            continue

        # De-duplicate query-string variants of the same image.
        key = (p.netloc.lower() + p.path).lower()
        if key in seen_path:
            continue
        seen_path.add(key)
        results.append(url)

    return results[:100]


def parse_listing(url: str, overrides: dict) -> tuple[dict, list[str], list[str], list[str]]:
    soup, raw_html, final_url = fetch(url)
    lines = page_lines(soup)
    text = "\n".join(lines)
    objects = parse_jsonld(soup)

    internal_id = extract_internal_id(final_url)
    listing_key = f"MRP-{internal_id}"
    mls = extract_mls(final_url, lines)

    address = extract_address(objects, soup, text)
    property_type, subtype = map_property_type(lines, text)

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

    if not mls:
        warnings.append("no MLS number visible; using stable MyRealPage record ID as ListingId")
    if record["InternetAddressDisplayYN"] == "0":
        warnings.append("address is hidden publicly; consider adding the true address in listing_overrides.json")
    if not record["StreetNumber"] or not record["StreetName"]:
        warnings.append("street address not fully available")
    if not record["PostalCode"]:
        warnings.append("postal code unavailable")
    if not record["PropertySubType"]:
        warnings.append("PropertySubType could not be confidently mapped")
    if not record["BedroomsTotal"] and record["PropertyType"] == "Residential":
        warnings.append("bedroom count unavailable")
    if not record["LivingArea"] and record["PropertyType"] == "Residential":
        warnings.append("living area unavailable")
    warnings.append("individual listing-agent attribution not yet enabled; using general Angell Hasman Member record")
    warnings.append("full/half bathroom split not yet available; both fields sent blank")

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
