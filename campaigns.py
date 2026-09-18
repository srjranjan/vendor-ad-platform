"""Campaign launch and lifecycle.

Campaigns go live on creation - there is no approval step. A campaign runs for
`duration_days` of ACTIVE time; pausing stops the clock and resuming pushes
end_date out by however long it was paused, so a vendor always gets the number
of days they paid for.

There is no scheduler in this deployment, so state is evaluated lazily: every
read and write calls refresh_campaign(), which accrues spend for elapsed active
days and applies SCHEDULED -> ACTIVE and ACTIVE -> EXPIRED. That keeps the data
correct without a cron job, at the cost of a campaign only changing state when
something touches it.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Column, Date, DateTime, ForeignKey, Integer, Numeric, String,
    UniqueConstraint, func, text,
)
from sqlalchemy.orm import Session, relationship

from database import Base, get_db

MAX_DURATION_DAYS = 90


class CampaignStatus(str, Enum):
    SCHEDULED = "SCHEDULED"   # start date is in the future
    ACTIVE = "ACTIVE"         # running, spend accruing
    PAUSED = "PAUSED"         # vendor stopped it; clock frozen
    EXPIRED = "EXPIRED"       # ran its full duration
    CANCELLED = "CANCELLED"   # stopped early; unspent budget refunded


# Terminal states never transition again.
TERMINAL = {CampaignStatus.EXPIRED, CampaignStatus.CANCELLED}

ALLOWED_TRANSITIONS = {
    CampaignStatus.SCHEDULED: {CampaignStatus.ACTIVE, CampaignStatus.CANCELLED},
    CampaignStatus.ACTIVE: {CampaignStatus.PAUSED, CampaignStatus.EXPIRED,
                            CampaignStatus.CANCELLED},
    CampaignStatus.PAUSED: {CampaignStatus.ACTIVE, CampaignStatus.EXPIRED,
                            CampaignStatus.CANCELLED},
    CampaignStatus.EXPIRED: set(),
    CampaignStatus.CANCELLED: set(),
}


class Campaign(Base):
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True, index=True)
    vendor_id = Column(
        String(16), ForeignKey("vendors.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    ad_template_id = Column(
        Integer, ForeignKey("ad_templates.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    name = Column(String(255), nullable=True)
    # Snapshot of the template's format: the rate card it was priced against.
    ad_format = Column(String(32), nullable=False)

    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    duration_days = Column(Integer, nullable=False)
    days_consumed = Column(Integer, nullable=False, default=0)
    last_accrued_on = Column(Date, nullable=True)

    daily_cost = Column(Numeric(12, 2), nullable=False)
    total_cost = Column(Numeric(12, 2), nullable=False)
    amount_spent = Column(Numeric(12, 2), nullable=False, default=0)
    amount_refunded = Column(Numeric(12, 2), nullable=False, default=0)

    status = Column(String(16), nullable=False, default=CampaignStatus.ACTIVE.value,
                    index=True)
    debit_transaction_id = Column(String(64), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    activated_at = Column(DateTime, nullable=True)
    paused_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)

    societies = relationship(
        "CampaignSociety", back_populates="campaign", cascade="all, delete-orphan"
    )


class CampaignSociety(Base):
    """A society targeted by a campaign, with the price frozen at launch.

    The rate card can change afterwards; what the vendor agreed to must not.
    """

    __tablename__ = "campaign_societies"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(
        Integer, ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    society_id = Column(
        String(64), ForeignKey("societies.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    price_per_day = Column(Numeric(10, 2), nullable=False)
    flat_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    campaign = relationship("Campaign", back_populates="societies")

    __table_args__ = (
        UniqueConstraint("campaign_id", "society_id", name="uq_campaign_society"),
    )


# --------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------


class CampaignStateError(HTTPException):
    def __init__(self, current: str, target: str):
        super().__init__(
            status_code=409,
            detail=f"Cannot move a campaign from {current} to {target}",
        )


def assert_transition(current: str, target: CampaignStatus) -> None:
    allowed = ALLOWED_TRANSITIONS.get(CampaignStatus(current), set())
    if target not in allowed:
        raise CampaignStateError(current, target.value)


def refresh_campaign(db: Session, campaign: Campaign, today: Optional[date] = None) -> Campaign:
    """Bring a campaign up to date: accrue spend and apply date-driven moves.

    Called on every read and write instead of a scheduled job.
    """
    today = today or date.today()
    status = CampaignStatus(campaign.status)

    if status in TERMINAL:
        return campaign

    # A scheduled campaign becomes active once its start date arrives.
    if status == CampaignStatus.SCHEDULED and today >= campaign.start_date:
        campaign.status = CampaignStatus.ACTIVE.value
        campaign.activated_at = datetime.utcnow()
        campaign.last_accrued_on = campaign.start_date - timedelta(days=1)
        status = CampaignStatus.ACTIVE

    # Spend only accrues while active.
    if status == CampaignStatus.ACTIVE:
        last = campaign.last_accrued_on or (campaign.start_date - timedelta(days=1))
        elapsed = (min(today, campaign.end_date) - last).days
        if elapsed > 0:
            remaining = campaign.duration_days - campaign.days_consumed
            days = max(0, min(elapsed, remaining))
            if days:
                campaign.days_consumed += days
                campaign.amount_spent = (
                    Decimal(campaign.days_consumed) * Decimal(campaign.daily_cost)
                )
            campaign.last_accrued_on = min(today, campaign.end_date)

        if campaign.days_consumed >= campaign.duration_days or today > campaign.end_date:
            campaign.status = CampaignStatus.EXPIRED.value
            campaign.ended_at = datetime.utcnow()

    return campaign


def unspent(campaign: Campaign) -> Decimal:
    spent = Decimal(campaign.amount_spent or 0)
    refunded = Decimal(campaign.amount_refunded or 0)
    return max(Decimal("0"), Decimal(campaign.total_cost) - spent - refunded)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


class CampaignCreate(BaseModel):
    vendor_id: str = Field(..., min_length=1, max_length=16)
    ad_template_id: int
    society_ids: List[str] = Field(..., min_length=1)
    duration_days: int = Field(..., ge=1, le=MAX_DURATION_DAYS)
    # Omit to start today ("Start Immediately"); a future date schedules it.
    start_date: Optional[date] = None
    name: Optional[str] = None


class CampaignSocietyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    society_id: str
    price_per_day: float
    flat_count: int


class CampaignOut(BaseModel):
    id: int
    vendor_id: str
    ad_template_id: int
    name: Optional[str] = None
    ad_format: str
    status: CampaignStatus
    start_date: date
    end_date: date
    duration_days: int
    days_consumed: int
    daily_cost: float
    total_cost: float
    amount_spent: float
    amount_refunded: float
    society_count: int
    est_impressions_per_day: int
    societies: List[CampaignSocietyOut] = []


IMPRESSIONS_PER_FLAT_PER_DAY = 2


def to_out(campaign: Campaign, include_societies: bool = True) -> CampaignOut:
    flats = sum(s.flat_count for s in campaign.societies)
    return CampaignOut(
        id=campaign.id,
        vendor_id=campaign.vendor_id,
        ad_template_id=campaign.ad_template_id,
        name=campaign.name,
        ad_format=campaign.ad_format,
        status=CampaignStatus(campaign.status),
        start_date=campaign.start_date,
        end_date=campaign.end_date,
        duration_days=campaign.duration_days,
        days_consumed=campaign.days_consumed,
        daily_cost=float(campaign.daily_cost),
        total_cost=float(campaign.total_cost),
        amount_spent=float(campaign.amount_spent or 0),
        amount_refunded=float(campaign.amount_refunded or 0),
        society_count=len(campaign.societies),
        est_impressions_per_day=flats * IMPRESSIONS_PER_FLAT_PER_DAY,
        societies=[CampaignSocietyOut.model_validate(s) for s in campaign.societies]
        if include_societies else [],
    )


campaign_router = APIRouter(prefix="/api/v1/campaigns", tags=["campaigns"])


@campaign_router.post("", response_model=CampaignOut, status_code=201)
def launch_campaign(payload: CampaignCreate, db: Session = Depends(get_db)):
    """Launch a campaign. It goes live immediately, or on its start date."""
    # Imported here rather than at module scope: main imports this module.
    from main import AdTemplate, Society, SocietyAdPricing, Vendor
    from wallet import DebitRequest, WalletService

    vendor = db.query(Vendor).filter(Vendor.id == payload.vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail=f"Unknown vendor_id {payload.vendor_id}")

    template = db.query(AdTemplate).filter(AdTemplate.id == payload.ad_template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail=f"Unknown ad_template_id {payload.ad_template_id}")
    if template.vendor_id != payload.vendor_id:
        raise HTTPException(status_code=403, detail="That ad template belongs to a different vendor")

    society_ids = sorted(set(payload.society_ids))

    # Price against the template's format, never a client-supplied one.
    priced = db.query(
        SocietyAdPricing.society_id, SocietyAdPricing.price_per_day, Society.total_flats
    ).join(Society, Society.id == SocietyAdPricing.society_id).filter(
        SocietyAdPricing.society_id.in_(society_ids),
        SocietyAdPricing.ad_format == template.format,
        SocietyAdPricing.is_active.is_(True),
    ).all()

    found = {row[0] for row in priced}
    missing = [sid for sid in society_ids if sid not in found]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(missing)} society(s) are not available for format "
                f"{template.format}: {missing[:5]}"
            ),
        )

    daily_cost = sum((Decimal(str(p)) for _, p, _ in priced), Decimal("0"))
    total_cost = daily_cost * Decimal(payload.duration_days)

    start = payload.start_date or date.today()
    if start < date.today():
        raise HTTPException(status_code=400, detail="start_date cannot be in the past")
    end = start + timedelta(days=payload.duration_days - 1)

    campaign = Campaign(
        vendor_id=payload.vendor_id,
        ad_template_id=template.id,
        name=payload.name or template.name or template.headline,
        ad_format=template.format,
        start_date=start,
        end_date=end,
        duration_days=payload.duration_days,
        days_consumed=0,
        daily_cost=daily_cost,
        total_cost=total_cost,
        amount_spent=Decimal("0"),
        amount_refunded=Decimal("0"),
        status=(CampaignStatus.ACTIVE if start <= date.today()
                else CampaignStatus.SCHEDULED).value,
        activated_at=datetime.utcnow() if start <= date.today() else None,
        last_accrued_on=start - timedelta(days=1),
    )
    db.add(campaign)
    db.flush()

    for sid, price, flats in priced:
        db.add(CampaignSociety(
            campaign_id=campaign.id, society_id=sid,
            price_per_day=price, flat_count=flats or 0,
        ))

    # Debit the whole budget up front so it cannot be spent twice across
    # campaigns. Anything unused comes back on cancel.
    result = WalletService.debit_wallet(
        db,
        user_id=payload.vendor_id,
        payload=DebitRequest(
            amount=float(total_cost),
            description=f"Campaign launch: {campaign.name}",
            reference_id=f"campaign:{campaign.id}",
        ),
        idempotency_key=f"campaign-launch-{campaign.id}",
    )
    campaign.debit_transaction_id = result.data.transaction.transaction_id

    db.commit()
    db.refresh(campaign)
    return to_out(campaign)


def _load(db: Session, campaign_id: int, vendor_id: Optional[str] = None) -> Campaign:
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail=f"Unknown campaign {campaign_id}")
    if vendor_id and campaign.vendor_id != vendor_id:
        raise HTTPException(status_code=403, detail="That campaign belongs to a different vendor")
    refresh_campaign(db, campaign)
    return campaign


class CampaignListItem(BaseModel):
    id: int
    name: Optional[str] = None
    status: CampaignStatus
    ad_template_id: int
    ad_headline: Optional[str] = None
    ad_goal: Optional[str] = None
    ad_format: str
    start_date: date
    end_date: date
    duration_days: int
    days_consumed: int
    days_remaining: int
    progress_pct: int
    society_count: int
    est_impressions_per_day: int
    daily_cost: float
    total_cost: float
    amount_spent: float
    amount_refunded: float


class DashboardSummary(BaseModel):
    total_campaigns: int
    by_status: dict
    live_campaigns: int
    live_daily_cost: float
    total_spent: float
    total_refunded: float
    committed_remaining: float
    wallet_balance: float


class CampaignDashboard(BaseModel):
    vendor_id: str
    summary: DashboardSummary
    campaigns: List[CampaignListItem]
    total: int
    limit: int
    offset: int


@campaign_router.get("", response_model=CampaignDashboard)
def list_campaigns(
    vendor_id: str = Query(..., description="Vendor whose dashboard this is"),
    status: Optional[CampaignStatus] = Query(None, description="Filter to one state"),
    limit: int = Query(20, gt=0, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Campaigns for a vendor's dashboard, with portfolio totals.

    Every campaign is refreshed first so a campaign that has silently expired
    is counted under the state it is actually in. The summary covers the whole
    portfolio rather than the returned page.
    """
    from main import AdTemplate, Vendor
    from wallet import Wallet

    if not db.query(Vendor.id).filter(Vendor.id == vendor_id).first():
        raise HTTPException(status_code=404, detail=f"Unknown vendor_id {vendor_id}")

    all_campaigns = db.query(Campaign).filter(Campaign.vendor_id == vendor_id).all()
    for c in all_campaigns:
        refresh_campaign(db, c)
    db.commit()

    by_status = {s.value: 0 for s in CampaignStatus}
    total_spent = Decimal("0")
    total_refunded = Decimal("0")
    committed = Decimal("0")
    live_daily = Decimal("0")
    for c in all_campaigns:
        by_status[c.status] = by_status.get(c.status, 0) + 1
        total_spent += Decimal(c.amount_spent or 0)
        total_refunded += Decimal(c.amount_refunded or 0)
        if c.status in (CampaignStatus.ACTIVE.value, CampaignStatus.SCHEDULED.value,
                        CampaignStatus.PAUSED.value):
            committed += unspent(c)
        if c.status == CampaignStatus.ACTIVE.value:
            live_daily += Decimal(c.daily_cost)

    selected = [c for c in all_campaigns
                if status is None or c.status == status.value]
    selected.sort(key=lambda c: c.created_at, reverse=True)
    page = selected[offset:offset + limit]

    # One query for the whole page instead of touching .societies per row.
    ids = [c.id for c in page]
    agg = {}
    if ids:
        for cid, count, flats in db.query(
            CampaignSociety.campaign_id,
            func.count(CampaignSociety.id),
            func.coalesce(func.sum(CampaignSociety.flat_count), 0),
        ).filter(CampaignSociety.campaign_id.in_(ids)).group_by(
            CampaignSociety.campaign_id
        ).all():
            agg[cid] = (count, int(flats or 0))

    templates = {}
    tids = {c.ad_template_id for c in page}
    if tids:
        templates = {
            t.id: t for t in db.query(AdTemplate).filter(AdTemplate.id.in_(tids)).all()
        }

    wallet = db.query(Wallet).filter(Wallet.user_id == vendor_id).first()

    items = []
    for c in page:
        count, flats = agg.get(c.id, (0, 0))
        tpl = templates.get(c.ad_template_id)
        remaining = max(0, c.duration_days - c.days_consumed)
        items.append(CampaignListItem(
            id=c.id,
            name=c.name,
            status=CampaignStatus(c.status),
            ad_template_id=c.ad_template_id,
            ad_headline=tpl.headline if tpl else None,
            ad_goal=tpl.goal if tpl else None,
            ad_format=c.ad_format,
            start_date=c.start_date,
            end_date=c.end_date,
            duration_days=c.duration_days,
            days_consumed=c.days_consumed,
            days_remaining=remaining,
            progress_pct=int(round(100 * c.days_consumed / c.duration_days))
            if c.duration_days else 0,
            society_count=count,
            est_impressions_per_day=flats * IMPRESSIONS_PER_FLAT_PER_DAY,
            daily_cost=float(c.daily_cost),
            total_cost=float(c.total_cost),
            amount_spent=float(c.amount_spent or 0),
            amount_refunded=float(c.amount_refunded or 0),
        ))

    return CampaignDashboard(
        vendor_id=vendor_id,
        summary=DashboardSummary(
            total_campaigns=len(all_campaigns),
            by_status=by_status,
            live_campaigns=by_status.get(CampaignStatus.ACTIVE.value, 0),
            live_daily_cost=float(live_daily),
            total_spent=float(total_spent),
            total_refunded=float(total_refunded),
            committed_remaining=float(committed),
            wallet_balance=float(wallet.balance) if wallet else 0.0,
        ),
        campaigns=items,
        total=len(selected),
        limit=limit,
        offset=offset,
    )


@campaign_router.get("/{campaign_id}", response_model=CampaignOut)
def get_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = _load(db, campaign_id)
    db.commit()
    return to_out(campaign)


@campaign_router.post("/{campaign_id}/pause", response_model=CampaignOut)
def pause_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = _load(db, campaign_id)
    assert_transition(campaign.status, CampaignStatus.PAUSED)
    campaign.status = CampaignStatus.PAUSED.value
    campaign.paused_at = datetime.utcnow()
    db.commit()
    db.refresh(campaign)
    return to_out(campaign)


@campaign_router.post("/{campaign_id}/resume", response_model=CampaignOut)
def resume_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = _load(db, campaign_id)
    assert_transition(campaign.status, CampaignStatus.ACTIVE)

    # Push end_date out by however long it was paused: the vendor paid for a
    # number of days, not a window on the calendar.
    if campaign.paused_at:
        paused_days = (date.today() - campaign.paused_at.date()).days
        if paused_days > 0:
            campaign.end_date = campaign.end_date + timedelta(days=paused_days)
    campaign.status = CampaignStatus.ACTIVE.value
    campaign.paused_at = None
    # Never move the cursor backwards: today may already have been accrued
    # before the pause, and resuming the same day must not charge for it twice.
    campaign.last_accrued_on = max(
        campaign.last_accrued_on or (campaign.start_date - timedelta(days=1)),
        date.today() - timedelta(days=1),
    )
    refresh_campaign(db, campaign)
    db.commit()
    db.refresh(campaign)
    return to_out(campaign)


@campaign_router.post("/{campaign_id}/cancel", response_model=CampaignOut)
def cancel_campaign(campaign_id: int, db: Session = Depends(get_db)):
    """Stop a campaign early and refund whatever was not consumed."""
    from wallet import CreditRequest, WalletService

    campaign = _load(db, campaign_id)
    assert_transition(campaign.status, CampaignStatus.CANCELLED)

    refund = unspent(campaign)
    if refund > 0:
        WalletService.credit_wallet(
            db,
            user_id=campaign.vendor_id,
            payload=CreditRequest(
                amount=float(refund),
                description=f"Refund for cancelled campaign {campaign.id}",
                reference_id=f"campaign:{campaign.id}:refund",
            ),
            idempotency_key=f"campaign-cancel-{campaign.id}",
        )
        campaign.amount_refunded = Decimal(campaign.amount_refunded or 0) + refund

    campaign.status = CampaignStatus.CANCELLED.value
    campaign.ended_at = datetime.utcnow()
    db.commit()
    db.refresh(campaign)
    return to_out(campaign)
