"""Import societies from the BigQuery xlsx export into the database.

Usage:
    python import_societies.py society_data.xlsx [--dry-run] [--limit N]

Targets whatever DATABASE_URL points at, so to load Railway Postgres from a
laptop use the DATABASE_PUBLIC_URL from the Railway dashboard:

    DATABASE_URL=<public url> python import_societies.py society_data.xlsx

Re-running is safe: rows are matched on external_id and updated in place.
"""

import argparse
import sys
from collections import Counter

import openpyxl

from main import Base, Society, SessionLocal, engine

BATCH_SIZE = 1000

# Generous bounding box for India, used only to report suspicious rows -
# nothing is dropped on this basis.
INDIA_LAT = (6.0, 37.5)
INDIA_LNG = (68.0, 97.5)


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value):
    f = to_float(value)
    return int(f) if f is not None else 0


def clean(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def clean_id(value):
    """Normalise a source id to text.

    Numeric ids arrive from the export as floats, so a plain str() would turn
    26019 into "26019.0" and stop it matching the source system.
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return clean(value)


def read_rows(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = ws.iter_rows(values_only=True)
    header = [clean(h) for h in next(rows)]

    expected = ["id", "society_name", "city", "society_latitude",
                "society_longitude", "active_flats"]
    if header[:len(expected)] != expected:
        sys.exit(f"Unexpected header.\n  expected: {expected}\n  found:    {header}")

    for row in rows:
        if not row or all(c is None for c in row):
            continue
        # The export emits ragged rows when trailing cells are empty.
        yield tuple(row) + (None,) * (6 - len(row))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default="society_data.xlsx")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and report without writing")
    ap.add_argument("--limit", type=int, help="only process the first N rows")
    args = ap.parse_args()

    Base.metadata.create_all(bind=engine)

    stats = Counter()
    outside_india = 0
    seen_external_ids = set()
    valid = []

    for i, (ext_id, name, city, lat, lng, flats) in enumerate(read_rows(args.path)):
        if args.limit and i >= args.limit:
            break
        stats["read"] += 1

        name = clean(name)
        if not name:
            stats["skipped_blank_name"] += 1
            continue

        a, b = to_float(lat), to_float(lng)
        if a is None or b is None:
            stats["skipped_missing_coords"] += 1
            continue
        if not (-90 <= a <= 90) or not (-180 <= b <= 180):
            stats["skipped_invalid_coords"] += 1
            continue
        if not (INDIA_LAT[0] <= a <= INDIA_LAT[1] and INDIA_LNG[0] <= b <= INDIA_LNG[1]):
            outside_india += 1

        ext_id = clean_id(ext_id)
        if not ext_id:
            # The source id is the primary key, so a row without one
            # cannot be stored.
            stats["skipped_missing_id"] += 1
            continue
        if ext_id in seen_external_ids:
            stats["skipped_duplicate_in_file"] += 1
            continue
        seen_external_ids.add(ext_id)

        valid.append({
            "id": ext_id,
            "name": name,
            "city": clean(city),
            "latitude": a,
            "longitude": b,
            "total_flats": to_int(flats),
        })

    print(f"read                     : {stats['read']}")
    print(f"skipped (blank name)     : {stats['skipped_blank_name']}")
    print(f"skipped (missing coords) : {stats['skipped_missing_coords']}")
    print(f"skipped (invalid coords) : {stats['skipped_invalid_coords']}")
    print(f"skipped (missing id)     : {stats['skipped_missing_id']}")
    print(f"skipped (dup in file)    : {stats['skipped_duplicate_in_file']}")
    print(f"importable               : {len(valid)}")
    print(f"  of which outside India : {outside_india}  (kept, flagged only)")

    if args.dry_run:
        print("\ndry run - nothing written")
        return

    db = SessionLocal()
    try:
        existing = {sid for (sid,) in db.query(Society.id).all()}
        inserted = updated = 0
        for start in range(0, len(valid), BATCH_SIZE):
            chunk = valid[start:start + BATCH_SIZE]
            for rec in chunk:
                if rec["id"] in existing:
                    db.query(Society).filter(Society.id == rec["id"]).update(rec)
                    updated += 1
                else:
                    db.add(Society(**rec))
                    inserted += 1
            db.commit()
            print(f"  committed {min(start + BATCH_SIZE, len(valid))}/{len(valid)}")
        print(f"\ninserted {inserted}, updated {updated}")
        print(f"societies in database    : {db.query(Society).count()}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
