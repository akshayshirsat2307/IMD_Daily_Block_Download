#!/usr/bin/env python3
"""
IMD Subdivision-wise Rainfall Distribution - Daily Downloader & CSV Converter
==============================================================================

Downloads the daily rainfall PDF published by IMD (India Meteorological
Department), extracts the subdivision-wise rainfall table, and saves it
as a dated CSV file. Built to run unattended every day: if a step fails
(network blip, IMD server down, PDF not yet updated, etc.) it retries
with exponential backoff, logs everything, and never crashes silently.

USAGE
-----
Run once (e.g. from cron):
    python3 imd_rainfall_downloader.py --once

Run continuously as a daemon that fires once a day at a fixed time
(good for a server/VM that stays on, no cron needed):
    python3 imd_rainfall_downloader.py --daemon --at 09:00

REQUIREMENTS
------------
    pip install requests pdfplumber schedule

FILES PRODUCED
--------------
    output/IMD_Subdivision_Rainfall_<YYYY-MM-DD>.csv   -- one per successful run
    output/latest.csv                                   -- always overwritten with newest data
    imd_rainfall.log                                    -- rolling log of every attempt

SCHEDULING WITHOUT --daemon (recommended for servers): add a cron entry
that runs a few times a day; the script skips work if today's file
already exists, so extra runs are harmless and act as automatic retries:

    0 9,12,15,18 * * *  cd /path/to/script && /usr/bin/python3 imd_rainfall_downloader.py --once >> cron.log 2>&1
"""

import argparse
import csv
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
PDF_URL = "https://mausam.imd.gov.in/Rainfall/SUBDIVISION_RAINFALL_DISTRIBUTION_COUNTRY_INDIA_cd.pdf"

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
RAW_DIR = BASE_DIR / "raw_pdfs"
LOG_FILE = BASE_DIR / "imd_rainfall.log"

MAX_ATTEMPTS = 6          # how many times to retry the whole pipeline in one run
INITIAL_BACKOFF_SEC = 30  # first wait before retry
MAX_BACKOFF_SEC = 30 * 60 # cap the backoff at 30 minutes
REQUEST_TIMEOUT = 30      # seconds per HTTP request

OUTPUT_DIR.mkdir(exist_ok=True)
RAW_DIR.mkdir(exist_ok=True)

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("imd_rainfall")

# --------------------------------------------------------------------------
# Regex patterns for the two row shapes in the PDF table
# --------------------------------------------------------------------------
SUB_PATTERN = re.compile(
    r"^(\d+)\s+(.+?)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+)%\s+(\w+)\s+"
    r"(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+)%\s+(\w+)$"
)
REGION_PATTERN = re.compile(
    r"^([A-Z][A-Z&.\s]+?)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)$"
)

CSV_HEADER = [
    "Region", "No", "Subdivision",
    "Day_Actual_mm", "Day_Normal_mm", "Day_%Dep", "Day_Category",
    "Period_Actual_mm", "Period_Normal_mm", "Period_%Dep", "Period_Category",
]


# --------------------------------------------------------------------------
# Step 1: download the PDF
# --------------------------------------------------------------------------
def download_pdf(url: str, dest: Path) -> None:
    log.info(f"Downloading PDF from {url}")
    resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "")
    if "pdf" not in content_type.lower() and not resp.content[:4] == b"%PDF":
        raise ValueError(f"Response does not look like a PDF (Content-Type: {content_type})")
    dest.write_bytes(resp.content)
    log.info(f"Saved PDF -> {dest} ({len(resp.content):,} bytes)")


# --------------------------------------------------------------------------
# Step 2: extract raw text from the PDF
# --------------------------------------------------------------------------
def extract_text(pdf_path: Path) -> str:
    if pdfplumber is None:
        raise RuntimeError("pdfplumber is not installed. Run: pip install pdfplumber")
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            text_parts.append(page_text)
    full_text = "\n".join(text_parts)
    if not full_text.strip():
        raise ValueError("No text could be extracted from the PDF (possibly a scanned/image PDF)")
    return full_text


# --------------------------------------------------------------------------
# Step 3: parse the extracted text into rows
# --------------------------------------------------------------------------
def parse_rows(raw_text: str) -> list:
    # Normalize whitespace: pdfplumber can emit tabs, non-breaking spaces,
    # or runs of multiple spaces depending on the PDF's internal layout.
    normalized = raw_text.replace("\xa0", " ").replace("\t", " ")
    normalized = re.sub(r"[ ]{2,}", " ", normalized)
    lines = [l.strip() for l in normalized.splitlines() if l.strip()]

    rows = []
    current_region = ""
    unmatched = []

    for line in lines:
        m = SUB_PATTERN.match(line)
        if m:
            no, name, d_act, d_norm, d_dep, d_cat, p_act, p_norm, p_dep, p_cat = m.groups()
            rows.append([current_region, no, name.strip(), d_act, d_norm, d_dep, d_cat,
                         p_act, p_norm, p_dep, p_cat])
            continue

        m2 = REGION_PATTERN.match(line)
        if m2:
            name, d_act, d_norm, p_act, p_norm = m2.groups()
            current_region = name.strip()
            rows.append([current_region, "", "(Region/Country Total)", d_act, d_norm, "", "",
                         p_act, p_norm, "", ""])
            continue

        unmatched.append(line)

    if not rows:
        debug_path = OUTPUT_DIR / "debug_last_failed_extract.txt"
        debug_path.write_text(raw_text)
        log.error(f"Zero rows parsed. Raw extracted text saved to {debug_path} for inspection.")
        log.error("First 40 lines of extracted text (for debugging):")
        for l in lines[:40]:
            log.error(f"  RAW> {l!r}")
        raise ValueError("Parsed zero rows - PDF layout may have changed")

    if unmatched:
        log.warning(f"{len(unmatched)} line(s) did not match known patterns "
                    f"(header/footer text - usually harmless): {unmatched[:3]}...")

    return rows


# --------------------------------------------------------------------------
# Step 4: write CSV
# --------------------------------------------------------------------------
def write_csv(rows: list, dest: Path) -> None:
    with open(dest, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        w.writerows(rows)
    log.info(f"Wrote {len(rows)} rows -> {dest}")


# --------------------------------------------------------------------------
# Full pipeline for a single run
# --------------------------------------------------------------------------
def run_pipeline() -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    pdf_path = RAW_DIR / f"rainfall_{today}.pdf"
    csv_path = OUTPUT_DIR / f"IMD_Subdivision_Rainfall_{today}.csv"
    latest_path = OUTPUT_DIR / "latest.csv"

    if csv_path.exists():
        log.info(f"{csv_path.name} already exists for today - skipping re-download.")
        return csv_path

    download_pdf(PDF_URL, pdf_path)
    raw_text = extract_text(pdf_path)
    rows = parse_rows(raw_text)
    write_csv(rows, csv_path)
    write_csv(rows, latest_path)  # convenience copy that's always the newest
    return csv_path


# --------------------------------------------------------------------------
# Retry wrapper: keeps trying the pipeline with exponential backoff
# --------------------------------------------------------------------------
def run_with_retries(max_attempts: int = MAX_ATTEMPTS) -> bool:
    backoff = INITIAL_BACKOFF_SEC
    for attempt in range(1, max_attempts + 1):
        try:
            log.info(f"Attempt {attempt}/{max_attempts}")
            result = run_pipeline()
            log.info(f"SUCCESS: {result}")
            return True
        except Exception as e:
            log.error(f"Attempt {attempt} failed: {e}")
            if attempt == max_attempts:
                log.error("All attempts exhausted for today. Giving up until next scheduled run.")
                return False
            log.info(f"Retrying in {backoff} seconds...")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SEC)
    return False


# --------------------------------------------------------------------------
# Daemon mode: stays running, fires the job once a day at a fixed time
# --------------------------------------------------------------------------
def run_daemon(at_time: str) -> None:
    try:
        import schedule
    except ImportError:
        log.error("schedule is not installed. Run: pip install schedule")
        sys.exit(1)

    def job():
        log.info("=== Scheduled daily run starting ===")
        run_with_retries()

    schedule.every().day.at(at_time).do(job)
    log.info(f"Daemon started. Will run every day at {at_time}. Press Ctrl+C to stop.")

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except Exception as e:
            # Never let the scheduler loop itself die
            log.error(f"Unexpected error in scheduler loop: {e}")
            time.sleep(60)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="IMD daily rainfall PDF -> CSV automation")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="Run a single download+convert attempt (with retries), then exit. Use this with cron.")
    mode.add_argument("--daemon", action="store_true", help="Run forever, firing once a day at --at HH:MM.")
    parser.add_argument("--at", default="09:00", help="Time of day (HH:MM, 24h) for --daemon mode. Default 09:00.")
    args = parser.parse_args()

    if args.once:
        success = run_with_retries()
        sys.exit(0 if success else 1)
    elif args.daemon:
        run_daemon(args.at)


if __name__ == "__main__":
    main()
