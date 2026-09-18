"""Seed demo recommendations so places do not all show zero.

Rows go into place_recommendations rather than a count column on places, so a
real recommendation from the app increments the same number the demo data
produced, and isRecommendedByCurrentUser keeps working.

Usage:
    python seed_recommendations.py [--dry-run] [--max 24] [--seed 42] [--reset]
"""

import argparse
import random

from database import Base, SessionLocal, engine
import main  # registers Place and the rest of the models
from main import Place
from places_api import PlaceRecommendation

BATCH_SIZE = 2000

# Demo identities. Real app users send their own opaque id, so these cannot
# collide with a genuine recommendation.
DEMO_USER_PREFIX = "demo_user_"


def pick_count(rng, max_count):
    """Skewed so a few places look popular and most have a handful.

    A uniform spread would make every place look equally liked, which reads as
    obviously fake on a map.
    """
    roll = rng.random()
    if roll < 0.12:
        return 0
    if roll < 0.70:
        return rng.randint(1, max(1, max_count // 4))
    if roll < 0.93:
        return rng.randint(max_count // 4, max(2, max_count // 2))
    return rng.randint(max_count // 2, max_count)


def main_():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max", type=int, default=24, help="highest count for a place")
    ap.add_argument("--seed", type=int, default=42, help="fixed so re-runs match")
    ap.add_argument("--reset", action="store_true",
                    help="delete existing demo rows first (real ones are kept)")
    args = ap.parse_args()

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if args.reset and not args.dry_run:
            removed = db.query(PlaceRecommendation).filter(
                PlaceRecommendation.app_user_id.like(f"{DEMO_USER_PREFIX}%")
            ).delete(synchronize_session=False)
            db.commit()
            print(f"removed {removed} existing demo rows")

        places = [p[0] for p in db.query(Place.id).all()]
        existing = {
            (pid, uid)
            for pid, uid in db.query(
                PlaceRecommendation.place_id, PlaceRecommendation.app_user_id
            ).all()
        }

        rng = random.Random(args.seed)
        rows, per_place = [], {}
        for pid in places:
            count = pick_count(rng, args.max)
            per_place[pid] = count
            for i in range(count):
                uid = f"{DEMO_USER_PREFIX}{i:03d}"
                if (pid, uid) not in existing:
                    rows.append({"place_id": pid, "app_user_id": uid})

        counts = sorted(per_place.values())
        print(f"places            : {len(places)}")
        print(f"rows to insert    : {len(rows)}")
        print(f"with zero         : {sum(1 for v in counts if v == 0)}")
        print(f"min / median / max: {counts[0]} / {counts[len(counts)//2]} / {counts[-1]}")
        print(f"total recommends  : {sum(counts)}")

        if args.dry_run:
            print("\ndry run - nothing written")
            return

        for start in range(0, len(rows), BATCH_SIZE):
            db.bulk_insert_mappings(PlaceRecommendation, rows[start:start + BATCH_SIZE])
            db.commit()
        print(f"\nplace_recommendations rows: {db.query(PlaceRecommendation).count()}")
    finally:
        db.close()


if __name__ == "__main__":
    main_()
