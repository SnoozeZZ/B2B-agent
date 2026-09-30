from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import (
    Customer,
    InventoryItem,
    InventoryMovement,
    Order,
    OrderItem,
    Payment,
    Quote,
    Shipment,
    ShipmentEvent,
)
from .schemas import (
    InventoryAdjust,
    OrderCreate,
    OrderPatch,
    PaymentCreate,
    ShipmentCreate,
    ShipmentEventCreate,
)
from .services import audit


def inventory_to_read(item: InventoryItem) -> dict:
    return {
        "id": item.id,
        "sku": item.sku,
        "brand": item.brand,
        "name": item.name,
        "on_hand": item.on_hand,
        "reserved": item.reserved,
        "available": item.on_hand - item.reserved,
        "reorder_point": item.reorder_point,
        "warehouse": item.warehouse,
        "updated_at": item.updated_at,
    }


def order_to_read(order: Order) -> dict:
    return {
        "id": order.id,
        "order_number": order.order_number,
        "customer_id": order.customer_id,
        "customer_name": order.customer.name,
        "quote_id": order.quote_id,
        "status": order.status,
        "payment_status": order.payment_status,
        "fulfillment_status": order.fulfillment_status,
        "currency": order.currency,
        "total_amount": order.total_amount,
        "shipping_address": order.shipping_address,
        "notes": order.notes,
        "items": [
            {
                "id": item.id,
                "sku": item.sku,
                "brand": item.brand,
                "description": item.description,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "line_total": item.line_total,
                "reserved_quantity": (
                    item.quantity
                    if order.status == "confirmed" and order.fulfillment_status != "fulfilled"
                    else 0
                ),
                "fulfilled_quantity": (
                    item.quantity if order.fulfillment_status == "fulfilled" else 0
                ),
            }
            for item in order.items
        ],
        "payments": [
            {
                "id": payment.id,
                "amount": payment.amount,
                "currency": payment.currency,
                "method": payment.method,
                "status": payment.status,
                "transaction_reference": payment.transaction_reference,
                "paid_at": payment.paid_at,
                "created_at": payment.created_at,
            }
            for payment in order.payments
        ],
        "shipments": [
            {
                "id": shipment.id,
                "carrier": shipment.carrier,
                "tracking_number": shipment.tracking_number,
                "status": shipment.status,
                "shipped_at": shipment.shipped_at,
                "delivered_at": shipment.delivered_at,
                "events": [
                    {
                        "id": event.id,
                        "status": event.status,
                        "location": event.location,
                        "description": event.description,
                        "occurred_at": event.occurred_at,
                    }
                    for event in shipment.events
                ],
            }
            for shipment in order.shipments
        ],
        "created_at": order.created_at,
        "updated_at": order.updated_at,
    }


def load_order(db: Session, order_id: int) -> Order | None:
    return db.scalar(
        select(Order)
        .options(
            selectinload(Order.items),
            selectinload(Order.payments),
            selectinload(Order.shipments).selectinload(Shipment.events),
        )
        .where(Order.id == order_id)
    )


def create_order(db: Session, data: OrderCreate, actor: str) -> Order:
    if db.get(Customer, data.customer_id) is None:
        raise ValueError("客户不存在")
    if data.quote_id is not None and db.get(Quote, data.quote_id) is None:
        raise ValueError("报价不存在")
    order = Order(
        order_number="pending",
        customer_id=data.customer_id,
        quote_id=data.quote_id,
        currency=data.currency,
        shipping_address=data.shipping_address,
        notes=data.notes,
        created_by=actor,
        total_amount=sum(
            (Decimal(item.unit_price) * item.quantity for item in data.items), Decimal(0)
        ),
    )
    order.items = [
        OrderItem(
            sku=item.sku,
            brand=item.brand,
            description=item.description,
            quantity=item.quantity,
            unit_price=item.unit_price,
            line_total=Decimal(item.unit_price) * item.quantity,
        )
        for item in data.items
    ]
    db.add(order)
    db.flush()
    order.order_number = f"ORD-{datetime.now(UTC):%Y%m%d}-{order.id:06d}"
    audit(
        db,
        entity_type="order",
        entity_id=order.id,
        action="created",
        actor=actor,
        payload={"order_number": order.order_number, "total_amount": order.total_amount},
    )
    db.commit()
    return load_order(db, order.id)  # type: ignore[return-value]


def adjust_inventory(db: Session, sku: str, data: InventoryAdjust, actor: str) -> InventoryItem:
    item = db.scalar(select(InventoryItem).where(InventoryItem.sku == sku).with_for_update())
    if item is None:
        if data.quantity < 0:
            raise ValueError("新库存不能以负数开始")
        item = InventoryItem(
            sku=sku,
            brand=data.brand,
            name=data.name,
            warehouse=data.warehouse,
            reorder_point=data.reorder_point or 0,
            on_hand=0,
            reserved=0,
        )
        db.add(item)
        db.flush()
    next_balance = item.on_hand + data.quantity
    if next_balance < item.reserved or next_balance < 0:
        raise ValueError("调整后库存不足以覆盖已预留数量")
    item.on_hand = next_balance
    if data.brand is not None:
        item.brand = data.brand
    if data.name is not None:
        item.name = data.name
    if data.warehouse is not None:
        item.warehouse = data.warehouse
    if data.reorder_point is not None:
        item.reorder_point = data.reorder_point
    db.add(
        InventoryMovement(
            inventory_item_id=item.id,
            movement_type=data.movement_type,
            quantity=data.quantity,
            balance_after=item.on_hand,
            note=data.note,
            actor=actor,
        )
    )
    audit(
        db,
        entity_type="inventory",
        entity_id=item.id,
        action="adjusted",
        actor=actor,
        payload={"sku": sku, "quantity": data.quantity, "balance": item.on_hand},
    )
    db.commit()
    db.refresh(item)
    return item


def _reserve_order_inventory(db: Session, order: Order, actor: str) -> None:
    locked: list[tuple[OrderItem, InventoryItem]] = []
    for order_item in order.items:
        inventory = db.scalar(
            select(InventoryItem)
            .where(InventoryItem.sku == order_item.sku)
            .with_for_update()
        )
        if inventory is None or inventory.on_hand - inventory.reserved < order_item.quantity:
            raise ValueError(f"SKU {order_item.sku} 可用库存不足")
        locked.append((order_item, inventory))
    for order_item, inventory in locked:
        inventory.reserved += order_item.quantity
        db.add(
            InventoryMovement(
                inventory_item_id=inventory.id,
                order_id=order.id,
                movement_type="reserve",
                quantity=order_item.quantity,
                balance_after=inventory.on_hand,
                note=f"订单 {order.order_number} 预留",
                actor=actor,
            )
        )


def _release_order_inventory(db: Session, order: Order, actor: str) -> None:
    for order_item in order.items:
        inventory = db.scalar(
            select(InventoryItem)
            .where(InventoryItem.sku == order_item.sku)
            .with_for_update()
        )
        if inventory is None:
            continue
        released = min(inventory.reserved, order_item.quantity)
        inventory.reserved -= released
        db.add(
            InventoryMovement(
                inventory_item_id=inventory.id,
                order_id=order.id,
                movement_type="release",
                quantity=-released,
                balance_after=inventory.on_hand,
                note=f"订单 {order.order_number} 释放预留",
                actor=actor,
            )
        )


def patch_order(db: Session, order: Order, patch: OrderPatch, actor: str) -> Order:
    changes = patch.model_dump(exclude_unset=True)
    next_status = changes.pop("status", None)
    if next_status and next_status != order.status:
        allowed = {
            "draft": {"confirmed", "cancelled"},
            "confirmed": {"cancelled", "completed"},
            "completed": set(),
            "cancelled": set(),
        }
        if next_status not in allowed.get(order.status, set()):
            raise ValueError(f"订单状态不能从 {order.status} 变更为 {next_status}")
        if next_status == "completed" and (
            order.payment_status != "paid" or order.fulfillment_status != "fulfilled"
        ):
            raise ValueError("订单完成前必须全额付款且全部送达")
        if next_status == "cancelled" and order.fulfillment_status == "fulfilled":
            raise ValueError("已完成履约的订单不能取消")
        if next_status == "confirmed":
            _reserve_order_inventory(db, order, actor)
        elif next_status == "cancelled" and order.status == "confirmed":
            _release_order_inventory(db, order, actor)
        order.status = next_status
    for field, value in changes.items():
        setattr(order, field, value)
    audit(
        db,
        entity_type="order",
        entity_id=order.id,
        action="updated",
        actor=actor,
        payload=patch.model_dump(exclude_unset=True),
    )
    db.commit()
    return load_order(db, order.id)  # type: ignore[return-value]


def add_payment(db: Session, order: Order, data: PaymentCreate, actor: str) -> Order:
    payment = Payment(
        order_id=order.id,
        amount=data.amount,
        currency=(data.currency or order.currency).upper(),
        method=data.method,
        status=data.status,
        transaction_reference=data.transaction_reference,
        paid_at=datetime.now(UTC) if data.status == "paid" else None,
    )
    if payment.currency != order.currency:
        raise ValueError("付款币种必须与订单币种一致")
    db.add(payment)
    db.flush()
    payment_rows = [*order.payments, payment]
    paid_total = sum(
        (Decimal(item.amount) for item in payment_rows if item.status == "paid"), Decimal(0)
    ) - sum(
        (Decimal(item.amount) for item in payment_rows if item.status == "refunded"), Decimal(0)
    )
    paid_total = max(Decimal(0), paid_total)
    order.payment_status = (
        "paid" if paid_total >= Decimal(order.total_amount) else "partial" if paid_total > 0 else "unpaid"
    )
    if order.payment_status == "paid" and order.fulfillment_status == "fulfilled":
        order.status = "completed"
    audit(
        db,
        entity_type="payment",
        entity_id=payment.id,
        action="recorded",
        actor=actor,
        payload={"order_id": order.id, "amount": payment.amount, "status": payment.status},
    )
    db.commit()
    return load_order(db, order.id)  # type: ignore[return-value]


def add_shipment(db: Session, order: Order, data: ShipmentCreate, actor: str) -> Order:
    if order.status != "confirmed":
        raise ValueError("订单确认并完成库存预留后才能创建物流")
    now = datetime.now(UTC)
    shipment = Shipment(
        order_id=order.id,
        carrier=data.carrier,
        tracking_number=data.tracking_number,
        status=data.status,
        shipped_at=now if data.status in {"shipped", "in_transit", "delivered"} else None,
        delivered_at=now if data.status == "delivered" else None,
    )
    db.add(shipment)
    db.flush()
    db.add(ShipmentEvent(shipment_id=shipment.id, status=data.status, occurred_at=now))
    _recalculate_fulfillment(db, order, shipment, actor)
    audit(
        db,
        entity_type="shipment",
        entity_id=shipment.id,
        action="created",
        actor=actor,
        payload={"order_id": order.id, "carrier": data.carrier, "status": data.status},
    )
    db.commit()
    return load_order(db, order.id)  # type: ignore[return-value]


def add_shipment_event(
    db: Session, shipment: Shipment, data: ShipmentEventCreate, actor: str
) -> Order:
    occurred_at = data.occurred_at or datetime.now(UTC)
    db.add(
        ShipmentEvent(
            shipment_id=shipment.id,
            status=data.status,
            location=data.location,
            description=data.description,
            occurred_at=occurred_at,
        )
    )
    shipment.status = data.status
    if data.status in {"shipped", "in_transit", "delivered"} and shipment.shipped_at is None:
        shipment.shipped_at = occurred_at
    if data.status == "delivered":
        shipment.delivered_at = occurred_at
    order = shipment.order
    _recalculate_fulfillment(db, order, shipment, actor)
    audit(
        db,
        entity_type="shipment",
        entity_id=shipment.id,
        action="status_changed",
        actor=actor,
        payload={"status": data.status, "location": data.location},
    )
    db.commit()
    return load_order(db, order.id)  # type: ignore[return-value]


def _recalculate_fulfillment(
    db: Session, order: Order, changed_shipment: Shipment, actor: str
) -> None:
    inventory_already_committed = any(
        shipment.id != changed_shipment.id and shipment.inventory_committed
        for shipment in order.shipments
    )
    if (
        changed_shipment.status == "delivered"
        and not changed_shipment.inventory_committed
        and not inventory_already_committed
    ):
        for order_item in order.items:
            inventory = db.scalar(
                select(InventoryItem)
                .where(InventoryItem.sku == order_item.sku)
                .with_for_update()
            )
            if inventory is None:
                raise ValueError(f"SKU {order_item.sku} 不存在库存记录")
            if inventory.reserved < order_item.quantity or inventory.on_hand < order_item.quantity:
                raise ValueError(f"SKU {order_item.sku} 预留库存不足")
            inventory.reserved -= order_item.quantity
            inventory.on_hand -= order_item.quantity
            db.add(
                InventoryMovement(
                    inventory_item_id=inventory.id,
                    order_id=order.id,
                    movement_type="shipment",
                    quantity=-order_item.quantity,
                    balance_after=inventory.on_hand,
                    note=f"订单 {order.order_number} 出库",
                    actor=actor,
                )
            )
        changed_shipment.inventory_committed = True
    statuses = [item.status for item in order.shipments if item.id != changed_shipment.id]
    statuses.append(changed_shipment.status)
    if statuses and all(status == "delivered" for status in statuses):
        order.fulfillment_status = "fulfilled"
        if order.payment_status == "paid":
            order.status = "completed"
    elif any(status in {"shipped", "in_transit", "delivered"} for status in statuses):
        order.fulfillment_status = "in_progress"
