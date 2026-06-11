"""
Redfin scraper — listings + recent sales data, stored in SQLite.

Strategy:
  - Redfin property detail pages are CloudFront-blocked for headless browsers.
  - The /stingray/api/gis endpoint is accessible from within a warm browser session
    and returns rich structured data (350 homes per page) including all sale fields.
  - We call it twice per city: once for active listings, once for recent sales
    (sold_within_days=365).  Both datasets go into SQLite.

Usage:
    python3 main.py                         # Seattle, active + recent sales
    python3 main.py --city austin
    python3 main.py --city denver
    python3 main.py --city miami
    python3 main.py --sold-days 180         # sales in last 180 days (default 365)
"""

import asyncio
import json
import random
import sqlite3
import time
import argparse
from datetime import datetime, timezone
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async


# ── Anti-detection ────────────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 2560, "height": 1440},
    {"width": 1280, "height": 800},
]

# ── City configs ──────────────────────────────────────────────────────────────

CITIES = {
    "seattle": {"market": "seattle", "region_id": 16163, "region_type": 6, "search_url": "https://www.redfin.com/city/16163/WA/Seattle"},
    "austin":  {"market": "austin",  "region_id": 30818, "region_type": 6, "search_url": "https://www.redfin.com/city/30818/TX/Austin"},
    "denver":  {"market": "denver",  "region_id": 7814,  "region_type": 6, "search_url": "https://www.redfin.com/city/7814/CO/Denver"},
    "miami":   {"market": "miami",   "region_id": 11458, "region_type": 6, "search_url": "https://www.redfin.com/city/11458/FL/Miami"},
}

DB_FILE = "redfin_data.db"

# ── Database ──────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS listings (
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
            full_baths      INTEGER,
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

        CREATE TABLE IF NOT EXISTS sales (
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _val(field: dict) -> any:
    """Redfin wraps most values in {value: X, level: N} — unwrap safely."""
    if isinstance(field, dict):
        return field.get("value")
    return field

def _ts_to_date(ts_ms) -> str | None:
    if not ts_ms:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

PROPERTY_TYPES = {1: "Single Family", 2: "Condo/Co-op", 3: "Townhouse", 4: "Multi-Family", 5: "Land", 6: "Other"}

def parse_home(h: dict, now: str) -> dict:
    return {
        "property_id":    h.get("propertyId"),
        "listing_id":     h.get("listingId"),
        "mls_number":     _val(h.get("mlsId", {})),
        "mls_status":     h.get("mlsStatus"),
        "street":         _val(h.get("streetLine", {})),
        "unit":           _val(h.get("unitNumber", {})),
        "city":           h.get("city"),
        "state":          h.get("state"),
        "zip":            h.get("zip"),
        "neighborhood":   _val(h.get("location", {})),
        "latitude":       _val(h.get("latLong", {}).get("value", {})) if isinstance(h.get("latLong"), dict) else None,
        "longitude":      None,
        "price":          _val(h.get("price", {})),
        "price_per_sqft": _val(h.get("pricePerSqFt", {})),
        "sqft":           _val(h.get("sqFt", {})),
        "lot_size":       _val(h.get("lotSize", {})),
        "beds":           h.get("beds"),
        "baths":          h.get("baths"),
        "full_baths":     h.get("fullBaths"),
        "year_built":     _val(h.get("yearBuilt", {})),
        "property_type":  PROPERTY_TYPES.get(h.get("uiPropertyType"), "Unknown"),
        "stories":        h.get("stories"),
        "hoa_monthly":    _val(h.get("hoa", {})),
        "dom":            _val(h.get("dom", {})),
        "sold_date":      _ts_to_date(h.get("soldDate")),
        "listing_broker": h.get("listingBroker", {}).get("name") if isinstance(h.get("listingBroker"), dict) else None,
        "selling_broker": h.get("sellingBroker", {}).get("name") if isinstance(h.get("sellingBroker"), dict) else None,
        "listing_agent":  h.get("listingAgent", {}).get("name") if isinstance(h.get("listingAgent"), dict) else None,
        "redfin_url":     "https://www.redfin.com" + h.get("url", "") if h.get("url") else None,
        "scraped_at":     now,
    }

def _fix_lat_long(p: dict, h: dict):
    """latLong nesting in the API: {value: {latitude, longitude}, level}"""
    ll = h.get("latLong", {})
    if isinstance(ll, dict):
        inner = ll.get("value", {})
        if isinstance(inner, dict):
            p["latitude"]  = inner.get("latitude")
            p["longitude"] = inner.get("longitude")

def upsert_listing(conn: sqlite3.Connection, p: dict):
    conn.execute("""
        INSERT INTO listings (
            property_id, listing_id, mls_number, mls_status, street, unit,
            city, state, zip, neighborhood, latitude, longitude,
            price, price_per_sqft, sqft, lot_size, beds, baths, full_baths,
            year_built, property_type, stories, hoa_monthly, dom,
            listing_broker, listing_agent, redfin_url, scraped_at
        ) VALUES (
            :property_id, :listing_id, :mls_number, :mls_status, :street, :unit,
            :city, :state, :zip, :neighborhood, :latitude, :longitude,
            :price, :price_per_sqft, :sqft, :lot_size, :beds, :baths, :full_baths,
            :year_built, :property_type, :stories, :hoa_monthly, :dom,
            :listing_broker, :listing_agent, :redfin_url, :scraped_at
        )
        ON CONFLICT(property_id) DO UPDATE SET
            listing_id = excluded.listing_id,
            mls_status = excluded.mls_status,
            price      = excluded.price,
            dom        = excluded.dom,
            scraped_at = excluded.scraped_at
    """, p)

def upsert_sale(conn: sqlite3.Connection, p: dict):
    conn.execute("""
        INSERT OR IGNORE INTO sales (
            property_id, listing_id, mls_number, street, unit,
            city, state, zip, neighborhood, latitude, longitude,
            sold_date, sold_price, price_per_sqft, sqft, lot_size,
            beds, baths, year_built, property_type, dom,
            listing_broker, selling_broker, listing_agent, redfin_url, scraped_at
        ) VALUES (
            :property_id, :listing_id, :mls_number, :street, :unit,
            :city, :state, :zip, :neighborhood, :latitude, :longitude,
            :sold_date, :price, :price_per_sqft, :sqft, :lot_size,
            :beds, :baths, :year_built, :property_type, :dom,
            :listing_broker, :selling_broker, :listing_agent, :redfin_url, :scraped_at
        )
    """, p)


# ── Browser setup ─────────────────────────────────────────────────────────────

async def build_context(playwright):
    ua       = random.choice(USER_AGENTS)
    viewport = random.choice(VIEWPORTS)
    browser  = await playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage", "--disable-gpu",
            f"--window-size={viewport['width']},{viewport['height']}",
        ],
    )
    context = await browser.new_context(
        user_agent=ua,
        viewport=viewport,
        locale=random.choice(["en-US", "en-GB", "en-CA"]),
        timezone_id="America/Los_Angeles",
        extra_http_headers={
            "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language":           "en-US,en;q=0.9",
            "Accept-Encoding":           "gzip, deflate, br",
            "DNT":                       "1",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest":            "document",
            "Sec-Fetch-Mode":            "navigate",
            "Sec-Fetch-Site":            "none",
        },
    )
    print(f"  UA: {ua[:75]}...")
    return browser, context


# ── GIS API fetch (runs inside browser to get proper cookies/headers) ─────────

GIS_BASE = "https://www.redfin.com/stingray/api/gis"
GIS_COMMON = "&sf=1,2,3,5,6,7&uipt=1,2,3,4,5,6,7,8&v=8&num_homes=350&ord=redfin-recommended-asc"

def gis_url(cfg: dict, page_num: int, sold_within_days: int | None = None) -> str:
    mpt = "99" if sold_within_days else "1"
    base = (
        f"{GIS_BASE}?al=1&market={cfg['market']}&mpt={mpt}"
        f"&region_id={cfg['region_id']}&region_type={cfg['region_type']}"
        f"&page_number={page_num}&start={(page_num-1)*350}"
        f"&status=9{GIS_COMMON}"
    )
    if sold_within_days:
        base += f"&sold_within_days={sold_within_days}"
    return base

async def gis_fetch(page, url: str) -> list[dict]:
    result = await page.evaluate(f"""
        async () => {{
            try {{
                const r = await fetch({json.dumps(url)}, {{
                    headers: {{
                        "Accept": "application/json, text/plain, */*",
                        "X-Requested-With": "XMLHttpRequest",
                    }}
                }});
                return {{ status: r.status, body: await r.text() }};
            }} catch(e) {{
                return {{ status: 0, body: "", error: e.toString() }};
            }}
        }}
    """)
    body = result.get("body", "")
    if not body:
        return []
    clean = body[4:] if body.startswith("{}&&") else body
    try:
        data = json.loads(clean)
    except Exception:
        return []
    return data.get("payload", {}).get("homes", [])


# ── Main scrape ───────────────────────────────────────────────────────────────

async def scrape_city(city_key: str, sold_within_days: int, conn: sqlite3.Connection):
    cfg = CITIES[city_key]
    now = datetime.now(timezone.utc).isoformat()

    async with async_playwright() as pw:
        browser, context = await build_context(pw)
        try:
            page = await context.new_page()
            await stealth_async(page)
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver',  { get: () => undefined });
                Object.defineProperty(navigator, 'plugins',    { get: () => [1, 2, 3] });
                Object.defineProperty(navigator, 'languages',  { get: () => ['en-US', 'en'] });
                window.chrome = { runtime: {} };
            """)

            # ── Step 1: warm the session ──────────────────────────────────────
            print(f"\n  [1] Warming session on {cfg['search_url']}")
            await page.goto(cfg["search_url"], wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(random.uniform(3, 5))

            # ── Step 2: active listings ───────────────────────────────────────
            print(f"  [2] Fetching active listings...")
            listings_total = 0
            for pg in range(1, 4):  # up to 3 pages = 1050 homes
                url = gis_url(cfg, pg)
                homes = await gis_fetch(page, url)
                if not homes:
                    break
                active = [h for h in homes if h.get("mlsStatus") not in ("Closed",)]
                for h in active:
                    p = parse_home(h, now)
                    _fix_lat_long(p, h)
                    upsert_listing(conn, p)
                conn.commit()
                listings_total += len(active)
                print(f"      Page {pg}: {len(active)} active listings  (total so far: {listings_total})")
                if len(homes) < 350:
                    break
                await asyncio.sleep(random.uniform(1, 2))

            # ── Step 3: recent sales ──────────────────────────────────────────
            print(f"\n  [3] Fetching recent sales (last {sold_within_days} days)...")
            sales_total = 0
            for pg in range(1, 6):  # up to 5 pages = 1750 sold homes
                url = gis_url(cfg, pg, sold_within_days)
                homes = await gis_fetch(page, url)
                if not homes:
                    break
                sold = [h for h in homes if h.get("mlsStatus") in ("Closed", "Sold")]
                for h in sold:
                    p = parse_home(h, now)
                    _fix_lat_long(p, h)
                    upsert_sale(conn, p)
                conn.commit()
                sales_total += len(sold)
                print(f"      Page {pg}: {len(sold)} sales  (total so far: {sales_total})")
                # GIS returns sold homes first — when a full page has none, we've exhausted them
                if len(homes) < 350 or len(sold) == 0:
                    break
                await asyncio.sleep(random.uniform(1, 2))

            print(f"\n  Done: {listings_total} active listings, {sales_total} sales stored")

        finally:
            await context.close()
            await browser.close()


# ── CLI / reporting ───────────────────────────────────────────────────────────

def print_summary(conn: sqlite3.Connection, city: str):
    print("\n" + "=" * 60)
    print(f"  Database: {DB_FILE}")
    print("=" * 60)

    # Active listings summary
    row = conn.execute(
        "SELECT COUNT(*) n, MIN(price) mn, MAX(price) mx, AVG(price) avg FROM listings WHERE city = ?",
        (city.title(),)
    ).fetchone()
    if row and row["n"]:
        print(f"\n  Active Listings ({row['n']} records)")
        print(f"    Price range: ${row['mn']:,} – ${row['mx']:,}  |  Avg: ${row['avg']:,.0f}")

    # Recent sales summary
    row2 = conn.execute(
        "SELECT COUNT(*) n, MIN(sold_price) mn, MAX(sold_price) mx, AVG(sold_price) avg, "
        "MIN(sold_date) earliest, MAX(sold_date) latest "
        "FROM sales WHERE city = ?",
        (city.title(),)
    ).fetchone()
    if row2 and row2["n"]:
        print(f"\n  Recent Sales ({row2['n']} records)")
        print(f"    Price range: ${row2['mn']:,} – ${row2['mx']:,}  |  Avg: ${row2['avg']:,.0f}")
        print(f"    Date range : {row2['earliest']} – {row2['latest']}")

    # Sample sales
    print(f"\n  Sample sales:")
    rows = conn.execute(
        "SELECT street, city, state, zip, sold_date, sold_price, beds, baths, sqft, property_type "
        "FROM sales WHERE city = ? ORDER BY sold_date DESC LIMIT 8",
        (city.title(),)
    ).fetchall()
    for r in rows:
        price_str = f"${r['sold_price']:,}" if r["sold_price"] else "N/A"
        print(f"    {r['sold_date']}  {price_str:>12s}  {r['street']}, {r['city']} {r['state']} {r['zip']}"
              f"  |  {r['beds']}bd/{r['baths']}ba  {r['sqft']}sqft  [{r['property_type']}]")

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Redfin scraper — listings + sales data")
    parser.add_argument("--city",       default="seattle", choices=list(CITIES), help="City to scrape")
    parser.add_argument("--sold-days",  type=int, default=365, help="Sales within N days (default 365)")
    args = parser.parse_args()

    print("=" * 60)
    print(f"  Redfin Scraper  |  city={args.city}  |  sold_days={args.sold_days}")
    print("=" * 60)

    conn  = init_db()
    start = time.perf_counter()

    asyncio.run(scrape_city(args.city, args.sold_days, conn))

    elapsed = time.perf_counter() - start
    print(f"\n  Completed in {elapsed:.1f}s")

    print_summary(conn, args.city)
    conn.close()


if __name__ == "__main__":
    main()
