"""Seed society_ad_pricing from society size.

Usage:
    python seed_pricing.py [--dry-run] [--formats ISLAND,GATE_ARCH] [--overwrite]

Base price comes from a flat-count tier, then a per-format multiplier is
applied. Existing rows are left alone unless --overwrite is passed, so
manually negotiated prices survive a re-seed.
"""

import argparse
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP

from main import AdFormat, Base, Society, SocietyAdPricing, SessionLocal, engine

BATCH_SIZE = 2000

# (exclusive upper bound on flats, base price per day)
TIERS = [
    (100, Decimal("300")),
    (300, Decimal("500")),
    (800, Decimal("900")),
    (None, Decimal("1200")),
]

# Relative value of each format against the tier base price.
# These are an assumption - retune once real rate cards exist.
FORMAT_MULTIPLIERS = {
    AdFormat.ISLAND: Decimal("1.00"),        # premium ground-floor display
    AdFormat.GATE_ARCH: Decimal("1.20"),     # highest footfall, every entry/exit
    AdFormat.LIFT_BRANDING: Decimal("0.80"),
    AdFormat.STANDEE: Decimal("0.60"),
    AdFormat.TWO_X: Decimal("1.50"),         # double-size unit
    AdFormat.NOTICE_BOARD: Decimal("0.50"),  # lowest dwell time
}

MIN_PRICE = Decimal("100")


def base_price(total_flats):
    flats = total_flats or 0
    for upper, price in TIERS:
        if upper is None or flats < upper:
            return price
    return TIERS[-1][1]


def price_for(total_flats, fmt):
    raw = base_price(total_flats) * FORMAT_MULTIPLIERS[fmt]
    rounded = raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return max(rounded, MIN_PRICE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--formats", help="comma-separated subset, default all")
    ap.add_argument("--overwrite", action="store_true",
                    help="also update rows that already exist")
    ap.add_argument("--include-unknown-size", action="store_true",
                    help="also price societies whose flat count is missing")
    args = ap.parse_args()

    formats = (
        [AdFormat(f.strip()) for f in args.formats.split(",")]
        if args.formats else list(AdFormat)
    )

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        societies = db.query(Society.id, Society.total_flats).all()
        # A society with no flat count yields an estimated-impressions figure
        # of zero, so selling it would quote a price against no reach.
        unknown = [s for s in societies if not s[1]]
        if not args.include_unknown_size:
            societies = [s for s in societies if s[1]]
        existing = {
            (sid, fmt)
            for sid, fmt in db.query(
                SocietyAdPricing.society_id, SocietyAdPricing.ad_format
            ).all()
        }
        print(f"societies       : {len(societies)}")
        print(f"unknown size    : {len(unknown)}"
              f"{' (included)' if args.include_unknown_size else ' (skipped, not sellable)'}")
        print(f"formats         : {[f.value for f in formats]}")
        print(f"existing rows   : {len(existing)}")

        to_insert, to_update = [], []
        dist = Counter()
        for sid, flats in societies:
            for fmt in formats:
                price = price_for(flats, fmt)
                dist[(fmt.value, str(price))] += 1
                if (sid, fmt.value) in existing:
                    if args.overwrite:
                        to_update.append((sid, fmt.value, price))
                else:
                    to_insert.append({
                        "society_id": sid,
                        "ad_format": fmt.value,
                        "price_per_day": price,
                        "currency": "INR",
                        "is_active": True,
                    })

        print(f"to insert       : {len(to_insert)}")
        print(f"to update       : {len(to_update)}"
              f"{'' if args.overwrite else '  (use --overwrite)'}")
        print("\nprice distribution:")
        for (fmt, price), n in sorted(dist.items()):
            print(f"  {fmt:16} INR {price:>6}  x{n}")

        if args.dry_run:
            print("\ndry run - nothing written")
            return

        for start in range(0, len(to_insert), BATCH_SIZE):
            db.bulk_insert_mappings(SocietyAdPricing, to_insert[start:start + BATCH_SIZE])
            db.commit()
        for start in range(0, len(to_update), BATCH_SIZE):
            for sid, fmt, price in to_update[start:start + BATCH_SIZE]:
                db.query(SocietyAdPricing).filter(
                    SocietyAdPricing.society_id == sid,
                    SocietyAdPricing.ad_format == fmt,
                ).update({"price_per_day": price})
            db.commit()

        print(f"\ninserted {len(to_insert)}, updated {len(to_update)}")
        print(f"pricing rows    : {db.query(SocietyAdPricing).count()}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
