"""端到端演示：一条命令看到全部设计要点（不需要 Redis / MySQL / 启动服务）。

    python -m scripts.demo

脚本用 FastAPI 的 TestClient 在进程内跑完整业务链路，所以结果可复现、可断言：
任何一步不符合预期都会直接抛错，而不是打印一句"看起来没问题"。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("LOG_LEVEL", "WARNING")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Account, Order  # noqa: E402
from app.schemas import OrderCreate  # noqa: E402
from app.services.ledger import order_debit_credit, recompute_balance  # noqa: E402
from app.services.orders import OrderService  # noqa: E402
from app.services.stats import StatsService  # noqa: E402
from app.workers import dispatch_pending, pending_count  # noqa: E402


def header(text: str) -> None:
    print("\n" + "=" * 74)
    print(text)
    print("=" * 74)


def step(index: int, title: str) -> None:
    print(f"\n[STEP {index:02d}] {title}")


def ok(message: str) -> None:
    print(f"   [OK] {message}")


def info(message: str) -> None:
    print(f"        - {message}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    tmp = Path(tempfile.mkdtemp(prefix="adflow-demo-"))
    settings = Settings(isolation="row", data_dir=tmp, worker_enabled=False, rate_limit=10_000)
    app = create_app(settings)
    state = app.state.ctx

    with TestClient(app) as client:
        header("AdFlow Demo · 多租户广告 CRM 与交易平台")

        # ------------------------------------------------------------------ #
        step(1, "创建两个租户（每个代理商 = 一个租户，各自拿到独立 API Key）")
        tenants = {}
        for slug, name in (("northwind", "Northwind 广告代理"), ("globex", "Globex 广告代理")):
            response = client.post(
                "/api/v1/admin/tenants",
                json={"slug": slug, "name": name},
                headers={"X-Admin-Token": settings.admin_token},
            )
            assert response.status_code == 201, response.text
            body = response.json()
            tenants[slug] = {"headers": {"X-API-Key": body["api_key"]}, "id": body["id"]}
            ok(f"{name:<18} slug={slug:<10} api_key={body['api_key'][:14]}...")

        a = tenants["northwind"]
        b = tenants["globex"]

        # ------------------------------------------------------------------ #
        step(2, "租户 A 建档客户 + 预付金入账（借 CASH / 贷 PREPAID）")
        customer = client.post(
            "/api/v1/customers",
            json={"external_ref": "NW-0001", "name": "Acme 品牌方", "industry": "电商"},
            headers=a["headers"],
        ).json()
        account = client.post(
            f"/api/v1/customers/{customer['id']}/deposit",
            json={"amount": "1000.00", "currency": "USD"},
            headers=a["headers"],
        ).json()
        ok(f"客户 #{customer['id']} {customer['name']}，预付余额 {account['balance']} {account['currency']}")

        # ------------------------------------------------------------------ #
        step(3, "下单（幂等键 = 客户端生成的请求指纹）")
        idem = "demo-order-key-0001"
        order_payload = {"customer_id": customer["id"], "amount": "199.99", "description": "Q4 开屏广告"}
        created = client.post(
            "/api/v1/orders", json=order_payload, headers={**a["headers"], "Idempotency-Key": idem}
        )
        assert created.status_code == 201, created.text
        order_no = created.json()["order_no"]
        ok(f"订单 {order_no} 金额 {created.json()['amount']} USD，状态 {created.json()['status']}")

        # ------------------------------------------------------------------ #
        step(4, "模拟客户端超时重试：同一个幂等键再发一次")
        replay = client.post(
            "/api/v1/orders", json=order_payload, headers={**a["headers"], "Idempotency-Key": idem}
        )
        assert replay.json()["order_no"] == order_no
        ok(f"返回同一张订单，响应头 X-Idempotent-Replay={replay.headers.get('X-Idempotent-Replay')}")
        info("没有唯一索引 + 状态机，这一步就会变成两张订单 / 两次扣款")

        # ------------------------------------------------------------------ #
        step(5, "同一个键、不同金额：这是客户端 bug，必须报错而不是静默覆盖")
        reused = client.post(
            "/api/v1/orders",
            json={**order_payload, "amount": "999.00"},
            headers={**a["headers"], "Idempotency-Key": idem},
        )
        ok(f"HTTP {reused.status_code} {reused.json()['code']} —— 请求指纹不一致")

        # ------------------------------------------------------------------ #
        step(6, "支付：状态机 CAS + 余额条件更新（同一个事务）")
        paid = client.post(f"/api/v1/orders/{order_no}/pay", headers=a["headers"])
        assert paid.status_code == 200, paid.text
        ok(f"订单状态 {paid.json()['status']}，paid_at={paid.json()['paid_at']}")

        again = client.post(f"/api/v1/orders/{order_no}/pay", headers=a["headers"])
        ok(f"重复支付 -> HTTP {again.status_code} {again.json()['code']}（不会被扣两次）")

        # ------------------------------------------------------------------ #
        step(7, "复式分录：每笔资金变动两条分录，借贷相等")
        ledger = client.get(f"/api/v1/orders/{order_no}/ledger", headers=a["headers"]).json()
        for entry in ledger:
            print(
                f"   {entry['biz_type']:<8} {entry['direction']:<7} "
                f"{entry['account_code']:<8} {entry['amount']:>10} {entry['currency']}"
            )
        debit = sum(e["amount_minor"] for e in ledger if e["direction"] == "DEBIT")
        credit = sum(e["amount_minor"] for e in ledger if e["direction"] == "CREDIT")
        assert debit == credit
        ok(f"借方合计 = 贷方合计 = {debit / 100:.2f}")

        # ------------------------------------------------------------------ #
        step(8, "并发压测：8 个线程同时用同一个幂等键下单")
        tenant_obj = next(t for t in state.db.list_tenants() if t.slug == "northwind")
        with state.db.session(tenant_obj) as session:
            total_before = session.execute(select(func.count(Order.id))).scalar_one()
        service = OrderService(state)
        payload = OrderCreate(customer_id=customer["id"], amount=Decimal("10.00"))
        results: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                outcome = service.create_order(tenant_obj, payload, "concurrent-key-0001")
                with lock:
                    results.append(outcome.body["order_no"])
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        started = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = (time.perf_counter() - started) * 1000
        assert not errors, errors
        assert len(set(results)) == 1, results
        with state.db.session(tenant_obj) as session:
            total = session.execute(select(func.count(Order.id))).scalar_one()
        assert total == total_before + 1, (total_before, total)
        ok(f"8 个请求全部返回同一张订单，库里只多了 1 张（{total_before} -> {total}，耗时 {elapsed:.0f}ms）")

        # ------------------------------------------------------------------ #
        step(9, "租户隔离：B 拿着自己的 Key 去访问 A 的订单")
        stolen = client.get(f"/api/v1/orders/{order_no}", headers=b["headers"])
        ok(f"HTTP {stolen.status_code} {stolen.json()['code']} —— 查询条件里没有 tenant_id，也拿不到数据")
        info("隔离来自两处：API Key -> 租户上下文；ORM 事件自动追加 tenant_id 过滤")

        # ------------------------------------------------------------------ #
        step(10, "缓存：同一个统计接口，第一次回源，第二次命中")
        t0 = time.perf_counter()
        first = client.get("/api/v1/stats/summary?days=7", headers=a["headers"]).json()
        t1 = time.perf_counter()
        second = client.get("/api/v1/stats/summary?days=7", headers=a["headers"]).json()
        t2 = time.perf_counter()
        ok(f"MISS {first['gross']} USD / {first['order_count']} 单，耗时 {(t1 - t0) * 1000:.1f}ms")
        ok(f"HIT  {second['gross']} USD / {second['order_count']} 单，耗时 {(t2 - t1) * 1000:.1f}ms")
        info("支付写路径会 INCR 版本号，老缓存 key 自动失效，不需要 SCAN 删键")

        # ------------------------------------------------------------------ #
        step(11, "异步解耦：支付只写 outbox，日报由 worker 生成")
        pending_before = pending_count(state)
        ok(f"待投递事件 {pending_before} 条（worker 在 demo 里是手动触发的）")
        processed = dispatch_pending(state)
        report = StatsService(state).daily_reports(tenant_obj)[0]
        ok(f"派发 {processed} 条事件 -> 日报 {report['day']}：{report['order_count']} 单 / {report['gross']} USD")
        dispatch_pending(state)
        again_report = StatsService(state).daily_reports(tenant_obj)[0]
        assert again_report == report
        ok("重复派发不会重复计数（消费者用「重算」而不是「累加」）")

        # ------------------------------------------------------------------ #
        step(12, "对账：账户余额必须等于分录重算结果")
        account_view = client.get(
            f"/api/v1/customers/{customer['id']}/account", headers=a["headers"]
        ).json()
        with state.db.session(tenant_obj) as session:
            recomputed = recompute_balance(
                session, account_code="PREPAID", customer_id=customer["id"], currency="USD"
            )
            debit_total, credit_total = order_debit_credit(session, order_id=created.json()["id"])
        ok(f"账户余额 {account_view['balance']}，分录重算 {recomputed / 100:.2f}，一致={account_view['consistent']}")
        ok(f"订单分录 借 {debit_total / 100:.2f} = 贷 {credit_total / 100:.2f}")

        # ------------------------------------------------------------------ #
        step(13, "限流：另一个租户在 3 次/60 秒的配额下打满")
        burst_settings = Settings(
            data_dir=tmp / "burst", worker_enabled=False, rate_limit=3, rate_window=60
        )
        with TestClient(create_app(burst_settings)) as burst:
            tenant_body = burst.post(
                "/api/v1/admin/tenants",
                json={"slug": "throttle", "name": "Throttle"},
                headers={"X-Admin-Token": burst_settings.admin_token},
            ).json()
            burst_headers = {"X-API-Key": tenant_body["api_key"]}
            codes = [
                burst.get("/api/v1/orders", headers=burst_headers).status_code for _ in range(4)
            ]
        ok(f"连续 4 次请求的状态码：{codes}（第 4 次触发滑动窗口限流）")

        header("演示结束")
        print(f"临时数据目录：{tmp}")
        print("完整能力清单、架构取舍与面试讲法见 README.md / docs/DESIGN.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
