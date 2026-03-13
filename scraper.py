"""
RG Pick-A-Part Inventory Scraper
Scrapes https://www.rgpick-a-part.com/inventory.php daily,
tracks arrivals/removals, and estimates add dates based on stock numbers.
"""

import os
import sys
import json
import logging
import hashlib
import re
from datetime import date, datetime, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
INVENTORY_URL = "https://www.rgpick-a-part.com/inventory.php"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Supabase client ───────────────────────────────────────────────────────────
db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ═════════════════════════════════════════════════════════════════════════════
#  SCRAPING
# ═════════════════════════════════════════════════════════════════════════════

def fetch_inventory() -> list[dict]:
    """
    Download and parse the RG Pick-A-Part inventory page.
    Returns a list of vehicle dicts:
      { stock, year, make, model, trim, row, lot, raw_text }
    """
    log.info("Fetching %s", INVENTORY_URL)
    resp = requests.get(INVENTORY_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return parse_inventory(resp.text)


def parse_inventory(html: str) -> list[dict]:
    """
    Parse the RG Pick-A-Part inventory page.

    The page uses a jQuery DataTables table with id="vehicles".
    Columns (fixed order): Year | Make | Model | Stock Number | Vehicle Row

    The full dataset is rendered in the HTML even though DataTables
    paginates it client-side — so a single fetch gets all ~3000 rows.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Target the known table id
    table = soup.find("table", {"id": "vehicles"})
    if not table:
        # Fallback: any table containing STK stock numbers
        for t in soup.find_all("table"):
            if re.search(r'STK\d{4,6}', t.get_text(), re.IGNORECASE):
                table = t
                log.warning("Fallback: using first table with STK numbers (id='vehicles' not found)")
                break

    if not table:
        log.error("Could not locate inventory table in page HTML")
        return []

    vehicles = []
    for row in table.find("tbody").find_all("tr"):
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cells) < 4:
            continue

        # Column order: Year | Make | Model | Stock Number | Vehicle Row
        year_raw   = cells[0]
        make       = cells[1]
        model      = cells[2]
        stock_raw  = cells[3]   # e.g. "STK63376"
        row_raw    = cells[4] if len(cells) > 4 else ""

        # Parse stock — strip the "STK" prefix
        stock_match = re.search(r'(\d{4,6})', stock_raw)
        if not stock_match:
            log.debug("Skipping row — no stock number found: %s", cells)
            continue
        stock = int(stock_match.group(1))

        try:
            year = int(year_raw) if year_raw.isdigit() else None
        except ValueError:
            year = None

        try:
            row_int = int(row_raw) if row_raw.isdigit() else None
        except ValueError:
            row_int = None

        vehicles.append({
            "stock": stock,
            "year": year,
            "make": make.strip().upper() or None,
            "model": model.strip().upper() or None,
            "trim": None,   # not in this table; reserved for future
            "row": row_int,
            "lot": None,    # not in this table; reserved for future
            "raw_text": " ".join(cells)[:500],
        })

    log.info("Parsed %d vehicles from inventory page", len(vehicles))
    return vehicles


# ═════════════════════════════════════════════════════════════════════════════
#  DATE ESTIMATION
# ═════════════════════════════════════════════════════════════════════════════

def estimate_add_date(stock: int, anchor_points: list[dict]) -> Optional[date]:
    """
    Estimate when a vehicle was added using stock number interpolation.

    anchor_points is a list of {'stock': int, 'date': date} dicts
    sorted by stock ascending. These come from your manually-confirmed dates.

    Strategy:
      - If we have two anchors on either side of the stock, interpolate.
      - If only one side, extrapolate using ~20 vehicles/day.
    """
    if not anchor_points:
        return None

    anchors = sorted(anchor_points, key=lambda a: a["stock"])

    # Find surrounding anchors
    lower = None
    upper = None
    for a in anchors:
        if a["stock"] <= stock:
            lower = a
        if a["stock"] >= stock and upper is None:
            upper = a

    CARS_PER_DAY = 20  # fallback rate

    if lower and upper and lower["stock"] != upper["stock"]:
        # Interpolate
        stock_span = upper["stock"] - lower["stock"]
        day_span = (upper["date"] - lower["date"]).days
        if day_span == 0:
            return lower["date"]
        rate = stock_span / day_span  # stocks per day
        offset_days = round((stock - lower["stock"]) / rate)
        return lower["date"] + timedelta(days=offset_days)

    elif lower:
        # Extrapolate forward
        offset_days = round((stock - lower["stock"]) / CARS_PER_DAY)
        return lower["date"] + timedelta(days=offset_days)

    elif upper:
        # Extrapolate backward
        offset_days = round((upper["stock"] - stock) / CARS_PER_DAY)
        return upper["date"] - timedelta(days=offset_days)

    return None


# ═════════════════════════════════════════════════════════════════════════════
#  DATABASE OPERATIONS
# ═════════════════════════════════════════════════════════════════════════════

def load_anchor_points() -> list[dict]:
    """Load manually-confirmed stock→date anchors from Supabase."""
    result = db.table("stock_anchors").select("*").order("stock").execute()
    anchors = []
    for row in result.data:
        anchors.append({
            "stock": row["stock"],
            "date": date.fromisoformat(row["confirmed_date"]),
            "note": row.get("note", ""),
        })
    return anchors


def get_active_stocks() -> set[int]:
    """Return all stock numbers currently marked active in DB."""
    result = db.table("vehicles").select("stock").eq("is_active", True).execute()
    return {row["stock"] for row in result.data}


def upsert_vehicles(vehicles: list[dict], anchors: list[dict], today: date):
    """
    Insert new vehicles, update existing ones, mark removed ones inactive.
    """
    current_stocks = {v["stock"] for v in vehicles}
    active_stocks = get_active_stocks()

    # ── Newly added vehicles ─────────────────────────────────────────────────
    new_stocks = current_stocks - active_stocks
    log.info("New vehicles today: %d", len(new_stocks))

    for v in vehicles:
        if v["stock"] not in new_stocks:
            continue

        est_date = estimate_add_date(v["stock"], anchors)
        record = {
            "stock": v["stock"],
            "year": v["year"],
            "make": v["make"],
            "model": v["model"],
            "trim": v["trim"],
            "row": v["row"],
            "lot": v["lot"],
            "raw_text": v["raw_text"],
            "first_seen_date": today.isoformat(),
            "estimated_add_date": est_date.isoformat() if est_date else None,
            "is_active": True,
            "last_seen_date": today.isoformat(),
        }
        db.table("vehicles").upsert(record, on_conflict="stock").execute()

    # ── Still-present vehicles – update row/last_seen ────────────────────────
    still_present = current_stocks & active_stocks
    for v in vehicles:
        if v["stock"] not in still_present:
            continue
        db.table("vehicles").update({
            "row": v["row"],
            "lot": v["lot"],
            "last_seen_date": today.isoformat(),
            "raw_text": v["raw_text"],
        }).eq("stock", v["stock"]).execute()

    # ── Removed vehicles ─────────────────────────────────────────────────────
    removed_stocks = active_stocks - current_stocks
    log.info("Vehicles removed today: %d", len(removed_stocks))
    if removed_stocks:
        db.table("vehicles").update({
            "is_active": False,
            "removed_date": today.isoformat(),
        }).in_("stock", list(removed_stocks)).execute()

    # ── Log the daily snapshot ───────────────────────────────────────────────
    db.table("daily_snapshots").insert({
        "snapshot_date": today.isoformat(),
        "total_active": len(current_stocks),
        "added_count": len(new_stocks),
        "removed_count": len(removed_stocks),
        "added_stocks": list(new_stocks),
        "removed_stocks": list(removed_stocks),
    }).execute()

    return {
        "added": len(new_stocks),
        "removed": len(removed_stocks),
        "total": len(current_stocks),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  REPORTING
# ═════════════════════════════════════════════════════════════════════════════

def report_period(days: int):
    """Print vehicles added in the last N days."""
    since = (date.today() - timedelta(days=days)).isoformat()
    result = (
        db.table("vehicles")
        .select("stock,year,make,model,trim,row,lot,estimated_add_date,first_seen_date")
        .gte("first_seen_date", since)
        .order("stock", desc=True)
        .execute()
    )
    print(f"\n{'═'*60}")
    print(f"  Vehicles added in last {days} day(s)  ({len(result.data)} total)")
    print(f"{'═'*60}")
    for v in result.data:
        est = v.get("estimated_add_date") or v.get("first_seen_date") or "?"
        print(
            f"  STK{v['stock']:>6}  {v.get('year','?')} {v.get('make','?'):10} "
            f"{v.get('model','?'):15}  Row:{v.get('row','?'):>4}  Est:{est}"
        )


def report_removed(days: int):
    """Print vehicles removed in the last N days."""
    since = (date.today() - timedelta(days=days)).isoformat()
    result = (
        db.table("vehicles")
        .select("stock,year,make,model,trim,row,removed_date")
        .eq("is_active", False)
        .gte("removed_date", since)
        .order("removed_date", desc=True)
        .execute()
    )
    print(f"\n{'─'*60}")
    print(f"  Vehicles REMOVED in last {days} day(s)  ({len(result.data)} total)")
    print(f"{'─'*60}")
    for v in result.data:
        print(
            f"  STK{v['stock']:>6}  {v.get('year','?')} {v.get('make','?'):10} "
            f"{v.get('model','?'):15}  Removed:{v.get('removed_date','?')}"
        )


# ═════════════════════════════════════════════════════════════════════════════
#  ANCHOR MANAGEMENT  (called from CLI)
# ═════════════════════════════════════════════════════════════════════════════

def add_anchor(stock: int, confirmed_date: date, note: str = ""):
    """
    Manually confirm that a stock number was added on a specific date.
    This improves date estimation for all nearby vehicles.
    """
    db.table("stock_anchors").upsert({
        "stock": stock,
        "confirmed_date": confirmed_date.isoformat(),
        "note": note,
    }, on_conflict="stock").execute()
    log.info("Anchor saved: STK%d → %s (%s)", stock, confirmed_date, note)

    # Recalculate estimated_add_date for all vehicles near this anchor
    anchors = load_anchor_points()
    # Pull vehicles within ±500 stock of anchor to recalculate
    lo, hi = stock - 500, stock + 500
    result = (
        db.table("vehicles")
        .select("stock")
        .gte("stock", lo)
        .lte("stock", hi)
        .execute()
    )
    updated = 0
    for row in result.data:
        s = row["stock"]
        est = estimate_add_date(s, anchors)
        if est:
            db.table("vehicles").update({
                "estimated_add_date": est.isoformat(),
            }).eq("stock", s).execute()
            updated += 1
    log.info("Recalculated estimated dates for %d nearby vehicles", updated)


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def run_scrape():
    today = date.today()
    log.info("Starting scrape for %s", today)

    anchors = load_anchor_points()
    log.info("Loaded %d anchor points", len(anchors))

    vehicles = fetch_inventory()
    if not vehicles:
        log.error("No vehicles parsed – aborting to avoid wiping DB")
        sys.exit(1)

    stats = upsert_vehicles(vehicles, anchors, today)
    log.info(
        "Done. Added=%d  Removed=%d  Total active=%d",
        stats["added"], stats["removed"], stats["total"],
    )

    # Print daily summary
    print(f"\n{'━'*60}")
    print(f"  RG Pick-A-Part  –  Daily Report  {today}")
    print(f"{'━'*60}")
    for days in [1, 3, 7, 14]:
        report_period(days)
    report_removed(3)


def run_report():
    print(f"\n  RG Pick-A-Part  –  Inventory Report  {date.today()}")
    for days in [1, 3, 7, 14]:
        report_period(days)
    report_removed(7)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="RG Pick-A-Part scraper")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("scrape", help="Run today's scrape and store results")
    sub.add_parser("report", help="Print inventory reports without scraping")

    anchor_p = sub.add_parser("anchor", help="Add a confirmed stock→date anchor")
    anchor_p.add_argument("stock", type=int, help="Stock number, e.g. 63376")
    anchor_p.add_argument("date", help="Confirmed add date, YYYY-MM-DD")
    anchor_p.add_argument("--note", default="", help="Optional note")

    args = parser.parse_args()

    if args.cmd == "scrape" or args.cmd is None:
        run_scrape()
    elif args.cmd == "report":
        run_report()
    elif args.cmd == "anchor":
        confirmed = date.fromisoformat(args.date)
        add_anchor(args.stock, confirmed, args.note)
