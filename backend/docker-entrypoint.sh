#!/bin/sh
set -eu

is_true() {
    case "${1:-}" in
        1|true|TRUE|yes|YES|on|ON) return 0 ;;
        *) return 1 ;;
    esac
}

if is_true "${BUILD_DATABASE_URL_FROM_MYSQL:-false}"; then
    : "${MYSQL_HOST:?MYSQL_HOST is required}"
    : "${MYSQL_PORT:=3306}"
    : "${MYSQL_USER:?MYSQL_USER is required}"
    : "${MYSQL_PASSWORD:?MYSQL_PASSWORD is required}"
    : "${MYSQL_DATABASE:?MYSQL_DATABASE is required}"
    DATABASE_URL="$(python - <<'PY'
import os
from sqlalchemy import URL

url = URL.create(
    "mysql+pymysql",
    username=os.environ["MYSQL_USER"],
    password=os.environ["MYSQL_PASSWORD"],
    host=os.environ["MYSQL_HOST"],
    port=int(os.environ.get("MYSQL_PORT", "3306")),
    database=os.environ["MYSQL_DATABASE"],
    query={"charset": "utf8mb4"},
)
print(url.render_as_string(hide_password=False))
PY
    )"
    export DATABASE_URL
fi

if is_true "${RUN_MIGRATIONS:-true}"; then
    python - <<'PY'
import os
import sys
import time

from sqlalchemy import create_engine, text

url = os.environ.get("DATABASE_URL", "sqlite:///./b2b_agent.db")
kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {
    "pool_pre_ping": True
}
engine = create_engine(url, **kwargs)
last_error = None
for attempt in range(1, 31):
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        sys.exit(0)
    except Exception as exc:  # container startup boundary
        last_error = exc
        print(f"Database is not ready (attempt {attempt}/30); retrying...", file=sys.stderr)
        time.sleep(2)
raise SystemExit(f"Database did not become ready: {type(last_error).__name__}")
PY

    migration_state="$(python - <<'PY'
import os

from sqlalchemy import create_engine, inspect, text

url = os.environ.get("DATABASE_URL", "sqlite:///./b2b_agent.db")
kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
engine = create_engine(url, **kwargs)
inspector = inspect(engine)
tables = set(inspector.get_table_names())
original = {
    "raw_messages",
    "inquiries",
    "quotes",
    "customers",
    "customer_brand_needs",
    "ops_tasks",
    "distributions",
    "audit_logs",
}
version = None
if "alembic_version" in tables:
    with engine.connect() as connection:
        version = connection.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
business_tables = tables - {"alembic_version"}
if version:
    print("managed")
elif not business_tables:
    print("fresh")
elif original.issubset(business_tables):
    print("legacy")
else:
    unknown = ", ".join(sorted(business_tables)) or "(none)"
    raise SystemExit(
        "Refusing to migrate a partially initialized database without an Alembic version. "
        f"Found tables: {unknown}"
    )
PY
    )"

    if [ "$migration_state" = "legacy" ]; then
        echo "Detected an unversioned installation; stamping the original schema at 0001."
        alembic stamp 0001
    fi
    alembic upgrade head
fi

exec "$@"
