"""Remove rows orphaned from vendors, and restore the vendor foreign keys.

DROP TABLE vendors CASCADE removes the foreign keys that other tables hold
against it. create_all cannot put them back, because those child tables still
exist, so the constraints stayed missing and rows could outlive their vendor.

Usage:
    python repair_vendor_fks.py [--dry-run]
"""

import argparse

from sqlalchemy import text

from database import IS_SQLITE, engine

# Child rows first, so a delete never trips a constraint that still exists.
ORPHAN_CHECKS = [
    ("campaign_societies", "campaign_id", "campaigns", "id"),
    ("campaigns", "vendor_id", "vendors", "id"),
    ("ad_templates", "vendor_id", "vendors", "id"),
    ("transactions", "user_id", "vendors", "id"),
    ("transactions", "wallet_id", "wallets", "id"),
    ("wallets", "user_id", "vendors", "id"),
    ("place_recommendations", "place_id", "places", "id"),
    ("society_ad_pricing", "society_id", "societies", "id"),
    ("campaign_societies", "society_id", "societies", "id"),
]

# name -> (table, column, parent, parent column, on delete)
WANTED_FKS = {
    "fk_wallets_user_id_vendors": ("wallets", "user_id", "vendors", "id", "RESTRICT"),
    "fk_transactions_user_id_vendors": ("transactions", "user_id", "vendors", "id", "RESTRICT"),
    "fk_campaigns_vendor_id_vendors": ("campaigns", "vendor_id", "vendors", "id", "RESTRICT"),
    "fk_ad_templates_vendor_id_vendors": ("ad_templates", "vendor_id", "vendors", "id", "CASCADE"),
}


def orphan_sql(tbl, col, parent, pcol):
    return (f"from {tbl} t left join {parent} p on p.{pcol} = t.{col} "
            f"where t.{col} is not null and p.{pcol} is null")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with engine.connect() as c:
        print("=== orphans ===")
        found = False
        for tbl, col, parent, pcol in ORPHAN_CHECKS:
            n = c.execute(text(f"select count(*) {orphan_sql(tbl, col, parent, pcol)}")).scalar()
            if n:
                found = True
                print(f"  {tbl}.{col} -> {parent}.{pcol}: {n}")
        if not found:
            print("  none")

    if args.dry_run:
        print("\ndry run - nothing written")
        return

    with engine.begin() as c:
        for tbl, col, parent, pcol in ORPHAN_CHECKS:
            n = c.execute(text(
                f"delete from {tbl} where {col} is not null and {col} not in "
                f"(select {pcol} from {parent} where {pcol} is not null)"
            )).rowcount
            if n:
                print(f"  deleted {n} orphaned rows from {tbl} ({col})")

    if IS_SQLITE:
        # SQLite cannot add a constraint to an existing table; it only matters
        # in Postgres, where the drop actually removed them.
        print("\nsqlite: constraint restore skipped (table rebuild required)")
        return

    with engine.begin() as c:
        present = {r[0] for r in c.execute(text(
            "select conname from pg_constraint where contype='f'"))}
        for name, (tbl, col, parent, pcol, rule) in WANTED_FKS.items():
            # cast(... as regclass) rather than ::regclass: SQLAlchemy reads
            # the :: in a text() block as the start of a bind parameter.
            exists = c.execute(text("""
                select count(*) from pg_constraint con
                join pg_attribute a on a.attrelid = con.conrelid and a.attnum = con.conkey[1]
                where con.contype='f'
                  and con.conrelid = cast(:t as regclass)
                  and a.attname = :c
            """), {"t": tbl, "c": col}).scalar()
            if exists:
                print(f"  {tbl}.{col}: foreign key already present")
                continue
            c.execute(text(
                f"alter table {tbl} add constraint {name} "
                f"foreign key ({col}) references {parent}({pcol}) on delete {rule}"))
            print(f"  restored {tbl}.{col} -> {parent}.{pcol} ON DELETE {rule}")

    with engine.connect() as c:
        print("\n=== vendor foreign keys now ===")
        for r in c.execute(text("""
            select conrelid::regclass::text, pg_get_constraintdef(oid)
            from pg_constraint where contype='f'
              and confrelid = cast('vendors' as regclass) order by 1""")):
            print(f"  {r[0]:20} {r[1]}")


if __name__ == "__main__":
    main()
