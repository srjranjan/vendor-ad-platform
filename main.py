import os
import re
import secrets
from datetime import datetime
import enum
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import campaigns
from campaigns import campaign_router
import places_api
from places_api import places_router
import uploads_api
from uploads_api import uploads_router
import wallet
from wallet import (
    Wallet,
    Transaction,
    WalletException,
    wallet_exception_handler,
    WalletService,
    wallet_router,
    get_current_user,
)
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    JSON,
    Enum as SQLEnum,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

# --------------------------------------------------------------------------
# Database configuration
# --------------------------------------------------------------------------

from database import (
    Base,
    DATABASE_URL,
    IS_SQLITE,
    SessionLocal,
    engine,
    get_db,
)
# Wallet and Transaction models are registered via wallet import


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


class AdFormat(str, Enum):
    """Physical ad inventory a society can carry.

    One enum shared by ad_templates.format and society_ad_pricing.ad_format:
    a template's format is looked up verbatim against the rate card, so the
    two must never drift apart.
    """

    ISLAND = "ISLAND"
    TWO_X = "TWO_X"
    NOTICE_BOARD = "NOTICE_BOARD"
    LIFT_BRANDING = "LIFT_BRANDING"
    GATE_ARCH = "GATE_ARCH"
    STANDEE = "STANDEE"


class SocietyAdPricing(Base):
    """Per-day rate card: one row per (society, ad format).

    A society with no active row for a format cannot be targeted with that
    format, so coverage here is effectively the sellable inventory.

    price_per_day is Numeric, not Float: these are summed across hundreds of
    societies and up to 90 days, and binary float drift would stop the total
    shown at Review reconciling with what the wallet is debited.
    """

    __tablename__ = "society_ad_pricing"

    id = Column(Integer, primary_key=True, index=True)
    society_id = Column(
        String(64), ForeignKey("societies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    ad_format = Column(String(32), nullable=False, index=True)
    price_per_day = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="INR")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("society_id", "ad_format", name="uq_society_ad_format"),
    )


class Place(Base):
    """Points of interest scraped around a locality.

    Unlike Society there is no id in the source data, so the primary key is
    generated and `url` (unique per place) is the natural key the importer
    matches on.
    """

    __tablename__ = "places"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    search_category = Column(String(128), nullable=True, index=True)
    category_label = Column(String(128), nullable=True)
    phone = Column(String(32), nullable=True)
    address = Column(String(1024), nullable=True)
    rating = Column(Float, nullable=True)
    reviews = Column(Integer, nullable=True)
    website = Column(String(1024), nullable=True)
    hours = Column(String(512), nullable=True)
    image_url = Column(String(1024), nullable=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    distance_km = Column(Float, nullable=True)
    url = Column(String(512), nullable=False, unique=True, index=True)
    # Extracted from the maps url at import. Surfaced as the place id the
    # mobile app sees, so it matches what Google would return.
    google_place_id = Column(String(128), nullable=True, index=True)
    source = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CategoryEnum(str, enum.Enum):
    RETAIL = "Retail"
    REAL_ESTATE = "Real Estate"
    FOOD = "Food"


class AdTemplate(Base):
    __tablename__ = "ad_templates"

    id = Column(Integer, primary_key=True, index=True)
    vendor_id = Column(
        String(16), ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name = Column(String(255), nullable=True)
    goal = Column(String(64), nullable=False)
    # Stored as text, validated by AdFormat. A native Postgres enum would need
    # ALTER TYPE to add a format, and there is no migration tooling here.
    format = Column(String(32), nullable=False, index=True)
    category = Column(SQLEnum(CategoryEnum), nullable=True)
    headline = Column(String(255), nullable=False)
    description = Column(String(2048), nullable=True)
    media = Column(JSON, nullable=True)
    cta = Column(JSON, nullable=True)
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
    # Present only when the request names an ad_format.
    price_per_day: Optional[float] = None
    est_impressions_per_day: Optional[int] = None


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


# Creatives live in Cloudinary. A delivery url looks like
#   https://res.cloudinary.com/<cloud>/image/upload[/<transforms>]/v<ver>/<public_id>
# and every size is the same image with a different transform segment, so the
# portal uploads once and the other two sizes are derived here.
_CLOUDINARY_URL = re.compile(
    r"^(https://res\.cloudinary\.com/[^/]+/image/upload/)(.*)$"
)
# A transform segment is comma-joined tokens like "w_800,c_limit,q_auto".
_TRANSFORM_SEGMENT = re.compile(r"^[a-z]{1,3}_[^,/]+(?:,[a-z]{1,3}_[^,/]+)*$")

MEDIA_WIDTHS = {"thumbnail": 200, "small_banner": 400, "url": 800}


def cloudinary_variant(url: str, width: int) -> Optional[str]:
    """Same Cloudinary asset at a different width, or None if not Cloudinary."""
    match = _CLOUDINARY_URL.match(url or "")
    if not match:
        return None
    prefix, rest = match.groups()
    parts = rest.split("/")
    # Drop any transform the caller already applied, rather than chaining a
    # second resize on top of it.
    if parts and _TRANSFORM_SEGMENT.match(parts[0]) and not re.fullmatch(r"v\d+", parts[0]):
        parts = parts[1:]
    return f"{prefix}w_{width},c_limit,q_auto,f_auto/" + "/".join(parts)


class MediaObject(BaseModel):
    url: str
    type: str
    thumbnail: Optional[str] = None
    small_banner: Optional[str] = None

    @field_validator("url")
    @classmethod
    def _url_must_be_http(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("media.url must be an http(s) URL")
        return v

    @model_validator(mode="after")
    def _derive_sizes(self):
        """Fill the missing sizes from the uploaded Cloudinary asset.

        Non-Cloudinary urls are left exactly as given, so an external image
        still works - it just has to supply its own sizes.
        """
        if cloudinary_variant(self.url, MEDIA_WIDTHS["url"]) is None:
            return self
        if not self.thumbnail:
            self.thumbnail = cloudinary_variant(self.url, MEDIA_WIDTHS["thumbnail"])
        if not self.small_banner:
            self.small_banner = cloudinary_variant(self.url, MEDIA_WIDTHS["small_banner"])
        return self

class CTAObject(BaseModel):
    text: str
    type: str
    redirection: str

class AdTemplateCreate(BaseModel):
    vendor_id: str
    name: Optional[str] = None
    goal: str
    format: AdFormat
    category: Optional[CategoryEnum] = None
    headline: str
    description: Optional[str] = None
    media: Optional[MediaObject] = None
    cta: Optional[CTAObject] = None

class AdTemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    vendor_id: str
    name: Optional[str] = None
    goal: str
    format: AdFormat
    category: Optional[CategoryEnum] = None
    headline: str
    description: Optional[str] = None
    media: Optional[MediaObject] = None
    cta: Optional[CTAObject] = None
    created_at: datetime


class AdTemplateListResponse(BaseModel):
    status: str = "success"
    sts: int = 1
    data: List[AdTemplateResponse]



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


@app.exception_handler(WalletException)
def handle_wallet_exception(request: Request, exc: WalletException):
    return wallet_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
def handle_validation_exception(request: Request, exc: RequestValidationError):
    formatted_errors = [
        {
            "loc": [str(l) for l in e.get("loc", [])],
            "msg": str(e.get("msg", "")),
            "type": str(e.get("type", "")),
        }
        for e in exc.errors()
    ]
    if "/wallet" in request.url.path:
        first_error = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(l) for l in first_error.get("loc", []))
        msg = first_error.get("msg", "Validation error")
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": {
                    "code": "INVALID_AMOUNT" if "amount" in loc else "VALIDATION_ERROR",
                    "message": f"Validation failed at '{loc}': {msg}",
                    "details": {"errors": formatted_errors},
                },
            },
        )
    return JSONResponse(status_code=422, content={"detail": formatted_errors})


# Mount wallet endpoints
app.include_router(wallet_router, prefix="/api/wallet")
app.include_router(wallet_router, prefix="/api/v1/wallet")


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
            POWER(SIN(RADIANS(:lat - s.latitude) / 2), 2)
            + COS(RADIANS(:lat)) * COS(RADIANS(s.latitude))
            * POWER(SIN(RADIANS(:lng - s.longitude) / 2), 2)
        )
    )
"""


# Each flat is assumed to yield this many ad impressions per day.
IMPRESSIONS_PER_FLAT_PER_DAY = 2


@app.get("/api/v1/societies/nearby", response_model=List[NearbySociety])
def societies_nearby(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(5.0, gt=0, le=500),
    limit: int = Query(50, gt=0, le=500),
    ad_format: Optional[AdFormat] = Query(
        None,
        description="Restrict to societies that carry this format, and return "
                    "its price. Omit to list all nearby societies.",
    ),
    db: Session = Depends(get_db),
):
    params = {"lat": lat, "lng": lng, "radius": radius_km, "limit": limit}

    if ad_format:
        # Inner join: a society with no active price for this format is not
        # sellable, so it must not appear in the audience picker at all.
        params["ad_format"] = ad_format.value
        sql = text(
            f"""
            SELECT s.id, s.name, s.city, s.latitude, s.longitude, s.total_flats,
                   p.price_per_day AS price_per_day,
                   {HAVERSINE_SQL} AS distance_km
            FROM societies s
            JOIN society_ad_pricing p
              ON p.society_id = s.id
             AND p.ad_format = :ad_format
             AND p.is_active = TRUE
            WHERE {HAVERSINE_SQL} <= :radius
            ORDER BY distance_km ASC
            LIMIT :limit
            """
        )
    else:
        sql = text(
            f"""
            SELECT s.id, s.name, s.city, s.latitude, s.longitude, s.total_flats,
                   NULL AS price_per_day,
                   {HAVERSINE_SQL} AS distance_km
            FROM societies s
            WHERE {HAVERSINE_SQL} <= :radius
            ORDER BY distance_km ASC
            LIMIT :limit
            """
        )

    rows = db.execute(sql, params).mappings().all()

    return [
        NearbySociety(
            id=r["id"],
            name=r["name"],
            city=r["city"],
            latitude=r["latitude"],
            longitude=r["longitude"],
            total_flats=r["total_flats"] or 0,
            distance_km=round(float(r["distance_km"]), 3),
            price_per_day=float(r["price_per_day"]) if r["price_per_day"] is not None else None,
            est_impressions_per_day=(
                (r["total_flats"] or 0) * IMPRESSIONS_PER_FLAT_PER_DAY if ad_format else None
            ),
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


# Excludes 0/O/1/I/L: a vendor id shows up in support calls and invoices,
# and those glyphs get misread aloud and mistyped.
_VENDOR_ID_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
VENDOR_ID_LENGTH = 16


def generate_vendor_id() -> str:
    """A random 16-character vendor id.

    Random rather than sequential so the id leaks no signup ordering or
    customer count. The keyspace is 31**16, so collisions are not a practical
    concern, but register() still retries on the off chance.
    """
    return "".join(secrets.choice(_VENDOR_ID_ALPHABET) for _ in range(VENDOR_ID_LENGTH))


class Vendor(Base):
    __tablename__ = "vendors"

    id = Column(String(16), primary_key=True, default=generate_vendor_id)
    business_name = Column(String(255), nullable=False)
    mobile_number = Column(String(20), nullable=False, unique=True, index=True)
    raw_description = Column(String(2048), nullable=True)
    ai_category = Column(String(255), nullable=True)
    address_text = Column(String(1024), nullable=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    tech_comfort_level = Column(String(32), nullable=True)
    # The scraped place this vendor operates. One vendor per place, so an ad
    # can be attributed to exactly one business on the map.
    place_id = Column(
        Integer, ForeignKey("places.id", ondelete="SET NULL"),
        nullable=True, unique=True, index=True,
    )
    is_verified = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


def normalize_mobile(mobile: str) -> str:
    """Standardize mobile numbers to +91XXXXXXXXXX format."""
    cleaned = re.sub(r"[^\d+]", "", mobile.strip())
    if not cleaned.startswith("+"):
        if len(cleaned) == 10:
            cleaned = f"+91{cleaned}"
        elif len(cleaned) == 12 and cleaned.startswith("91"):
            cleaned = f"+{cleaned}"
    return cleaned


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
    # Links the vendor to their listing on the map, so their campaigns can be
    # attached to that place in the consumer app.
    place_id: Optional[int] = None


class VendorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    place_id: Optional[int] = None
    business_name: str
    mobile_number: str
    raw_description: Optional[str] = None
    ai_category: Optional[str] = None
    address_text: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    tech_comfort_level: Optional[str] = None
    is_verified: bool


class VendorLoginRequest(BaseModel):
    mobile_number: str = Field(..., min_length=10, max_length=20)
    otp: Optional[str] = None


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
    mobile = normalize_mobile(payload.mobile_number)

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

    mobile = normalize_mobile(payload.mobile_number)

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


def _unique_vendor_id(db: Session, attempts: int = 5) -> str:
    for _ in range(attempts):
        candidate = generate_vendor_id()
        if not db.query(Vendor.id).filter(Vendor.id == candidate).first():
            return candidate
    raise HTTPException(status_code=500, detail="Could not allocate a vendor id")


@app.post("/api/v1/vendors/register", response_model=VendorResponse, status_code=201)
def register_vendor(payload: VendorRegistrationRequest, db: Session = Depends(get_db)):
    mobile = normalize_mobile(payload.mobile_number)

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

    if payload.place_id is not None:
        place = db.query(Place).filter(Place.id == payload.place_id).first()
        if not place:
            raise HTTPException(status_code=400, detail=f"Unknown place_id {payload.place_id}")
        taken = db.query(Vendor.id).filter(Vendor.place_id == payload.place_id).first()
        if taken:
            raise HTTPException(
                status_code=409,
                detail=f"place_id {payload.place_id} is already claimed by another vendor",
            )

    # Handle exact latitude and longitude coordinates and formatted address
    lat_val = float(payload.lat) if payload.lat is not None else None
    lng_val = float(payload.lng) if payload.lng is not None else None
    addr_val = (payload.address_text or "").strip()[:1024] if payload.address_text else None

    vendor = Vendor(
        id=_unique_vendor_id(db),
        place_id=payload.place_id,
        business_name=payload.business_name.strip(),
        mobile_number=mobile,
        raw_description=payload.raw_description,
        ai_category=(
            payload.ai_category.value
            if payload.ai_category
            else mock_ai_categorize(payload.raw_description or "").value
        ),
        address_text=addr_val,
        lat=lat_val,
        lng=lng_val,
        tech_comfort_level=(
            payload.tech_comfort_level.value if payload.tech_comfort_level else None
        ),
        is_verified=True,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    # Automatically initialize vendor wallet with INR currency and 0 balance
    WalletService.get_or_create_wallet(db, user_id=str(vendor.id))

    return VendorResponse.model_validate(vendor)


@app.post("/api/v1/vendors/login", response_model=VendorResponse)
def login_vendor(payload: VendorLoginRequest, db: Session = Depends(get_db)):
    mobile = normalize_mobile(payload.mobile_number)
    vendor = db.query(Vendor).filter(Vendor.mobile_number == mobile).first()
    if not vendor:
        raise HTTPException(
            status_code=404,
            detail=f"No account found with mobile number {mobile}. Please sign up first.",
        )
    if payload.otp and payload.otp != MOCK_OTP:
        raise HTTPException(status_code=400, detail="Invalid OTP")
    return VendorResponse.model_validate(vendor)


@app.get("/api/v1/vendors/me", response_model=VendorResponse)
def get_current_vendor_profile(
    user_id: str = Depends(get_current_user), db: Session = Depends(get_db)
):
    vendor = db.query(Vendor).filter(Vendor.id == user_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return VendorResponse.model_validate(vendor)


@app.get("/api/v1/vendors/categories", response_model=List[str])
def list_categories():
    """Allowed `ai_category` values, so the client dropdown and the server
    validation cannot drift apart."""
    return [c.value for c in VendorCategory]


# --------------------------------------------------------------------------
# Ad template endpoints
# --------------------------------------------------------------------------


@app.post("/api/v1/ad-templates", response_model=AdTemplateResponse, status_code=201)
def create_ad_template(payload: AdTemplateCreate, db: Session = Depends(get_db)):
    # SQLite does not enforce foreign keys unless switched on per connection,
    # so check explicitly and return 400 rather than relying on the database.
    if not db.query(Vendor.id).filter(Vendor.id == payload.vendor_id).first():
        raise HTTPException(
            status_code=400, detail=f"Unknown vendor_id {payload.vendor_id}"
        )

    template = AdTemplate(
        vendor_id=payload.vendor_id,
        name=payload.name or payload.headline,
        goal=payload.goal,
        # .value so the column holds "ISLAND", not "AdFormat.ISLAND".
        format=payload.format.value,
        category=payload.category,
        headline=payload.headline,
        description=payload.description,
        media=payload.media.model_dump() if payload.media else None,
        cta=payload.cta.model_dump() if payload.cta else None,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


@app.get("/api/v1/ad-templates", response_model=AdTemplateListResponse)
def list_ad_templates(vendor_id: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(AdTemplate)
    if vendor_id:
        query = query.filter(AdTemplate.vendor_id == vendor_id)
    templates = query.all()
    return AdTemplateListResponse(
        status="success",
        sts=1,
        data=templates,
    )


# ==========================================================================
# Campaign audience selection (Launch Campaign - step 2)
# ==========================================================================


class TargetingMode(str, Enum):
    RADIUS = "RADIUS"
    ENTIRE_CITY = "ENTIRE_CITY"
    ENTIRE_STATE = "ENTIRE_STATE"
    MULTI_STATE = "MULTI_STATE"
    PAN_INDIA = "PAN_INDIA"


class TargetSociety(BaseModel):
    id: str
    name: str
    city: Optional[str] = None
    latitude: float
    longitude: float
    flat_count: int
    distance_km: Optional[float] = None
    price_per_day: float
    est_impressions_per_day: int


class AudienceCenter(BaseModel):
    latitude: float
    longitude: float
    source: str = "vendor_business_location"


class AudienceSummary(BaseModel):
    """Totals over every matching society, not just the returned page."""

    society_count: int
    total_flats: int
    est_impressions_per_day: int
    total_price_per_day: float


class NearbySocietiesResponse(BaseModel):
    ad_format: AdFormat
    targeting_mode: TargetingMode
    radius_km: Optional[float] = None
    center: Optional[AudienceCenter] = None
    summary: AudienceSummary
    societies: List[TargetSociety]
    limit: int
    offset: int


@app.get("/api/v1/campaigns/nearby-societies", response_model=NearbySocietiesResponse)
def campaign_nearby_societies(
    vendor_id: str = Query(..., description="Vendor launching the campaign"),
    ad_template_id: int = Query(..., description="Template being launched; its format sets the rate card"),
    targeting: TargetingMode = Query(TargetingMode.RADIUS),
    radius_km: float = Query(5.0, ge=1, le=30, description="Only used when targeting=RADIUS"),
    limit: int = Query(50, gt=0, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Societies a vendor can target for a given template.

    The centre is the vendor's own business location and the format comes from
    the template, so the caller supplies neither. Societies with no active
    price for that format are not sellable and are left out entirely.
    """
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail=f"Unknown vendor_id {vendor_id}")

    template = db.query(AdTemplate).filter(AdTemplate.id == ad_template_id).first()
    if not template:
        raise HTTPException(
            status_code=404, detail=f"Unknown ad_template_id {ad_template_id}"
        )
    if template.vendor_id != vendor_id:
        raise HTTPException(
            status_code=403,
            detail="That ad template belongs to a different vendor",
        )

    if targeting in (
        TargetingMode.ENTIRE_STATE,
        TargetingMode.MULTI_STATE,
        TargetingMode.PAN_INDIA,
    ):
        # societies carries no state or region column, so these tiers cannot be
        # resolved. Refusing beats silently returning the whole catalogue and
        # quoting a price for it.
        raise HTTPException(
            status_code=501,
            detail=(
                f"{targeting.value} targeting is not available: societies have no "
                "state or region data. Use RADIUS or ENTIRE_CITY."
            ),
        )

    params = {"ad_format": template.format, "limit": limit, "offset": offset}
    center = None

    if targeting == TargetingMode.RADIUS:
        if vendor.lat is None or vendor.lng is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Vendor has no business location set, so radius targeting has "
                    "no centre. Set lat/lng on the vendor, or use ENTIRE_CITY."
                ),
            )
        center = AudienceCenter(latitude=vendor.lat, longitude=vendor.lng)
        params.update({"lat": vendor.lat, "lng": vendor.lng, "radius": radius_km})
        where = f"{HAVERSINE_SQL} <= :radius"
        distance_select = f"{HAVERSINE_SQL} AS distance_km"
        order_by = "distance_km ASC"
    else:
        # Entire city: the vendor has no city column, so use the city of the
        # society nearest their business location.
        city = None
        if vendor.lat is not None and vendor.lng is not None:
            city = db.execute(
                text(
                    f"SELECT s.city FROM societies s "
                    f"WHERE s.city IS NOT NULL ORDER BY {HAVERSINE_SQL} ASC LIMIT 1"
                ),
                {"lat": vendor.lat, "lng": vendor.lng},
            ).scalar()
        if not city:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Could not determine the vendor's city: no business location "
                    "is set on the vendor."
                ),
            )
        params["city"] = city
        where = "LOWER(s.city) = LOWER(:city)"
        distance_select = "NULL AS distance_km"
        order_by = "s.total_flats DESC"

    join = """
        FROM societies s
        JOIN society_ad_pricing p
          ON p.society_id = s.id
         AND p.ad_format = :ad_format
         AND p.is_active = TRUE
    """

    summary = db.execute(
        text(
            f"""
            SELECT COUNT(*) AS society_count,
                   COALESCE(SUM(s.total_flats), 0) AS total_flats,
                   COALESCE(SUM(p.price_per_day), 0) AS total_price_per_day
            {join}
            WHERE {where}
            """
        ),
        params,
    ).mappings().one()

    rows = db.execute(
        text(
            f"""
            SELECT s.id, s.name, s.city, s.latitude, s.longitude, s.total_flats,
                   p.price_per_day, {distance_select}
            {join}
            WHERE {where}
            ORDER BY {order_by}
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()

    total_flats = int(summary["total_flats"] or 0)
    return NearbySocietiesResponse(
        ad_format=AdFormat(template.format),
        targeting_mode=targeting,
        radius_km=radius_km if targeting == TargetingMode.RADIUS else None,
        center=center,
        summary=AudienceSummary(
            society_count=summary["society_count"],
            total_flats=total_flats,
            est_impressions_per_day=total_flats * IMPRESSIONS_PER_FLAT_PER_DAY,
            total_price_per_day=float(summary["total_price_per_day"] or 0),
        ),
        societies=[
            TargetSociety(
                id=str(r["id"]),
                name=r["name"],
                city=r["city"],
                latitude=r["latitude"],
                longitude=r["longitude"],
                flat_count=r["total_flats"] or 0,
                distance_km=round(float(r["distance_km"]), 2) if r["distance_km"] is not None else None,
                price_per_day=float(r["price_per_day"]),
                est_impressions_per_day=(r["total_flats"] or 0) * IMPRESSIONS_PER_FLAT_PER_DAY,
            )
            for r in rows
        ],
        limit=limit,
        offset=offset,
    )


# Registered last: campaign_router declares GET /{campaign_id}, which would
# otherwise shadow the static /api/v1/campaigns/nearby-societies path above.
app.include_router(campaign_router)
app.include_router(places_router)
app.include_router(uploads_router)


# --------------------------------------------------------------------------
# Vendor <-> place mapping
# --------------------------------------------------------------------------


class VendorPlaceRequest(BaseModel):
    place_id: int


class VendorPlaceResponse(BaseModel):
    vendor_id: str
    business_name: str
    place_id: Optional[int] = None
    place_name: Optional[str] = None
    place_address: Optional[str] = None
    place_category: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


def _vendor_place_response(vendor: Vendor, place: Optional[Place]) -> VendorPlaceResponse:
    return VendorPlaceResponse(
        vendor_id=vendor.id,
        business_name=vendor.business_name,
        place_id=vendor.place_id,
        place_name=place.name if place else None,
        place_address=place.address if place else None,
        place_category=place.category_label if place else None,
        latitude=place.latitude if place else None,
        longitude=place.longitude if place else None,
    )


@app.get("/api/v1/vendors/{vendor_id}/place", response_model=VendorPlaceResponse)
def get_vendor_place(vendor_id: str, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail=f"Unknown vendor_id {vendor_id}")
    place = db.query(Place).filter(Place.id == vendor.place_id).first() if vendor.place_id else None
    return _vendor_place_response(vendor, place)


@app.put("/api/v1/vendors/{vendor_id}/place", response_model=VendorPlaceResponse)
def set_vendor_place(
    vendor_id: str, payload: VendorPlaceRequest, db: Session = Depends(get_db)
):
    """Point a vendor at the place they operate from.

    Registration can set this, but a vendor who signed up without one would
    otherwise have no way to claim their listing later. Re-pointing an already
    linked vendor is allowed; taking a place from another vendor is not.
    """
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail=f"Unknown vendor_id {vendor_id}")

    place = db.query(Place).filter(Place.id == payload.place_id).first()
    if not place:
        raise HTTPException(status_code=400, detail=f"Unknown place_id {payload.place_id}")

    holder = db.query(Vendor).filter(
        Vendor.place_id == payload.place_id, Vendor.id != vendor_id
    ).first()
    if holder:
        raise HTTPException(
            status_code=409,
            detail=f"place_id {payload.place_id} is already claimed by vendor {holder.id}",
        )

    vendor.place_id = payload.place_id
    db.commit()
    db.refresh(vendor)
    return _vendor_place_response(vendor, place)


@app.delete("/api/v1/vendors/{vendor_id}/place", response_model=VendorPlaceResponse)
def clear_vendor_place(vendor_id: str, db: Session = Depends(get_db)):
    """Unlink a vendor from their place. Their ads stop appearing on the map."""
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail=f"Unknown vendor_id {vendor_id}")
    vendor.place_id = None
    db.commit()
    db.refresh(vendor)
    return _vendor_place_response(vendor, None)
