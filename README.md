# Vendor Ad Platform

FastAPI + SQLAlchemy backend for targeting vendor ad campaigns at residential
societies by geographic proximity.

## Endpoints

| Method | Path                          | Description                                         |
|--------|-------------------------------|-----------------------------------------------------|
| GET    | `/health`                     | Healthcheck (used by Railway)                       |
| GET    | `/api/v1/societies/nearby`    | Societies within `radius_km` of `lat`/`lng`         |
| POST   | `/api/v1/ads`                 | Create a campaign + its targeted society mappings   |
| GET    | `/docs`                       | Swagger UI                                          |

Nearby lookup runs the Haversine formula in SQL and returns results sorted by
ascending distance, with `distance_km` on each row.

## Local development

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

With no `DATABASE_URL` set the app falls back to local sqlite
(`./vendor_ads.db`) so it boots with no database to configure. Tables are
created automatically on startup.

To run against Postgres locally, copy `.env.example` and export the URL:

```bash
export DATABASE_URL=postgresql://user:pass@host:5432/dbname
uvicorn main:app --reload
```

## Deploying to Railway

1. Push this repo to GitHub.
2. In Railway: **New Project → Deploy from GitHub repo**, select this repo.
3. In the same project: **New → Database → Add PostgreSQL**.
4. Select the app service → **Variables** → **Add Variable Reference** →
   pick `DATABASE_URL` from the Postgres service. This link is what injects
   the URL; without it the app silently falls back to sqlite on an ephemeral
   disk and loses data on every redeploy.
5. Deploy. Railway builds with Railpack, installs `requirements.txt`, and
   starts the command in `railway.json`.

### Why Python is pinned to 3.11

`.python-version` pins 3.11. Railpack defaults to 3.13, where
`pydantic-core==2.6.3` (via `pydantic==2.3.0`) has no prebuilt wheel and needs
a Rust toolchain that is not in the build image, and `psycopg2-binary==2.9.7`
has no wheel either and falls back to a slow source build. On 3.11 every
pinned dependency resolves to a prebuilt wheel.

Bumping `psycopg2-binary` to `2.9.11` and `pydantic` to a current 2.x release
would let you drop the pin and move to a newer Python.

## Ad creatives (Cloudinary)

Creatives are hosted on Cloudinary. The backend stores only URLs, so no
credentials live on the server and there is no upload endpoint.

**Setup (once):** set `CLOUDINARY_URL` on the service (Railway -> Variables):

```
CLOUDINARY_URL=cloudinary://<api_key>:<api_secret>@<cloud_name>
```

Uploads are **signed**: the browser sends the file straight to Cloudinary, but
this service issues the signature, so the API secret never reaches the client
and the account needs no unsigned preset (which would let anyone who learns the
cloud name upload into it).

```js
// 1. ask this service to sign an upload
const s = await fetch(`${API}/api/v1/uploads/cloudinary-signature?folder=ads`)
              .then(r => r.json())

// 2. upload straight to Cloudinary with that signature
const form = new FormData()
form.append("file", file)
form.append("api_key", s.apiKey)
form.append("timestamp", s.timestamp)
form.append("folder", s.folder)
form.append("signature", s.signature)

const { secure_url } = await fetch(s.uploadUrl, { method: "POST", body: form })
                              .then(r => r.json())

// 3. create the ad template with just the one url
media: { url: secure_url, type: "image" }
```

`folder` must be one of `ads`, `places`, `logos`. A signature is good for 10
minutes. If `CLOUDINARY_URL` is unset the endpoint returns 503 rather than
failing at upload time.

**Send `url` only.** `thumbnail` (w_200) and `small_banner` (w_400) are derived
server side from the same asset, with `c_limit,q_auto,f_auto` so images are not
upscaled and are served in the best format the browser accepts:

```
POST { "media": { "url": "https://res.cloudinary.com/<cloud>/image/upload/v1/ads/x.jpg",
                  "type": "image" } }

->   { "url":          ".../image/upload/v1/ads/x.jpg",
       "small_banner": ".../image/upload/w_400,c_limit,q_auto,f_auto/v1/ads/x.jpg",
       "thumbnail":    ".../image/upload/w_200,c_limit,q_auto,f_auto/v1/ads/x.jpg" }
```

Passing a size explicitly keeps it. A url that already carries a transform has
it replaced rather than chained, so sizes never compound. Non-Cloudinary urls
are stored as given and must supply their own sizes. `media.url` must be
http(s) or the request is rejected with 422.

## Configuration

| Variable       | Required | Notes                                                        |
|----------------|----------|--------------------------------------------------------------|
| `DATABASE_URL` | No       | Injected by Railway. Falls back to sqlite. Legacy `postgres://` scheme is rewritten automatically. |
| `PORT`         | No       | Set by Railway; the start command binds to it.               |
| `CORS_ORIGINS` | No       | Comma-separated allowed origins. Defaults to `*` (credentials disabled). Set explicit origins in production to enable credentials. |
