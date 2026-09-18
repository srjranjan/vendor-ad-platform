"""Purge vendor-side data and prune places to a keep list.

Wipes vendors, wallets, transactions, ad templates and campaigns so a demo can
be built fresh, then removes every place except those listed. Reference data
(societies, pricing) is untouched.

Usage:
    python reset_demo_data.py [--dry-run]
"""

import argparse

from sqlalchemy import bindparam, text

from database import engine

KEEP_PLACES = [
    17, 19, 20, 29, 30, 33, 46, 49, 56, 63, 69, 70, 76, 83, 86, 87, 92, 99,
    103, 106, 112, 114, 120, 136, 141, 152, 162, 166, 176, 220, 223, 230, 236,
    245, 249, 253, 255, 256, 261, 264, 285, 287, 322, 333, 339, 349, 352, 357,
    358, 360, 361, 399,
]

# Child rows first: campaigns and templates point at vendors, and wallets hold
# a RESTRICT foreign key that blocks a vendor delete while one exists.
PURGE_ORDER = [
    "campaign_societies",
    "campaigns",
    "ad_templates",
    "transactions",
    "wallets",
    "otp_verifications",
    "vendors",
    "ad_targets",
    "ad_campaigns",
]


def counts(c, tables):
    return {t: c.execute(text(f"select count(*) from {t}")).scalar() for t in tables}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    watched = PURGE_ORDER + ["places", "place_recommendations", "societies",
                             "society_ad_pricing"]
    with engine.connect() as c:
        before = counts(c, watched)
        # expanding=True renders one placeholder per id; SQLite rejects a
        # tuple bound to a single parameter, which Postgres happens to accept.
        keep_present = c.execute(
            text("select count(*) from places where id in :k").bindparams(
                bindparam("k", expanding=True)),
            {"k": KEEP_PLACES},
        ).scalar()

    print(f"keep list          : {len(KEEP_PLACES)} ids, {keep_present} present in places")
    print(f"places to delete   : {before['places'] - keep_present}")
    print("\nto purge:")
    for t in PURGE_ORDER:
        print(f"  {t:22} {before[t]}")

    if args.dry_run:
        print("\ndry run - nothing written")
        return

    with engine.begin() as c:
        for t in PURGE_ORDER:
            n = c.execute(text(f"delete from {t}")).rowcount
            print(f"  purged {t:22} {n}")
        # Recommendations on places that are going away.
        n = c.execute(
            text("delete from place_recommendations where place_id not in :k").bindparams(
                bindparam("k", expanding=True)),
            {"k": KEEP_PLACES},
        ).rowcount
        print(f"  removed {n} recommendations on dropped places")
        n = c.execute(
            text("delete from places where id not in :k").bindparams(
                bindparam("k", expanding=True)),
            {"k": KEEP_PLACES},
        ).rowcount
        print(f"  removed {n} places")

    with engine.connect() as c:
        after = counts(c, watched)
    print("\n=== after ===")
    for t in watched:
        print(f"  {t:22} {after[t]}")


if __name__ == "__main__":
    main()
