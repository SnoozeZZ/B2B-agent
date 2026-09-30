import os
import tempfile
from io import BytesIO
from pathlib import Path

db_dir = tempfile.mkdtemp(prefix="b2b-agent-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(db_dir) / 'test.db'}"
os.environ["AUTH_DISABLED"] = "true"
os.environ["ENQUEUE_DISTRIBUTIONS"] = "false"
os.environ.pop("DASHSCOPE_API_KEY", None)

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.config import get_settings
from app.database import engine
from app.main import app


def test_end_to_end_inquiry_quote_match_and_distribution():
    with TestClient(app) as client:
        created = client.post(
            "/api/inquiries",
            json={
                "channel": "manual",
                "external_id": "msg-001",
                "sender_name": "上游供应商",
                "sender_contact": "supplier@example.com",
                "content": "品牌: Nike; SKU: DD123; 采购价: 100 USD; MOQ: 20; 交货地: 上海",
            },
        )
        assert created.status_code == 201, created.text
        inquiry = created.json()
        assert inquiry["status"] == "ready_to_quote"
        assert inquiry["missing_fields"] == []

        duplicate = client.post(
            "/api/inquiries",
            json={
                "channel": "manual",
                "external_id": "msg-001",
                "sender_name": "上游供应商",
                "content": "这条内容不会创建第二条记录",
            },
        )
        assert duplicate.json()["id"] == inquiry["id"]

        customer_response = client.post(
            "/api/customers",
            json={
                "name": "目标客户 A",
                "email": "buyer@example.com",
                "channel": "email",
                "brands": ["Nike"],
            },
        )
        assert customer_response.status_code == 201, customer_response.text
        customer = customer_response.json()

        quote_response = client.post(
            f"/api/inquiries/{inquiry['id']}/quotes",
            json={
                "scenario": "A",
                "output_currency": "CNY",
                "fx_rate": "7.2",
                "freight_per_unit": "5",
            },
        )
        assert quote_response.status_code == 200, quote_response.text
        quote = quote_response.json()
        assert quote["unit_price"] == "761.0000"
        assert quote["sensitive_scan_passed"] is True
        assert "purchase_price" not in quote["downstream_snapshot"]
        assert "sender_contact" not in quote["downstream_snapshot"]

        exported = client.get(f"/api/quotes/{quote['id']}/export.xlsx")
        assert exported.status_code == 200, exported.text
        assert "quote-" in exported.headers["content-disposition"]
        workbook = load_workbook(BytesIO(exported.content), data_only=True)
        values = " ".join(
            str(cell.value)
            for row in workbook.active.iter_rows()
            for cell in row
            if cell.value is not None
        )
        assert "supplier@example.com" not in values
        assert "采购价" not in values
        assert "报价单价" in values

        matches = client.get(f"/api/inquiries/{inquiry['id']}/matches")
        assert matches.status_code == 200
        assert matches.json()[0]["customer"]["id"] == customer["id"]

        distribution = client.post(
            f"/api/quotes/{quote['id']}/distributions",
            json={"customer_ids": [customer["id"]], "confirm": True},
        )
        assert distribution.status_code == 200, distribution.text
        assert distribution.json()[0]["status"] == "queued"
        assert distribution.json()[0]["confirmed_by"] == "local-admin"

        audit = client.get("/api/audit-logs")
        assert audit.status_code == 200
        actions = {item["action"] for item in audit.json()}
        assert {"created_and_extracted", "created", "distribution_created"} <= actions


def test_missing_fields_create_ops_task_and_block_quote():
    with TestClient(app) as client:
        response = client.post(
            "/api/inquiries",
            json={
                "channel": "manual",
                "external_id": "msg-missing",
                "sender_name": "零散消息供应商",
                "content": "有 Adidas 品牌，要吗？",
            },
        )
        inquiry = response.json()
        assert inquiry["status"] == "need_info"
        assert "sku" in inquiry["missing_fields"]

        blocked = client.post(
            f"/api/inquiries/{inquiry['id']}/quotes",
            json={"scenario": "A", "output_currency": "CNY", "fx_rate": "7.2"},
        )
        assert blocked.status_code == 409

        tasks = client.get("/api/ops-tasks").json()
        task = next(item for item in tasks if item["inquiry_id"] == inquiry["id"])
        assert task["task_type"] == "missing_fields"
        assert "请补充" in task["suggested_reply"]

        updated = client.patch(
            f"/api/inquiries/{inquiry['id']}",
            json={
                "brand": "Adidas",
                "sku": "AD-001",
                "purchase_price": "50",
                "currency": "USD",
                "moq": 10,
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["status"] == "ready_to_quote"

        refreshed_tasks = client.get("/api/ops-tasks").json()
        resolved = next(item for item in refreshed_tasks if item["id"] == task["id"])
        assert resolved["status"] == "resolved"


def test_account_login_and_authenticated_session(monkeypatch):
    username = "enterprise-admin"
    password = "Strong-Test-Password-2026"
    with TestClient(app) as client:
        created = client.post(
            "/api/users",
            json={
                "username": username,
                "display_name": "企业管理员",
                "password": password,
                "role": "admin",
            },
        )
        assert created.status_code == 201, created.text

        monkeypatch.setenv("AUTH_DISABLED", "false")
        monkeypatch.setenv("JWT_SECRET", "test-secret-that-is-long-enough-for-hs256")
        get_settings.cache_clear()
        logged_in = client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        assert logged_in.status_code == 200, logged_in.text
        token = logged_in.json()["access_token"]
        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200, me.text
        assert me.json()["username"] == username
        assert me.json()["role"] == "admin"

    get_settings.cache_clear()


def test_advanced_matching_and_order_fulfillment():
    with TestClient(app) as client:
        inquiry = client.post(
            "/api/inquiries",
            json={
                "external_id": "enterprise-match-1",
                "sender_name": "供应商",
                "content": "品牌: Nike; SKU: DD123; 采购价: 100 CNY; MOQ: 10; 交货地: 上海",
            },
        ).json()
        updated = client.patch(
            f"/api/inquiries/{inquiry['id']}",
            json={"industry": "运动零售", "target_region": "华东上海"},
        )
        assert updated.status_code == 200, updated.text

        customer = client.post(
            "/api/customers",
            json={
                "name": "华东运动客户",
                "email": "east@example.com",
                "channel": "email",
                "brands": ["Nike"],
                "industries": ["运动零售"],
                "regions": ["上海"],
                "sku_patterns": ["DD*"],
                "match_threshold": 70,
            },
        )
        assert customer.status_code == 201, customer.text
        customer_data = customer.json()

        matches = client.get(f"/api/inquiries/{inquiry['id']}/matches")
        assert matches.status_code == 200, matches.text
        matched = next(
            item for item in matches.json() if item["customer"]["id"] == customer_data["id"]
        )
        assert matched["score"] == 100
        assert set(matched["matched_fields"]) == {"brand", "sku", "industry", "region"}

        inventory = client.post(
            "/api/inventory/DD123/adjust",
            json={"quantity": 100, "movement_type": "receipt", "brand": "Nike"},
        )
        assert inventory.status_code == 200, inventory.text
        assert inventory.json()["available"] == 100

        order = client.post(
            "/api/orders",
            json={
                "customer_id": customer_data["id"],
                "currency": "CNY",
                "shipping_address": "上海市",
                "items": [{"sku": "DD123", "brand": "Nike", "quantity": 10, "unit_price": 20}],
            },
        )
        assert order.status_code == 201, order.text
        order_data = order.json()
        assert float(order_data["total_amount"]) == 200

        confirmed = client.patch(
            f"/api/orders/{order_data['id']}", json={"status": "confirmed"}
        )
        assert confirmed.status_code == 200, confirmed.text
        assert client.get("/api/inventory").json()[0]["reserved"] == 10

        paid = client.post(
            f"/api/orders/{order_data['id']}/payments",
            json={"amount": 200, "method": "bank_transfer", "status": "paid"},
        )
        assert paid.status_code == 200, paid.text
        assert paid.json()["payment_status"] == "paid"

        delivered = client.post(
            f"/api/orders/{order_data['id']}/shipments",
            json={"carrier": "顺丰", "tracking_number": "SF001", "status": "delivered"},
        )
        assert delivered.status_code == 200, delivered.text
        assert delivered.json()["fulfillment_status"] == "fulfilled"
        assert delivered.json()["status"] == "completed"
        final_inventory = next(
            item for item in client.get("/api/inventory").json() if item["sku"] == "DD123"
        )
        assert final_inventory["on_hand"] == 90
        assert final_inventory["reserved"] == 0

        verified = client.get("/api/audit-logs/verify")
        assert verified.status_code == 200, verified.text
        assert verified.json()["valid"] is True


def test_audit_rows_are_database_enforced_append_only():
    with TestClient(app) as client:
        created = client.post(
            "/api/inquiries",
            json={
                "external_id": "audit-guard-test",
                "sender_name": "审计测试",
                "content": "品牌: Puma; SKU: P-1; 采购价: 80 CNY; MOQ: 5",
            },
        )
        assert created.status_code == 201
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(text("UPDATE audit_logs SET actor = 'tampered'"))
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(text("DELETE FROM audit_logs"))
