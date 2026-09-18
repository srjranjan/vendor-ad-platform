# Mobile App API

Consumer-facing endpoints for the HoodPicks mobile app: nearby places, with any
live vendor ad attached, plus recommendations.

**Base URL**

```
https://vendor-ad-platform-production.up.railway.app
```

Interactive reference: [`/docs`](https://vendor-ad-platform-production.up.railway.app/docs)
· OpenAPI spec: `/openapi.json`

No authentication. All endpoints return JSON. CORS is open to any origin.

---

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/places/nearby` | Places around a coordinate, with ads |
| `POST` | `/api/v1/places/{placeRef}/recommend` | Recommend a place |
| `DELETE` | `/api/v1/places/{placeRef}/recommend` | Undo a recommendation |
| `GET` | `/health` | Service check |

---

## 1. Nearby places

```
GET /api/v1/places/nearby
```

### Query parameters

| Param | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| `lat` | float | **yes** | – | −90 … 90 |
| `lng` | float | **yes** | – | −180 … 180 |
| `radius_km` | float | no | `2` | 0 < r ≤ 50 |
| `society_id` | string | no | – | Resident's society. Gates which ads appear — see [Ad targeting](#ad-targeting). |
| `app_user_id` | string | no | – | Opaque app user id. Drives `isRecommendedByCurrentUser`. |
| `category` | string | no | – | Exact place category, case-insensitive (`cafe`, `gym`, `bakery`, …) |
| `limit` | int | no | `50` | 1 … 200 |
| `location_id` | string | no | derived | Echoed as `data.locationId`. Defaults to `loc_<lat>_<lng>`. |
| `location_name` | string | no | `null` | Echoed as `data.locationName`. The server does no reverse geocoding — pass the label you want shown. |

### Example

```bash
curl "https://vendor-ad-platform-production.up.railway.app/api/v1/places/nearby\
?lat=12.9178893&lng=77.6804043&radius_km=1&limit=3\
&app_user_id=user_42&society_id=8a9699849cf93832019cfb15c7e76c4d\
&location_name=Bellandur%2C%20BLR"
```

### Response

```json
{
  "status": "success",
  "data": {
    "locationId": "loc_12.9179_77.6804",
    "locationName": "Bellandur, BLR",
    "center": { "latitude": 12.9178893, "longitude": 77.6804043 },
    "places": [
      {
        "id": "0x3bae1370c9de531d:0xdc88a20e35a6b3f3",
        "placeId": 57,
        "name": "Mind Blowing Cafe",
        "category": "cafe",
        "latitude": 12.9178893,
        "longitude": 77.6804043,
        "address": "24, Bellandur, Bengaluru, Karnataka 560103",
        "phoneNumber": null,
        "rating": null,
        "photoUrls": ["https://lh3.googleusercontent.com/gps-cs-s/AHRPTW..."],
        "distanceKm": 0.0,
        "isPromoted": true,
        "campaign": {
          "id": 6,
          "vendor_id": "QM2JKZHCANRRJ74K",
          "name": "Weekend Brew & Dine Perk",
          "goal": "visits",
          "format": "HOOD_PICKS_BANNER",
          "category": "Food",
          "headline": "20% Off Craft Brews for Society Residents",
          "description": "Show your verified apartment badge and get flat 20% off.",
          "media": {
            "url": "https://.../photo.jpg?w=800",
            "type": "image",
            "thumbnail": "https://.../photo.jpg?w=200",
            "small_banner": "https://.../photo.jpg?w=400"
          },
          "cta": {
            "text": "Claim Society Perk",
            "type": "whatsapp",
            "redirection": "https://wa.me/919012345678"
          },
          "created_at": "2026-09-18T14:56:18.827365"
        },
        "recommendationCount": 2,
        "isRecommendedByCurrentUser": true
      },
      {
        "id": "0x3bae133257f63915:0x1e44b4a993a87f4e",
        "placeId": 112,
        "name": "Third Wave Coffee",
        "category": "cafe",
        "latitude": 12.9147799,
        "longitude": 77.680188,
        "address": "12th Main Rd, HAL 2nd Stage, Bengaluru",
        "phoneNumber": "078999 24470",
        "rating": 4.4,
        "photoUrls": ["https://lh3.googleusercontent.com/gps-cs-s/AHRPTWk..."],
        "distanceKm": 0.345,
        "isPromoted": false,
        "campaign": null,
        "recommendationCount": 0,
        "isRecommendedByCurrentUser": false
      }
    ]
  }
}
```

### Place fields

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | Google place id. **Send this back** to the recommend endpoints. |
| `placeId` | int | Internal id. Also accepted by the recommend endpoints. |
| `name` | string | |
| `category` | string \| null | Source category, lowercase (`cafe`, `gym`, `bank`, …) |
| `latitude` / `longitude` | float | |
| `address` | string \| null | Cleaned of map-pin glyphs and newlines |
| `phoneNumber` | string \| null | **null for ~13% of places** |
| `rating` | float \| null | **null for ~95% of places** — the scrape rarely captured it. Hide the stars when null. |
| `photoUrls` | string[] | 0 or 1 entry today. Always an array. |
| `distanceKm` | float | From the requested `lat`/`lng`, 3 dp |
| `isPromoted` | bool | `true` ⟺ `campaign != null` |
| `campaign` | object \| null | See below |
| `recommendationCount` | int | Total across all users |
| `isRecommendedByCurrentUser` | bool | Always `false` if `app_user_id` was omitted |

**Ordering:** promoted places first, then ascending `distanceKm`.

### Campaign object

Present only when the place's vendor has a campaign running today.

| Field | Type | Notes |
|-------|------|-------|
| `id` | int | Campaign id |
| `vendor_id` | string | 16-char vendor id |
| `name` | string \| null | Campaign name |
| `goal` | string \| null | Free text from the ad template, e.g. `visits`, `Calls` |
| `format` | string | **UPPERCASE**: `HOOD_PICKS_BANNER`, `ISLAND_BANNER`, `DOUBLE_WIDTH_BANNER` |
| `category` | string \| null | `Retail`, `Real Estate`, `Food` |
| `headline` | string | The ad's main line |
| `description` | string \| null | |
| `media` | object \| null | `{ url, type, thumbnail, small_banner }` — any key may be absent |
| `cta` | object \| null | `{ text, type, redirection }`; `type` is e.g. `whatsapp`, `call` |
| `created_at` | string | ISO-8601, no timezone suffix |

`media` and `cta` are stored as free-form JSON on the ad template. Treat every
key as optional and fall back gracefully.

---

## Ad targeting

A place is promoted when the chain below resolves:

```
place ──(vendors.place_id)──► vendor ──► campaign  (status ACTIVE, today within start/end)
                                            │
                        society_id ────────►│  must be in the campaign's targeted societies
```

Vendors buy ad slots **per residential society**. `society_id` makes delivery
match what they paid for:

| Request | Behaviour |
|---------|-----------|
| `society_id` omitted | Any live campaign for that place attaches. Use for anonymous browsing. |
| `society_id` = a society the campaign targeted | Ad shows. |
| `society_id` = a society it did not target | `campaign: null`, `isPromoted: false`. |

If a vendor is running several campaigns at once, the **most recently created**
one wins.

---

## 2. Recommend a place

```
POST /api/v1/places/{placeRef}/recommend
```

`{placeRef}` is either the `id` (Google place id) or the `placeId` (integer)
from the feed — both resolve to the same place.

```bash
curl -X POST "$BASE/api/v1/places/0x3bae1370c9de531d:0xdc88a20e35a6b3f3/recommend" \
  -H 'Content-Type: application/json' \
  -d '{"app_user_id":"user_42"}'
```

```json
{
  "status": "success",
  "data": {
    "placeId": 57,
    "recommendationCount": 2,
    "isRecommendedByCurrentUser": true
  }
}
```

`201`. Recommending twice with the same `app_user_id` is a no-op — the count
does not move, so the button is safe to double-tap.

## 3. Undo a recommendation

```
DELETE /api/v1/places/{placeRef}/recommend?app_user_id=user_42
```

```json
{
  "status": "success",
  "data": {
    "placeId": 57,
    "recommendationCount": 1,
    "isRecommendedByCurrentUser": false
  }
}
```

`200`. Deleting one that does not exist is also a no-op.

---

## About `app_user_id`

There is no consumer account system — vendors are the only registered users.
`app_user_id` is an opaque string the app chooses (a device id, or your own
user id). It has one job: telling `recommendationCount` apart from
`isRecommendedByCurrentUser`.

```
?app_user_id=user_42  →  count: 2,  isRecommendedByCurrentUser: true
?app_user_id=user_7   →  count: 2,  isRecommendedByCurrentUser: false
(omitted)             →  count: 2,  isRecommendedByCurrentUser: false
```

It must be **stable across launches**. A value regenerated each session makes
every place look un-recommended again.

---

## Errors

| Status | When | Body |
|--------|------|------|
| `404` | Unknown place ref | `{"detail": "Unknown place 0xdeadbeef"}` |
| `422` | Bad/missing query params (`lat` out of range, missing `lng`, …) | FastAPI validation array |

A coordinate with no places nearby is **not** an error — it returns `200` with
`"places": []`.

---

## Demo data

The place catalogue is a 429-row scrape covering **Sarjapur Road / HSR /
Bellandur only**:

```
latitude   12.906 … 12.924
longitude  77.669 … 77.686
```

Use this centre for demos:

```
lat=12.9178893  lng=77.6804043
```

Anywhere else — including Indiranagar (12.9784, 77.6408) — returns an empty
list. `location_name` is a free-text label, so the header can still read
whatever you want.

Available categories include `restaurant`, `cafe`, `bakery`, `gym`, `salon`,
`bank`, `clinic`, `hospital`, `jewellery store`, `clothing store`,
`grocery store`, `electronics store`, `ATM`, and more.

---

## Notes for integration

- **`format` is uppercase** (`HOOD_PICKS_BANNER`). Map to display names
  client-side.
- **`rating` is usually `null`** — only 21 of 429 places have one.
- **`photoUrls` is always an array**, currently 0 or 1 URL. Do not assume
  index `0` exists.
- **`created_at` has no timezone suffix.** Treat it as UTC.
- Promoted places are already sorted first; no client-side reordering needed.
