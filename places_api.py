"""Consumer (mobile app) API: nearby places, with any live ad attached.

A place is joined to a vendor through vendors.place_id, and a vendor to their
campaigns. A place is promoted when its vendor has a campaign that is ACTIVE
today. If the caller passes society_id, only campaigns that actually targeted
that society are eligible, so delivery matches what the vendor paid for.
"""

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, func, text
from sqlalchemy.orm import Session

from database import Base, get_db, ist_now, ist_today


class PlaceRecommendation(Base):
    """A consumer vouching for a place.

    app_user_id is an opaque identifier supplied by the mobile app; there is no
    consumer account model in this system, only vendors.
    """

    __tablename__ = "place_recommendations"

    id = Column(Integer, primary_key=True, index=True)
    place_id = Column(
        Integer, ForeignKey("places.id", ondelete="CASCADE"), nullable=False, index=True
    )
    app_user_id = Column(String(128), nullable=False, index=True)
    created_at = Column(DateTime, default=ist_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("place_id", "app_user_id", name="uq_place_recommendation"),
    )


# --------------------------------------------------------------------------
# Response schemas (camelCase: this contract is consumed by the mobile app)
# --------------------------------------------------------------------------


class Center(BaseModel):
    latitude: float
    longitude: float


class CampaignOut(BaseModel):
    id: int
    vendor_id: str
    name: Optional[str] = None
    goal: Optional[str] = None
    format: str
    category: Optional[str] = None
    headline: Optional[str] = None
    description: Optional[str] = None
    media: Optional[Dict[str, Any]] = None
    cta: Optional[Dict[str, Any]] = None
    created_at: datetime


class PlaceOut(BaseModel):
    id: str            # Google place id - what the app should send back
    placeId: int       # internal id, accepted by the same endpoints
    name: str
    category: Optional[str] = None
    latitude: float
    longitude: float
    address: Optional[str] = None
    phoneNumber: Optional[str] = None
    rating: Optional[float] = None
    photoUrls: List[str] = []
    distanceKm: float
    isPromoted: bool
    campaign: Optional[CampaignOut] = None
    recommendationCount: int
    isRecommendedByCurrentUser: bool


class NearbyData(BaseModel):
    locationId: str
    locationName: Optional[str] = None
    center: Center
    places: List[PlaceOut]


class NearbyResponse(BaseModel):
    status: str = "success"
    data: NearbyData


places_router = APIRouter(prefix="/api/v1/places", tags=["places"])

HAVERSINE = """
    6371 * 2 * ASIN(
        SQRT(
            POWER(SIN(RADIANS(:lat - p.latitude) / 2), 2)
            + COS(RADIANS(:lat)) * COS(RADIANS(p.latitude))
            * POWER(SIN(RADIANS(:lng - p.longitude) / 2), 2)
        )
    )
"""


@places_router.get("/nearby", response_model=NearbyResponse)
def nearby_places(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(2.0, gt=0, le=50),
    society_id: Optional[str] = Query(
        None,
        description="Resident's society. Restricts ads to campaigns that "
                    "targeted it; omit to allow any live campaign.",
    ),
    app_user_id: Optional[str] = Query(
        None, description="Opaque app user id, used for isRecommendedByCurrentUser"
    ),
    category: Optional[str] = Query(None, description="Filter by place category"),
    limit: int = Query(50, gt=0, le=200),
    location_id: Optional[str] = Query(None),
    location_name: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    from campaigns import Campaign, CampaignSociety, CampaignStatus, refresh_campaign
    from main import AdTemplate, Place, Vendor

    params = {"lat": lat, "lng": lng, "radius": radius_km, "limit": limit}
    category_clause = ""
    if category:
        params["category"] = category
        # Match either field: the client sees category_label, but existing
        # callers filter on search_category.
        category_clause = (
            "AND (LOWER(p.category_label) = LOWER(:category)"
            " OR LOWER(p.search_category) = LOWER(:category))"
        )

    rows = db.execute(
        text(
            f"""
            SELECT p.id, p.google_place_id, p.name, p.search_category,
                   p.category_label, p.latitude,
                   p.longitude, p.address, p.phone, p.rating, p.image_url,
                   {HAVERSINE} AS distance_km
            FROM places p
            WHERE {HAVERSINE} <= :radius
            {category_clause}
            ORDER BY distance_km ASC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()

    place_ids = [r["id"] for r in rows]
    if not place_ids:
        return NearbyResponse(
            data=NearbyData(
                locationId=location_id or f"loc_{lat:.4f}_{lng:.4f}",
                locationName=location_name,
                center=Center(latitude=lat, longitude=lng),
                places=[],
            )
        )

    # Recommendation counts for the whole page in one query.
    counts = dict(
        db.query(PlaceRecommendation.place_id, func.count(PlaceRecommendation.id))
        .filter(PlaceRecommendation.place_id.in_(place_ids))
        .group_by(PlaceRecommendation.place_id)
        .all()
    )
    mine = set()
    if app_user_id:
        mine = {
            pid for (pid,) in db.query(PlaceRecommendation.place_id).filter(
                PlaceRecommendation.place_id.in_(place_ids),
                PlaceRecommendation.app_user_id == app_user_id,
            ).all()
        }

    # Vendors that operate one of these places.
    vendors = {
        v.place_id: v for v in db.query(Vendor).filter(Vendor.place_id.in_(place_ids)).all()
    }

    campaign_by_place: Dict[int, Any] = {}
    if vendors:
        today = ist_today()
        candidates = db.query(Campaign).filter(
            Campaign.vendor_id.in_([v.id for v in vendors.values()]),
            Campaign.status.in_([CampaignStatus.ACTIVE.value,
                                 CampaignStatus.SCHEDULED.value]),
        ).all()
        for c in candidates:
            refresh_campaign(db, c)
        db.commit()

        eligible = [
            c for c in candidates
            if c.status == CampaignStatus.ACTIVE.value
            and c.start_date <= today <= c.end_date
        ]

        # Honour what the vendor bought: if the caller names a society, only
        # campaigns that targeted it may be shown.
        if society_id and eligible:
            targeted = {
                cid for (cid,) in db.query(CampaignSociety.campaign_id).filter(
                    CampaignSociety.campaign_id.in_([c.id for c in eligible]),
                    CampaignSociety.society_id == society_id,
                ).all()
            }
            eligible = [c for c in eligible if c.id in targeted]

        templates = {
            t.id: t for t in db.query(AdTemplate).filter(
                AdTemplate.id.in_({c.ad_template_id for c in eligible})
            ).all()
        } if eligible else {}

        vendor_place = {v.id: v.place_id for v in vendors.values()}
        # Newest campaign wins when a vendor is running several.
        for c in sorted(eligible, key=lambda c: c.created_at):
            pid = vendor_place.get(c.vendor_id)
            tpl = templates.get(c.ad_template_id)
            if pid is None or tpl is None:
                continue
            campaign_by_place[pid] = CampaignOut(
                id=c.id,
                vendor_id=c.vendor_id,
                name=c.name,
                goal=tpl.goal,
                format=c.ad_format,
                category=tpl.category.value if hasattr(tpl.category, "value") else tpl.category,
                headline=tpl.headline,
                description=tpl.description,
                media=tpl.media,
                cta=tpl.cta,
                created_at=c.created_at,
            )

    places = []
    for r in rows:
        campaign = campaign_by_place.get(r["id"])
        places.append(PlaceOut(
            id=r["google_place_id"] or str(r["id"]),
            placeId=r["id"],
            name=r["name"],
            # category_label is the display name ("Sweet shop"); search_category
            # is the term the scrape searched for ("sweet shop"). The app shows
            # the former, so fall back only when a place has no label.
            category=r["category_label"] or r["search_category"],
            latitude=r["latitude"],
            longitude=r["longitude"],
            address=r["address"],
            phoneNumber=r["phone"],
            rating=float(r["rating"]) if r["rating"] is not None else None,
            photoUrls=[r["image_url"]] if r["image_url"] else [],
            distanceKm=round(float(r["distance_km"]), 3),
            isPromoted=campaign is not None,
            campaign=campaign,
            recommendationCount=counts.get(r["id"], 0),
            isRecommendedByCurrentUser=r["id"] in mine,
        ))

    # Promoted places first, then by distance.
    places.sort(key=lambda p: (not p.isPromoted, p.distanceKm))

    return NearbyResponse(
        data=NearbyData(
            locationId=location_id or f"loc_{lat:.4f}_{lng:.4f}",
            locationName=location_name,
            center=Center(latitude=lat, longitude=lng),
            places=places,
        )
    )


class RecommendRequest(BaseModel):
    app_user_id: str = Field(..., min_length=1, max_length=128)


def _resolve_place_id(db: Session, place_ref: str) -> int:
    """Accept whichever id the app has.

    The feed exposes the Google place id, so that is what a client naturally
    sends back; the internal integer id is accepted too.
    """
    from main import Place

    row = db.query(Place.id).filter(Place.google_place_id == place_ref).first()
    if row:
        return row[0]
    if place_ref.isdigit():
        row = db.query(Place.id).filter(Place.id == int(place_ref)).first()
        if row:
            return row[0]
    raise HTTPException(status_code=404, detail=f"Unknown place {place_ref}")


@places_router.post("/{place_ref}/recommend", status_code=201)
def recommend_place(place_ref: str, payload: RecommendRequest, db: Session = Depends(get_db)):
    place_id = _resolve_place_id(db, place_ref)

    existing = db.query(PlaceRecommendation).filter(
        PlaceRecommendation.place_id == place_id,
        PlaceRecommendation.app_user_id == payload.app_user_id,
    ).first()
    if not existing:
        db.add(PlaceRecommendation(place_id=place_id, app_user_id=payload.app_user_id))
        db.commit()

    count = db.query(func.count(PlaceRecommendation.id)).filter(
        PlaceRecommendation.place_id == place_id
    ).scalar()
    return {"status": "success",
            "data": {"placeId": place_id, "recommendationCount": count,
                     "isRecommendedByCurrentUser": True}}


@places_router.delete("/{place_ref}/recommend")
def unrecommend_place(place_ref: str, app_user_id: str = Query(...), db: Session = Depends(get_db)):
    place_id = _resolve_place_id(db, place_ref)
    db.query(PlaceRecommendation).filter(
        PlaceRecommendation.place_id == place_id,
        PlaceRecommendation.app_user_id == app_user_id,
    ).delete(synchronize_session=False)
    db.commit()
    count = db.query(func.count(PlaceRecommendation.id)).filter(
        PlaceRecommendation.place_id == place_id
    ).scalar()
    return {"status": "success",
            "data": {"placeId": place_id, "recommendationCount": count,
                     "isRecommendedByCurrentUser": False}}


# --------------------------------------------------------------------------
# Place search (vendor portal picker)
# --------------------------------------------------------------------------


class PlaceSearchResult(BaseModel):
    placeId: int
    googlePlaceId: Optional[str] = None
    name: str
    category: Optional[str] = None
    address: Optional[str] = None
    latitude: float
    longitude: float
    distanceKm: Optional[float] = None
    matchScore: Optional[float] = None
    claimedByVendorId: Optional[str] = None


class PlaceSearchResponse(BaseModel):
    status: str = "success"
    query: Optional[str] = None
    total: int
    places: List[PlaceSearchResult]


@places_router.get("/search", response_model=PlaceSearchResponse)
def search_places(
    q: Optional[str] = Query(None, min_length=2, description="Match on name or address"),
    lat: Optional[float] = Query(None, ge=-90, le=90),
    lng: Optional[float] = Query(None, ge=-180, le=180),
    radius_km: Optional[float] = Query(None, gt=0, le=50),
    category: Optional[str] = Query(None),
    unclaimed_only: bool = Query(False, description="Hide places another vendor already holds"),
    limit: int = Query(20, gt=0, le=100),
    db: Session = Depends(get_db),
):
    """Find a place so a vendor can claim it.

    Ordered by distance when lat/lng are given, otherwise by name. Each result
    carries claimedByVendorId, so the portal can grey out places that are
    already taken instead of letting the claim fail with a 409.
    """
    from main import Place, Vendor

    if not q and lat is None and not category:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one of: q, category, or lat+lng",
        )
    if (lat is None) != (lng is None):
        raise HTTPException(status_code=400, detail="lat and lng must be given together")

    query = db.query(Place)
    tokens = [t for t in re.split(r"\s+", q.strip().lower()) if t] if q else []
    if tokens:
        # Require every token somewhere in name or address, rather than the
        # whole phrase as one substring: "natural ice" should still find
        # "Naturals Ice Cream".
        for token in tokens:
            like = f"%{token}%"
            query = query.filter(
                func.lower(Place.name).like(like)
                | func.lower(Place.address).like(like)
            )
    if category:
        from sqlalchemy import or_
        cat_lower = category.strip().lower()
        terms = [cat_lower]
        if 'salon' in cat_lower:
            terms.append('beauty parlour')
        if 'beauty' in cat_lower:
            terms.append('salon')
        if 'grocery' in cat_lower:
            terms.extend(['supermarket', 'general store'])
        if 'restaurant' in cat_lower:
            terms.extend(['food', 'sweets'])

        clauses = []
        for term in terms:
            clauses.append(func.lower(Place.search_category).like(f"%{term}%"))
            clauses.append(func.lower(Place.category_label).like(f"%{term}%"))
            clauses.append(func.lower(Place.name).like(f"%{term}%"))
        query = query.filter(or_(*clauses))

    rows = query.all()

    phrase = q.strip().lower() if q else ""

    def relevance(place) -> float:
        """How well a place matches the typed query.

        Name matches always beat address matches, and the earlier and more
        completely the phrase appears in the name, the higher it ranks.
        """
        if not phrase:
            return 0.0
        name = (place.name or "").lower()
        address = (place.address or "").lower()

        if name == phrase:
            score = 100.0
        elif name.startswith(phrase):
            score = 90.0
        elif re.search(rf"\b{re.escape(phrase)}", name):
            score = 80.0
        elif phrase in name:
            score = 70.0
        elif phrase in address:
            score = 40.0
        else:
            # Only individual tokens matched; rank by how many landed in the
            # name rather than merely the address.
            in_name = sum(1 for t in tokens if t in name)
            score = 30.0 + 20.0 * (in_name / len(tokens)) if tokens else 0.0

        # A shorter name containing the phrase is the more precise match:
        # "Naturals Ice Cream" beats "Naturals Signature Salon Sarjapur Road".
        if name:
            score += 5.0 * len(phrase) / len(name)
        return round(score, 3)

    def distance(place):
        if lat is None:
            return None
        from math import asin, cos, radians, sin, sqrt
        dlat = radians(lat - place.latitude)
        dlng = radians(lng - place.longitude)
        a = sin(dlat / 2) ** 2 + cos(radians(lat)) * cos(radians(place.latitude)) * sin(dlng / 2) ** 2
        return 6371 * 2 * asin(sqrt(a))

    scored = [(p, distance(p)) for p in rows]
    if radius_km is not None and lat is not None:
        scored = [(p, d) for p, d in scored if d is not None and d <= radius_km]

    claimed = {
        v.place_id: v.id
        for v in db.query(Vendor).filter(Vendor.place_id.isnot(None)).all()
    }
    if unclaimed_only:
        scored = [(p, d) for p, d in scored if p.id not in claimed]

    if phrase:
        # Best match first; distance and then name only break ties.
        scored.sort(key=lambda pair: (
            -relevance(pair[0]),
            pair[1] if pair[1] is not None else 0,
            pair[0].name,
        ))
    else:
        scored.sort(key=lambda pair: (pair[1] if pair[1] is not None else 0, pair[0].name))
    total = len(scored)

    return PlaceSearchResponse(
        query=q,
        total=total,
        places=[
            PlaceSearchResult(
                placeId=p.id,
                googlePlaceId=p.google_place_id,
                name=p.name,
                category=p.category_label or p.search_category,
                address=p.address,
                latitude=p.latitude,
                longitude=p.longitude,
                distanceKm=round(d, 3) if d is not None else None,
                matchScore=relevance(p) if phrase else None,
                claimedByVendorId=claimed.get(p.id),
            )
            for p, d in scored[:limit]
        ],
    )
