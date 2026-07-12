from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api.routes import auth, materials, ocr, words, users, wordbooks
from app.services.pronunciation_service import initialize_pronunciation_model


@asynccontextmanager
async def lifespan(_: FastAPI):
    await initialize_pronunciation_model()
    yield

app = FastAPI(
    title="Vocamine API",
    description="教材から未知単語を抽出し単語帳を管理するバックエンド",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(set(settings.cors_origins + [
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ])),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ocr.router, prefix="/api/v1")
app.include_router(words.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")
app.include_router(materials.router, prefix="/api/v1")
app.include_router(wordbooks.router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok"}
