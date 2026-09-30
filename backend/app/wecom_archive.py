from __future__ import annotations

import base64
import binascii
import ctypes
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .database import SessionLocal
from .models import (
    Inquiry,
    RawMessage,
    WecomArchiveCursor,
    WecomArchiveFailure,
)
from .schemas import InboundMessageCreate
from .services import audit, create_inquiry


class WecomArchiveError(RuntimeError):
    """Base error for Enterprise WeChat conversation archive ingestion."""


class WecomArchiveConfigurationError(WecomArchiveError):
    """Raised when the licensed archive integration is not configured correctly."""


class WecomArchiveSdkError(WecomArchiveError):
    """Raised when the native Finance SDK cannot fetch or decrypt data."""


class WecomArchiveNonReplayableError(WecomArchiveError):
    """Raised when an old or incomplete dead letter cannot be replayed."""


@dataclass(frozen=True)
class ArchiveEnvelope:
    seq: int
    msgid: str
    publickey_ver: int
    encrypt_random_key: str
    encrypt_chat_msg: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ArchiveEnvelope:
        try:
            seq = int(value["seq"])
            msgid = str(value["msgid"])
            publickey_ver = int(value["publickey_ver"])
            encrypt_random_key = str(value["encrypt_random_key"])
            encrypt_chat_msg = str(value["encrypt_chat_msg"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WecomArchiveSdkError("Finance SDK 返回了不完整的会话存档记录") from exc
        if seq < 0 or not msgid or not encrypt_random_key or not encrypt_chat_msg:
            raise WecomArchiveSdkError("Finance SDK 返回了无效的会话存档记录")
        return cls(
            seq=seq,
            msgid=msgid,
            publickey_ver=publickey_ver,
            encrypt_random_key=encrypt_random_key,
            encrypt_chat_msg=encrypt_chat_msg,
        )


class FinanceArchiveClient(Protocol):
    def fetch_chat_data(self, *, seq: int, limit: int) -> list[ArchiveEnvelope]: ...

    def decrypt_message(
        self, envelope: ArchiveEnvelope, *, private_key_pem: str
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        value = default
    value = max(minimum, value)
    return min(maximum, value) if maximum is not None else value


def _private_key_value(raw: str, *, variable_name: str) -> str:
    value = raw.strip()
    if value.startswith("file:"):
        path = Path(value.removeprefix("file:")).expanduser()
        try:
            value = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WecomArchiveConfigurationError(
                f"{variable_name} 指向的私钥文件不可读：{path}"
            ) from exc
    elif value.startswith("base64:"):
        try:
            value = base64.b64decode(value.removeprefix("base64:"), validate=True).decode()
        except (ValueError, UnicodeDecodeError) as exc:
            raise WecomArchiveConfigurationError(f"{variable_name} 不是有效的 base64 私钥") from exc
    return value.replace("\\n", "\n").strip()


def _load_private_keys() -> tuple[dict[int, str], str | None]:
    keys: dict[int, str] = {}
    raw_mapping = os.getenv("WECOM_ARCHIVE_PRIVATE_KEYS_JSON")
    if raw_mapping:
        try:
            parsed = json.loads(raw_mapping)
        except json.JSONDecodeError as exc:
            raise WecomArchiveConfigurationError(
                "WECOM_ARCHIVE_PRIVATE_KEYS_JSON 必须是版本号到私钥的 JSON 对象"
            ) from exc
        if not isinstance(parsed, dict):
            raise WecomArchiveConfigurationError("WECOM_ARCHIVE_PRIVATE_KEYS_JSON 必须是 JSON 对象")
        for version, value in parsed.items():
            try:
                key_version = int(version)
            except (TypeError, ValueError) as exc:
                raise WecomArchiveConfigurationError(
                    f"无效的企业微信公钥版本号：{version}"
                ) from exc
            if not isinstance(value, str):
                raise WecomArchiveConfigurationError(
                    f"WECOM_ARCHIVE_PRIVATE_KEYS_JSON[{version}] 必须是字符串"
                )
            keys[key_version] = _private_key_value(
                value, variable_name=f"WECOM_ARCHIVE_PRIVATE_KEYS_JSON[{version}]"
            )

    raw_default = os.getenv("WECOM_ARCHIVE_PRIVATE_KEY")
    key_file = os.getenv("WECOM_ARCHIVE_PRIVATE_KEY_FILE")
    if raw_default and key_file:
        raise WecomArchiveConfigurationError(
            "WECOM_ARCHIVE_PRIVATE_KEY 与 WECOM_ARCHIVE_PRIVATE_KEY_FILE 只能配置一个"
        )
    if key_file:
        raw_default = f"file:{key_file}"
    default_key = (
        _private_key_value(raw_default, variable_name="WECOM_ARCHIVE_PRIVATE_KEY")
        if raw_default
        else None
    )
    version = os.getenv("WECOM_ARCHIVE_PRIVATE_KEY_VERSION")
    if default_key and version:
        try:
            keys[int(version)] = default_key
        except ValueError as exc:
            raise WecomArchiveConfigurationError(
                "WECOM_ARCHIVE_PRIVATE_KEY_VERSION 必须是整数"
            ) from exc
        default_key = None
    return keys, default_key


@dataclass(frozen=True)
class WecomArchiveConfig:
    enabled: bool
    source_id: str
    corp_id: str | None
    secret: str | None
    sdk_library_path: Path
    private_keys: Mapping[int, str] = field(default_factory=dict)
    default_private_key: str | None = None
    batch_size: int = 100
    timeout_seconds: int = 5
    max_pages_per_poll: int = 10
    poison_max_attempts: int = 3
    lease_seconds: int = 300
    proxy: str = ""
    proxy_password: str = ""
    room_ids: frozenset[str] = field(default_factory=frozenset)
    external_only: bool = True
    filter_candidates: bool = True
    trigger_prefix: str | None = None

    @classmethod
    def from_env(cls) -> WecomArchiveConfig:
        keys, default_key = _load_private_keys()
        room_ids = frozenset(
            item.strip()
            for item in os.getenv("WECOM_ARCHIVE_ROOM_IDS", "").split(",")
            if item.strip()
        )
        return cls(
            enabled=_env_bool("WECOM_ARCHIVE_ENABLED", False),
            source_id=os.getenv("WECOM_ARCHIVE_SOURCE_ID", "default").strip() or "default",
            corp_id=os.getenv("WECOM_ARCHIVE_CORP_ID") or os.getenv("WECOM_CORP_ID") or None,
            secret=os.getenv("WECOM_ARCHIVE_SECRET") or None,
            sdk_library_path=Path(
                os.getenv(
                    "WECOM_ARCHIVE_SDK_LIBRARY_PATH",
                    "/opt/wecom-finance-sdk/libWeWorkFinanceSdk_C.so",
                )
            ),
            private_keys=keys,
            default_private_key=default_key,
            batch_size=_env_int("WECOM_ARCHIVE_BATCH_SIZE", 100, minimum=1, maximum=1000),
            timeout_seconds=_env_int("WECOM_ARCHIVE_TIMEOUT_SECONDS", 5, minimum=1, maximum=120),
            max_pages_per_poll=_env_int(
                "WECOM_ARCHIVE_MAX_PAGES_PER_POLL", 10, minimum=1, maximum=100
            ),
            poison_max_attempts=_env_int(
                "WECOM_ARCHIVE_POISON_MAX_ATTEMPTS", 3, minimum=1, maximum=100
            ),
            lease_seconds=_env_int("WECOM_ARCHIVE_LEASE_SECONDS", 300, minimum=30),
            proxy=os.getenv("WECOM_ARCHIVE_PROXY", "").strip(),
            proxy_password=os.getenv("WECOM_ARCHIVE_PROXY_PASSWORD", "").strip(),
            room_ids=room_ids,
            external_only=_env_bool("WECOM_ARCHIVE_EXTERNAL_ONLY", True),
            filter_candidates=_env_bool("WECOM_ARCHIVE_FILTER_CANDIDATES", True),
            trigger_prefix=os.getenv("WECOM_ARCHIVE_TRIGGER_PREFIX") or None,
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        missing: list[str] = []
        if not self.corp_id:
            missing.append("WECOM_ARCHIVE_CORP_ID（或 WECOM_CORP_ID）")
        if not self.secret:
            missing.append("WECOM_ARCHIVE_SECRET")
        if not self.private_keys and not self.default_private_key:
            missing.append(
                "WECOM_ARCHIVE_PRIVATE_KEY、WECOM_ARCHIVE_PRIVATE_KEY_FILE 或 "
                "WECOM_ARCHIVE_PRIVATE_KEYS_JSON"
            )
        if missing:
            raise WecomArchiveConfigurationError(
                "企业微信会话内容存档缺少配置：" + "、".join(missing)
            )
        if not self.sdk_library_path.is_file():
            raise WecomArchiveConfigurationError(
                "未找到企业微信官方 Finance SDK 动态库："
                f"{self.sdk_library_path}；请下载与容器架构匹配的官方 SDK 并挂载该文件"
            )

    def private_key_for(self, version: int) -> str:
        key = self.private_keys.get(version) or self.default_private_key
        if not key:
            raise WecomArchiveConfigurationError(
                f"会话消息使用 publickey_ver={version}，但没有配置对应的 RSA 私钥"
            )
        return key


class CtypesFinanceArchiveClient:
    """Thin wrapper around the official ``libWeWorkFinanceSdk_C`` library."""

    def __init__(self, config: WecomArchiveConfig):
        config.validate()
        self._config = config
        try:
            self._lib = ctypes.CDLL(str(config.sdk_library_path))
        except OSError as exc:
            raise WecomArchiveConfigurationError(
                "企业微信 Finance SDK 动态库加载失败；请确认系统架构和依赖库匹配："
                f"{config.sdk_library_path}: {exc}"
            ) from exc
        try:
            self._configure_signatures()
            self._sdk = self._lib.NewSdk()
        except AttributeError as exc:
            raise WecomArchiveConfigurationError(
                "Finance SDK 动态库缺少所需的 C 接口，请使用企业微信官方 C SDK"
            ) from exc
        if not self._sdk:
            raise WecomArchiveConfigurationError("Finance SDK NewSdk 初始化失败")
        result = self._lib.Init(
            self._sdk,
            str(config.corp_id).encode(),
            str(config.secret).encode(),
        )
        if result != 0:
            self._lib.DestroySdk(self._sdk)
            self._sdk = None
            raise WecomArchiveConfigurationError(
                f"Finance SDK Init 失败（错误码 {result}）；请检查存档专用 Secret 和 Corp ID"
            )

    def _configure_signatures(self) -> None:
        self._lib.NewSdk.argtypes = []
        self._lib.NewSdk.restype = ctypes.c_void_p
        self._lib.Init.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        self._lib.Init.restype = ctypes.c_int
        self._lib.DestroySdk.argtypes = [ctypes.c_void_p]
        self._lib.DestroySdk.restype = None
        self._lib.NewSlice.argtypes = []
        self._lib.NewSlice.restype = ctypes.c_void_p
        self._lib.FreeSlice.argtypes = [ctypes.c_void_p]
        self._lib.FreeSlice.restype = None
        self._lib.GetContentFromSlice.argtypes = [ctypes.c_void_p]
        self._lib.GetContentFromSlice.restype = ctypes.c_char_p
        self._lib.GetChatData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulonglong,
            ctypes.c_uint,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        self._lib.GetChatData.restype = ctypes.c_int
        self._lib.DecryptData.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p]
        self._lib.DecryptData.restype = ctypes.c_int

    def _read_slice(self, slice_pointer: int, *, operation: str) -> dict[str, Any]:
        content = self._lib.GetContentFromSlice(slice_pointer)
        if not content:
            raise WecomArchiveSdkError(f"Finance SDK {operation} 返回空数据")
        try:
            decoded = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WecomArchiveSdkError(f"Finance SDK {operation} 返回的不是有效 JSON") from exc
        if not isinstance(decoded, dict):
            raise WecomArchiveSdkError(f"Finance SDK {operation} 返回格式错误")
        return decoded

    def fetch_chat_data(self, *, seq: int, limit: int) -> list[ArchiveEnvelope]:
        result_slice = self._lib.NewSlice()
        if not result_slice:
            raise WecomArchiveSdkError("Finance SDK NewSlice 失败")
        try:
            result = self._lib.GetChatData(
                self._sdk,
                seq,
                limit,
                self._config.proxy.encode(),
                self._config.proxy_password.encode(),
                self._config.timeout_seconds,
                result_slice,
            )
            if result != 0:
                raise WecomArchiveSdkError(
                    f"Finance SDK GetChatData 失败（错误码 {result}，seq={seq}）"
                )
            payload = self._read_slice(result_slice, operation="GetChatData")
        finally:
            self._lib.FreeSlice(result_slice)
        if int(payload.get("errcode", 0) or 0) != 0:
            raise WecomArchiveSdkError(
                "企业微信返回会话存档错误："
                f"{payload.get('errcode')} {payload.get('errmsg', '')}".strip()
            )
        chatdata = payload.get("chatdata", [])
        if not isinstance(chatdata, list):
            raise WecomArchiveSdkError("Finance SDK chatdata 字段不是数组")
        return [ArchiveEnvelope.from_mapping(item) for item in chatdata]

    def decrypt_message(self, envelope: ArchiveEnvelope, *, private_key_pem: str) -> dict[str, Any]:
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
        except ImportError as exc:
            raise WecomArchiveConfigurationError(
                "缺少 cryptography 依赖，无法解密企业微信 encrypt_random_key"
            ) from exc
        try:
            private_key = serialization.load_pem_private_key(
                private_key_pem.encode("utf-8"), password=None
            )
        except Exception as exc:  # cryptography exposes several backend-specific errors.
            raise WecomArchiveConfigurationError(
                f"无法解析配置的 RSA 私钥 （publickey_ver={envelope.publickey_ver}）"
            ) from exc
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise WecomArchiveConfigurationError(
                f"企业微信会话存档私钥必须是 RSA 私钥 （publickey_ver={envelope.publickey_ver}）"
            )
        try:
            encrypted_key = base64.b64decode(envelope.encrypt_random_key, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WecomArchiveSdkError(
                "会话存档 encrypt_random_key 不是有效 Base64 "
                f"（seq={envelope.seq}，publickey_ver={envelope.publickey_ver}）"
            ) from exc
        try:
            random_key = private_key.decrypt(encrypted_key, padding.PKCS1v15())
        except Exception as exc:  # malformed envelope errors vary by crypto backend.
            raise WecomArchiveSdkError(
                "会话存档 encrypt_random_key RSA 解密失败 "
                f"（seq={envelope.seq}，publickey_ver={envelope.publickey_ver}）"
            ) from exc

        result_slice = self._lib.NewSlice()
        if not result_slice:
            raise WecomArchiveSdkError("Finance SDK NewSlice 失败")
        try:
            result = self._lib.DecryptData(
                random_key,
                envelope.encrypt_chat_msg.encode("utf-8"),
                result_slice,
            )
            if result != 0:
                raise WecomArchiveSdkError(
                    f"Finance SDK DecryptData 失败（错误码 {result}，seq={envelope.seq}）"
                )
            payload = self._read_slice(result_slice, operation="DecryptData")
        finally:
            self._lib.FreeSlice(result_slice)
        return payload

    def close(self) -> None:
        if getattr(self, "_sdk", None):
            self._lib.DestroySdk(self._sdk)
            self._sdk = None


_CANDIDATE_FIELDS = (
    re.compile(r"品牌\s*[:：]", re.IGNORECASE),
    re.compile(r"(?:SKU|货号|型号)\s*[:：]", re.IGNORECASE),
    re.compile(r"(?:采购价|价格|单价)\s*[:：]", re.IGNORECASE),
    re.compile(r"(?:MOQ|最小起订量|起订量)\s*[:：]", re.IGNORECASE),
    re.compile(r"(?:交货地|交付地|收货地)\s*[:：]", re.IGNORECASE),
    re.compile(r"(?:行业|区域|币种)\s*[:：]", re.IGNORECASE),
)


def inquiry_candidate_content(content: str, config: WecomArchiveConfig) -> str | None:
    text = content.strip()
    if not text:
        return None
    if config.trigger_prefix:
        if not text.startswith(config.trigger_prefix):
            return None
        text = text[len(config.trigger_prefix) :].lstrip(" ：:")
        return text or None
    if not config.filter_candidates:
        return text
    if "询价" in text or "报价" in text:
        return text
    matches = sum(bool(pattern.search(text)) for pattern in _CANDIDATE_FIELDS)
    return text if matches >= 2 else None


def _is_external_message(message: Mapping[str, Any]) -> bool:
    sender = str(message.get("from") or "")
    return sender.startswith(("wm", "wo"))


def _ensure_cursor(session_factory: sessionmaker[Session], source_id: str) -> None:
    with session_factory() as db:
        if db.get(WecomArchiveCursor, source_id) is not None:
            return
        db.add(WecomArchiveCursor(source_id=source_id, last_seq="0"))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()


def _claim_cursor(
    session_factory: sessionmaker[Session], config: WecomArchiveConfig
) -> tuple[str, int] | None:
    _ensure_cursor(session_factory, config.source_id)
    now = datetime.now(UTC)
    token = uuid.uuid4().hex
    with session_factory() as db:
        result = db.execute(
            update(WecomArchiveCursor)
            .where(
                WecomArchiveCursor.source_id == config.source_id,
                or_(
                    WecomArchiveCursor.lease_expires_at.is_(None),
                    WecomArchiveCursor.lease_expires_at <= now,
                ),
            )
            .values(
                lease_token=token,
                lease_expires_at=now + timedelta(seconds=config.lease_seconds),
                last_polled_at=now,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            db.rollback()
            return None
        db.commit()
        state = db.get(WecomArchiveCursor, config.source_id)
        if state is None:
            raise WecomArchiveError("会话存档游标创建后丢失")
        return token, int(state.last_seq)


def _advance_cursor(
    session_factory: sessionmaker[Session], config: WecomArchiveConfig, token: str, seq: int
) -> None:
    now = datetime.now(UTC)
    with session_factory() as db:
        result = db.execute(
            update(WecomArchiveCursor)
            .where(
                WecomArchiveCursor.source_id == config.source_id,
                WecomArchiveCursor.lease_token == token,
            )
            .values(
                last_seq=str(seq),
                lease_expires_at=now + timedelta(seconds=config.lease_seconds),
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            db.rollback()
            raise WecomArchiveError("企业微信会话存档轮询租约已丢失")
        db.commit()


def _release_cursor(
    session_factory: sessionmaker[Session],
    config: WecomArchiveConfig,
    token: str,
    *,
    error: str | None,
) -> None:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "lease_token": None,
        "lease_expires_at": None,
        "last_error": error[:4000] if error else None,
        "updated_at": now,
    }
    if error is None:
        values["last_success_at"] = now
    with session_factory() as db:
        db.execute(
            update(WecomArchiveCursor)
            .where(
                WecomArchiveCursor.source_id == config.source_id,
                WecomArchiveCursor.lease_token == token,
            )
            .values(**values)
        )
        db.commit()


def _stable_external_id(msgid: str) -> str:
    if len(msgid) <= 128:
        return msgid
    return "sha256:" + hashlib.sha256(msgid.encode("utf-8")).hexdigest()


def _ingest_message(
    session_factory: sessionmaker[Session],
    envelope: ArchiveEnvelope,
    message: Mapping[str, Any],
    content: str,
) -> bool:
    external_id = _stable_external_id(envelope.msgid)
    sender = str(message.get("from") or "unknown")[:128]
    room_id = str(message.get("roomid") or message.get("chatid") or "")
    metadata = {
        "type": "wecom_archive_context",
        "seq": str(envelope.seq),
        "msgid": envelope.msgid,
        "roomid": room_id,
        "chatid": room_id,
        "msgtime": message.get("msgtime"),
        "tolist": [str(item) for item in (message.get("tolist") or [])],
    }
    with session_factory() as db:
        existing = db.scalar(
            select(RawMessage.id).where(
                RawMessage.channel == "wecom_archive",
                RawMessage.external_id == external_id,
            )
        )
        if existing is not None:
            return False
        inbound = InboundMessageCreate(
            channel="wecom_archive",
            external_id=external_id,
            sender_name=sender,
            sender_contact=sender,
            content=content[:50_000],
            attachments=[metadata],
        )
        try:
            inquiry: Inquiry | None = create_inquiry(db, inbound, actor="wecom-archive-worker")
        except IntegrityError:
            db.rollback()
            duplicate = db.scalar(
                select(RawMessage.id).where(
                    RawMessage.channel == "wecom_archive",
                    RawMessage.external_id == external_id,
                )
            )
            if duplicate is not None:
                return False
            raise
        if inquiry is None:
            raise WecomArchiveError(f"消息 {external_id} 未能创建询价")
        return True


def _record_message_failure(
    session_factory: sessionmaker[Session],
    config: WecomArchiveConfig,
    envelope: ArchiveEnvelope,
    exc: Exception,
) -> tuple[int, bool]:
    now = datetime.now(UTC)
    error = f"{type(exc).__name__}: {exc}"[:4000]
    msgid = _stable_external_id(envelope.msgid)
    with session_factory() as db:
        failure = db.scalar(
            select(WecomArchiveFailure)
            .where(
                WecomArchiveFailure.source_id == config.source_id,
                WecomArchiveFailure.seq == str(envelope.seq),
            )
            .with_for_update()
        )
        if failure is not None and failure.status == "dead_letter":
            failure.publickey_ver = envelope.publickey_ver
            failure.encrypt_random_key = envelope.encrypt_random_key
            failure.encrypt_chat_msg = envelope.encrypt_chat_msg
            db.commit()
            return failure.attempt_count, True
        if failure is None:
            failure = WecomArchiveFailure(
                source_id=config.source_id,
                seq=str(envelope.seq),
                msgid=msgid,
                publickey_ver=envelope.publickey_ver,
                encrypt_random_key=envelope.encrypt_random_key,
                encrypt_chat_msg=envelope.encrypt_chat_msg,
                attempt_count=0,
                status="retrying",
                last_error=error,
                first_failed_at=now,
                last_failed_at=now,
            )
            db.add(failure)
        else:
            failure.publickey_ver = envelope.publickey_ver
            failure.encrypt_random_key = envelope.encrypt_random_key
            failure.encrypt_chat_msg = envelope.encrypt_chat_msg
        failure.attempt_count += 1
        failure.last_error = error
        failure.last_failed_at = now
        dead_lettered = failure.attempt_count >= config.poison_max_attempts
        if dead_lettered:
            failure.status = "dead_letter"
            failure.dead_lettered_at = now
            db.flush()
            audit(
                db,
                entity_type="wecom_archive_message",
                entity_id=msgid,
                action="dead_lettered",
                actor="wecom-archive-worker",
                payload={
                    "source_id": config.source_id,
                    "seq": str(envelope.seq),
                    "msgid": msgid,
                    "publickey_ver": envelope.publickey_ver,
                    "attempt_count": failure.attempt_count,
                    "error_type": type(exc).__name__,
                },
            )
        db.commit()
        return failure.attempt_count, dead_lettered


def _resolve_message_failure(
    session_factory: sessionmaker[Session], config: WecomArchiveConfig, seq: int
) -> None:
    now = datetime.now(UTC)
    with session_factory() as db:
        failure = db.scalar(
            select(WecomArchiveFailure).where(
                WecomArchiveFailure.source_id == config.source_id,
                WecomArchiveFailure.seq == str(seq),
                WecomArchiveFailure.status == "retrying",
            )
        )
        if failure is None:
            return
        failure.status = "recovered"
        failure.resolved_at = now
        db.commit()


def _mark_dead_letter_replayed(
    session_factory: sessionmaker[Session],
    failure_id: int,
    *,
    outcome: str,
) -> None:
    now = datetime.now(UTC)
    with session_factory() as db:
        failure = db.scalar(
            select(WecomArchiveFailure)
            .where(WecomArchiveFailure.id == failure_id)
            .with_for_update()
        )
        if failure is None:
            raise WecomArchiveNonReplayableError(f"企业微信存档死信 {failure_id} 不存在")
        failure.status = "replayed"
        failure.resolved_at = now
        audit(
            db,
            entity_type="wecom_archive_message",
            entity_id=failure.msgid,
            action="dead_letter_replayed",
            actor="wecom-archive-worker",
            payload={
                "failure_id": failure.id,
                "source_id": failure.source_id,
                "seq": failure.seq,
                "msgid": failure.msgid,
                "publickey_ver": failure.publickey_ver,
                "outcome": outcome,
            },
        )
        db.commit()


def _record_dead_letter_replay_failure(
    session_factory: sessionmaker[Session], failure_id: int, exc: Exception
) -> None:
    now = datetime.now(UTC)
    error = f"{type(exc).__name__}: {exc}"[:4000]
    with session_factory() as db:
        failure = db.scalar(
            select(WecomArchiveFailure)
            .where(WecomArchiveFailure.id == failure_id)
            .with_for_update()
        )
        if failure is None:
            return
        failure.attempt_count += 1
        failure.status = "dead_letter"
        failure.last_error = error
        failure.last_failed_at = now
        audit(
            db,
            entity_type="wecom_archive_message",
            entity_id=failure.msgid,
            action="dead_letter_replay_failed",
            actor="wecom-archive-worker",
            payload={
                "failure_id": failure.id,
                "source_id": failure.source_id,
                "seq": failure.seq,
                "msgid": failure.msgid,
                "publickey_ver": failure.publickey_ver,
                "attempt_count": failure.attempt_count,
                "error_type": type(exc).__name__,
            },
        )
        db.commit()


def _skip_reason(
    message: Mapping[str, Any],
    config: WecomArchiveConfig,
) -> tuple[str | None, str | None]:
    if str(message.get("action") or "send").lower() != "send":
        return "ignored_action", None
    if str(message.get("msgtype") or "").lower() != "text":
        return "ignored_non_text", None
    room_id = str(message.get("roomid") or message.get("chatid") or "")
    if not room_id:
        return "ignored_direct_message", None
    if config.room_ids and room_id not in config.room_ids:
        return "ignored_room", None
    if config.external_only and not _is_external_message(message):
        return "ignored_internal_sender", None
    text_payload = message.get("text")
    if not isinstance(text_payload, Mapping):
        return "ignored_invalid_text", None
    content = text_payload.get("content")
    if not isinstance(content, str):
        return "ignored_invalid_text", None
    candidate = inquiry_candidate_content(content, config)
    if candidate is None:
        return "ignored_not_candidate", None
    return None, candidate


ClientFactory = Callable[[WecomArchiveConfig], FinanceArchiveClient]


def replay_wecom_archive_dead_letter(
    failure_id: int,
    *,
    config: WecomArchiveConfig | None = None,
    session_factory: sessionmaker[Session] = SessionLocal,
    client_factory: ClientFactory = CtypesFinanceArchiveClient,
) -> dict[str, Any]:
    """Replay one retained encrypted dead letter after its cause is fixed."""

    config = config or WecomArchiveConfig.from_env()
    if not config.enabled:
        return {"status": "disabled", "failure_id": failure_id}
    config.validate()
    with session_factory() as db:
        failure = db.get(WecomArchiveFailure, failure_id)
        if failure is None:
            raise WecomArchiveNonReplayableError(f"企业微信存档死信 {failure_id} 不存在")
        if failure.source_id != config.source_id:
            raise WecomArchiveConfigurationError(
                f"死信来源 {failure.source_id} 与当前来源 {config.source_id} 不一致"
            )
        if failure.status == "replayed":
            return {"status": "replayed", "failure_id": failure_id, "skipped": True}
        if failure.status != "dead_letter":
            raise WecomArchiveNonReplayableError(
                f"死信 {failure_id} 当前状态为 {failure.status}，不能重放"
            )
        if (
            failure.publickey_ver is None
            or not failure.encrypt_random_key
            or not failure.encrypt_chat_msg
        ):
            raise WecomArchiveNonReplayableError(
                f"死信 {failure_id} 缺少 0004 版本保存的加密 envelope，无法重放"
            )
        envelope = ArchiveEnvelope(
            seq=int(failure.seq),
            msgid=failure.msgid,
            publickey_ver=failure.publickey_ver,
            encrypt_random_key=failure.encrypt_random_key,
            encrypt_chat_msg=failure.encrypt_chat_msg,
        )

    client: FinanceArchiveClient | None = None
    try:
        client = client_factory(config)
        message = client.decrypt_message(
            envelope,
            private_key_pem=config.private_key_for(envelope.publickey_ver),
        )
        reason, content = _skip_reason(message, config)
        if reason:
            outcome = reason
        elif content is None:
            outcome = "ignored_invalid_text"
        elif _ingest_message(session_factory, envelope, message, content):
            outcome = "created"
        else:
            outcome = "duplicate"
        _mark_dead_letter_replayed(session_factory, failure_id, outcome=outcome)
    except (WecomArchiveConfigurationError, WecomArchiveNonReplayableError):
        raise
    except Exception as exc:
        _record_dead_letter_replay_failure(session_factory, failure_id, exc)
        raise
    finally:
        if client is not None:
            client.close()
    return {
        "status": "replayed",
        "failure_id": failure_id,
        "source_id": config.source_id,
        "seq": str(envelope.seq),
        "msgid": envelope.msgid,
        "outcome": outcome,
    }


def poll_wecom_archive_once(
    *,
    config: WecomArchiveConfig | None = None,
    session_factory: sessionmaker[Session] = SessionLocal,
    client_factory: ClientFactory = CtypesFinanceArchiveClient,
) -> dict[str, Any]:
    """Fetch and ingest a bounded number of archived group text messages."""

    config = config or WecomArchiveConfig.from_env()
    if not config.enabled:
        return {"status": "disabled", "source_id": config.source_id}
    config.validate()
    claimed = _claim_cursor(session_factory, config)
    if claimed is None:
        return {"status": "busy", "source_id": config.source_id}
    token, cursor = claimed
    client: FinanceArchiveClient | None = None
    stats: dict[str, int] = {
        "fetched": 0,
        "created": 0,
        "duplicates": 0,
        "ignored_action": 0,
        "ignored_non_text": 0,
        "ignored_direct_message": 0,
        "ignored_room": 0,
        "ignored_internal_sender": 0,
        "ignored_invalid_text": 0,
        "ignored_not_candidate": 0,
        "dead_lettered": 0,
    }
    try:
        client = client_factory(config)
        for _ in range(config.max_pages_per_poll):
            records = client.fetch_chat_data(seq=cursor, limit=config.batch_size)
            if not records:
                break
            records.sort(key=lambda item: item.seq)
            new_records = [record for record in records if record.seq > cursor]
            if not new_records:
                break
            for envelope in new_records:
                stats["fetched"] += 1
                try:
                    message = client.decrypt_message(
                        envelope,
                        private_key_pem=config.private_key_for(envelope.publickey_ver),
                    )
                    reason, content = _skip_reason(message, config)
                    if reason:
                        stats[reason] += 1
                    elif content is not None:
                        if _ingest_message(session_factory, envelope, message, content):
                            stats["created"] += 1
                        else:
                            stats["duplicates"] += 1
                    _resolve_message_failure(session_factory, config, envelope.seq)
                except (WecomArchiveConfigurationError, OperationalError):
                    raise
                except Exception as exc:
                    _, dead_lettered = _record_message_failure(
                        session_factory, config, envelope, exc
                    )
                    if not dead_lettered:
                        raise
                    stats["dead_lettered"] += 1
                cursor = envelope.seq
                _advance_cursor(session_factory, config, token, cursor)
            if len(records) < config.batch_size:
                break
        _release_cursor(session_factory, config, token, error=None)
    except Exception as exc:
        _release_cursor(
            session_factory,
            config,
            token,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        if client is not None:
            client.close()
    return {
        "status": "ok",
        "source_id": config.source_id,
        "last_seq": str(cursor),
        **stats,
    }
