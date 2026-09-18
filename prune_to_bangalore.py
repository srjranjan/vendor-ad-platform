"""Keep only Bangalore societies; drop the rest and their pricing rows.

Usage:
    python prune_to_bangalore.py [--dry-run] [--city bangalore]

Societies are recoverable by re-running import_societies.py against the
source export, and pricing by re-running seed_pricing.py.
"""

import argparse

from main import (AdTarget, Base, Society, SocietyAdPricing, SessionLocal,
                  engine)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="bangalore")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        from sqlalchemy import func
        keep_filter = func.lower(Society.city) == args.city.lower()

        total = db.query(Society).count()
        keep = db.query(Society).filter(keep_filter).count()
        drop = total - keep
        keep_ids = [r[0] for r in db.query(Society.id).filter(keep_filter).all()]

        pricing_total = db.query(SocietyAdPricing).count()
        pricing_keep = db.query(SocietyAdPricing).filter(
            SocietyAdPricing.society_id.in_(keep_ids)
        ).count() if keep_ids else 0

        print(f"societies total   : {total}")
        print(f"  keep ({args.city:9}): {keep}")
        print(f"  drop            : {drop}")
        print(f"pricing total     : {pricing_total}")
        print(f"  keep            : {pricing_keep}")
        print(f"  drop            : {pricing_total - pricing_keep}")

        linked = db.query(AdTarget).filter(
            ~AdTarget.society_id.in_(keep_ids)
        ).count() if keep_ids else db.query(AdTarget).count()
        print(f"ad_targets pointing at dropped societies: {linked}")
        if linked:
            print("  REFUSING: campaigns reference societies that would be deleted")
            return

        if args.dry_run:
            print("\ndry run - nothing deleted")
            return

        # Delete pricing first: SQLite does not enforce ON DELETE CASCADE
        # unless foreign keys are switched on per-connection.
        p = db.query(SocietyAdPricing).filter(
            ~SocietyAdPricing.society_id.in_(keep_ids)
        ).delete(synchronize_session=False)
        # Match on the keep-list rather than negating keep_filter: a NULL city
        # makes `NOT (lower(city) = ...)` evaluate to NULL, so those rows would
        # silently survive the delete.
        s = db.query(Society).filter(
            Society.id.notin_(keep_ids)
        ).delete(synchronize_session=False)
        db.commit()

        print(f"\ndeleted {p} pricing rows, {s} societies")
        print(f"societies remaining : {db.query(Society).count()}")
        print(f"pricing remaining   : {db.query(SocietyAdPricing).count()}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
