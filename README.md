# IMD Rainfall Tracker

Automatically downloads India Meteorological Department's (IMD) daily
**Subdivision-wise Rainfall Distribution** PDF, converts it to CSV, and
keeps a dated history — with automatic retries so a single network
hiccup or a stale/unavailable file never breaks the pipeline.

Source PDF: `https://mausam.imd.gov.in/Rainfall/SUBDIVISION_RAINFALL_DISTRIBUTION_COUNTRY_INDIA_cd.pdf`

## What you get

- `output/latest.csv` — always the most recent successful conversion
- `output/IMD_Subdivision_Rainfall_<YYYY-MM-DD>.csv` — one file per day, so you build a history over time
- `imd_rainfall.log` — a log of every attempt (useful for debugging if IMD changes their PDF format)

## Ways to run this

### 1. GitHub Actions (recommended — no server needed)

Already set up in `.github/workflows/daily_rainfall.yml`. Once you push
this repo to GitHub:

- It runs automatically at ~09:00, ~12:00, and ~15:00 IST every day.
- Each run first checks whether today's CSV already exists — if it
  does, the run is a no-op. So the 12:00 and 15:00 runs act as free
  automatic retries if the 09:00 run failed for any reason.
- Successful runs commit the new CSV straight back into `output/` in
  this repo, so your history builds up automatically.
- You can also trigger it manually any time from the **Actions** tab →
  "Daily IMD Rainfall Download" → **Run workflow**.

No setup beyond pushing the repo — `permissions: contents: write` in
the workflow lets the Action commit on your behalf.

### 2. Your own machine/server via cron

```bash
pip install -r requirements.txt
```

```cron
0 9,12,15,18 * * *  cd /path/to/imd-rainfall-tracker && /usr/bin/python3 imd_rainfall_downloader.py --once >> cron.log 2>&1
```

### 3. Standalone daemon (always-on machine, no cron available)

```bash
python3 imd_rainfall_downloader.py --daemon --at 09:00
```

## How the retry logic works

Every run (`--once` or a daemon tick) tries the full pipeline —
download → extract text → parse → write CSV — up to **6 times** with
exponential backoff (30s, 1m, 2m, 4m, 8m, capped at 30m) before giving
up for that run. Combined with the staggered GitHub Actions schedule
above, a single day's data has multiple independent chances to succeed
before it's considered a miss.

## Data columns

| Column | Meaning |
|---|---|
| `Region` | One of East & NE India, North West India, Central India, South Peninsula, or Country as a Whole |
| `No` | Subdivision number within its region (blank for region/country total rows) |
| `Subdivision` | Name of the meteorological subdivision |
| `Day_Actual_mm` / `Day_Normal_mm` | Actual and normal rainfall (mm) for the reporting day |
| `Day_%Dep` / `Day_Category` | % departure from normal and category (LE=Large Excess, E=Excess, N=Normal, D=Deficient, LD=Large Deficient, NR=No Rain) |
| `Period_Actual_mm` / `Period_Normal_mm` | Actual and normal rainfall (mm) for the cumulative season-to-date period |
| `Period_%Dep` / `Period_Category` | % departure and category for that cumulative period |

## Notes / limitations

- IMD publishes this PDF at a static filename, but doesn't always
  refresh its contents daily — the script faithfully converts whatever
  is currently posted, but can't guarantee IMD updates it every day.
- If IMD changes the PDF's layout, `parse_rows()` in
  `imd_rainfall_downloader.py` will need its regex patterns updated —
  check `imd_rainfall.log` for "did not match known patterns" warnings
  as an early signal.

## License

MIT — see `LICENSE`.
