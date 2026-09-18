import os
from datetime import datetime
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
    text,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

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
# SQLAlchemy models
# --------------------------------------------------------------------------


class Society(Base):
    __tablename__ = "societies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    address = Column(String(512), nullable=True)
    city = Column(String(128), nullable=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    total_flats = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class AdCampaign(Base):
    __tablename__ = "ad_campaigns"

    id = Column(Integer, primary_key=True, index=True)
    vendor_name = Column(String(255), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(String(2048), nullable=True)
    image_url = Column(String(512), nullable=True)
    budget = Column(Float, default=0.0)
    status = Column(String(32), default="ACTIVE")
    created_at = Column(DateTime, default=datetime.utcnow)


class AdTarget(Base):
    __tablename__ = "ad_targets"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(
        Integer, ForeignKey("ad_campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    society_id = Column(
        Integer, ForeignKey("societies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at = Column(DateTime, default=datetime.utcnow)


# --------------------------------------------------------------------------
# Pydantic schemas
# --------------------------------------------------------------------------


class NearbySociety(BaseModel):
    id: int
    name: str
    city: Optional[str] = None
    latitude: float
    longitude: float
    total_flats: Optional[int] = 0
    distance_km: float


class AdCreate(BaseModel):
    vendor_name: str = Field(..., min_length=1, max_length=255)
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    image_url: Optional[str] = None
    budget: float = 0.0
    society_ids: List[int] = Field(..., min_length=1)


class AdResponse(BaseModel):
    id: int
    vendor_name: str
    title: str
    description: Optional[str] = None
    image_url: Optional[str] = None
    budget: float
    status: str
    targeted_society_ids: List[int]


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

app = FastAPI(title="Vendor Ad Platform", version="1.0.0")


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)


@app.get("/health")
def health():
    return {"status": "ok", "db": "sqlite" if IS_SQLITE else "postgres"}


# Haversine distance in km. Works on both Postgres and SQLite as long as the
# math functions are available; SQLite 3.35+ ships them by default.
HAVERSINE_SQL = """
    6371 * 2 * ASIN(
        SQRT(
            POWER(SIN(RADIANS(:lat - latitude) / 2), 2)
            + COS(RADIANS(:lat)) * COS(RADIANS(latitude))
            * POWER(SIN(RADIANS(:lng - longitude) / 2), 2)
        )
    )
"""


@app.get("/api/v1/societies/nearby", response_model=List[NearbySociety])
def societies_nearby(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(5.0, gt=0, le=500),
    limit: int = Query(50, gt=0, le=500),
    db: Session = Depends(get_db),
):
    sql = text(
        f"""
        SELECT id, name, city, latitude, longitude, total_flats,
               {HAVERSINE_SQL} AS distance_km
        FROM societies
        WHERE {HAVERSINE_SQL} <= :radius
        ORDER BY distance_km ASC
        LIMIT :limit
        """
    )
    rows = db.execute(
        sql, {"lat": lat, "lng": lng, "radius": radius_km, "limit": limit}
    ).mappings().all()

    return [
        NearbySociety(
            id=r["id"],
            name=r["name"],
            city=r["city"],
            latitude=r["latitude"],
            longitude=r["longitude"],
            total_flats=r["total_flats"] or 0,
            distance_km=round(float(r["distance_km"]), 3),
        )
        for r in rows
    ]


@app.post("/api/v1/ads", response_model=AdResponse, status_code=201)
def create_ad(payload: AdCreate, db: Session = Depends(get_db)):
    society_ids = sorted(set(payload.society_ids))

    found = {
        s.id for s in db.query(Society.id).filter(Society.id.in_(society_ids)).all()
    }
    missing = [sid for sid in society_ids if sid not in found]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown society ids: {missing}")

    campaign = AdCampaign(
        vendor_name=payload.vendor_name,
        title=payload.title,
        description=payload.description,
        image_url=payload.image_url,
        budget=payload.budget,
        status="ACTIVE",
    )
    db.add(campaign)
    db.flush()  # populate campaign.id before inserting targets

    db.add_all(
        [AdTarget(campaign_id=campaign.id, society_id=sid) for sid in society_ids]
    )
    db.commit()
    db.refresh(campaign)

    return AdResponse(
        id=campaign.id,
        vendor_name=campaign.vendor_name,
        title=campaign.title,
        description=campaign.description,
        image_url=campaign.image_url,
        budget=campaign.budget,
        status=campaign.status,
        targeted_society_ids=society_ids,
    )
