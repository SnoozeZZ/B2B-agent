from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import AuditLog, Inquiry, RawMessage, WecomArchiveCursor, WecomArchiveFailure
from app.wecom_archive import (
    ArchiveEnvelope,
    CtypesFinanceArchiveClient,
    WecomArchiveConfig,
    WecomArchiveConfigurationError,
    WecomArchiveSdkError,
    inquiry_candidate_content,
    poll_wecom_archive_once,
    replay_wecom_archive_dead_letter,
)


def _database(tmp_path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'wecom-archive.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _config(tmp_path, **changes) -> WecomArchiveConfig:
    sdk_path = tmp_path / "libWeWorkFinanceSdk_C.so"
    sdk_path.write_bytes(b"test-placeholder")
    config = WecomArchiveConfig(
        enabled=True,
        source_id="test-source",
        corp_id="ww-test",
        secret="archive-secret",
        sdk_library_path=sdk_path,
        default_private_key="test-private-key",
        room_ids=frozenset({"room-allowed"}),
    )
    return replace(config, **changes)


def _envelope(seq: int, *, external: bool = True) -> ArchiveEnvelope:
    suffix = "_external" if external else ""
    return ArchiveEnvelope(
        seq=seq,
        msgid=f"message-{seq}{suffix}",
        publickey_ver=1,
        encrypt_random_key="encrypted-key",
        encrypt_chat_msg="encrypted-message",
    )


class FakeFinanceClient:
    def __init__(self, records, messages, *, fail_seq: int | None = None):
        self.records = list(records)
        self.messages = messages
        self.fail_seq = fail_seq
        self.fetch_sequences: list[int] = []
        self.closed = False

    def fetch_chat_data(self, *, seq: int, limit: int):
        self.fetch_sequences.append(seq)
        return [record for record in self.records if record.seq > seq][:limit]

    def decrypt_message(self, envelope, *, private_key_pem: str):
        assert private_key_pem == "test-private-key"
        if envelope.seq == self.fail_seq:
            raise WecomArchiveSdkError("temporary decrypt failure")
        return self.messages[envelope.seq]

    def close(self):
        self.closed = True


def _message(
    *,
    sender: str = "wm_external_customer",
    roomid: str = "room-allowed",
    msgtype: str = "text",
    content: str = "品牌：Nike\nSKU：DD123",
    action: str = "send",
):
    payload = {
        "action": action,
        "from": sender,
        "roomid": roomid,
        "msgtime": 1_700_000_000_000,
        "msgtype": msgtype,
        "tolist": ["employee-1"],
    }
    if msgtype == "text":
        payload["text"] = {"content": content}
    return payload


def test_candidate_filter_accepts_keyword_two_fields_or_prefix(tmp_path) -> None:
    config = _config(tmp_path)
    assert inquiry_candidate_content("请报价 Nike DD123", config) is not None
    assert inquiry_candidate_content("品牌：Nike\nSKU：DD123", config) is not None
    assert inquiry_candidate_content("品牌：Nike", config) is None
    assert inquiry_candidate_content("大家下午好", config) is None

    prefixed = replace(config, trigger_prefix="#询价")
    assert inquiry_candidate_content("#询价 品牌：Nike", prefixed) == "品牌：Nike"
    assert inquiry_candidate_content("品牌：Nike\nSKU：DD123", prefixed) is None


def test_poll_filters_messages_creates_inquiry_and_advances_cursor(tmp_path) -> None:
    factory = _database(tmp_path)
    # The _external suffix describes an external conversation, not the sender.
    # An employee in that room must still be rejected when external_only=true.
    records = [_envelope(seq) for seq in range(1, 8)]
    messages = {
        1: _message(roomid=""),
        2: _message(msgtype="image"),
        3: _message(sender="employee-1"),
        4: _message(content="今天天气不错"),
        5: _message(
            content=(
                "品牌：Nike\nSKU：DD123\n采购价：100 CNY\n"
                "MOQ：20\n行业：运动零售\n区域：上海\n交货地：上海"
            )
        ),
        6: _message(roomid="room-not-allowed"),
        7: _message(action="recall"),
    }
    client = FakeFinanceClient(records, messages)
    config = _config(tmp_path)

    result = poll_wecom_archive_once(
        config=config,
        session_factory=factory,
        client_factory=lambda _: client,
    )

    assert result["status"] == "ok"
    assert result["last_seq"] == "7"
    assert result["created"] == 1
    assert result["ignored_direct_message"] == 1
    assert result["ignored_non_text"] == 1
    assert result["ignored_internal_sender"] == 1
    assert result["ignored_not_candidate"] == 1
    assert result["ignored_room"] == 1
    assert result["ignored_action"] == 1
    assert client.closed is True

    with factory() as db:
        raw_messages = db.scalars(select(RawMessage)).all()
        inquiries = db.scalars(select(Inquiry)).all()
        cursor = db.get(WecomArchiveCursor, config.source_id)
        assert len(raw_messages) == len(inquiries) == 1
        assert raw_messages[0].channel == "wecom_archive"
        assert raw_messages[0].external_id == "message-5_external"
        assert raw_messages[0].attachments[0]["roomid"] == "room-allowed"
        assert raw_messages[0].attachments[0]["chatid"] == "room-allowed"
        assert raw_messages[0].attachments[0]["seq"] == "5"
        assert cursor is not None
        assert cursor.last_seq == "7"
        assert cursor.last_success_at is not None
        assert cursor.last_error is None

    replay = FakeFinanceClient(records, messages)
    replay_result = poll_wecom_archive_once(
        config=config,
        session_factory=factory,
        client_factory=lambda _: replay,
    )
    assert replay_result["created"] == 0
    assert replay.fetch_sequences == [7]
    with factory() as db:
        assert len(db.scalars(select(RawMessage)).all()) == 1


def test_internal_group_can_be_enabled_for_current_testing(tmp_path) -> None:
    factory = _database(tmp_path)
    config = _config(tmp_path, external_only=False)
    record = _envelope(1, external=False)
    client = FakeFinanceClient(
        [record],
        {1: _message(sender="employee-1", content="品牌：Nike\nSKU：DD123")},
    )

    result = poll_wecom_archive_once(
        config=config,
        session_factory=factory,
        client_factory=lambda _: client,
    )

    assert result["created"] == 1


def test_external_suffix_does_not_accept_employee_sender(tmp_path) -> None:
    factory = _database(tmp_path)
    config = _config(tmp_path, external_only=True)
    record = _envelope(1, external=True)
    client = FakeFinanceClient(
        [record],
        {1: _message(sender="employee-1", content="品牌：Nike\nSKU：DD123")},
    )

    result = poll_wecom_archive_once(
        config=config,
        session_factory=factory,
        client_factory=lambda _: client,
    )

    assert result["created"] == 0
    assert result["ignored_internal_sender"] == 1
    with factory() as db:
        assert db.scalar(select(Inquiry)) is None


def test_decrypt_failure_preserves_last_successful_sequence(tmp_path) -> None:
    factory = _database(tmp_path)
    config = _config(tmp_path)
    records = [_envelope(1), _envelope(2)]
    client = FakeFinanceClient(
        records,
        {
            1: _message(msgtype="image"),
            2: _message(content="品牌：Nike\nSKU：DD123"),
        },
        fail_seq=2,
    )

    with pytest.raises(WecomArchiveSdkError, match="temporary decrypt failure"):
        poll_wecom_archive_once(
            config=config,
            session_factory=factory,
            client_factory=lambda _: client,
        )

    with factory() as db:
        cursor = db.get(WecomArchiveCursor, config.source_id)
        assert cursor is not None
        assert cursor.last_seq == "1"
        assert "temporary decrypt failure" in str(cursor.last_error)
        assert cursor.lease_token is None
    assert client.closed is True


def test_enabled_archive_requires_licensed_sdk_credentials(tmp_path) -> None:
    config = WecomArchiveConfig(
        enabled=True,
        source_id="missing",
        corp_id=None,
        secret=None,
        sdk_library_path=tmp_path / "missing.so",
    )

    with pytest.raises(WecomArchiveConfigurationError, match="缺少配置"):
        config.validate()


def test_poison_message_is_dead_lettered_after_threshold(tmp_path) -> None:
    factory = _database(tmp_path)
    config = _config(tmp_path, poison_max_attempts=3)
    record = _envelope(1)
    messages = {1: _message(content="品牌：Nike\nSKU：DD123")}

    for _ in range(2):
        client = FakeFinanceClient([record], messages, fail_seq=1)
        with pytest.raises(WecomArchiveSdkError, match="temporary decrypt failure"):
            poll_wecom_archive_once(
                config=config,
                session_factory=factory,
                client_factory=lambda _, current=client: current,
            )

    final_client = FakeFinanceClient([record], messages, fail_seq=1)
    result = poll_wecom_archive_once(
        config=config,
        session_factory=factory,
        client_factory=lambda _: final_client,
    )

    assert result["status"] == "ok"
    assert result["last_seq"] == "1"
    assert result["dead_lettered"] == 1
    with factory() as db:
        failure = db.scalar(select(WecomArchiveFailure))
        cursor = db.get(WecomArchiveCursor, config.source_id)
        audit_log = db.scalar(select(AuditLog).where(AuditLog.action == "dead_lettered"))
        assert failure is not None
        assert failure.attempt_count == 3
        assert failure.status == "dead_letter"
        assert failure.dead_lettered_at is not None
        assert failure.publickey_ver == record.publickey_ver
        assert failure.encrypt_random_key == record.encrypt_random_key
        assert failure.encrypt_chat_msg == record.encrypt_chat_msg
        assert cursor is not None
        assert cursor.last_seq == "1"
        assert audit_log is not None
        assert audit_log.payload["seq"] == "1"
        assert record.encrypt_random_key not in json.dumps(audit_log.payload)
        assert record.encrypt_chat_msg not in json.dumps(audit_log.payload)
        failure_id = failure.id

    replay_client = FakeFinanceClient([record], messages)
    replayed = replay_wecom_archive_dead_letter(
        failure_id,
        config=config,
        session_factory=factory,
        client_factory=lambda _: replay_client,
    )
    assert replayed["status"] == "replayed"
    assert replayed["outcome"] == "created"
    assert replay_client.closed is True
    with factory() as db:
        failure = db.get(WecomArchiveFailure, failure_id)
        replay_audit = db.scalar(select(AuditLog).where(AuditLog.action == "dead_letter_replayed"))
        assert failure is not None
        assert failure.status == "replayed"
        assert failure.resolved_at is not None
        assert len(db.scalars(select(Inquiry)).all()) == 1
        assert replay_audit is not None
        serialized = json.dumps(replay_audit.payload)
        assert record.encrypt_random_key not in serialized
        assert record.encrypt_chat_msg not in serialized


def test_native_decrypt_classifies_key_config_and_envelope_failures() -> None:
    cryptography = pytest.importorskip("cryptography")
    assert cryptography is not None
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    client = object.__new__(CtypesFinanceArchiveClient)
    invalid_pem = _envelope(1)
    with pytest.raises(WecomArchiveConfigurationError, match="无法解析"):
        client.decrypt_message(invalid_pem, private_key_pem="not a PEM")

    ec_key = ec.generate_private_key(ec.SECP256R1())
    ec_pem = ec_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    with pytest.raises(WecomArchiveConfigurationError, match="必须是 RSA"):
        client.decrypt_message(invalid_pem, private_key_pem=ec_pem)

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    with pytest.raises(WecomArchiveSdkError, match="不是有效 Base64"):
        client.decrypt_message(invalid_pem, private_key_pem=rsa_pem)

    bad_cipher = replace(
        invalid_pem,
        # Valid Base64 but not a 2048-bit RSA ciphertext, so decrypt must fail.
        encrypt_random_key=base64.b64encode(b"x").decode(),
    )
    with pytest.raises(WecomArchiveSdkError, match="RSA 解密失败"):
        client.decrypt_message(bad_cipher, private_key_pem=rsa_pem)
