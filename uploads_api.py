"""Signed Cloudinary uploads.

The portal uploads files straight to Cloudinary, but signs each request with a
signature this service issues. The API secret therefore never reaches the
browser, and the account needs no unsigned preset - which would otherwise let
anyone who finds the cloud name upload into it.

Configured by CLOUDINARY_URL: cloudinary://<api_key>:<api_secret>@<cloud_name>
"""

import hashlib
import os
import re
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

_CLOUDINARY_URL = re.compile(r"^cloudinary://([^:]+):([^@]+)@(.+)$")

# Folders the portal may upload into. An open folder parameter would let a
# caller scatter assets anywhere in the account.
ALLOWED_FOLDERS = {"ads", "places", "logos"}

# How long a signature stays usable.
SIGNATURE_TTL_SECONDS = 600


def _config():
    raw = os.getenv("CLOUDINARY_URL", "")
    match = _CLOUDINARY_URL.match(raw)
    if not match:
        return None
    api_key, api_secret, cloud_name = match.groups()
    return {"api_key": api_key, "api_secret": api_secret, "cloud_name": cloud_name}


def sign(params: dict, api_secret: str) -> str:
    """Cloudinary signature: sorted k=v pairs, then the secret, SHA-1 hexed."""
    payload = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.sha1(f"{payload}{api_secret}".encode()).hexdigest()


class UploadSignature(BaseModel):
    cloudName: str
    apiKey: str
    timestamp: int
    folder: str
    signature: str
    uploadUrl: str
    expiresInSeconds: int


uploads_router = APIRouter(prefix="/api/v1/uploads", tags=["uploads"])


@uploads_router.get("/cloudinary-signature", response_model=UploadSignature)
def cloudinary_signature(
    folder: str = Query("ads", description=f"One of: {', '.join(sorted(ALLOWED_FOLDERS))}"),
):
    """Credentials for one direct-to-Cloudinary upload.

    The browser POSTs the file plus these fields to `uploadUrl`; the secret
    stays here.
    """
    config = _config()
    if not config:
        raise HTTPException(
            status_code=503,
            detail="Uploads are not configured: CLOUDINARY_URL is not set on the server",
        )
    if folder not in ALLOWED_FOLDERS:
        raise HTTPException(
            status_code=400,
            detail=f"folder must be one of {sorted(ALLOWED_FOLDERS)}",
        )

    timestamp = int(time.time())
    signature = sign({"folder": folder, "timestamp": timestamp}, config["api_secret"])

    return UploadSignature(
        cloudName=config["cloud_name"],
        apiKey=config["api_key"],
        timestamp=timestamp,
        folder=folder,
        signature=signature,
        uploadUrl=f"https://api.cloudinary.com/v1_1/{config['cloud_name']}/image/upload",
        expiresInSeconds=SIGNATURE_TTL_SECONDS,
    )
