from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    supabase_url: str
    supabase_service_role_key: str
    google_application_credentials: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    wiktionary_user_agent: str = "Vocamine/0.1 (local development; contact unset)"
    app_env: str = "development"
    cors_origins: List[str] = ["http://localhost:3000"]
    oracle_object_storage_namespace: str = Field(
        "",
        validation_alias=AliasChoices("ORACLE_OBJECT_STORAGE_NAMESPACE", "NAMESPACE"),
    )
    oracle_object_storage_region: str = Field(
        "",
        validation_alias=AliasChoices("ORACLE_OBJECT_STORAGE_REGION", "REGION"),
    )
    oracle_object_storage_access_key_id: str = Field(
        "",
        validation_alias=AliasChoices("ORACLE_OBJECT_STORAGE_ACCESS_KEY_ID", "ACCESS_KEY_ID"),
    )
    oracle_object_storage_secret_access_key: str = Field(
        "",
        validation_alias=AliasChoices("ORACLE_OBJECT_STORAGE_SECRET_ACCESS_KEY", "SECRET_ACCESS_KEY"),
    )
    oracle_object_storage_bucket: str = Field(
        "",
        validation_alias=AliasChoices("ORACLE_OBJECT_STORAGE_BUCKET", "BUCKET"),
    )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
