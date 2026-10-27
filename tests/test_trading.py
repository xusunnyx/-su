"""交易正确性：余额、状态机、复式分录、退款。"""

from __future__ import annotations

from sqlalchemy import select

from app.models import Account
from app.services.ledger import order_debit_credit, recompute_balance
from helpers import create_customer, create_order, deposit, load_tenant, pay, setup_funded_customer


def test_pay_without_funds_returns_402_and_keeps_order_payable(client, tenant):
    customer = create_customer(client, tenant["headers"])
    order = create_order(client, tenant["headers"], customer["id"], "500.00", key="k-402-0001")

    rejected = client.post(
        f"/api/v1/orders/{order['order_no']}/pay", headers=tenant["headers"]
    )
    assert rejected.status_code == 402
    assert rejected.json()["code"] == "INSUFFICIENT_FUNDS"

    # 关键：扣款失败必须整体回滚，订单不能卡在 PAID
    still_pending = client.get(
        f"/api/v1/orders/{order['order_no']}", headers=tenant["headers"]
    ).json()
    assert still_pending["status"] == "PENDING_PAYMENT"

    deposit(client, tenant["headers"], customer["id"], "800.00")
    paid = client.post(
        f"/api/v1/orders/{order['order_no']}/pay", headers=tenant["headers"]
    )
    assert paid.status_code == 200
    assert paid.json()["status"] == "PAID"


def test_pay_writes_balanced_double_entry(client, tenant, state):
    customer = setup_funded_customer(client, tenant["headers"], balance="1000.00")
    order = create_order(client, tenant["headers"], customer["id"], "199.99", key="k-ledger")
    pay(client, tenant["headers"], order["order_no"])

    ledger = client.get(
        f"/api/v1/orders/{order['order_no']}/ledger", headers=tenant["headers"]
    ).json()
    assert len(ledger) == 2
    debit = sum(e["amount_minor"] for e in ledger if e["direction"] == "DEBIT")
    credit = sum(e["amount_minor"] for e in ledger if e["direction"] == "CREDIT")
    assert debit == credit == 19999
    assert {e["account_code"] for e in ledger} == {"PREPAID", "REVENUE"}

    tenant_obj = load_tenant(state, "acme")
    with state.db.session(tenant_obj) as session:
        assert order_debit_credit(session, order_id=order["id"]) == (19999, 19999)

    account = client.get(
        f"/api/v1/customers/{customer['id']}/account", headers=tenant["headers"]
    ).json()
    assert account["balance_minor"] == 100000 - 19999
    assert account["consistent"] is True
    assert account["recomputed_balance_minor"] == account["balance_minor"]


def test_pay_twice_is_rejected_by_state_machine(client, tenant):
    customer = setup_funded_customer(client, tenant["headers"])
    order = create_order(client, tenant["headers"], customer["id"], "10.00", key="k-twice-01")
    pay(client, tenant["headers"], order["order_no"])

    again = client.post(
        f"/api/v1/orders/{order['order_no']}/pay", headers=tenant["headers"]
    )
    assert again.status_code == 409
    assert again.json()["code"] == "INVALID_ORDER_STATE"

    account = client.get(
        f"/api/v1/customers/{customer['id']}/account", headers=tenant["headers"]
    ).json()
    assert account["balance_minor"] == 100000 - 1000  # 只扣了一次


def test_refund_restores_balance_and_reverses_ledger(client, tenant, state):
    customer = setup_funded_customer(client, tenant["headers"], balance="500.00")
    order = create_order(client, tenant["headers"], customer["id"], "80.00", key="k-refund")
    pay(client, tenant["headers"], order["order_no"])

    refunded = client.post(
        f"/api/v1/orders/{order['order_no']}/refund", headers=tenant["headers"]
    )
    assert refunded.status_code == 200
    assert refunded.json()["status"] == "REFUNDED"

    ledger = client.get(
        f"/api/v1/orders/{order['order_no']}/ledger", headers=tenant["headers"]
    ).json()
    assert len(ledger) == 4

    account = client.get(
        f"/api/v1/customers/{customer['id']}/account", headers=tenant["headers"]
    ).json()
    assert account["balance_minor"] == 50000
    assert account["consistent"] is True


def test_refund_twice_is_rejected(client, tenant):
    customer = setup_funded_customer(client, tenant["headers"])
    order = create_order(client, tenant["headers"], customer["id"], "20.00", key="k-r2-0001")
    pay(client, tenant["headers"], order["order_no"])
    client.post(f"/api/v1/orders/{order['order_no']}/refund", headers=tenant["headers"])

    again = client.post(
        f"/api/v1/orders/{order['order_no']}/refund", headers=tenant["headers"]
    )
    assert again.status_code == 409


def test_refund_unpaid_order_is_rejected(client, tenant):
    customer = setup_funded_customer(client, tenant["headers"])
    order = create_order(client, tenant["headers"], customer["id"], "20.00", key="k-r3-0001")
    response = client.post(
        f"/api/v1/orders/{order['order_no']}/refund", headers=tenant["headers"]
    )
    assert response.status_code == 409
    assert response.json()["code"] == "INVALID_ORDER_STATE"


def test_amount_precision_is_rejected(client, tenant):
    customer = create_customer(client, tenant["headers"])
    response = client.post(
        "/api/v1/orders",
        json={"customer_id": customer["id"], "amount": "10.001"},
        headers={**tenant["headers"], "Idempotency-Key": "k-precision"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_AMOUNT"


def test_balance_never_goes_negative(client, tenant, state):
    customer = setup_funded_customer(client, tenant["headers"], balance="100.00")
    order = create_order(client, tenant["headers"], customer["id"], "150.00", key="k-neg-0001")
    response = client.post(
        f"/api/v1/orders/{order['order_no']}/pay", headers=tenant["headers"]
    )
    assert response.status_code == 402

    tenant_obj = load_tenant(state, "acme")
    with state.db.session(tenant_obj) as session:
        account = session.execute(
            select(Account).where(Account.customer_id == customer["id"])
        ).scalar_one()
        assert account.balance_minor == 10000
        assert recompute_balance(
            session,
            account_code="PREPAID",
            customer_id=customer["id"],
            currency="USD",
        ) == 10000
