"""订单与资金的核心业务逻辑。

资金安全相关的三条铁律：

1. **状态迁移用条件更新（CAS）**，不靠锁、不靠“先查再改”——
   ``UPDATE ... WHERE status='PENDING_PAYMENT'`` 的 rowcount 才是并发下的裁判。
2. **扣款用条件更新**：``UPDATE ... WHERE balance >= amount``，
   余额永远不可能被扣成负数。
3. **每笔资金变动同事务写复式分录 + outbox 事件**，保证「钱」和「账」不脱节。
"""

from __future__ import annotations

import hashlib
import json
import secrets

from sqlalchemy import select, update

from ..errors import AppError, conflict, not_found, unprocessable
from ..idempotency import IdempotentOutcome, run_idempotent
from ..models import Account, Customer, LedgerEntry, Order, OutboxEvent, utcnow
from ..money import from_minor, to_minor
from ..schemas import OrderCreate, account_out, ledger_out, order_out
from ..state import AppState

OPEN_STATUSES = ("PAID", "SETTLED", "REFUNDED")


def fingerprint(payload: dict) -> str:  # 请求指纹函数
    """请求指纹：同一幂等键 + 不同请求体 = 客户端 bug，必须报错。"""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class OrderService:  # 订单服务类
    def __init__(self, state: AppState) -> None:
        self.state = state

    # ------------------------------------------------------------------ #
    # 下单
    # ------------------------------------------------------------------ #
    def create_order(  # 下单方法
        self, tenant, payload: OrderCreate, idempotency_key: str
    ) -> IdempotentOutcome:
        currency = (payload.currency or self.state.settings.currency).upper()
        try:  # 校验金额
            amount_minor = to_minor(payload.amount, currency)
        except ValueError as exc:
            raise unprocessable("INVALID_AMOUNT", str(exc)) from exc

        request_hash = fingerprint(  # 生成请求指纹
            {
                "op": "create_order",
                "customer_id": payload.customer_id,
                "amount_minor": amount_minor,
                "currency": currency,
                "description": payload.description,
            }
        )

        def _create(session) -> tuple[int, dict, str | None]:  # 下单事务函数
            customer = session.execute(
                select(Customer).where(
                    Customer.id == payload.customer_id, Customer.status == "ACTIVE"
                )
            ).scalar_one_or_none()
            if customer is None:
                raise not_found("客户", payload.customer_id)

            order_no = f"AD{utcnow():%Y%m%d}{secrets.token_hex(3).upper()}"
            order = Order(  # 创建订单
                tenant_id=tenant.id,
                order_no=order_no,
                customer_id=customer.id,
                currency=currency,
                amount_minor=amount_minor,
                description=payload.description,
                idempotency_key=idempotency_key,
            )
            session.add(order)
            session.flush()
            return 201, order_out(order), order_no

        return run_idempotent(  # 下单幂等函数
            self.state.db,
            tenant,
            idempotency_key,
            request_hash,
            _create,
            ttl_seconds=self.state.settings.idempotency_ttl,
        )

    # ------------------------------------------------------------------ #
    # 支付
    # ------------------------------------------------------------------ #
    def pay_order(  # 支付方法
        self, tenant, order_no: str, idempotency_key: str | None = None
    ) -> IdempotentOutcome:
        def _pay(session) -> tuple[int, dict, str | None]:  # 支付事务函数
            order = session.execute(
                select(Order).where(Order.order_no == order_no)
            ).scalar_one_or_none()
            if order is None:
                raise not_found("订单", order_no)

            # 1) 状态 CAS：并发重复支付只有一个能成功
            moved = session.execute(
                update(Order)
                .where(Order.id == order.id, Order.status == "PENDING_PAYMENT") 
                .values(status="PAID", paid_at=utcnow()) 
            )
            if moved.rowcount != 1: #并发重复支付，只有一个能赢
                raise conflict(
                    "INVALID_ORDER_STATE",
                    f"订单状态为 {order.status}，不能支付",
                    current_status=order.status,
                )

            # 2) 余额 CAS：不允许扣成负数（等价于乐观锁 + 条件更新）
            #CAS方法：适用于冲突少，“判断余额足够”和“写入”合成了一条 SQL，数据库保证这条 SQL 是原子的 。
            debited = session.execute(
                update(Account)
                .where(
                    Account.customer_id == order.customer_id,
                    Account.currency == order.currency,
                    Account.balance_minor >= order.amount_minor,  # 校验余额是否足够
                )
                .values(
                    balance_minor=Account.balance_minor - order.amount_minor,
                    version=Account.version + 1,
                )
            )
            if debited.rowcount != 1: #冲突的代价只是“输掉的那一方收到一个错误响应”
                raise AppError(
                    402,
                    "INSUFFICIENT_FUNDS",
                    "客户预付余额不足",
                    details={
                        "required": str(from_minor(order.amount_minor, order.currency)),
                        "currency": order.currency,
                    },
                )

            # 3) 复式分录 + 事件（与上面同一事务，要么全成要么全滚）（两条，借贷相等）
            from .ledger import PAYMENT, PREPAID, REVENUE, post_double_entry

            post_double_entry(  # 复式分录
                session,
                tenant_id=tenant.id,
                order_id=order.id,
                customer_id=order.customer_id,
                debit_account=PREPAID,
                credit_account=REVENUE,
                amount_minor=order.amount_minor,
                currency=order.currency,
                biz_type=PAYMENT,
            )
            session.add(  # 事件
                OutboxEvent(
                    tenant_id=tenant.id,
                    event_type="order.paid",
                    payload={"order_no": order.order_no, "amount_minor": order.amount_minor},
                )
            )

            session.refresh(order)  # 刷新订单状态
            return 200, order_out(order), order.order_no  # 返回订单详情

        outcome = self._run(tenant, order_no, idempotency_key, "pay", _pay)  # 支付幂等函数
        if not outcome.replayed:  # 如果是新订单，需要更新实时数据
            self._bump_realtime(tenant, outcome.body)
        return outcome

    # ------------------------------------------------------------------ #
    # 退款
    # ------------------------------------------------------------------ #
    def refund_order(  # 退款方法   
        self, tenant, order_no: str, idempotency_key: str | None = None
    ) -> IdempotentOutcome:
        def _refund(session) -> tuple[int, dict, str | None]:  # 退款事务函数
            order = session.execute(
                select(Order).where(Order.order_no == order_no)
            ).scalar_one_or_none()
            if order is None:
                raise not_found("订单", order_no)

            moved = session.execute(  # 退款状态迁移
                update(Order)
                .where(Order.id == order.id, Order.status == "PAID")
                .values(status="REFUNDED", refunded_at=utcnow())
            )
            if moved.rowcount != 1:
                raise conflict(
                    "INVALID_ORDER_STATE",
                    f"订单状态为 {order.status}，不能退款",
                    current_status=order.status,
                )

            session.execute(  # 退款余额迁移
                update(Account)
                .where(
                    Account.customer_id == order.customer_id,
                    Account.currency == order.currency,
                )
                .values(
                    balance_minor=Account.balance_minor + order.amount_minor,
                    version=Account.version + 1,
                )
            )

            from .ledger import PREPAID, REFUND, REVENUE, post_double_entry

            post_double_entry(  # 退款复式分录
                session,
                tenant_id=tenant.id,
                order_id=order.id,
                customer_id=order.customer_id,
                debit_account=REVENUE,
                credit_account=PREPAID,
                amount_minor=order.amount_minor,
                currency=order.currency,
                biz_type=REFUND,
            )
            session.add(  # 退款事件
                OutboxEvent(
                    tenant_id=tenant.id,
                    event_type="order.refunded",
                    payload={"order_no": order.order_no, "amount_minor": order.amount_minor},
                )
            )

            session.refresh(order)
            return 200, order_out(order), order.order_no

        outcome = self._run(tenant, order_no, idempotency_key, "refund", _refund)
        if not outcome.replayed:
            body = dict(outcome.body)
            body["amount_minor"] = -int(body["amount_minor"])
            self._bump_realtime(tenant, body)
        return outcome

    # ------------------------------------------------------------------ #
    # 充值
    # ------------------------------------------------------------------ #
    def deposit(self, tenant, customer_id: int, amount, currency: str | None) -> dict:
        curr = (currency or self.state.settings.currency).upper()
        try:
            amount_minor = to_minor(amount, curr)
        except ValueError as exc:
            raise unprocessable("INVALID_AMOUNT", str(exc)) from exc

        with self.state.db.session(tenant) as session:  # 充值会话
            customer = session.execute(
                select(Customer).where(Customer.id == customer_id)
            ).scalar_one_or_none()
            if customer is None:
                raise not_found("客户", customer_id)

            from .ledger import CASH, DEPOSIT, PREPAID, ensure_account, post_double_entry

            account = ensure_account(  # 确保账户存在
                session, tenant_id=tenant.id, customer_id=customer_id, currency=curr
            )
            session.execute(  # 充值余额迁移
                update(Account)
                .where(Account.id == account.id)
                .values(
                    balance_minor=Account.balance_minor + amount_minor,
                    version=Account.version + 1,
                )
            )
            post_double_entry(  # 充值复式分录
                session,
                tenant_id=tenant.id,
                order_id=None,
                customer_id=customer_id,
                debit_account=CASH,
                credit_account=PREPAID,
                amount_minor=amount_minor,
                currency=curr,
                biz_type=DEPOSIT,
            )
            session.refresh(account)
            return account_out(account)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get_order(self, tenant, order_no: str) -> dict:  # 查询订单
        with self.state.db.session(tenant) as session:  # 查询订单会话
            order = session.execute(
                select(Order).where(Order.order_no == order_no)
            ).scalar_one_or_none()
            if order is None:
                raise not_found("订单", order_no)
            return order_out(order)

    def list_orders(
        self, tenant, *, status: str | None = None, limit: int = 20, cursor: int | None = None
    ) -> dict:  # 查询订单列表
        with self.state.db.session(tenant) as session:  # 查询订单列表会话
            stmt = select(Order).order_by(Order.id.desc()).limit(limit + 1)
            if status:
                stmt = stmt.where(Order.status == status)
            if cursor:  # 分页查询
                stmt = stmt.where(Order.id < cursor)
            rows = list(session.execute(stmt).scalars().all())

            has_more = len(rows) > limit
            rows = rows[:limit]
            return {
                "items": [order_out(o) for o in rows],
                "next_cursor": rows[-1].id if has_more and rows else None,
            }

    def list_ledger(self, tenant, order_no: str) -> list[dict]:  # 查询订单账单
        with self.state.db.session(tenant) as session:  # 查询订单账单会话
            order = session.execute(
                select(Order).where(Order.order_no == order_no)
            ).scalar_one_or_none()
            if order is None:
                raise not_found("订单", order_no)
            rows = session.execute(
                select(LedgerEntry)
                .where(LedgerEntry.order_id == order.id)
                .order_by(LedgerEntry.id)
            ).scalars().all()
            return [ledger_out(e) for e in rows]

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _run(self, tenant, order_no: str, idempotency_key: str | None, op: str, fn):  # 运行订单操作
        """运行订单操作，支持幂等性。"""
        if idempotency_key:  # 幂等性校验
            return run_idempotent(
                self.state.db,
                tenant,
                idempotency_key,
                fingerprint({"op": op, "order_no": order_no}),
                fn,
                ttl_seconds=self.state.settings.idempotency_ttl,
            )
        with self.state.db.session(tenant) as session:  # 运行订单操作会话
            status_code, body, resource_id = fn(session)
        return IdempotentOutcome( 
            status_code=status_code, body=body, replayed=False, resource_id=resource_id
        )

    def _bump_realtime(self, tenant, body: dict) -> None:  # 写后计数
        """写后计数（Best-effort）：失败只影响看板，不影响交易。"""
        try:
            paid_at = body.get("paid_at") or utcnow()
            day = paid_at.date().isoformat() if hasattr(paid_at, "date") else str(paid_at)[:10]
            key = f"rt:{tenant.slug}:{day}"
            self.state.cache.incr(f"{key}:gross", int(body["amount_minor"]), ttl=3 * 86400)
            self.state.cache.incr(f"{key}:count", 1, ttl=3 * 86400)
            # 版本号 +1 -> 所有统计缓存 key 前缀变化，老缓存自然失效
            self.state.cache.incr(f"stats:ver:{tenant.slug}", 1, ttl=7 * 86400)
        except Exception:  # pragma: no cover - 看板降级
            pass
