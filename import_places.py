"""Import scraped places from places.csv into the database.

Usage:
    python import_places.py places.csv [--dry-run] [--limit N]

Targets whatever DATABASE_URL points at. Re-running is safe: rows are matched
on `url`, which is unique per place, and updated in place.
"""

import argparse
import csv
import re
import sys
from collections import Counter

from main import Base, Place, SessionLocal, engine

BATCH_SIZE = 500

EXPECTED = [
    "name", "search_category", "category_label", "phone", "address", "rating",
    "reviews", "website", "hours", "image_url", "latitude", "longitude",
    "distance_km", "url", "source",
]

# The scrape leaves Private Use Area glyphs (map pin icons) in address text.
_PUA = re.compile(r"[-]")
_WS = re.compile(r"\s+")

# Sentinels the scraper writes instead of leaving a field empty.
_NULL_VALUES = {"", "not listed", "n/a", "na", "none", "null", "-"}


def clean(value, max_len=None):
    if value is None:
        return None
    text = _WS.sub(" ", _PUA.sub("", str(value))).strip()
    if text.lower() in _NULL_VALUES:
        return None
    if max_len and len(text) > max_len:
        text = text[:max_len]
    return text


def to_float(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def to_int(value):
    # Counts arrive float-formatted, e.g. "44.0".
    f = to_float(str(value).replace(",", "") if value else value)
    return int(f) if f is not None else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default="places.csv")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    Base.metadata.create_all(bind=engine)

    with open(args.path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != EXPECTED:
            sys.exit(f"Unexpected header.\n  expected: {EXPECTED}\n  found:    {reader.fieldnames}")
        rows = list(reader)

    stats = Counter()
    seen_urls = set()
    valid = []

    for i, row in enumerate(rows):
        if args.limit and i >= args.limit:
            break
        stats["read"] += 1

        name = clean(row["name"], 255)
        if not name:
            stats["skipped_blank_name"] += 1
            continue

        url = clean(row["url"], 512)
        if not url:
            stats["skipped_missing_url"] += 1
            continue
        if url in seen_urls:
            stats["skipped_duplicate_in_file"] += 1
            continue
        seen_urls.add(url)

        lat, lng = to_float(row["latitude"]), to_float(row["longitude"])
        if lat is None or lng is None:
            stats["skipped_missing_coords"] += 1
            continue
        if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            stats["skipped_invalid_coords"] += 1
            continue

        valid.append({
            "name": name,
            "search_category": clean(row["search_category"], 128),
            "category_label": clean(row["category_label"], 128),
            "phone": clean(row["phone"], 32),
            "address": clean(row["address"], 1024),
            "rating": to_float(row["rating"]),
            "reviews": to_int(row["reviews"]),
            "website": clean(row["website"], 1024),
            "hours": clean(row["hours"], 512),
            "image_url": clean(row["image_url"], 1024),
            "latitude": lat,
            "longitude": lng,
            "distance_km": to_float(row["distance_km"]),
            "url": url,
            "source": clean(row["source"], 64),
        })

    print(f"read                     : {stats['read']}")
    print(f"skipped (blank name)     : {stats['skipped_blank_name']}")
    print(f"skipped (missing url)    : {stats['skipped_missing_url']}")
    print(f"skipped (dup in file)    : {stats['skipped_duplicate_in_file']}")
    print(f"skipped (missing coords) : {stats['skipped_missing_coords']}")
    print(f"skipped (invalid coords) : {stats['skipped_invalid_coords']}")
    print(f"importable               : {len(valid)}")

    if args.dry_run:
        print("\ndry run - nothing written")
        return

    db = SessionLocal()
    try:
        existing = {u: pid for u, pid in db.query(Place.url, Place.id).all()}
        inserted = updated = 0
        for start in range(0, len(valid), BATCH_SIZE):
            for rec in valid[start:start + BATCH_SIZE]:
                if rec["url"] in existing:
                    db.query(Place).filter(Place.id == existing[rec["url"]]).update(rec)
                    updated += 1
                else:
                    db.add(Place(**rec))
                    inserted += 1
            db.commit()
        print(f"\ninserted {inserted}, updated {updated}")
        print(f"places in database       : {db.query(Place).count()}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
