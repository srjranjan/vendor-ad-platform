import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# --------------------------------------------------------------------------
# Database configuration
# --------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    # Local/testing fallback so the app boots without a Postgres instance.
    DATABASE_URL = "sqlite:///./vendor_ads.db"
    print("[warn] DATABASE_URL not set - falling back to local sqlite (vendor_ads.db)")

# Heroku-style URLs use the legacy "postgres://" scheme that SQLAlchemy 2.x rejects.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

IS_SQLITE = DATABASE_URL.startswith("sqlite")

if IS_SQLITE:
    engine_kwargs = {"connect_args": {"check_same_thread": False}}
else:
    # Railway Postgres caps concurrent connections, and drops idle ones.
    # pool_pre_ping discards dead connections; pool_recycle stays under that
    # idle timeout so we never hand out a socket the server already closed.
    engine_kwargs = {
        "pool_size": 5,
        "max_overflow": 5,
        "pool_timeout": 30,
        "pool_recycle": 1800,
    }

engine = create_engine(DATABASE_URL, pool_pre_ping=True, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _timezone

# The product is India-only, and campaign start and end dates are calendar days
# a vendor picked in IST. Running the date logic in UTC made a campaign
# launched before 05:30 IST start on the previous day and expire the same
# morning.
IST = _timezone(_timedelta(hours=5, minutes=30))


def ist_now() -> _datetime:
    """Current IST time, naive, matching the naive DateTime columns."""
    return _datetime.now(IST).replace(tzinfo=None)


def ist_today():
    """Today's date in IST."""
    return _datetime.now(IST).date()
