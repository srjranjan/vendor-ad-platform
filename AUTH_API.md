# Vendor Auth API

Sign-up and sign-in for the vendor portal.

**Base URL**

```
https://vendor-ad-platform-production.up.railway.app
```

Interactive reference: [`/docs`](https://vendor-ad-platform-production.up.railway.app/docs)

> **OTP is `1234`.** No SMS is sent; any number accepts that code.

---

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/vendors/send-otp` | Sign-up: start, and report whether the number is already registered |
| `POST` | `/api/v1/vendors/verify-otp` | Sign-up: confirm the OTP |
| `POST` | `/api/v1/vendors/register` | Sign-up: create the vendor |
| `POST` | `/api/v1/vendors/login/send-otp` | **Sign-in: start. Fails if the number has no account** |
| `POST` | `/api/v1/vendors/login/verify-otp` | **Sign-in: confirm OTP, return the vendor** |
| `GET` | `/api/v1/vendors/me` | Current vendor, from the bearer token |

Sign-up and sign-in are separate on purpose: sign-up *reports* whether a number
exists and lets the UI decide, sign-in *refuses* an unknown number.

---

## Mobile numbers

Every endpoint normalises the number to `+91XXXXXXXXXX` before looking it up, so
these all resolve to one account:

```
9097970001   →  +919097970001
919097970001 →  +919097970001
+919097970001 → +919097970001
```

Responses always echo the normalised form. Store that, and send whichever form
you like.

---

# Sign-in

## 1. Start sign-in

```
POST /api/v1/vendors/login/send-otp
```

```json
{ "mobile_number": "9097970001" }
```

**200** — account exists, OTP "sent":

```json
{
  "success": true,
  "message": "OTP sent to +919097970001.",
  "mobile_number": "+919097970001",
  "business_name": "Kanti Sweets"
}
```

`business_name` is returned so the OTP screen can show *"Signing in as Kanti
Sweets"* before the vendor has authenticated.

**404** — no account for that number:

```json
{ "detail": "No account found with mobile number +919000000000. Please sign up first." }
```

Route the user to sign-up on this error.

## 2. Verify and sign in

```
POST /api/v1/vendors/login/verify-otp
```

```json
{ "mobile_number": "9097970001", "otp": "1234" }
```

**200** — the vendor:

```json
{
  "id": "QXZFGRHXFU49DHTP",
  "place_id": 99,
  "business_name": "Kanti Sweets",
  "mobile_number": "+919097970001",
  "raw_description": "sweets and savouries",
  "ai_category": "Retail",
  "address_text": null,
  "lat": 12.9144311,
  "lng": 77.67744,
  "tech_comfort_level": null,
  "is_verified": true
}
```

Identical in shape to what `register` returns, so one client handler covers
both. **Store `id`** — the 16-character vendor id is what every other endpoint
takes, as a query parameter or as the bearer token.

| Status | When |
|--------|------|
| `400` | `{"detail": "Invalid OTP"}` |
| `404` | Number has no account — same message as step 1 |
| `422` | Malformed body (`otp` must be exactly 4 digits) |

The number is re-checked here as well as in step 1: this endpoint can be called
directly, and an account removed between the two steps must not still sign in.

---

# Sign-up

## 1. Start sign-up

```
POST /api/v1/vendors/send-otp
```

```json
{ "mobile_number": "9099990001" }
```

**200** — new number:

```json
{ "success": true, "message": "OTP sent to +919099990001.", "vendor_exists": false }
```

**200** — already registered:

```json
{
  "success": true,
  "message": "Vendor already registered with +919097970001. OTP sent for sign-in.",
  "vendor_exists": true
}
```

This is **not** an error. Branch on `vendor_exists`: when `true`, take the user
to the sign-in OTP screen instead of the sign-up form.

## 2. Verify the OTP

```
POST /api/v1/vendors/verify-otp
```

```json
{ "mobile_number": "9099990001", "otp": "1234" }
```

**200** → `{ "verified": true }` · **400** → `{"detail": "Invalid OTP"}`

The verification is recorded server side; step 3 refuses without it.

## 3. Register

```
POST /api/v1/vendors/register
```

```json
{
  "business_name": "Kanti Sweets",
  "mobile_number": "9099990001",
  "place_id": 99,
  "raw_description": "sweets and savouries",
  "address_text": "Sarjapur Main Rd, Kasavanahalli, Bengaluru",
  "lat": 12.9144311,
  "lng": 77.67744,
  "tech_comfort_level": "Beginner",
  "ai_category": "Retail"
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `business_name` | **yes** | |
| `mobile_number` | **yes** | Must have passed step 2 |
| `place_id` | no | The business listing this vendor operates. See `MOBILE_API.md`; without it their ads cannot appear on the map. |
| `raw_description` | no | Free text; drives `ai_category` when that is omitted |
| `address_text`, `lat`, `lng` | no | `lat`/`lng` are required later for radius targeting |
| `tech_comfort_level` | no | Exactly `Beginner`, `Moderate` or `Advanced` |
| `ai_category` | no | One of `GET /api/v1/vendors/categories`; derived from `raw_description` if omitted |

**201** — the vendor object shown above. A wallet is created automatically with
a zero balance.

| Status | When |
|--------|------|
| `403` | `"Mobile number … is not verified. Call /api/v1/vendors/verify-otp before registering."` |
| `409` | `"A vendor is already registered with mobile number …"` |
| `409` | `"place_id N is already claimed by another vendor"` |
| `400` | `"Unknown place_id N"` |
| `422` | Field validation |

---

## Current vendor

```
GET /api/v1/vendors/me
Authorization: Bearer <vendor id>
```

Returns the same vendor object. The bearer token **is** the vendor id — there
are no JWTs here.

**401** when the header is missing or empty, in the wallet error envelope:

```json
{
  "success": false,
  "error": {
    "code": "UNAUTHORIZED",
    "message": "Authentication credentials missing or invalid. Please provide 'Authorization: Bearer <token>'",
    "details": { "required_header": "Authorization: Bearer <token>" }
  }
}
```

---

## Error shapes

Two envelopes are in use. Handle both.

**Most endpoints** — FastAPI's:

```json
{ "detail": "Invalid OTP" }
```

**Validation (422)** — `detail` is an array:

```json
{ "detail": [ { "loc": ["body","otp"], "msg": "String should have at least 4 characters", "type": "string_too_short" } ] }
```

**Wallet and `/vendors/me`** — the wallet envelope with `success` and `error`,
as shown above.

---

## After sign-in

`id` from the login response is all the portal needs:

| Use | How |
|-----|-----|
| Wallet | `Authorization: Bearer <id>` |
| Ad creatives | `GET /api/v1/ad-templates?vendor_id=<id>` |
| Campaign dashboard | `GET /api/v1/campaigns?vendor_id=<id>` |
| Business listing | `GET/PUT/DELETE /api/v1/vendors/{id}/place` |
| Audience for a campaign | `GET /api/v1/campaigns/nearby-societies?vendor_id=<id>&ad_template_id=…` |

---

## Notes

- There is **no session or token expiry**. The vendor id is a long-lived
  identifier; keep it in local storage and treat sign-out as discarding it.
- **A number that is signed up is not automatically signed in** — the two OTP
  flows are independent, though both accept `1234`.
- `POST /api/v1/vendors/login` (single call, optional OTP) **was removed**. Use
  the two-step pair.
