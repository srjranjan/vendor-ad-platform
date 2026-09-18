"""Database migration script for Wallet and Transaction tables.
Run with: python migrate_wallet.py
"""

import sys
from sqlalchemy import inspect
from database import engine, Base
import wallet  # Ensure Wallet and Transaction are registered with Base.metadata


def run_migration():
    print(f"Connecting to database: {engine.url}")
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()

    print(f"Existing tables before migration: {existing_tables}")

    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    updated_tables = inspector.get_table_names()
    print(f"Tables after migration: {updated_tables}")

    for target in ["wallets", "transactions"]:
        if target in updated_tables:
            columns = [col["name"] for col in inspector.get_columns(target)]
            print(f"Table '{target}' columns: {columns}")
        else:
            print(f"[ERROR] Table '{target}' was NOT created!")
            sys.exit(1)

    print("Migration completed successfully.")


if __name__ == "__main__":
    run_migration()
