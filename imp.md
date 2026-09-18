# Ad Library APIs Implementation Plan

This plan proposes the necessary backend changes to support the "Ads Library" feature seen in the `vendor-portal` UI.

## Open Questions

> [!IMPORTANT]
> Please review and answer these before I proceed:
> 1. **Endpoint Naming:** The mock data calls templates `ads` and campaigns `campaigns`. However, the current backend already uses `POST /api/v1/ads` to create campaigns. Should we rename the existing campaign endpoints to `/api/v1/campaigns` and use `/api/v1/ad-templates` for templates? Or just use `/api/v1/ad-templates`?
> 2. **Vendor Identification:** Currently, campaigns just store a `vendor_name` string. The mock UI has a `vendorId` ("v123"). Do you want me to add a simple `vendor_id` (string) column to the new `AdTemplate` table to identify which vendor owns it, or should we create a full `Vendor` table?
> 3. **Ad "Name" Field:** The UI mock data for `ads` includes a `name` field (e.g., "Diwali Sale Banner"), but the `CreateAd.tsx` form doesn't seem to ask the user for an ad name. Should the backend generate a name automatically, or just make it an optional field for now?

## Proposed Changes

### 1. Database Models (`main.py`)
I will add a new SQLAlchemy model called `AdTemplate` with the fields updated based on your feedback. For the objects (`media` and `cta`), I will use a JSON column (supported by both SQLite and Postgres).

#### [NEW] `AdTemplate` Model
- `id`: Integer (Primary Key)
- `vendor_id`: String (Identifier for the vendor)
- `name`: String (e.g. "Diwali Sale Banner")
- `goal`: String (e.g., calls, visits, leads, sales, awareness)
- `format`: Enum (e.g., 'island', '2x')
- `category`: Enum (e.g., 'Retail', 'Real Estate', 'Food')
- `headline`: String
- `description`: String
- `media`: JSON (Will store: `{ url: string, type: string, thumbnail: string, small_banner: string }`)
- `cta`: JSON (Will store: `{ text: string, type: string, redirection: string }`)
- `created_at`: DateTime

### 2. Pydantic Schemas (`main.py`)
I will create the necessary validation schemas for incoming requests and outgoing responses. I will also create Python `Enum` classes for `FormatEnum` and `CategoryEnum`.

#### [NEW] Schemas
- `MediaObject`: `url`, `type`, `thumbnail`, `small_banner`
- `CTAObject`: `text`, `type`, `redirection`
- `AdTemplateCreate`: Will map the UI's `formData` structure (goal, format enum, category enum, headline, desc, media object, CTA object).
- `AdTemplateResponse`: Will include the `id` and all the fields, formatted exactly how the frontend expects it.

### 3. API Endpoints (`main.py`)
I will add the following endpoints to handle the Ad Templates:

#### [NEW] `POST /api/v1/ad-templates`
- Accepts `AdTemplateCreate`.
- Saves the ad template to the database.
- Returns the created `AdTemplateResponse`.

#### [NEW] `GET /api/v1/ad-templates`
- Accepts an optional `vendor_id` query parameter.
- Returns a list of `AdTemplateResponse` for the given vendor.

## Verification Plan

### Automated/Manual Verification
- Restart the FastAPI server.
- Use Swagger UI (`/docs`) to test the `POST` endpoint by creating an ad template with the new Enums and JSON objects.
- Use Swagger UI to test the `GET` endpoint to ensure the created template is returned correctly.
