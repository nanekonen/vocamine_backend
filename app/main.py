from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api.routes import ocr, words, users

app = FastAPI(
    title="Vocamine API",
    description="教材から未知単語を抽出し単語帳を管理するバックエンド",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ocr.router, prefix="/api/v1")
app.include_router(words.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok"}
