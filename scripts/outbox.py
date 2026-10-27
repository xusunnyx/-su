"""运维小工具：查看 / 手动派发 outbox 事件。

    python -m scripts.outbox status      # 还剩多少条没投递
    python -m scripts.outbox dispatch    # 立刻派发（后台 worker 关掉时很有用）

配合学习路线 Day 6 使用：把 WORKER_ENABLED 设成 false，支付几笔订单，
你会发现日报一直是空的、而 outbox 里堆着事件——这就是"解耦"的证据。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings  # noqa: E402
from app.state import AppState  # noqa: E402
from app.workers import dispatch_pending, pending_count  # noqa: E402

USAGE = "用法: python -m scripts.outbox [status|dispatch]"


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    command = argv[1] if len(argv) > 1 else "status"
    settings = Settings.from_env()
    state = AppState.build(settings)
    state.db.init()

    if command == "status":
        print(f"待投递事件: {pending_count(state)} 条")
        for tenant in state.db.list_tenants():
            print(f"  - 租户 {tenant.slug} (id={tenant.id})")
        return 0

    if command == "dispatch":
        processed = dispatch_pending(state)
        print(f"本次派发 {processed} 条，剩余 {pending_count(state)} 条")
        return 0

    print(USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
