"""Consumer (mobile app) API: nearby places, with any live ad attached.

A place is joined to a vendor through vendors.place_id, and a vendor to their
campaigns. A place is promoted when its vendor has a campaign that is ACTIVE
today. If the caller passes society_id, only campaigns that actually targeted
that society are eligible, so delivery matches what the vendor paid for.
"""

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, func, text
from sqlalchemy.orm import Session

from database import Base, get_db


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
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

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
    id: str
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
        category_clause = "AND LOWER(p.search_category) = LOWER(:category)"

    rows = db.execute(
        text(
            f"""
            SELECT p.id, p.google_place_id, p.name, p.search_category, p.latitude,
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
        today = date.today()
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
            name=r["name"],
            category=r["search_category"],
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


@places_router.post("/{place_id}/recommend", status_code=201)
def recommend_place(place_id: int, payload: RecommendRequest, db: Session = Depends(get_db)):
    from main import Place

    if not db.query(Place.id).filter(Place.id == place_id).first():
        raise HTTPException(status_code=404, detail=f"Unknown place {place_id}")

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


@places_router.delete("/{place_id}/recommend")
def unrecommend_place(place_id: int, app_user_id: str = Query(...), db: Session = Depends(get_db)):
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
