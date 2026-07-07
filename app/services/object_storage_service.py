from __future__ import annotations

from functools import lru_cache

from fastapi import HTTPException

from app.core.config import settings


def _require_object_storage_settings() -> None:
    missing = [
        name
        for name, value in {
            "ORACLE_OBJECT_STORAGE_NAMESPACE": settings.oracle_object_storage_namespace,
            "ORACLE_OBJECT_STORAGE_REGION": settings.oracle_object_storage_region,
            "ORACLE_OBJECT_STORAGE_ACCESS_KEY_ID": settings.oracle_object_storage_access_key_id,
            "ORACLE_OBJECT_STORAGE_SECRET_ACCESS_KEY": settings.oracle_object_storage_secret_access_key,
            "ORACLE_OBJECT_STORAGE_BUCKET": settings.oracle_object_storage_bucket,
        }.items()
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Oracle Object Storage is not configured. Missing: {', '.join(missing)}",
        )


@lru_cache(maxsize=1)
def _client():
    _require_object_storage_settings()
    try:
        import boto3
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail="boto3 is not installed. Run pip install -r requirements.txt.",
        ) from exc

    endpoint = (
        f"https://{settings.oracle_object_storage_namespace}"
        f".compat.objectstorage.{settings.oracle_object_storage_region}.oraclecloud.com"
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.oracle_object_storage_access_key_id,
        aws_secret_access_key=settings.oracle_object_storage_secret_access_key,
        region_name=settings.oracle_object_storage_region,
    )


def put_bytes(key: str, data: bytes, content_type: str | None = None) -> str:
    _require_object_storage_settings()
    extra_args = {}
    if content_type:
        extra_args["ContentType"] = content_type
    _client().put_object(
        Bucket=settings.oracle_object_storage_bucket,
        Key=key,
        Body=data,
        **extra_args,
    )
    return key


def get_bytes(key: str) -> bytes:
    _require_object_storage_settings()
    response = _client().get_object(
        Bucket=settings.oracle_object_storage_bucket,
        Key=key,
    )
    return response["Body"].read()
