from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    supabase_url: str
    supabase_service_role_key: str
    google_application_credentials: str = ""
    app_env: str = "development"
    cors_origins: List[str] = ["http://localhost:3000"]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
