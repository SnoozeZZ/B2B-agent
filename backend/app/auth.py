from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .database import get_db
from .models import User


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    user_id: int | None = None


password_hash = PasswordHash.recommended()
bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    try:
        return password_hash.verify(password, encoded)
    except (TypeError, ValueError):
        return False


def create_access_token(user: User, settings: Settings | None = None) -> tuple[str, int]:
    settings = settings or get_settings()
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=settings.jwt_expire_minutes)
    token = jwt.encode(
        {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role,
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    return token, int((expires - now).total_seconds())


def authenticate_user(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.active or not verify_password(password, user.password_hash):
        return None
    user.last_login_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)
    return user


def bootstrap_admin(db: Session, settings: Settings | None = None) -> User | None:
    settings = settings or get_settings()
    if not settings.bootstrap_admin_username or not settings.bootstrap_admin_password:
        return None
    existing = db.scalar(select(User).where(User.username == settings.bootstrap_admin_username))
    if existing:
        return existing
    if len(settings.bootstrap_admin_password) < 10:
        raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD 至少需要 10 个字符")
    user = User(
        username=settings.bootstrap_admin_username,
        display_name=settings.bootstrap_admin_display_name,
        password_hash=hash_password(settings.bootstrap_admin_password),
        role="admin",
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def validate_auth_settings(settings: Settings) -> None:
    if settings.app_env == "production" and settings.auth_disabled:
        raise RuntimeError("生产环境不能启用 AUTH_DISABLED")
    if settings.jwt_algorithm not in {"HS256", "HS384", "HS512"}:
        raise RuntimeError("JWT_ALGORITHM 仅支持 HS256、HS384 或 HS512")
    if (
        settings.app_env == "production"
        and not settings.auth_disabled
        and (settings.jwt_secret == "development-only-change-me" or len(settings.jwt_secret) < 32)
    ):
        raise RuntimeError("生产环境必须设置长度不少于 32 位的 JWT_SECRET")


def current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> Principal:
    settings = get_settings()
    if settings.auth_disabled:
        return Principal(name="local-admin", role="admin")
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少访问令牌")

    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user_id = int(payload["sub"])
        user = db.get(User, user_id)
        if user is None or not user.active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已停用")
        return Principal(name=user.username, role=user.role, user_id=user.id)
    except (InvalidTokenError, KeyError, TypeError, ValueError):
        role = settings.api_tokens.get(token)
        if role is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="访问令牌无效")
        return Principal(name=f"token:{token[:6]}", role=role)


def require_roles(*roles: str):
    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if principal.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="权限不足")
        return principal

    return dependency
