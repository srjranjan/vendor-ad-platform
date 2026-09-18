import os
from datetime import datetime
from enum import Enum
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Boolean,
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

    # The id from the source export, used directly as the primary key, so
    # re-importing updates rows in place and ids match the source system.
    # Not auto-generated: every society must arrive with an id.
    id = Column(String(64), primary_key=True)
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
        String(64), ForeignKey("societies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at = Column(DateTime, default=datetime.utcnow)


# --------------------------------------------------------------------------
# Pydantic schemas
# --------------------------------------------------------------------------


class NearbySociety(BaseModel):
    id: str
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
    society_ids: List[str] = Field(..., min_length=1)


class AdResponse(BaseModel):
    id: int
    vendor_name: str
    title: str
    description: Optional[str] = None
    image_url: Optional[str] = None
    budget: float
    status: str
    targeted_society_ids: List[str]


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

app = FastAPI(title="Vendor Ad Platform", version="1.0.0")

# Comma-separated list of allowed origins, e.g.
#   CORS_ORIGINS=https://app.example.com,http://localhost:5173
# Defaults to "*" so a frontend works out of the box during the hackathon.
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")
_allowed_origins = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]
_allow_all_origins = _allowed_origins == ["*"]

# Browsers reject a wildcard origin combined with credentials, so credentials
# are only enabled once explicit origins are configured.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=not _allow_all_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


# ==========================================================================
# Vendor registration
# ==========================================================================

# Dev-only: the OTP flow is simulated, no SMS provider is wired up.
MOCK_OTP = "1234"


class TechComfortLevel(str, Enum):
    BEGINNER = "Beginner"
    MODERATE = "Moderate"
    ADVANCED = "Advanced"


class VendorCategory(str, Enum):
    """Closed set of vendor categories.

    The client sends one of these values directly; mock_ai_categorize also
    returns a member, so client-supplied and AI-derived categories share one
    vocabulary and the column never holds a free-form string.
    """

    ORGANIC_GROCERIES = "Organic Groceries"
    GROCERIES = "Groceries"
    FRUITS_VEGETABLES = "Fruits & Vegetables"
    DAIRY = "Dairy"
    BAKERY = "Bakery"
    FOOD_CATERING = "Food & Catering"
    LAUNDRY = "Laundry"
    SALON_BEAUTY = "Salon & Beauty"
    HOME_SERVICES = "Home Services"
    CLEANING_SERVICES = "Cleaning Services"
    EDUCATION_TUTORING = "Education & Tutoring"
    FITNESS_WELLNESS = "Fitness & Wellness"
    PHARMACY = "Pharmacy"
    RETAIL = "Retail"
    UNCATEGORIZED = "Uncategorized"


class Vendor(Base):
    __tablename__ = "vendors"

    id = Column(Integer, primary_key=True, index=True)
    business_name = Column(String(255), nullable=False)
    mobile_number = Column(String(20), nullable=False, unique=True, index=True)
    raw_description = Column(String(2048), nullable=True)
    ai_category = Column(String(255), nullable=True)
    address_text = Column(String(512), nullable=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    tech_comfort_level = Column(String(32), nullable=True)
    is_verified = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class OTPVerification(Base):
    """Record that a mobile number passed OTP verification.

    Kept in the database rather than process memory so a restart or a second
    Railway instance cannot lose it - an in-memory set would let a redeploy
    silently drop verifications mid-signup.
    """

    __tablename__ = "otp_verifications"

    id = Column(Integer, primary_key=True, index=True)
    mobile_number = Column(String(20), nullable=False, unique=True, index=True)
    verified_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# --------------------------------------------------------------------------
# Vendor schemas
# --------------------------------------------------------------------------


class SendOTPRequest(BaseModel):
    mobile_number: str = Field(..., min_length=10, max_length=20)


class SendOTPResponse(BaseModel):
    success: bool
    message: str
    vendor_exists: bool


class VerifyOTPRequest(BaseModel):
    mobile_number: str = Field(..., min_length=10, max_length=20)
    otp: str = Field(..., min_length=4, max_length=4, pattern=r"^\d{4}$")


class VerifyOTPResponse(BaseModel):
    verified: bool


class VendorRegistrationRequest(BaseModel):
    business_name: str = Field(..., min_length=1, max_length=255)
    mobile_number: str = Field(..., min_length=10, max_length=20)
    raw_description: Optional[str] = None
    address_text: Optional[str] = None
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lng: Optional[float] = Field(None, ge=-180, le=180)
    tech_comfort_level: Optional[TechComfortLevel] = None
    # Sent by the client when the user confirms the AI's suggestion (or picks
    # their own). Omitted -> the server derives it from raw_description.
    ai_category: Optional[VendorCategory] = None


class VendorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    business_name: str
    mobile_number: str
    raw_description: Optional[str] = None
    ai_category: Optional[str] = None
    address_text: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    tech_comfort_level: Optional[str] = None
    is_verified: bool


# --------------------------------------------------------------------------
# Mock AI categorisation
# --------------------------------------------------------------------------

# Ordered longest-phrase-first so "organic vegetables" wins over "vegetables".
_CATEGORY_KEYWORDS = [
    (("organic", "natural farming"), VendorCategory.ORGANIC_GROCERIES),
    (("grocery", "groceries", "kirana", "provision"), VendorCategory.GROCERIES),
    (("vegetable", "fruit", "sabzi", "produce"), VendorCategory.FRUITS_VEGETABLES),
    (("milk", "dairy", "curd", "paneer"), VendorCategory.DAIRY),
    (("bakery", "cake", "bread", "pastry"), VendorCategory.BAKERY),
    (("tiffin", "meal", "food", "restaurant", "catering"), VendorCategory.FOOD_CATERING),
    (("laundry", "dry clean", "ironing"), VendorCategory.LAUNDRY),
    (("salon", "spa", "haircut", "beauty"), VendorCategory.SALON_BEAUTY),
    (("plumb", "electric", "carpenter", "repair", "maintenance"), VendorCategory.HOME_SERVICES),
    (("clean", "housekeeping", "pest"), VendorCategory.CLEANING_SERVICES),
    (("tutor", "coaching", "class", "academy"), VendorCategory.EDUCATION_TUTORING),
    (("gym", "fitness", "yoga", "trainer"), VendorCategory.FITNESS_WELLNESS),
    (("pharma", "medical", "medicine", "chemist"), VendorCategory.PHARMACY),
]


def mock_ai_categorize(description: str) -> VendorCategory:
    """Stand-in for the real AI categoriser.

    Deterministic keyword matching so the endpoint behaves predictably until a
    model is wired in. Swap the body out; the signature is what callers depend on.
    """
    if not description or not description.strip():
        return VendorCategory.UNCATEGORIZED

    text_lower = description.lower()
    for keywords, category in _CATEGORY_KEYWORDS:
        if any(keyword in text_lower for keyword in keywords):
            return category
    return VendorCategory.RETAIL


# --------------------------------------------------------------------------
# Vendor endpoints
# --------------------------------------------------------------------------


@app.post("/api/v1/vendors/send-otp", response_model=SendOTPResponse)
def send_otp(payload: SendOTPRequest, db: Session = Depends(get_db)):
    mobile = payload.mobile_number.strip()

    existing = db.query(Vendor).filter(Vendor.mobile_number == mobile).first()
    if existing:
        # Not an error: the frontend uses this to route to sign-in instead of
        # sign-up. Returning 200 keeps that branch simple.
        return SendOTPResponse(
            success=True,
            message=f"Vendor already registered with {mobile}. OTP sent for sign-in.",
            vendor_exists=True,
        )

    return SendOTPResponse(
        success=True,
        message=f"OTP sent to {mobile}.",
        vendor_exists=False,
    )


@app.post("/api/v1/vendors/verify-otp", response_model=VerifyOTPResponse)
def verify_otp(payload: VerifyOTPRequest, db: Session = Depends(get_db)):
    if payload.otp != MOCK_OTP:
        raise HTTPException(status_code=400, detail="Invalid OTP")

    mobile = payload.mobile_number.strip()

    # Upsert: re-verifying the same number refreshes the timestamp rather than
    # tripping the unique constraint.
    record = (
        db.query(OTPVerification)
        .filter(OTPVerification.mobile_number == mobile)
        .first()
    )
    if record:
        record.verified_at = datetime.utcnow()
    else:
        db.add(OTPVerification(mobile_number=mobile))
    db.commit()

    return VerifyOTPResponse(verified=True)


@app.post("/api/v1/vendors/register", response_model=VendorResponse, status_code=201)
def register_vendor(payload: VendorRegistrationRequest, db: Session = Depends(get_db)):
    mobile = payload.mobile_number.strip()

    verified = (
        db.query(OTPVerification)
        .filter(OTPVerification.mobile_number == mobile)
        .first()
    )
    if not verified:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Mobile number {mobile} is not verified. "
                "Call /api/v1/vendors/verify-otp before registering."
            ),
        )

    if db.query(Vendor).filter(Vendor.mobile_number == mobile).first():
        raise HTTPException(
            status_code=409,
            detail=f"A vendor is already registered with mobile number {mobile}",
        )

    vendor = Vendor(
        business_name=payload.business_name,
        mobile_number=mobile,
        raw_description=payload.raw_description,
        ai_category=(
            payload.ai_category.value
            if payload.ai_category
            else mock_ai_categorize(payload.raw_description or "").value
        ),
        address_text=payload.address_text,
        lat=payload.lat,
        lng=payload.lng,
        tech_comfort_level=(
            payload.tech_comfort_level.value if payload.tech_comfort_level else None
        ),
        is_verified=True,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    return VendorResponse.model_validate(vendor)


@app.get("/api/v1/vendors/categories", response_model=List[str])
def list_categories():
    """Allowed `ai_category` values, so the client dropdown and the server
    validation cannot drift apart."""
    return [c.value for c in VendorCategory]
