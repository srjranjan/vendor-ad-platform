# Ad Asset Upload API

How the vendor portal gets an image onto a campaign.

**Base URL**

```
https://vendor-ad-platform-production.up.railway.app
```

Assets live in **Cloudinary**. This service stores only URLs — it never
receives the file. The browser uploads directly to Cloudinary, but signs the
request with a signature this service issues, so the Cloudinary API secret
never reaches the client and the account has no unsigned upload preset that
anyone could abuse.

```
 portal ──1. GET signature──►  this API
 portal ──2. POST file ─────►  Cloudinary  ──► secure_url
 portal ──3. POST template ─►  this API    (media.url = secure_url)
```

---

## Step 1 — Get a signature

```
GET /api/v1/uploads/cloudinary-signature?folder=ads
```

| Param | Required | Default | Notes |
|-------|----------|---------|-------|
| `folder` | no | `ads` | One of `ads`, `places`, `logos` |

```bash
curl "https://vendor-ad-platform-production.up.railway.app/api/v1/uploads/cloudinary-signature?folder=ads"
```

**200**

```json
{
  "cloudName": "keyfcrgk",
  "apiKey": "532332591639284",
  "timestamp": 1789755156,
  "folder": "ads",
  "signature": "8b5f176f13a9baee3f563b8f6c5cad0e8b3093e6",
  "uploadUrl": "https://api.cloudinary.com/v1_1/keyfcrgk/image/upload",
  "expiresInSeconds": 600
}
```

| Field | Use |
|-------|-----|
| `uploadUrl` | Where to POST the file in step 2 |
| `apiKey`, `timestamp`, `folder`, `signature` | Send all four alongside the file, unchanged |
| `cloudName` | Informational |
| `expiresInSeconds` | Advisory. Cloudinary rejects a stale `timestamp`, so request a fresh signature per upload rather than caching one. |

**Errors**

| Status | When | Body |
|--------|------|------|
| `400` | Folder not allowed | `{"detail": "folder must be one of ['ads', 'logos', 'places']"}` |
| `503` | `CLOUDINARY_URL` not set on the server | `{"detail": "Uploads are not configured: CLOUDINARY_URL is not set on the server"}` |

A signature covers exactly one upload into one folder. Requesting another is
cheap — do it per file.

---

## Step 2 — Upload to Cloudinary

POST the file plus the four signed fields to `uploadUrl`. **This request does
not go to our API.**

```js
const s = await fetch(`${API}/api/v1/uploads/cloudinary-signature?folder=ads`)
                .then(r => r.json())

const form = new FormData()
form.append("file", file)          // File / Blob from the input
form.append("api_key",   s.apiKey)
form.append("timestamp", s.timestamp)
form.append("folder",    s.folder)
form.append("signature", s.signature)

const uploaded = await fetch(s.uploadUrl, { method: "POST", body: form })
                       .then(r => r.json())
// uploaded.secure_url  <-- this is what you send on
```

```bash
curl -X POST "https://api.cloudinary.com/v1_1/keyfcrgk/image/upload" \
  -F "file=@creative.png" \
  -F "api_key=532332591639284" \
  -F "timestamp=1789755156" \
  -F "folder=ads" \
  -F "signature=8b5f176f13a9baee3f563b8f6c5cad0e8b3093e6"
```

**200** (trimmed — Cloudinary returns ~20 fields)

```json
{
  "public_id": "ads/k1kqhjdwtxag9j9p3e2f",
  "secure_url": "https://res.cloudinary.com/keyfcrgk/image/upload/v1789755157/ads/k1kqhjdwtxag9j9p3e2f.png",
  "url": "http://res.cloudinary.com/keyfcrgk/image/upload/v1789755157/ads/k1kqhjdwtxag9j9p3e2f.png",
  "format": "png",
  "width": 1200,
  "height": 628,
  "bytes": 84213,
  "resource_type": "image",
  "version": 1789755157
}
```

Use **`secure_url`** (https), not `url` (http).

**Cloudinary errors** come back in their own envelope:

```json
{ "error": { "message": "Invalid Signature deadbeef. String to sign - 'folder=ads&timestamp=1789755156'." } }
```

Usually means the signature was altered, a field was dropped, or the timestamp
went stale. Fetch a new signature and retry once.

---

## Step 3 — Attach it to an ad template

Send **only `url`**. The other sizes are derived server side.

```
POST /api/v1/ad-templates
```

```json
{
  "vendor_id": "QM2JKZHCANRRJ74K",
  "goal": "visits",
  "format": "ISLAND",
  "category": "Food",
  "headline": "20% Off Craft Brews for Society Residents",
  "description": "Show your verified apartment badge.",
  "media": {
    "url": "https://res.cloudinary.com/keyfcrgk/image/upload/v1789755157/ads/k1kqhjdwtxag9j9p3e2f.png",
    "type": "image"
  },
  "cta": {
    "text": "Claim Society Perk",
    "type": "whatsapp",
    "redirection": "https://wa.me/919012345678"
  }
}
```

**201** — `media` comes back with all three sizes filled in:

```json
"media": {
  "url":          ".../image/upload/v1789755157/ads/k1kqhjdwtxag9j9p3e2f.png",
  "small_banner": ".../image/upload/w_400,c_limit,q_auto,f_auto/v1789755157/ads/k1kqhjdwtxag9j9p3e2f.png",
  "thumbnail":    ".../image/upload/w_200,c_limit,q_auto,f_auto/v1789755157/ads/k1kqhjdwtxag9j9p3e2f.png"
}
```

### Media object

| Field | Required | Notes |
|-------|----------|-------|
| `url` | **yes** | Must be `http(s)` or the request is **422**. This is the full-size creative. |
| `type` | **yes** | `"image"` |
| `thumbnail` | no | Derived at w_200 if omitted |
| `small_banner` | no | Derived at w_400 if omitted |

### How derivation works

| Size | Transform | Used for |
|------|-----------|----------|
| `url` | as uploaded | Full creative |
| `small_banner` | `w_400,c_limit,q_auto,f_auto` | In-card banner |
| `thumbnail` | `w_200,c_limit,q_auto,f_auto` | List / marker thumb |

- **`c_limit`** never upscales, so a small source stays sharp rather than blurring.
- **`q_auto,f_auto`** serve WebP/AVIF where the browser supports it.
- A url that **already carries a transform** has it replaced, not chained — sending
  `/upload/w_800,c_fill/...` yields a real `w_400`, not a 400-then-800 resize.
- **Passing a size explicitly keeps it.** Send your own `thumbnail` and it is used as given.
- **Non-Cloudinary urls pass through untouched.** An Unsplash or S3 link is stored
  as-is and must supply its own `thumbnail` and `small_banner`, or those stay `null`.

---

## Where the sizes surface

The mobile feed returns the whole `media` object inside `campaign` on a
promoted place — see `MOBILE_API.md`. Treat every key as optional and fall
back gracefully; `media` is stored as free-form JSON.

---

## Folders

| Folder | For |
|--------|-----|
| `ads` | Campaign creatives |
| `places` | Place photos |
| `logos` | Business logos / map markers |

Anything else is rejected with 400, so a caller cannot scatter assets across
the account.

---

## Server configuration

One environment variable, set on the service (Railway → Variables):

```
CLOUDINARY_URL=cloudinary://<api_key>:<api_secret>@<cloud_name>
```

Unset → step 1 returns **503** with a clear message rather than failing
mid-upload. The secret is never sent to the client and is not committed to the
repo.

---

## Notes

- There is **no delete endpoint**. Replacing a creative uploads a new asset and
  points `media.url` at it; the old one stays in Cloudinary.
- Uploads are **unauthenticated** — any caller can request a signature. Fine for
  the demo; the folder allowlist is the only limit.
- Only `resource_type: image` is signed. Video would need a separate
  `/video/upload` endpoint.
