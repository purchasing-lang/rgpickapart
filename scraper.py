"""
RG Pick-A-Part Inventory Scraper
Scrapes https://www.rgpick-a-part.com/inventory.php daily,
tracks arrivals/removals, and estimates add dates based on stock numbers.

Key findings from data analysis:
- Stock numbers are NOT purely sequential over time — there are massive gaps
  (e.g. STK5642 → STK11227, a gap of 5,585) indicating numbering resets or
  different lot systems used over the years.
- Date estimation is ONLY reliable for STK50000+ (the modern sequential range).
  Below that, gaps are too large and irregular to interpolate meaningfully.
- In the STK60000-63500 range: ~17-18 vehicles added per day on average.
- Anchor STK63376 confirmed added ~3 days before 2026-03-13 = 2026-03-10.
"""

import os
import sys
import re
import logging
from datetime import date, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_KEY"]
INVENTORY_URL = "https://www.rgpick-a-part.com/inventory.php"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Only attempt date estimation for stocks in the modern sequential range.
# Below this threshold the numbering has too many gaps to be meaningful.
ESTIMATION_MIN_STOCK = 50000
CARS_PER_DAY         = 18   # calibrated from data: ~17.6/day in STK60000-63500

# ── Supabase client ───────────────────────────────────────────────────────────
db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# =============================================================================
#  SCRAPING
# =============================================================================

def fetch_inventory() -> list[dict]:
    log.info("Fetching %s", INVENTORY_URL)
    resp = requests.get(INVENTORY_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return parse_inventory(resp.text)


def parse_inventory(html: str) -> list[dict]:
    """
    Table id="vehicles" — columns: Year | Make | Model | Stock Number | Vehicle Row
    Full dataset rendered in HTML — DataTables paginates client-side only.
    Stock numbers on site are zero-padded (e.g. STK00007, STK63376).
    We store the integer value but preserve the original display string.
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", {"id": "vehicles"})
    if not table:
        for t in soup.find_all("table"):
            if re.search(r'STK\d{4,6}', t.get_text(), re.IGNORECASE):
                table = t
                log.warning("Fallback: using first table with STK numbers")
                break
    if not table:
        log.error("Could not locate inventory table in page HTML")
        return []

    vehicles = []
    for row in table.find("tbody").find_all("tr"):
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cells) < 4:
            continue

        year_raw  = cells[0]
        make      = cells[1]
        model     = cells[2]
        stock_raw = cells[3]   # e.g. "STK63376" or "STK00007"
        row_raw   = cells[4] if len(cells) > 4 else ""

        # Parse stock — strip STK prefix, convert to int (drops leading zeros)
        m = re.search(r'STK(\d+)', stock_raw, re.IGNORECASE)
        if not m:
            m = re.search(r'(\d{4,6})', stock_raw)
        if not m:
            continue
        stock        = int(m.group(1))
        stock_display = stock_raw.strip()  # preserve original e.g. "STK00007"

        try:
            year = int(year_raw) if year_raw.isdigit() else None
        except ValueError:
            year = None

        try:
            row_int = int(row_raw) if row_raw.isdigit() else None
        except ValueError:
            row_int = None

        # Skip row=0 — site uses 0 as a placeholder for "no row assigned"
        if row_int == 0:
            row_int = None

        vehicles.append({
            "stock":         stock,
            "stock_display": stock_display,
            "year":          year,
            "make":          make.strip().upper() or None,
            "model":         model.strip().upper() or None,
            "row":           row_int,
            "raw_text":      " ".join(cells)[:500],
        })

    log.info("Parsed %d vehicles from inventory page", len(vehicles))
    return vehicles


# =============================================================================
#  DATE ESTIMATION
# =============================================================================

def estimate_add_date(stock: int, anchor_points: list[dict]) -> Optional[date]:
    """
    Estimate when a vehicle was added using stock number interpolation.

    Only applied to STK50000+ — below that the numbering is too irregular.

    anchor_points: list of {'stock': int, 'date': date} — your confirmed dates.
    Interpolates between two surrounding anchors for best accuracy.
    Falls back to ~18 vehicles/day extrapolation from nearest anchor.
    """
    if stock < ESTIMATION_MIN_STOCK:
        return None  # too old / irregular numbering
    if not anchor_points:
        return None

    # Only use anchors in the modern range too
    anchors = sorted(
        [a for a in anchor_points if a["stock"] >= ESTIMATION_MIN_STOCK],
        key=lambda a: a["stock"]
    )
    if not anchors:
        return None

    lower = None
    upper = None
    for a in anchors:
        if a["stock"] <= stock:
            lower = a
        if a["stock"] >= stock and upper is None:
            upper = a

    if lower and upper and lower["stock"] != upper["stock"]:
        # Interpolate between two anchors — most accurate
        stock_span  = upper["stock"] - lower["stock"]
        day_span    = (upper["date"] - lower["date"]).days
        if day_span == 0:
            return lower["date"]
        rate        = stock_span / day_span   # stocks per day
        offset_days = round((stock - lower["stock"]) / rate)
        return lower["date"] + timedelta(days=offset_days)

    elif lower:
        # Extrapolate forward from nearest lower anchor
        offset_days = round((stock - lower["stock"]) / CARS_PER_DAY)
        return lower["date"] + timedelta(days=offset_days)

    elif upper:
        # Extrapolate backward from nearest upper anchor
        offset_days = round((upper["stock"] - stock) / CARS_PER_DAY)
        return upper["date"] - timedelta(days=offset_days)

    return None


# =============================================================================
#  DATABASE OPERATIONS
# =============================================================================

def load_anchor_points() -> list[dict]:
    result = db.table("stock_anchors").select("*").order("stock").execute()
    return [
        {
            "stock": r["stock"],
            "date":  date.fromisoformat(r["confirmed_date"]),
            "note":  r.get("note", ""),
        }
        for r in result.data
    ]


def get_last_snapshot() -> Optional[dict]:
    result = (
        db.table("daily_snapshots")
        .select("*")
        .order("snapshot_date", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def get_active_stocks() -> set[int]:
    result = db.table("vehicles").select("stock").eq("is_active", True).execute()
    return {row["stock"] for row in result.data}


def upsert_vehicles(vehicles: list[dict], anchors: list[dict], today: date) -> dict:
    current_stocks = {v["stock"] for v in vehicles}

    last_snapshot = get_last_snapshot()
    if last_snapshot:
        prev_stocks  = get_active_stocks()
        days_skipped = (today - date.fromisoformat(last_snapshot["snapshot_date"])).days - 1
        if days_skipped > 0:
            log.warning("Missed %d day(s) since last scrape — gap noted, comparisons still valid", days_skipped)
    else:
        prev_stocks  = set()
        days_skipped = 0

    # ── NEW vehicles ──────────────────────────────────────────────────────────
    new_stocks   = current_stocks - prev_stocks
    new_vehicles = [v for v in vehicles if v["stock"] in new_stocks]
    log.info("New vehicles: %d", len(new_vehicles))

    if new_vehicles:
        batch = []
        for v in new_vehicles:
            est_date = estimate_add_date(v["stock"], anchors)
            batch.append({
                "stock":              v["stock"],
                "year":               v["year"],
                "make":               v["make"],
                "model":              v["model"],
                "row":                v["row"],
                "raw_text":           v["raw_text"],
                "first_seen_date":    today.isoformat(),
                "estimated_add_date": est_date.isoformat() if est_date else None,
                "is_active":          True,
                "last_seen_date":     today.isoformat(),
            })
        for i in range(0, len(batch), 100):
            db.table("vehicles").upsert(batch[i:i+100], on_conflict="stock").execute()
        log.info("Inserted %d new vehicles in %d batches", len(batch), -(-len(batch)//100))

    # ── STILL PRESENT — update last_seen + row in batches ────────────────────
    # Use update (NOT upsert) to avoid touching NOT NULL cols like first_seen_date
    still_present = [v for v in vehicles if v["stock"] in (current_stocks & prev_stocks)]
    for i in range(0, len(still_present), 100):
        chunk_stocks = [v["stock"] for v in still_present[i:i+100]]
        db.table("vehicles").update({
            "last_seen_date": today.isoformat(),
        }).in_("stock", chunk_stocks).execute()
    # Update row + raw_text only where row is known
    for v in still_present:
        if v["row"] is not None:
            db.table("vehicles").update({
                "row":      v["row"],
                "raw_text": v["raw_text"],
            }).eq("stock", v["stock"]).execute()

    # ── REMOVED — single call ─────────────────────────────────────────────────
    removed_stocks = prev_stocks - current_stocks
    log.info("Vehicles removed: %d", len(removed_stocks))
    if removed_stocks:
        db.table("vehicles").update({
            "is_active":    False,
            "removed_date": today.isoformat(),
        }).in_("stock", list(removed_stocks)).execute()

    # ── Daily snapshot ─────────────────────────────────────────────────────────
    db.table("daily_snapshots").insert({
        "snapshot_date":  today.isoformat(),
        "total_active":   len(current_stocks),
        "added_count":    len(new_stocks),
        "removed_count":  len(removed_stocks),
        "added_stocks":   list(new_stocks),
        "removed_stocks": list(removed_stocks),
    }).execute()

    return {
        "added":          len(new_stocks),
        "removed":        len(removed_stocks),
        "total":          len(current_stocks),
        "new_list":       new_vehicles,
        "removed_stocks": list(removed_stocks),
    }


# =============================================================================
#  ANCHOR MANAGEMENT
# =============================================================================

def add_anchor(stock: int, confirmed_date: date, note: str = ""):
    """
    Manually confirm a stock → date. Only meaningful for STK50000+.
    Recalculates estimated dates for all vehicles within ±1000 stock numbers.
    """
    if stock < ESTIMATION_MIN_STOCK:
        log.warning("STK%d is below the estimation threshold (%d) — anchor saved but won't affect estimates", stock, ESTIMATION_MIN_STOCK)

    db.table("stock_anchors").upsert({
        "stock":          stock,
        "confirmed_date": confirmed_date.isoformat(),
        "note":           note,
    }, on_conflict="stock").execute()
    log.info("Anchor saved: STK%d → %s  (%s)", stock, confirmed_date, note)

    anchors = load_anchor_points()
    result = (
        db.table("vehicles")
        .select("stock")
        .gte("stock", stock - 1000)
        .lte("stock", stock + 1000)
        .execute()
    )
    updated = 0
    for row in result.data:
        est = estimate_add_date(row["stock"], anchors)
        if est:
            db.table("vehicles").update({
                "estimated_add_date": est.isoformat()
            }).eq("stock", row["stock"]).execute()
            updated += 1
    log.info("Recalculated estimated dates for %d nearby vehicles", updated)


# =============================================================================
#  REPORTING
# =============================================================================

def fmt_stock(stock: int) -> str:
    """Format stock number matching the site's display (STK00007, STK63376)."""
    return f"STK{stock:05d}"


def print_added(vehicles: list[dict], label: str):
    print(f"\n{'*'*66}")
    print(f"  *** {label} — {len(vehicles)} vehicles ***")
    print(f"{'*'*66}")
    if not vehicles:
        print("  (none)")
        return
    for v in sorted(vehicles, key=lambda x: (x.get("row") or 99999, -x["stock"])):
        est = v.get("estimated_add_date") or v.get("first_seen_date") or "?"
        row = f"Row:{v.get('row')}" if v.get("row") else "Row:—"
        print(
            f"  + {fmt_stock(v['stock'])}  "
            f"{str(v.get('year') or '?'):4}  "
            f"{(v.get('make') or '?'):10}  "
            f"{(v.get('model') or '?'):22}  "
            f"{row:<8}  Est:{est}"
        )


def print_removed(stocks: list[int], label: str):
    if not stocks:
        return
    result = db.table("vehicles").select("stock,year,make,model,row,removed_date").in_("stock", stocks).execute()
    rows = result.data
    print(f"\n{'~'*66}")
    print(f"  ~~~ {label} — {len(rows)} vehicles ~~~")
    print(f"{'~'*66}")
    for v in sorted(rows, key=lambda x: (x.get("row") or 99999, -x["stock"])):
        print(
            f"  - {fmt_stock(v['stock'])}  "
            f"{str(v.get('year') or '?'):4}  "
            f"{(v.get('make') or '?'):10}  "
            f"{(v.get('model') or '?'):22}"
        )


def report_period(days: int):
    since = (date.today() - timedelta(days=days)).isoformat()
    result = (
        db.table("vehicles")
        .select("stock,year,make,model,row,estimated_add_date,first_seen_date")
        .gte("first_seen_date", since)
        .order("row", desc=False, nullsfirst=False)
        .execute()
    )
    print(f"\n{'='*66}")
    print(f"  Added in last {days} day(s)  —  {len(result.data)} vehicles")
    print(f"{'='*66}")
    for v in result.data:
        est = v.get("estimated_add_date") or v.get("first_seen_date") or "?"
        row = f"Row:{v.get('row')}" if v.get("row") else "Row:—"
        print(
            f"  {fmt_stock(v['stock'])}  "
            f"{str(v.get('year') or '?'):4}  "
            f"{(v.get('make') or '?'):10}  "
            f"{(v.get('model') or '?'):22}  "
            f"{row:<8}  Est:{est}"
        )


# =============================================================================
#  ENTRY POINTS
# =============================================================================

def run_scrape():
    today    = date.today()
    log.info("Starting scrape for %s", today)

    anchors  = load_anchor_points()
    log.info("Loaded %d anchor points", len(anchors))

    vehicles = fetch_inventory()
    if not vehicles:
        log.error("No vehicles parsed — aborting to avoid wiping DB")
        sys.exit(1)

    stats = upsert_vehicles(vehicles, anchors, today)
    log.info("Done. Added=%d  Removed=%d  Total=%d", stats["added"], stats["removed"], stats["total"])

    print(f"\n{'#'*66}")
    print(f"  RG Pick-A-Part  —  {today}")
    print(f"  Total: {stats['total']}  |  Added: {stats['added']}  |  Removed: {stats['removed']}")
    print(f"{'#'*66}")

    print_added(stats["new_list"], "ADDED TODAY")
    print_removed(stats["removed_stocks"], "REMOVED TODAY")

    for days in [3, 7, 14]:
        report_period(days)

    log.info("Complete.")


def run_report():
    print(f"\n  RG Pick-A-Part  —  Report  {date.today()}")
    for days in [1, 3, 7, 14]:
        report_period(days)


# =============================================================================
#  CLI
# =============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="RG Pick-A-Part scraper")
    sub    = parser.add_subparsers(dest="cmd")

    sub.add_parser("scrape", help="Run today's scrape and store results")
    sub.add_parser("report", help="Print reports without scraping")

    ap = sub.add_parser("anchor", help="Add a confirmed stock → date anchor")
    ap.add_argument("stock", type=int, help="Stock number e.g. 63376")
    ap.add_argument("date",            help="Confirmed add date YYYY-MM-DD")
    ap.add_argument("--note", default="")

    args = parser.parse_args()

    if   args.cmd == "scrape" or args.cmd is None: run_scrape()
    elif args.cmd == "report":  run_report()
    elif args.cmd == "anchor":  add_anchor(args.stock, date.fromisoformat(args.date), args.note)
