"""Seed realistic Bangalore societies and ad pricing for local development."""

from decimal import Decimal
from database import SessionLocal, Base, engine
from main import Society, SocietyAdPricing, AdFormat, Vendor, AdTemplate
from wallet import Wallet, WalletService

SAMPLE_SOCIETIES = [
    {
        "id": 1,
        "name": "Sobha Silicon Oasis",
        "address": "Hosa Road, HSR Extension",
        "city": "Bangalore",
        "latitude": 12.9081,
        "longitude": 77.6476,
        "total_flats": 550,
    },
    {
        "id": 2,
        "name": "Purva Vantage",
        "address": "19th Main, HSR Layout Sector 2",
        "city": "Bangalore",
        "latitude": 12.9145,
        "longitude": 77.6510,
        "total_flats": 420,
    },
    {
        "id": 3,
        "name": "Salarpuria Greenage",
        "address": "Hosur Main Road, Bommanahalli",
        "city": "Bangalore",
        "latitude": 12.9123,
        "longitude": 77.6250,
        "total_flats": 1600,
    },
    {
        "id": 4,
        "name": "Prestige St Johns Wood",
        "address": "Tavarekere, Koramangala",
        "city": "Bangalore",
        "latitude": 12.9310,
        "longitude": 77.6180,
        "total_flats": 480,
    },
    {
        "id": 5,
        "name": "Mantri Espana",
        "address": "Outer Ring Road, Bellandur",
        "city": "Bangalore",
        "latitude": 12.9260,
        "longitude": 77.6780,
        "total_flats": 850,
    },
    {
        "id": 6,
        "name": "Rohan Jharoka",
        "address": "HAL Airport Road, Yemalur",
        "city": "Bangalore",
        "latitude": 12.9420,
        "longitude": 77.6710,
        "total_flats": 600,
    },
    {
        "id": 7,
        "name": "Adarsh Palm Retreat",
        "address": "Marathahalli - Sarjapur Outer Ring Rd",
        "city": "Bangalore",
        "latitude": 12.9210,
        "longitude": 77.6850,
        "total_flats": 1200,
    },
    {
        "id": 8,
        "name": "Godrej Lake Gardens",
        "address": "Harlur Road, Off Sarjapur Road",
        "city": "Bangalore",
        "latitude": 12.9050,
        "longitude": 77.6620,
        "total_flats": 750,
    },
    {
        "id": 9,
        "name": "Brigade Millennium",
        "address": "JP Nagar 7th Phase",
        "city": "Bangalore",
        "latitude": 12.8950,
        "longitude": 77.5850,
        "total_flats": 1400,
    },
    {
        "id": 10,
        "name": "Aparna Elixir",
        "address": "Sarjapur Main Road",
        "city": "Bangalore",
        "latitude": 12.9150,
        "longitude": 77.6950,
        "total_flats": 680,
    },
]

TIERS = [
    (300, Decimal("350")),
    (600, Decimal("550")),
    (1000, Decimal("850")),
    (None, Decimal("1200")),
]

FORMAT_MULTIPLIERS = {
    AdFormat.ISLAND: Decimal("1.00"),
    AdFormat.TWO_X: Decimal("1.50"),
    AdFormat.GATE_ARCH: Decimal("1.20"),
    AdFormat.LIFT_BRANDING: Decimal("0.80"),
    AdFormat.STANDEE: Decimal("0.60"),
    AdFormat.NOTICE_BOARD: Decimal("0.50"),
}


def get_base_price(flats: int) -> Decimal:
    for upper, price in TIERS:
        if upper is None or flats < upper:
            return price
    return TIERS[-1][1]


def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        for s_data in SAMPLE_SOCIETIES:
            soc = db.query(Society).filter(Society.id == s_data["id"]).first()
            if not soc:
                soc = Society(**s_data)
                db.add(soc)
                db.flush()

            base_p = get_base_price(s_data["total_flats"])
            for fmt, mult in FORMAT_MULTIPLIERS.items():
                price = (base_p * mult).quantize(Decimal("1.00"))
                existing_p = (
                    db.query(SocietyAdPricing)
                    .filter(
                        SocietyAdPricing.society_id == soc.id,
                        SocietyAdPricing.ad_format == fmt.value,
                    )
                    .first()
                )
                if not existing_p:
                    db.add(
                        SocietyAdPricing(
                            society_id=soc.id,
                            ad_format=fmt.value,
                            price_per_day=price,
                            is_active=True,
                        )
                    )
        # Seed default development vendor v123 if not present
        dev_vendor = db.query(Vendor).filter(Vendor.id == "v123").first()
        if not dev_vendor:
            dev_vendor = Vendor(
                id="v123",
                business_name="Aithon Retailers",
                mobile_number="+919876543210",
                address_text="123 Tech Park, HSR Layout, Bangalore",
                lat=12.9121,
                lng=77.6446,
                tech_comfort_level="Moderate",
                ai_category="Retail",
                is_verified=True,
            )
            db.add(dev_vendor)
            db.flush()
            WalletService.get_or_create_wallet(db, user_id="v123")
            w = db.query(Wallet).filter(Wallet.user_id == "v123").first()
            if w:
                w.balance = 15000.0
            print("Seeded default vendor v123 with INR 15,000 wallet balance.")
        else:
            w = db.query(Wallet).filter(Wallet.user_id == "v123").first()
            if not w:
                WalletService.get_or_create_wallet(db, user_id="v123")
                w = db.query(Wallet).filter(Wallet.user_id == "v123").first()
            if w and w.balance < 5000:
                w.balance = 15000.0

        # Seed initial ad templates for v123
        dev_ad = db.query(AdTemplate).filter(AdTemplate.vendor_id == "v123").first()
        if not dev_ad:
            dev_ad = AdTemplate(
                vendor_id="v123",
                name="Diwali Sale Banner",
                goal="calls",
                format="ISLAND",
                headline="50% Off Electronics",
                description="Visit Aithon Retailers today!",
                media={
                    "url": "https://images.unsplash.com/photo-1607082348824-0a96f2a4b9da?auto=format&fit=crop&w=800&q=80",
                    "type": "image",
                },
                cta={
                    "text": "Call Now",
                    "type": "call",
                    "redirection": "tel:+919876543210",
                },
            )
            db.add(dev_ad)
            print("Seeded initial ad template for v123.")

        db.commit()
        print(f"Successfully seeded {len(SAMPLE_SOCIETIES)} societies with pricing!")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
