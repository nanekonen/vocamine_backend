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

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
