from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    cors_origins: tuple[str, ...]
    auth_disabled: bool
    api_tokens: dict[str, str]
    dashscope_api_key: str | None
    dashscope_base_url: str
    model_name: str
    model_timeout_seconds: float
    default_fx_rate: Decimal
    upload_dir: Path
    auto_create_schema: bool
    jwt_secret: str
    jwt_algorithm: str
    jwt_expire_minutes: int
    bootstrap_admin_username: str | None
    bootstrap_admin_password: str | None
    bootstrap_admin_display_name: str
    redis_url: str
    celery_broker_url: str
    celery_result_backend: str
    enqueue_distributions: bool
    smtp_host: str | None
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_from_email: str | None
    smtp_use_tls: bool
    smtp_use_ssl: bool
    wecom_corp_id: str | None
    wecom_agent_id: int | None
    wecom_secret: str | None
    wecom_aibot_id: str | None
    wecom_aibot_secret: str | None
    wecom_aibot_ws_url: str | None
    wecom_aibot_allowed_chat_ids: tuple[str, ...]
    wecom_aibot_accept_single_chat: bool
    wecom_aibot_auto_reply: bool
    feishu_app_id: str | None
    feishu_app_secret: str | None
    channel_http_timeout_seconds: float
    delivery_webhook_secret: str | None
    fx_api_url: str
    fx_sync_pairs: tuple[str, ...]


@lru_cache
def get_settings() -> Settings:
    app_env = os.getenv("APP_ENV", "development")
    raw_tokens = os.getenv(
        "API_TOKENS",
        '{"local-admin":"admin","local-operator":"operator","local-viewer":"viewer"}',
    )
    return Settings(
        app_env=app_env,
        database_url=os.getenv("DATABASE_URL", "sqlite:///./b2b_agent.db"),
        cors_origins=tuple(
            origin.strip()
            for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
            if origin.strip()
        ),
        auth_disabled=_as_bool(os.getenv("AUTH_DISABLED"), app_env != "production"),
        api_tokens=json.loads(raw_tokens),
        dashscope_api_key=os.getenv("DASHSCOPE_API_KEY") or None,
        dashscope_base_url=os.getenv(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ).rstrip("/"),
        model_name=os.getenv("MODEL_NAME", "qwen-plus"),
        model_timeout_seconds=float(os.getenv("MODEL_TIMEOUT_SECONDS", "30")),
        default_fx_rate=Decimal(os.getenv("DEFAULT_FX_RATE", "7.20")),
        upload_dir=Path(os.getenv("UPLOAD_DIR", "./uploads")),
        auto_create_schema=_as_bool(os.getenv("AUTO_CREATE_SCHEMA"), app_env != "production"),
        jwt_secret=os.getenv("JWT_SECRET", "development-only-change-me"),
        jwt_algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
        jwt_expire_minutes=int(os.getenv("JWT_EXPIRE_MINUTES", "480")),
        bootstrap_admin_username=os.getenv("BOOTSTRAP_ADMIN_USERNAME") or None,
        bootstrap_admin_password=os.getenv("BOOTSTRAP_ADMIN_PASSWORD") or None,
        bootstrap_admin_display_name=os.getenv("BOOTSTRAP_ADMIN_DISPLAY_NAME", "系统管理员"),
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        celery_broker_url=os.getenv("CELERY_BROKER_URL", os.getenv("REDIS_URL", "redis://redis:6379/0")),
        celery_result_backend=os.getenv(
            "CELERY_RESULT_BACKEND", os.getenv("REDIS_URL", "redis://redis:6379/1")
        ),
        enqueue_distributions=_as_bool(os.getenv("ENQUEUE_DISTRIBUTIONS"), True),
        smtp_host=os.getenv("SMTP_HOST") or None,
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_username=os.getenv("SMTP_USERNAME") or None,
        smtp_password=os.getenv("SMTP_PASSWORD") or None,
        smtp_from_email=os.getenv("SMTP_FROM_EMAIL") or None,
        smtp_use_tls=_as_bool(os.getenv("SMTP_USE_TLS"), True),
        smtp_use_ssl=_as_bool(os.getenv("SMTP_USE_SSL"), False),
        wecom_corp_id=os.getenv("WECOM_CORP_ID") or None,
        wecom_agent_id=int(os.getenv("WECOM_AGENT_ID")) if os.getenv("WECOM_AGENT_ID") else None,
        wecom_secret=os.getenv("WECOM_SECRET") or None,
        wecom_aibot_id=os.getenv("WECOM_AIBOT_ID") or None,
        wecom_aibot_secret=os.getenv("WECOM_AIBOT_SECRET") or None,
        wecom_aibot_ws_url=os.getenv("WECOM_AIBOT_WS_URL") or None,
        wecom_aibot_allowed_chat_ids=tuple(
            chat_id.strip()
            for chat_id in os.getenv("WECOM_AIBOT_ALLOWED_CHAT_IDS", "").split(",")
            if chat_id.strip()
        ),
        wecom_aibot_accept_single_chat=_as_bool(
            os.getenv("WECOM_AIBOT_ACCEPT_SINGLE_CHAT"), False
        ),
        wecom_aibot_auto_reply=_as_bool(os.getenv("WECOM_AIBOT_AUTO_REPLY"), True),
        feishu_app_id=os.getenv("FEISHU_APP_ID") or None,
        feishu_app_secret=os.getenv("FEISHU_APP_SECRET") or None,
        channel_http_timeout_seconds=float(os.getenv("CHANNEL_HTTP_TIMEOUT_SECONDS", "15")),
        delivery_webhook_secret=os.getenv("DELIVERY_WEBHOOK_SECRET") or None,
        fx_api_url=os.getenv(
            "FX_API_URL", "https://api.frankfurter.dev/v1/latest"
        ).rstrip("/"),
        fx_sync_pairs=tuple(
            pair.strip().upper()
            for pair in os.getenv("FX_SYNC_PAIRS", "USD/CNY,EUR/CNY,GBP/CNY").split(",")
            if pair.strip()
        ),
    )
