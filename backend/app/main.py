from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import router
from .auth import bootstrap_admin, validate_auth_settings
from .config import get_settings
from .database import SessionLocal, create_schema
from .enterprise_api import router as enterprise_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    validate_auth_settings(settings)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    if settings.auto_create_schema:
        create_schema()
    with SessionLocal() as db:
        bootstrap_admin(db, settings)
    yield


app = FastAPI(
    title="B2B 分销自动化 API",
    version="0.1.0",
    description="消息抽取、运营补全、定价、脱敏报价、客户匹配和审计。",
    lifespan=lifespan,
)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(enterprise_router)


@app.get("/")
def root() -> dict:
    return {"service": "b2b-distribution-agent", "docs": "/docs"}
