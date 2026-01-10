# storage.py
import os
import uuid
from typing import Optional

import boto3
from botocore.client import Config

R2_ACCOUNT_ID = (os.getenv("R2_ACCOUNT_ID") or "").strip()
R2_ACCESS_KEY_ID = (os.getenv("R2_ACCESS_KEY_ID") or "").strip()
R2_SECRET_ACCESS_KEY = (os.getenv("R2_SECRET_ACCESS_KEY") or "").strip()
R2_BUCKET = (os.getenv("R2_BUCKET") or "").strip()
R2_REGION = (os.getenv("R2_REGION") or "auto").strip()


def _endpoint() -> str:
    if not R2_ACCOUNT_ID:
        raise RuntimeError("R2_ACCOUNT_ID is missing.")
    return f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"


def _client():
    missing = []
    for k in ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]:
        if not os.getenv(k):
            missing.append(k)
    if missing:
        raise RuntimeError(f"Missing R2 env vars: {', '.join(missing)}")

    return boto3.client(
        "s3",
        endpoint_url=_endpoint(),
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name=R2_REGION or "auto",
        config=Config(signature_version="s3v4"),
    )


def make_storage_key(*parts: str, filename: Optional[str] = None) -> str:
    clean_parts = [p.strip("/").replace("..", ".") for p in parts if p]
    base = "/".join(clean_parts)
    suffix = uuid.uuid4().hex
    if filename:
        fn = filename.strip().replace("/", "_")
        return f"{base}/{suffix}_{fn}"
    return f"{base}/{suffix}"


def put_object_bytes(storage_key: str, data: bytes, content_type: str) -> None:
    s3 = _client()
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=storage_key,
        Body=data,
        ContentType=content_type or "application/octet-stream",
    )


def delete_object(storage_key: str) -> None:
    if not storage_key:
        return
    s3 = _client()
    s3.delete_object(Bucket=R2_BUCKET, Key=storage_key)


def presign_get_url(storage_key: str, expires_seconds: int = 900) -> str:
    if not storage_key:
        return ""
    s3 = _client()
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": R2_BUCKET, "Key": storage_key},
        ExpiresIn=max(60, int(expires_seconds or 900)),
    )


