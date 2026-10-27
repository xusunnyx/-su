"""给「真正跑起来的服务」准备演示数据。

    python -m scripts.seed

会创建两个代理商租户、若干客户与预付余额，并跑出几笔已支付订单，
这样打开 /docs 就能直接查数据。租户 Key 会写进 ``data/api_keys.json``
（该文件已被 .gitignore 忽略，仅本地可见）。
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.models import Customer, Tenant  # noqa: E402
from app.schemas import OrderCreate  # noqa: E402
from app.services.orders import OrderService  # noqa: E402
from app.services.tenants import create_tenant  # noqa: E402
from app.state import AppState  # noqa: E402
from app.workers import dispatch_pending  # noqa: E402

SEED = [
    (
        "northwind",
        "Northwind 广告代理",
        [("NW-0001", "Acme 品牌方", "电商", "5000.00"), ("NW-0002", "Blue Bottle 咖啡", "餐饮", "2000.00")],
    ),
    (
        "globex",
        "Globex 广告代理",
        [("GX-0001", "Initech 软件", "SaaS", "8000.00")],
    ),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    settings = Settings.from_env()
    state = AppState.build(settings)
    state.db.init()
    service = OrderService(state)

    keys: dict[str, str] = {}
    for slug, name, customers in SEED:
        with state.db.session(None) as session:
            tenant = session.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none()

        if tenant is None:
            created = create_tenant(state, slug=slug, name=name)
            keys[slug] = created["api_key"]
            with state.db.session(None) as session:
                tenant = session.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one()
            print(f"[+] 租户 {slug} 已创建")
        else:
            print(f"[=] 租户 {slug} 已存在，跳过（密钥只在创建时返回一次）")

        for ref, cname, industry, balance in customers:
            with state.db.session(tenant) as session:
                customer = session.execute(
                    select(Customer).where(Customer.external_ref == ref)
                ).scalar_one_or_none()
                is_new = customer is None
                if is_new:
                    customer = Customer(
                        tenant_id=tenant.id, external_ref=ref, name=cname, industry=industry
                    )
                    session.add(customer)
                    session.flush()
            # 只给「新建的客户」充值，避免脚本重复执行把余额越充越多
            if is_new:
                service.deposit(tenant, customer.id, Decimal(balance), None)
                print(f"    客户 {ref} {cname} 充值 {balance}")
            else:
                print(f"    客户 {ref} {cname} 已存在，跳过充值")

        # 两笔订单：一笔已支付，一笔待支付，方便演示状态机和报表
        with state.db.session(tenant) as session:
            customer_ids = [
                c.id
                for c in session.execute(select(Customer).order_by(Customer.id)).scalars().all()
            ]
        amounts = ["199.00", "89.50", "420.00"]
        for index, amount in enumerate(amounts):
            outcome = service.create_order(
                tenant,
                OrderCreate(customer_id=customer_ids[index % len(customer_ids)], amount=Decimal(amount)),
                f"seed-{slug}-{index:03d}",
            )
            current = service.get_order(tenant, outcome.body["order_no"])
            if index % 2 == 0 and current["status"] == "PENDING_PAYMENT":
                service.pay_order(tenant, current["order_no"])
                current = service.get_order(tenant, current["order_no"])
            print(f"    订单 {current['order_no']} {current['amount']} USD {current['status']}")

    processed = dispatch_pending(state)
    print(f"\n已派发 {processed} 条异步事件（相当于后台 worker 跑了一轮），日报已生成")

    if keys:
        key_file = settings.data_dir / "api_keys.json"
        key_file.write_text(json.dumps(keys, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n租户密钥已写入 {key_file}")
        for slug, key in keys.items():
            print(f"  {slug}: {key}")

    print("\n完成。随后启动服务：")
    print("  python -m uvicorn app.main:create_app --factory --port 8000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
