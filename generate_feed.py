#!/usr/bin/env python3
"""
Generate a LeadingRE XML feed from selected MyRealPage listing pages.

Initial test version:
- Uses two known listing URLs.
- Refreshes current price/status/remarks when they can be read from MyRealPage.
- Collects public listing-photo URLs from the listing page.
- Refuses to overwrite leadingre.xml if a listing has fewer than 6 usable photos.
- Uses stable OfficeKey, MemberKey, ListingKey, and MediaKey values.

After this two-listing test succeeds, the listing discovery can be expanded to the
full MyRealPage "Leading Real Estate" page.
"""

from __future__ import annotations

import html as html_lib
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from xml.etree import ElementTree as ET


OUTPUT_FILE = Path("leadingre.xml")

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (compatible; AngellHasman-LeadingREFeed/1.0; "
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

MEMBER = {
    "MemberKey": "MALCOLM-HASMAN",
    "OfficeKey": OFFICE["OfficeKey"],
    "MemberMlsId": "MALCOLM-HASMAN",
    "MemberLastName": "Hasman",
    "MemberFirstName": "Malcolm",
    "MemberStatus": "Active",
    "MemberMobilePhone": "604-290-1679",
    "MemberEmail": "",
}

# For the first test, use two listings only.
# Static values are fallbacks for fields that MyRealPage may not expose cleanly.
LISTINGS = [
    {
        "url": "https://maxhasman.ca/leading-real-estate.html/listing.r3154478-3820-sunridge-drive.109620749",
        "ListingKey": "R3154478",
        "ListAgentKey": MEMBER["MemberKey"],
        "ListOfficeKey": OFFICE["OfficeKey"],
        "ListingId": "R3154478",
        "PropertyType": "Residential",
        "PropertySubType": "Single Family Residence",
        "StreetNumber": "3820",
        "StreetName": "Sunridge",
        "StreetSuffix": "Drive",
        "UnitNumber": "",
        "City": "Whistler",
        "StateOrProvince": "BC",
        "PostalCode": "V8E 0W1",
        "Country": "CAN",
        "ListPrice": "15800000",
        "PublicRemarks": (
            "Architecturally significant ski-in mountain estate on Whistler's "
            "Sunridge Plateau, offering six bedrooms, seven bathrooms, expansive "
            "alpine views, a dramatic cedar-centered staircase, media room, wine "
            "cellar, indoor lap pool, steam room and outdoor hot tub."
        ),
        "BedroomsTotal": "6",
        # MyRealPage exposes total bathrooms but not necessarily the full/half split.
        # LeadingRE allows Recommended fields to be present and blank.
        "BathroomsFull": "",
        "BathroomsHalf": "",
        "StandardStatus": "Active",
        "LivingArea": "6345",
        "LivingAreaUnits": "Square Feet",
        "InternetAddressDisplayYN": "1",
        "InternetPriceDisplayYN": "1",
        "Currency": "CAD",
    },
    {
        "url": "https://maxhasman.ca/leading-real-estate.html/listing.r3136700-address-on-request.109085993",
        "ListingKey": "R3136700",
        "ListAgentKey": MEMBER["MemberKey"],
        "ListOfficeKey": OFFICE["OfficeKey"],
        "ListingId": "R3136700",
        "PropertyType": "Residential",
        "PropertySubType": "Single Family Residence",
        "StreetNumber": "2408",
        "StreetName": "Halston",
        "StreetSuffix": "Court",
        "UnitNumber": "",
        "City": "West Vancouver",
        "StateOrProvince": "BC",
        "PostalCode": "V7S 3K3",
        "Country": "CAN",
        "ListPrice": "23750000",
        "PublicRemarks": (
            "Luxury Whitby Estates residence with sweeping ocean and city views, "
            "five bedroom suites, expansive entertaining spaces, two chef's kitchens, "
            "wine display, home theatre, fitness studio, private office and smart-home "
            "automation."
        ),
        "BedroomsTotal": "5",
        "BathroomsFull": "",
        "BathroomsHalf": "",
        "StandardStatus": "Active",
        "LivingArea": "9549",
        "LivingAreaUnits": "Square Feet",
        # The MyRealPage URL identifies this listing as "address on request".
        # Keep the street address in the feed for geocoding but suppress public display.
        "InternetAddressDisplayYN": "0",
        "InternetPriceDisplayYN": "1",
        "Currency": "CAD",
    },
]


def fetch(url: str) -> tuple[BeautifulSoup, str]:
    """Fetch a page with retries and return BeautifulSoup + raw HTML."""
    last_error = None
    for attempt in range(3):
        try:
            response = SESSION.get(url, timeout=30)
            response.raise_for_status()
            return BeautifulSoup(response.text, "lxml"), response.text
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = html_lib.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def page_text(soup: BeautifulSoup) -> str:
    return "\n".join(
        clean_text(line)
        for line in soup.get_text("\n", strip=True).splitlines()
        if clean_text(line)
    )


def find_label_value(text: str, labels: list[str]) -> str:
    """Read common MyRealPage label/value pairs from rendered page text."""
    lines = [clean_text(x) for x in text.splitlines() if clean_text(x)]
    normalized = [re.sub(r"[:\s]+$", "", x).lower() for x in lines]
    targets = [x.lower() for x in labels]

    for i, label in enumerate(normalized):
        if label in targets and i + 1 < len(lines):
            return lines[i + 1]

    # Also handle labels and values appearing on the same line.
    for label in labels:
        match = re.search(
            rf"{re.escape(label)}\s*:?\s*([^\n]+)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return clean_text(match.group(1))
    return ""


def extract_price(text: str, soup: BeautifulSoup) -> str:
    # Structured/meta values first.
    for selector, attr in [
        ('meta[property="product:price:amount"]', "content"),
        ('meta[itemprop="price"]', "content"),
    ]:
        tag = soup.select_one(selector)
        if tag and tag.get(attr):
            digits = re.sub(r"[^\d.]", "", tag.get(attr, ""))
            if digits:
                return str(int(float(digits)))

    # Visible Canadian-dollar price.
    match = re.search(r"\$\s*([0-9][0-9,]*)", text)
    if match:
        return match.group(1).replace(",", "")
    return ""


def extract_status(text: str) -> str:
    raw = find_label_value(text, ["Status"])
    raw_l = raw.lower()
    if "active under contract" in raw_l:
        return "Active Under Contract"
    if "pending" in raw_l:
        return "Pending"
    if "active" in raw_l:
        return "Active"
    if any(x in raw_l for x in ["sold", "cancel", "expired", "terminated", "withdrawn"]):
        return "Off Market"
    return ""


def extract_remarks(soup: BeautifulSoup) -> str:
    # Prefer likely listing-description containers.
    candidates: list[str] = []
    selectors = [
        '[class*="remarks"]',
        '[class*="description"]',
        '[class*="listing-description"]',
        '[class*="listingRemarks"]',
        '[id*="remarks"]',
        '[id*="description"]',
    ]
    for selector in selectors:
        for node in soup.select(selector):
            text = clean_text(node.get_text(" ", strip=True))
            if 80 <= len(text) <= 5000:
                candidates.append(text)

    # Meta description is a useful fallback.
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if meta and meta.get("content"):
        text = clean_text(meta["content"])
        if len(text) >= 80:
            candidates.append(text)

    if not candidates:
        return ""

    # Prefer the longest plausible descriptive block.
    return max(candidates, key=len)


def _add_srcset_urls(value: str, base_url: str, output: list[str]) -> None:
    parts = []
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
        parts.append((width, url))
    # Prefer larger renditions first.
    for _, url in sorted(parts, reverse=True):
        output.append(url)


def extract_photo_urls(soup: BeautifulSoup, raw_html: str, base_url: str, mls: str) -> list[str]:
    candidates: list[str] = []

    # Open Graph image.
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        candidates.append(urljoin(base_url, og["content"]))

    # JSON-LD often contains image arrays.
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not script.string:
            continue
        # Avoid requiring a full JSON parser for malformed publisher markup:
        # collect explicit image URLs found inside the block.
        for match in re.findall(
            r'https?:\\?/\\?/[^"\'\s<>]+?\.(?:jpe?g|webp|png)(?:\?[^"\'\s<>]*)?',
            script.string,
            flags=re.I,
        ):
            candidates.append(match.replace("\\/", "/"))

    # Image and source elements, including lazy-load attributes.
    for tag in soup.find_all(["img", "source"]):
        for attr in ["src", "data-src", "data-original", "data-lazy-src", "data-image"]:
            value = tag.get(attr)
            if value:
                candidates.append(urljoin(base_url, value))
        for attr in ["srcset", "data-srcset"]:
            value = tag.get(attr)
            if value:
                _add_srcset_urls(value, base_url, candidates)

    # Links directly to image files.
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if re.search(r"\.(?:jpe?g|webp|png)(?:\?|$)", href, flags=re.I):
            candidates.append(urljoin(base_url, href))

    # Last-resort scan of raw HTML for public image URLs.
    unescaped_html = raw_html.replace("\\/", "/")
    candidates.extend(
        re.findall(
            r'https?://[^"\'\s<>]+?\.(?:jpe?g|webp|png)(?:\?[^"\'\s<>]*)?',
            unescaped_html,
            flags=re.I,
        )
    )

    bad_terms = (
        "logo",
        "favicon",
        "icon",
        "avatar",
        "agent",
        "profile",
        "reciprocity",
        "facebook",
        "instagram",
        "linkedin",
        "youtube",
        "map",
        "marker",
        "captcha",
        "spinner",
    )
    good_terms = (
        "photo",
        "image",
        "listing",
        "property",
        "media",
        "cdn",
        "mls",
        "ddf",
        mls.lower(),
    )

    results: list[str] = []
    seen_paths: set[str] = set()

    for url in candidates:
        url = clean_text(url).strip("'\"")
        if not url.startswith(("http://", "https://")):
            continue

        low = url.lower()
        if any(term in low for term in bad_terms):
            continue

        parsed = urlparse(url)
        if not parsed.netloc:
            continue

        # Only keep image-like URLs or URLs with a strong listing/photo signal.
        image_ext = bool(re.search(r"\.(?:jpe?g|webp|png)$", parsed.path, flags=re.I))
        if not image_ext and not any(term in low for term in good_terms):
            continue

        # Deduplicate alternate query-string renditions of the same underlying file.
        path_key = (parsed.netloc.lower() + parsed.path).lower()
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        results.append(url)

    return results[:60]


def update_from_live_page(listing: dict) -> tuple[dict, list[str]]:
    soup, raw_html = fetch(listing["url"])
    text = page_text(soup)

    live = dict(listing)

    price = extract_price(text, soup)
    if price:
        live["ListPrice"] = price

    status = extract_status(text)
    if status:
        live["StandardStatus"] = status

    bedrooms = find_label_value(text, ["Bedrooms", "Bedrooms Total"])
    bedroom_match = re.search(r"\d+", bedrooms)
    if bedroom_match:
        live["BedroomsTotal"] = bedroom_match.group(0)

    floor_area = find_label_value(text, ["Floor Area", "Floor Area Total", "Living Area"])
    floor_match = re.search(r"[\d,]+", floor_area)
    if floor_match:
        live["LivingArea"] = floor_match.group(0).replace(",", "")

    remarks = extract_remarks(soup)
    # Only replace the curated fallback when the page produced a meaningful paragraph.
    if len(remarks) >= 120:
        live["PublicRemarks"] = remarks

    photos = extract_photo_urls(soup, raw_html, listing["url"], listing["ListingId"])
    return live, photos


def add_text(parent: ET.Element, name: str, value: str | None) -> ET.Element:
    node = ET.SubElement(parent, name)
    if value is not None and value != "":
        node.text = str(value)
    return node


def build_xml(active_records: list[tuple[dict, list[str]]]) -> ET.ElementTree:
    root = ET.Element("Data")
    root.append(
        ET.Comment(
            " Generated automatically by Angell Hasman LeadingRE feed "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} "
        )
    )

    offices = ET.SubElement(root, "Offices")
    office = ET.SubElement(offices, "Office")
    for key in [
        "OfficeKey",
        "OfficeStatus",
        "OfficeName",
        "OfficeAddress1",
        "OfficeAddress2",
        "OfficeCity",
        "OfficeStateOrProvince",
        "OfficePostalCode",
        "OfficeCountry",
        "OfficePhone",
        "OfficeEmail",
        "OfficeMlsId",
    ]:
        add_text(office, key, OFFICE.get(key, ""))

    members = ET.SubElement(root, "Members")
    member = ET.SubElement(members, "Member")
    for key in [
        "MemberKey",
        "OfficeKey",
        "MemberMlsId",
        "MemberLastName",
        "MemberFirstName",
        "MemberStatus",
        "MemberMobilePhone",
        "MemberEmail",
    ]:
        add_text(member, key, MEMBER.get(key, ""))

    properties = ET.SubElement(root, "Properties")
    for listing, _photos in active_records:
        prop = ET.SubElement(properties, "Property")
        # Order follows LeadingRE Specification v1.1.
        ordered_fields = [
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
        ]
        for key in ordered_fields:
            add_text(prop, key, listing.get(key, ""))

        add_text(prop, "ListingURL", listing["url"])
        add_text(prop, "PublicRemarks", listing.get("PublicRemarks", ""))
        add_text(prop, "BedroomsTotal", listing.get("BedroomsTotal", ""))
        add_text(prop, "BathroomsFull", listing.get("BathroomsFull", ""))
        add_text(prop, "BathroomsHalf", listing.get("BathroomsHalf", ""))
        add_text(prop, "StandardStatus", listing.get("StandardStatus", "Active"))
        add_text(prop, "LivingArea", listing.get("LivingArea", ""))
        add_text(prop, "LivingAreaUnits", listing.get("LivingAreaUnits", "Square Feet"))
        add_text(prop, "InternetAddressDisplayYN", listing.get("InternetAddressDisplayYN", "1"))
        add_text(prop, "InternetPriceDisplayYN", listing.get("InternetPriceDisplayYN", "1"))
        add_text(prop, "Currency", listing.get("Currency", "CAD"))

    medias = ET.SubElement(root, "Medias")
    for listing, photos in active_records:
        for order, photo_url in enumerate(photos, start=1):
            media = ET.SubElement(medias, "Media")
            add_text(media, "MediaKey", f"{listing['ListingKey']}-PHOTO-{order}")
            add_text(media, "Order", str(order))
            add_text(media, "MediaCategory", "Photo")
            add_text(media, "MediaURL", photo_url)
            add_text(media, "ResourceName", "Property")
            add_text(media, "ResourceRecordID", listing["ListingKey"])

    ET.indent(root, space="  ")
    return ET.ElementTree(root)


def main() -> int:
    active_records: list[tuple[dict, list[str]]] = []

    for configured in LISTINGS:
        print(f"Reading {configured['ListingId']}...")
        live, photos = update_from_live_page(configured)
        status = live.get("StandardStatus", "").strip()

        if status not in {"Active", "Active Under Contract", "Pending"}:
            print(f"  Skipping {configured['ListingId']} because status is {status!r}.")
            continue

        if not live.get("ListPrice"):
            raise RuntimeError(f"{configured['ListingId']}: no ListPrice found.")
        if not live.get("PublicRemarks"):
            raise RuntimeError(f"{configured['ListingId']}: no PublicRemarks found.")
        if len(photos) < 6:
            raise RuntimeError(
                f"{configured['ListingId']}: found only {len(photos)} usable photo URLs. "
                "The existing leadingre.xml was NOT overwritten. "
                "Open the GitHub Actions log and send this message back so the photo "
                "extractor can be adjusted safely."
            )

        print(
            f"  Active | ${int(live['ListPrice']):,} | "
            f"{live.get('BedroomsTotal', '?')} beds | "
            f"{live.get('LivingArea', '?')} sq ft | {len(photos)} photos"
        )
        active_records.append((live, photos))

    if not active_records:
        raise RuntimeError("No active listings were found; refusing to create an empty feed.")

    tree = build_xml(active_records)

    temp = OUTPUT_FILE.with_suffix(".xml.tmp")
    tree.write(temp, encoding="utf-8", xml_declaration=True)
    temp.replace(OUTPUT_FILE)

    print(f"Wrote {OUTPUT_FILE} with {len(active_records)} active listing(s).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
