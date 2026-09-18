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

## Configuration

| Variable       | Required | Notes                                                        |
|----------------|----------|--------------------------------------------------------------|
| `DATABASE_URL` | No       | Injected by Railway. Falls back to sqlite. Legacy `postgres://` scheme is rewritten automatically. |
| `PORT`         | No       | Set by Railway; the start command binds to it.               |
