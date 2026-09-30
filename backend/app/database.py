from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True, "pool_recycle": 3600}


settings = get_settings()
engine = create_engine(settings.database_url, **_engine_kwargs(settings.database_url))
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_schema() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    install_audit_guards()


def install_audit_guards() -> None:
    """Enforce append-only audit rows at the database layer for dev-created schemas.

    Production schemas install the same guards through Alembic. This keeps local
    SQLite and direct ``create_all`` test environments aligned with that rule.
    """

    dialect = engine.dialect.name
    with engine.begin() as connection:
        if dialect == "sqlite":
            connection.execute(
                text(
                    """
                    CREATE TRIGGER IF NOT EXISTS audit_logs_no_update
                    BEFORE UPDATE ON audit_logs
                    BEGIN
                        SELECT RAISE(ABORT, 'audit_logs are append-only');
                    END
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TRIGGER IF NOT EXISTS audit_logs_no_delete
                    BEFORE DELETE ON audit_logs
                    BEGIN
                        SELECT RAISE(ABORT, 'audit_logs are append-only');
                    END
                    """
                )
            )
        elif dialect in {"mysql", "mariadb"}:
            schema = connection.execute(text("SELECT DATABASE()")).scalar_one()
            for trigger_name, operation in (
                ("audit_logs_no_update", "UPDATE"),
                ("audit_logs_no_delete", "DELETE"),
            ):
                exists = connection.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM information_schema.TRIGGERS
                        WHERE TRIGGER_SCHEMA = :schema AND TRIGGER_NAME = :name
                        """
                    ),
                    {"schema": schema, "name": trigger_name},
                ).scalar_one()
                if not exists:
                    connection.execute(
                        text(
                            f"""
                            CREATE TRIGGER {trigger_name}
                            BEFORE {operation} ON audit_logs
                            FOR EACH ROW
                            SIGNAL SQLSTATE '45000'
                            SET MESSAGE_TEXT = 'audit_logs are append-only'
                            """
                        )
                    )
