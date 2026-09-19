"""Link a place to the societies it serves, matching societies by name.

Usage:
    python link_place_societies.py 331 "ITTINA VEERU APARTMENT" "JAIN HEIGHTS" ...
    python link_place_societies.py 331 --names-file societies.txt

Names are matched case-insensitively and must resolve to exactly one society;
an ambiguous or unknown name is reported rather than guessed at.
"""

import argparse

from sqlalchemy import func

from database import SessionLocal
import main  # registers the models
from main import Place, PlaceSociety, Society


def resolve(db, name):
    exact = db.query(Society).filter(func.lower(Society.name) == name.strip().lower()).all()
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, f"{len(exact)} societies share that exact name"
    like = db.query(Society).filter(Society.name.ilike(f"%{name.strip()}%")).all()
    if len(like) == 1:
        return like[0], None
    if not like:
        return None, "no society matches"
    return None, f"ambiguous, {len(like)} matches: " + ", ".join(s.name for s in like[:3])


def main_():
    ap = argparse.ArgumentParser()
    ap.add_argument("place_id", type=int)
    ap.add_argument("names", nargs="*")
    ap.add_argument("--names-file")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    names = list(args.names)
    if args.names_file:
        with open(args.names_file) as fh:
            names += [n.strip() for n in fh.read().split(",") if n.strip()]
    if not names:
        raise SystemExit("no society names given")

    db = SessionLocal()
    try:
        place = db.query(Place).filter(Place.id == args.place_id).first()
        if not place:
            raise SystemExit(f"unknown place_id {args.place_id}")
        print(f"place {place.id}: {place.name}\n")

        linked = skipped = 0
        for name in names:
            society, problem = resolve(db, name)
            if problem:
                print(f"  SKIP  {name[:44]:46} {problem}")
                skipped += 1
                continue
            exists = db.query(PlaceSociety).filter(
                PlaceSociety.place_id == place.id,
                PlaceSociety.society_id == society.id,
            ).first()
            if exists:
                print(f"  have  {society.name[:44]:46} already linked")
                continue
            if not args.dry_run:
                db.add(PlaceSociety(place_id=place.id, society_id=society.id))
            print(f"  link  {society.name[:44]:46} flats={society.total_flats}")
            linked += 1

        if args.dry_run:
            db.rollback()
            print("\ndry run - nothing written")
            return
        db.commit()
        total = db.query(PlaceSociety).filter(PlaceSociety.place_id == place.id).count()
        print(f"\nlinked {linked}, skipped {skipped}; place now has {total} societies")
    finally:
        db.close()


if __name__ == "__main__":
    main_()
