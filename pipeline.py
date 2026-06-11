"""
Property Comparables Pipeline
==============================
Given a property address:
  1. Geocode it (Nominatim) → lat, lng, zip, city, state
  2. Discover the Redfin region for that zip (navigates to /zipcode/ZIP → reads GIS cache key)
  3. Pull all listings for that zip + city; filter to those within 2 miles
  4. Match the subject property in the results (closest address/coords)
  5. Filter comparables: ±20% of subject's sqft AND lot_size
  6. Fetch recent sales (last 365 days) in the same area; match to comparables
  7. Store everything in a SQLite file named after the address + key stats

Usage:
    python3 pipeline.py "8052 Ridge Dr NE, Seattle WA 98115"
    python3 pipeline.py "5250 45th Ave SW, Seattle WA 98136"
    python3 pipeline.py --sold-days 180 "150 NW 84th St, Seattle WA 98117"
"""

import argparse
import asyncio
import json
import math
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright
from playwright_stealth import stealth_async


# ── Anti-detection ─────────────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]
VIEWPORTS = [{"width": 1920, "height": 1080}, {"width": 1440, "height": 900}, {"width": 1366, "height": 768}]


# ── Geo helpers ────────────────────────────────────────────────────────────────

def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Redfin data helpers ────────────────────────────────────────────────────────

PROPERTY_TYPES = {1: "Single Family", 2: "Condo/Co-op", 3: "Townhouse", 4: "Multi-Family", 5: "Land", 6: "Other"}

def _v(field):
    return field.get("value") if isinstance(field, dict) else field

def _ts(ts_ms):
    if not ts_ms:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

def parse_home(h: dict) -> dict:
    ll = h.get("latLong", {}).get("value", {}) if isinstance(h.get("latLong"), dict) else {}
    return {
        "property_id":    h.get("propertyId"),
        "listing_id":     h.get("listingId"),
        "mls_number":     _v(h.get("mlsId", {})),
        "mls_status":     h.get("mlsStatus"),
        "street":         _v(h.get("streetLine", {})),
        "unit":           _v(h.get("unitNumber", {})),
        "city":           h.get("city"),
        "state":          h.get("state"),
        "zip":            h.get("zip"),
        "neighborhood":   _v(h.get("location", {})),
        "latitude":       ll.get("latitude"),
        "longitude":      ll.get("longitude"),
        "price":          _v(h.get("price", {})),
        "price_per_sqft": _v(h.get("pricePerSqFt", {})),
        "sqft":           _v(h.get("sqFt", {})),
        "lot_size":       _v(h.get("lotSize", {})),
        "beds":           h.get("beds"),
        "baths":          h.get("baths"),
        "full_baths":     h.get("fullBaths"),
        "year_built":     _v(h.get("yearBuilt", {})),
        "property_type":  PROPERTY_TYPES.get(h.get("uiPropertyType"), "Unknown"),
        "stories":        h.get("stories"),
        "hoa_monthly":    _v(h.get("hoa", {})),
        "dom":            _v(h.get("dom", {})),
        "sold_date":      _ts(h.get("soldDate")),
        "listing_broker": h.get("listingBroker", {}).get("name") if isinstance(h.get("listingBroker"), dict) else None,
        "selling_broker": h.get("sellingBroker", {}).get("name") if isinstance(h.get("sellingBroker"), dict) else None,
        "listing_agent":  h.get("listingAgent", {}).get("name") if isinstance(h.get("listingAgent"), dict) else None,
        "redfin_url":     "https://www.redfin.com" + h["url"] if h.get("url") else None,
    }


# ── Browser / page helpers ─────────────────────────────────────────────────────

async def build_context(playwright):
    browser = await playwright.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
              "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )
    vp = random.choice(VIEWPORTS)
    ctx = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport=vp,
        locale=random.choice(["en-US", "en-GB"]),
        timezone_id="America/Los_Angeles",
        extra_http_headers={
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "DNT": "1",
        },
    )
    return browser, ctx

STEALTH_SCRIPT = """
    Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
    Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});
    Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
    window.chrome={runtime:{}};
"""

async def fetch_json_in_page(page, url: str):
    """Call fetch() from within the browser page so cookies/session are used."""
    result = await page.evaluate("""async (url) => {
        try {
            const r = await fetch(url, {headers:{"Accept":"*/*","X-Requested-With":"XMLHttpRequest"}});
            const t = await r.text();
            const c = t.startsWith("{}&&") ? t.slice(4) : t;
            return {status: r.status, data: JSON.parse(c)};
        } catch(e) {
            return {status: 0, data: null, err: e.toString()};
        }
    }""", url)
    return result.get("status"), result.get("data")

GIS_BASE = "https://www.redfin.com/stingray/api/gis"
GIS_COMMON = "&sf=1,2,3,5,6,7&uipt=1,2,3,4,5,6,7,8&v=8&num_homes=350&ord=redfin-recommended-asc"

def make_gis_url(market, region_id, region_type, page_num=1, sold_within_days=None):
    mpt = "99" if sold_within_days else "1"
    url = (
        f"{GIS_BASE}?al=1&include_nearby_homes=true&market={market}&mpt={mpt}"
        f"&page_number={page_num}&region_id={region_id}&region_type={region_type}"
        f"&start={(page_num-1)*350}&status=9{GIS_COMMON}"
    )
    if sold_within_days:
        url += f"&sold_within_days={sold_within_days}"
    return url

async def gis_fetch_all(page, market, region_id, region_type, sold_within_days=None, max_pages=5) -> list[dict]:
    """Paginate the GIS API and return all homes."""
    all_homes = []
    for pg in range(1, max_pages + 1):
        url = make_gis_url(market, region_id, region_type, pg, sold_within_days)
        _, data = await fetch_json_in_page(page, url)
        if not data:
            break
        homes = data.get("payload", {}).get("homes", [])
        if not homes:
            break
        all_homes.extend(homes)
        if len(homes) < 350:
            break
        # For sold searches stop when no more closed homes
        if sold_within_days and not any(h.get("mlsStatus") in ("Closed", "Sold") for h in homes):
            break
        await asyncio.sleep(random.uniform(0.8, 1.5))
    return all_homes


# ── Pipeline steps ─────────────────────────────────────────────────────────────

async def geocode(page, address: str) -> dict:
    """Geocode an address via OpenStreetMap Nominatim."""
    encoded = address.replace(" ", "+").replace(",", "%2C")
    url = f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1&addressdetails=1"
    _, data = await fetch_json_in_page(page, url)
    if not data or not isinstance(data, list) or not data:
        raise RuntimeError(f"Nominatim could not geocode: {address}")
    geo = data[0]
    addr = geo.get("address", {})
    return {
        "lat":   float(geo["lat"]),
        "lng":   float(geo["lon"]),
        "zip":   addr.get("postcode", "").replace(" ", ""),
        "city":  addr.get("city") or addr.get("town") or addr.get("village") or "",
        "state": addr.get("state", ""),
    }


STATE_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN",
    "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
}

async def discover_region(page, zip_code: str, city: str, state: str) -> dict:
    """
    Navigate to the Redfin zip code page and extract market+region_id+region_type
    from the GIS API URL cached in __reactServerState.
    Falls back to the city page if zip fails.
    """
    async def _try_page(redfin_url) -> dict | None:
        await page.goto(redfin_url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(3)
        info = await page.evaluate("""() => {
            const ctx = window.__reactServerState && window.__reactServerState.InitialContext;
            if (!ctx) return null;
            const cache = ctx['ReactServerAgent.cache'] && ctx['ReactServerAgent.cache'].dataCache;
            if (!cache) return null;
            for (const key of Object.keys(cache)) {
                if (key.includes('/stingray/api/gis?') && !key.includes('aggregate') && !key.includes('filter')) {
                    return key;
                }
            }
            return null;
        }""")
        if not info:
            return None
        m_market    = re.search(r"market=([^&]+)", info)
        m_region_id = re.search(r"region_id=(\d+)", info)
        m_region_t  = re.search(r"region_type=(\d+)", info)
        if not (m_market and m_region_id and m_region_t):
            return None
        return {
            "market":      m_market.group(1),
            "region_id":   int(m_region_id.group(1)),
            "region_type": int(m_region_t.group(1)),
        }

    # Attempt 1: zip code page (tight geographic scope)
    result = await _try_page(f"https://www.redfin.com/zipcode/{zip_code}")
    if result:
        return result

    # Attempt 2: city page  e.g. /city/16163/WA/Seattle
    state_abbr = STATE_ABBR.get(state, state[:2].upper())
    city_slug = city.replace(" ", "_")
    result = await _try_page(f"https://www.redfin.com/{state_abbr}/{city_slug}")
    if result:
        return result

    raise RuntimeError(f"Could not discover Redfin region for zip={zip_code} city={city} state={state}")


def find_subject_in_listings(listings: list[dict], sub_lat: float, sub_lng: float) -> dict | None:
    """Find the closest listing to the geocoded coordinates — that's our subject property."""
    best, best_d = None, float("inf")
    for p in listings:
        lat, lng = p.get("latitude"), p.get("longitude")
        if lat and lng:
            d = haversine_miles(sub_lat, sub_lng, lat, lng)
            if d < best_d:
                best_d, best = d, p
    if best and best_d <= 0.1:  # within ~530 ft — confident match
        return best
    return best  # return closest even if farther


def filter_nearby(listings: list[dict], sub_lat: float, sub_lng: float, radius_miles: float) -> list[dict]:
    result = []
    for p in listings:
        lat, lng = p.get("latitude"), p.get("longitude")
        if lat and lng:
            d = haversine_miles(sub_lat, sub_lng, lat, lng)
            if d <= radius_miles:
                p["distance_miles"] = round(d, 3)
                result.append(p)
    return sorted(result, key=lambda x: x["distance_miles"])


def filter_comparables(subject: dict, nearby: list[dict], tolerance: float = 0.20) -> list[dict]:
    sub_sqft = subject.get("sqft")
    sub_lot  = subject.get("lot_size")
    comps = []
    for p in nearby:
        if p.get("property_id") == subject.get("property_id"):
            continue  # exclude subject itself
        sqft = p.get("sqft")
        lot  = p.get("lot_size")
        sqft_ok = (not sub_sqft or not sqft or
                   abs(sqft - sub_sqft) / sub_sqft <= tolerance)
        lot_ok  = (not sub_lot  or not lot  or
                   abs(lot  - sub_lot)  / sub_lot  <= tolerance)
        if sqft_ok and lot_ok:
            pct_sqft = round((sqft - sub_sqft) / sub_sqft * 100, 1) if (sqft and sub_sqft) else None
            pct_lot  = round((lot  - sub_lot)  / sub_lot  * 100, 1) if (lot and sub_lot)   else None
            p["pct_diff_sqft"] = pct_sqft
            p["pct_diff_lot"]  = pct_lot
            comps.append(p)
    return comps


# ── Database ────────────────────────────────────────────────────────────────────

def sanitize_filename(text: str) -> str:
    return re.sub(r"[^\w\-]", "_", text).strip("_")[:60]

def db_name(input_address: str, subject: dict) -> str:
    """Name: <input_address>__<sqft>sqft_<lot>lot_<price>.db"""
    addr   = sanitize_filename(input_address)
    sqft   = subject.get("sqft") or "Xsqft"
    lot    = subject.get("lot_size") or "Xlot"
    price  = subject.get("price") or "Xprice"
    return f"{addr}__{sqft}sqft_{lot}lot_{price}.db"

def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subject_property (
            property_id     INTEGER PRIMARY KEY,
            listing_id      INTEGER,
            mls_number      TEXT,
            mls_status      TEXT,
            street          TEXT,
            unit            TEXT,
            city            TEXT,
            state           TEXT,
            zip             TEXT,
            neighborhood    TEXT,
            latitude        REAL,
            longitude       REAL,
            price           INTEGER,
            price_per_sqft  INTEGER,
            sqft            INTEGER,
            lot_size        INTEGER,
            beds            REAL,
            baths           REAL,
            year_built      INTEGER,
            property_type   TEXT,
            stories         REAL,
            hoa_monthly     INTEGER,
            dom             INTEGER,
            listing_broker  TEXT,
            listing_agent   TEXT,
            redfin_url      TEXT,
            scraped_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS nearby_listings (
            property_id     INTEGER PRIMARY KEY,
            listing_id      INTEGER,
            mls_number      TEXT,
            mls_status      TEXT,
            street          TEXT,
            unit            TEXT,
            city            TEXT,
            state           TEXT,
            zip             TEXT,
            neighborhood    TEXT,
            latitude        REAL,
            longitude       REAL,
            distance_miles  REAL,
            price           INTEGER,
            price_per_sqft  INTEGER,
            sqft            INTEGER,
            lot_size        INTEGER,
            beds            REAL,
            baths           REAL,
            year_built      INTEGER,
            property_type   TEXT,
            dom             INTEGER,
            listing_broker  TEXT,
            redfin_url      TEXT,
            scraped_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS comparables (
            property_id     INTEGER PRIMARY KEY,
            listing_id      INTEGER,
            mls_number      TEXT,
            mls_status      TEXT,
            street          TEXT,
            unit            TEXT,
            city            TEXT,
            state           TEXT,
            zip             TEXT,
            neighborhood    TEXT,
            latitude        REAL,
            longitude       REAL,
            distance_miles  REAL,
            price           INTEGER,
            price_per_sqft  INTEGER,
            sqft            INTEGER,
            lot_size        INTEGER,
            beds            REAL,
            baths           REAL,
            year_built      INTEGER,
            property_type   TEXT,
            dom             INTEGER,
            pct_diff_sqft   REAL,
            pct_diff_lot    REAL,
            listing_broker  TEXT,
            redfin_url      TEXT,
            scraped_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS comparable_sales (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            property_id     INTEGER,
            listing_id      INTEGER,
            mls_number      TEXT,
            street          TEXT,
            unit            TEXT,
            city            TEXT,
            state           TEXT,
            zip             TEXT,
            neighborhood    TEXT,
            latitude        REAL,
            longitude       REAL,
            distance_miles  REAL,
            sold_date       TEXT,
            sold_price      INTEGER,
            price_per_sqft  INTEGER,
            sqft            INTEGER,
            lot_size        INTEGER,
            beds            REAL,
            baths           REAL,
            year_built      INTEGER,
            property_type   TEXT,
            dom             INTEGER,
            listing_broker  TEXT,
            selling_broker  TEXT,
            listing_agent   TEXT,
            redfin_url      TEXT,
            scraped_at      TEXT,
            UNIQUE(property_id, sold_date)
        );
    """)
    conn.commit()
    return conn

NOW = datetime.now(timezone.utc).isoformat()

def insert_subject(conn, p):
    conn.execute("""INSERT OR REPLACE INTO subject_property VALUES (
        :property_id,:listing_id,:mls_number,:mls_status,:street,:unit,:city,:state,:zip,
        :neighborhood,:latitude,:longitude,:price,:price_per_sqft,:sqft,:lot_size,
        :beds,:baths,:year_built,:property_type,:stories,:hoa_monthly,:dom,
        :listing_broker,:listing_agent,:redfin_url,:scraped_at)""",
        {**p, "scraped_at": NOW})

def insert_nearby(conn, listings):
    for p in listings:
        conn.execute("""INSERT OR REPLACE INTO nearby_listings VALUES (
            :property_id,:listing_id,:mls_number,:mls_status,:street,:unit,:city,:state,:zip,
            :neighborhood,:latitude,:longitude,:distance_miles,:price,:price_per_sqft,:sqft,:lot_size,
            :beds,:baths,:year_built,:property_type,:dom,:listing_broker,:redfin_url,:scraped_at)""",
            {**p, "scraped_at": NOW, "distance_miles": p.get("distance_miles")})

def insert_comparables(conn, comps):
    for p in comps:
        conn.execute("""INSERT OR REPLACE INTO comparables VALUES (
            :property_id,:listing_id,:mls_number,:mls_status,:street,:unit,:city,:state,:zip,
            :neighborhood,:latitude,:longitude,:distance_miles,:price,:price_per_sqft,:sqft,:lot_size,
            :beds,:baths,:year_built,:property_type,:dom,:pct_diff_sqft,:pct_diff_lot,
            :listing_broker,:redfin_url,:scraped_at)""",
            {**p, "scraped_at": NOW})

def insert_sales(conn, sales):
    for p in sales:
        conn.execute("""INSERT OR IGNORE INTO comparable_sales VALUES (
            NULL,:property_id,:listing_id,:mls_number,:street,:unit,:city,:state,:zip,
            :neighborhood,:latitude,:longitude,:distance_miles,
            :sold_date,:price,:price_per_sqft,:sqft,:lot_size,:beds,:baths,
            :year_built,:property_type,:dom,:listing_broker,:selling_broker,
            :listing_agent,:redfin_url,:scraped_at)""",
            {**p, "scraped_at": NOW, "distance_miles": p.get("distance_miles")})


# ── Print helpers ───────────────────────────────────────────────────────────────

def fmt_price(v):
    return f"${v:,}" if v else "N/A"

def print_property(label: str, p: dict):
    print(f"\n  {label}")
    print(f"    Address    : {p.get('street')}{', ' + p['unit'] if p.get('unit') else ''}")
    print(f"    City       : {p.get('city')}, {p.get('state')} {p.get('zip')}")
    print(f"    Price      : {fmt_price(p.get('price'))}")
    print(f"    Sqft       : {p.get('sqft') or 'N/A'}  |  Lot: {p.get('lot_size') or 'N/A'} sqft")
    print(f"    Beds/Baths : {p.get('beds')} bd / {p.get('baths')} ba")
    print(f"    Built      : {p.get('year_built') or 'N/A'}  |  Type: {p.get('property_type')}")
    print(f"    Status     : {p.get('mls_status')}  |  DOM: {p.get('dom') or 'N/A'}")
    if p.get("redfin_url"):
        print(f"    Redfin URL : {p['redfin_url']}")


# ── Main pipeline ───────────────────────────────────────────────────────────────

async def run_pipeline(address: str, radius_miles: float, tolerance: float, sold_days: int, input_address: str = ""):
    async with async_playwright() as pw:
        browser, ctx = await build_context(pw)
        try:
            page = await ctx.new_page()
            await stealth_async(page)
            await page.add_init_script(STEALTH_SCRIPT)

            # ── 1. Geocode ─────────────────────────────────────────────────────
            print(f"\n[1] Geocoding: {address}")
            # Need a warm page for Nominatim fetch to work
            await page.goto("https://nominatim.openstreetmap.org", wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(1)
            geo = await geocode(page, address)
            print(f"    lat={geo['lat']:.6f}  lng={geo['lng']:.6f}  zip={geo['zip']}  city={geo['city']}  state={geo['state']}")

            # ── 2. Discover Redfin region ──────────────────────────────────────
            print(f"\n[2] Discovering Redfin region for zip={geo['zip']}...")
            region = await discover_region(page, geo["zip"], geo["city"], geo["state"])
            print(f"    market={region['market']}  region_id={region['region_id']}  region_type={region['region_type']}")

            # ── 3. Fetch all listings in the region ───────────────────────────
            print(f"\n[3] Fetching listings...")
            raw_listings = await gis_fetch_all(page, region["market"], region["region_id"], region["region_type"], max_pages=3)
            all_props = [parse_home(h) for h in raw_listings]
            print(f"    {len(all_props)} properties from Redfin")

            # Filter to radius
            nearby = filter_nearby(all_props, geo["lat"], geo["lng"], radius_miles)
            print(f"    {len(nearby)} within {radius_miles} mile(s)")

            # ── 4. Identify subject property ──────────────────────────────────
            subject = find_subject_in_listings(all_props, geo["lat"], geo["lng"])
            if subject:
                dist = haversine_miles(geo["lat"], geo["lng"], subject.get("latitude") or 0, subject.get("longitude") or 0)
                match_note = "exact match" if dist < 0.05 else f"closest active listing, {dist:.2f} mi from geocoded point — subject may be off-market"
                print_property(f"[4] Subject property  [{match_note}]", subject)
            else:
                print("\n[4] Subject property not found in any listings (off-market)")
                # Build a synthetic subject from geocode + address string
                subject = {
                    "property_id": None, "listing_id": None, "mls_number": None,
                    "mls_status": "Unknown", "street": address, "unit": None,
                    "city": geo["city"], "state": STATE_ABBR.get(geo["state"], geo["state"][:2].upper()),
                    "zip": geo["zip"], "neighborhood": None,
                    "latitude": geo["lat"], "longitude": geo["lng"],
                    "price": None, "price_per_sqft": None, "sqft": None, "lot_size": None,
                    "beds": None, "baths": None, "full_baths": None,
                    "year_built": None, "property_type": None, "stories": None,
                    "hoa_monthly": None, "dom": None, "sold_date": None,
                    "listing_broker": None, "selling_broker": None, "listing_agent": None,
                    "redfin_url": None,
                }

            # ── 5. Filter comparables ─────────────────────────────────────────
            comps = filter_comparables(subject, nearby, tolerance)
            print(f"\n[5] Comparables: {len(comps)} properties within ±{int(tolerance*100)}% sqft/lot")
            for p in comps[:5]:
                sq_s = f"sqft={p.get('sqft')} ({p.get('pct_diff_sqft'):+.0f}%)" if p.get('pct_diff_sqft') is not None else f"sqft={p.get('sqft')}"
                lot_s = f"lot={p.get('lot_size')} ({p.get('pct_diff_lot'):+.0f}%)" if p.get('pct_diff_lot') is not None else f"lot={p.get('lot_size')}"
                print(f"    {p.get('distance_miles'):.2f}mi | {p.get('street')} | {fmt_price(p.get('price'))} | {sq_s} | {lot_s} | {p.get('mls_status')}")
            if len(comps) > 5:
                print(f"    ... and {len(comps)-5} more")

            # ── 6. Fetch recent sales in the area ─────────────────────────────
            print(f"\n[6] Fetching recent sales (last {sold_days} days)...")
            raw_sales = await gis_fetch_all(page, region["market"], region["region_id"], region["region_type"],
                                            sold_within_days=sold_days, max_pages=5)
            all_sales  = [parse_home(h) for h in raw_sales if h.get("mlsStatus") in ("Closed", "Sold")]
            area_sales = filter_nearby(all_sales, geo["lat"], geo["lng"], radius_miles)
            print(f"    {len(area_sales)} sales within {radius_miles} mile(s)")

            # Filter sales that match our comparables (by property_id)
            comp_ids = {p.get("property_id") for p in comps}
            comp_sales = [s for s in area_sales if s.get("property_id") in comp_ids]

            # Also include any sales that pass the 20% filter
            for s in area_sales:
                if s.get("property_id") not in comp_ids:
                    s_filtered = filter_comparables(subject, [s], tolerance)
                    if s_filtered:
                        comp_sales.append(s_filtered[0])

            # Deduplicate
            seen = set()
            unique_sales = []
            for s in comp_sales:
                key = (s.get("property_id"), s.get("sold_date"))
                if key not in seen:
                    seen.add(key)
                    unique_sales.append(s)

            print(f"    {len(unique_sales)} sales match comparable criteria")
            for s in sorted(unique_sales, key=lambda x: x.get("sold_date") or "", reverse=True)[:8]:
                print(f"    {s.get('sold_date')} | {fmt_price(s.get('price'))} | {s.get('street')}"
                      f" | sqft={s.get('sqft')} lot={s.get('lot_size')}")

            # ── 7. Store to database ───────────────────────────────────────────
            db_path = db_name(input_address or address, subject)
            print(f"\n[7] Saving to database: {db_path}")
            conn = init_db(db_path)

            insert_subject(conn, subject)
            insert_nearby(conn, nearby)
            insert_comparables(conn, comps)
            insert_sales(conn, unique_sales)
            conn.commit()

            print(f"    subject_property  : 1 record")
            print(f"    nearby_listings   : {len(nearby)} records")
            print(f"    comparables       : {len(comps)} records")
            print(f"    comparable_sales  : {len(unique_sales)} records")
            conn.close()

            return db_path

        finally:
            await ctx.close()
            await browser.close()


# ── CLI ─────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Property comparables pipeline")
    parser.add_argument("address", help="Property address (e.g. '8052 Ridge Dr NE, Seattle WA 98115')")
    parser.add_argument("--radius", type=float, default=2.0, help="Search radius in miles (default 2.0)")
    parser.add_argument("--tolerance", type=float, default=0.20, help="Comparable tolerance 0-1 (default 0.20 = ±20%%)")
    parser.add_argument("--sold-days", type=int, default=365, help="Sales lookback window in days (default 365)")
    args = parser.parse_args()

    print("=" * 65)
    print("  Property Comparables Pipeline")
    print(f"  Address    : {args.address}")
    print(f"  Radius     : {args.radius} miles")
    print(f"  Tolerance  : ±{int(args.tolerance*100)}% sqft/lot")
    print(f"  Sales back : {args.sold_days} days")
    print("=" * 65)

    start = time.perf_counter()
    db_path = asyncio.run(run_pipeline(args.address, args.radius, args.tolerance, args.sold_days, input_address=args.address))
    elapsed = time.perf_counter() - start

    print(f"\n  Completed in {elapsed:.1f}s  →  {db_path}")
    print("=" * 65)


if __name__ == "__main__":
    main()
