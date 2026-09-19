"""Delete vendors and everything hanging off them.

Usage:
    python delete_vendors.py VENDOR_ID [VENDOR_ID ...] [--dry-run]

Order matters: the vendor foreign keys are RESTRICT, and campaigns hold a
RESTRICT key to ad_templates, so campaigns go before the templates they used.
"""

import argparse

from sqlalchemy import bindparam, text

from database import engine


def expand(sql):
    return text(sql).bindparams(bindparam("ids", expanding=True))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vendor_ids", nargs="+")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    ids = args.vendor_ids

    with engine.connect() as c:
        found = c.execute(
            expand("select id, business_name, mobile_number from vendors where id in :ids"),
            {"ids": ids},
        ).all()
        missing = [v for v in ids if v not in {r[0] for r in found}]
        for r in found:
            print(f"  {r[0]}  {r[1]}  {r[2]}")
        if missing:
            print(f"  not found: {missing}")
        if not found:
            print("nothing to do")
            return
        mobiles = [r[2] for r in found]

    if args.dry_run:
        print("\ndry run - nothing written")
        return

    with engine.begin() as c:
        n = c.execute(expand(
            "delete from campaign_societies where campaign_id in "
            "(select id from campaigns where vendor_id in :ids)"), {"ids": ids}).rowcount
        print(f"  campaign_societies {n}")
        for tbl, col in (("campaigns", "vendor_id"),
                         ("transactions", "user_id"),
                         ("wallets", "user_id"),
                         ("ad_templates", "vendor_id")):
            n = c.execute(expand(f"delete from {tbl} where {col} in :ids"), {"ids": ids}).rowcount
            print(f"  {tbl:18} {n}")
        n = c.execute(
            text("delete from otp_verifications where mobile_number in :m").bindparams(
                bindparam("m", expanding=True)), {"m": mobiles}).rowcount
        print(f"  otp_verifications  {n}")
        n = c.execute(expand("delete from vendors where id in :ids"), {"ids": ids}).rowcount
        print(f"  vendors            {n}")

    with engine.connect() as c:
        left = c.execute(expand("select count(*) from vendors where id in :ids"), {"ids": ids}).scalar()
        print(f"\n  vendors remaining from that list: {left}")


if __name__ == "__main__":
    main()
