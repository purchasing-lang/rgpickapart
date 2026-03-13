# RG Pick-A-Part Scraper – Setup & Usage

## 1. Supabase Setup

1. Go to your Supabase project → SQL Editor
2. Paste and run the contents of `schema.sql`
3. Copy your **Project URL** and **anon/service_role key** from
   Settings → API

## 2. Install dependencies

```bash
pip install -r requirements.txt
```

## 3. Set environment variables

```bash
export SUPABASE_URL="https://your-project.supabase.co"
export SUPABASE_KEY="your-service-role-key"
```

Add these to `~/.bashrc` or `~/.zshrc` so they persist.

Or create a `.env` file and load it:
```bash
# .env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-service-role-key
```

## 4. Run a manual scrape (test it first)

```bash
python scraper.py scrape
```

Output will show:
- How many vehicles were found
- How many are new today
- How many were removed
- A breakdown by 1/3/7/14-day windows

## 5. Schedule daily at 8 AM with cron

```bash
crontab -e
```

Add this line:
```
0 8 * * * cd /path/to/rgpickapart && SUPABASE_URL=... SUPABASE_KEY=... python scraper.py scrape >> /var/log/rgpickapart.log 2>&1
```

Or if you use a .env file, use:
```
0 8 * * * cd /path/to/rgpickapart && set -a && source .env && python scraper.py scrape >> /var/log/rgpickapart.log 2>&1
```

---

## Commands

### Run the daily scrape
```bash
python scraper.py scrape
```

### Just print reports without scraping
```bash
python scraper.py report
```

### Add a confirmed date anchor (most important for accuracy!)
```bash
python scraper.py anchor 63376 2026-03-10 --note "Santa Fe XL imports section"
python scraper.py anchor 63200 2026-03-07
```

Each anchor you add will automatically recalculate estimated dates for
all vehicles within ±500 stock numbers of that anchor point.

---

## Accuracy Tips

- **More anchors = more accuracy.** Add 2-3 per week at first.
- Anchors on either side of a range let the script interpolate precisely.
- A single anchor still extrapolates at ~20 cars/day as fallback.
- 1-3 day error is normal early on; shrinks as anchors accumulate.

## Database Queries (Supabase dashboard or SQL)

```sql
-- What came in today?
SELECT * FROM v_today;

-- What came in last 3 days?
SELECT * FROM v_recent_3;

-- Last 7 days
SELECT * FROM v_recent_7;

-- Last 14 days
SELECT * FROM v_recent_14;

-- What was crushed/removed this week?
SELECT * FROM v_removed_7;

-- Find a specific make/model added recently
SELECT * FROM v_recent_7 WHERE make = 'HONDA' AND model LIKE '%CIVIC%';

-- How many cars added per day this month?
SELECT first_seen_date, COUNT(*) as added
FROM vehicles
WHERE first_seen_date >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY first_seen_date
ORDER BY first_seen_date DESC;
```
