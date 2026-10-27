"""实验 1：同一笔钱被扣两次。

    python -m labs.lab_race

目的：亲眼看见「先查再改」在并发下会丢钱，而「条件更新」不会。
只用 Python 自带的 sqlite3，不依赖项目代码，方便你把注意力放在 SQL 上。

背景（一句话）：
    两个请求同时到达，都读到了余额 100 元，都判断"够扣"，于是都扣了 60 元。
    结果：账户上只少了 60 元，但系统认为成功了两笔扣款 —— 白送了一笔服务。
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

START_MINOR = 10_000   # 初始余额 100.00 USD
AMOUNT_MINOR = 6_000   # 每笔扣 60.00 USD
THREADS = 2


def fresh_db() -> Path:
    path = Path(tempfile.mkdtemp(prefix="lab-race-")) / "bank.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE accounts (id INTEGER PRIMARY KEY, balance_minor INTEGER NOT NULL)"
    )
    conn.execute("INSERT INTO accounts (id, balance_minor) VALUES (1, ?)", (START_MINOR,))
    conn.commit()
    conn.close()
    return path


def balance_of(path: Path) -> int:
    conn = sqlite3.connect(path)
    value = conn.execute("SELECT balance_minor FROM accounts WHERE id = 1").fetchone()[0]
    conn.close()
    return int(value)


# --------------------------------------------------------------------------- #
# 三种写法
# --------------------------------------------------------------------------- #
def check_then_write(path: Path, barrier, results: list, index: int) -> None:
    """坏味道 A：读出来 -> 判断 -> 把「算好的绝对值」写回去。"""
    conn = sqlite3.connect(path, timeout=10)
    balance = conn.execute("SELECT balance_minor FROM accounts WHERE id = 1").fetchone()[0]
    barrier.wait()                    # 两个线程都读完了，才继续往下走
    ok = balance >= AMOUNT_MINOR
    if ok:
        time.sleep(0.02)              # 模拟业务处理耗时，放大竞态窗口
        conn.execute(
            "UPDATE accounts SET balance_minor = ? WHERE id = 1",
            (balance - AMOUNT_MINOR,),
        )
        conn.commit()
    results[index] = ok
    conn.close()


def check_then_increment(path: Path, barrier, results: list, index: int) -> None:
    """坏味道 B：判断照做，但写回时用 balance = balance - x（不再判断余额）。"""
    conn = sqlite3.connect(path, timeout=10)
    balance = conn.execute("SELECT balance_minor FROM accounts WHERE id = 1").fetchone()[0]
    barrier.wait()
    ok = balance >= AMOUNT_MINOR
    if ok:
        time.sleep(0.02)
        conn.execute(
            "UPDATE accounts SET balance_minor = balance_minor - ? WHERE id = 1",
            (AMOUNT_MINOR,),
        )
        conn.commit()
    results[index] = ok
    conn.close()


def conditional_update(path: Path, barrier, results: list, index: int) -> None:
    """正确做法：把「判断」和「扣减」合并成一条 SQL，由数据库原子执行。"""
    conn = sqlite3.connect(path, timeout=10)
    barrier.wait()
    cursor = conn.execute(
        "UPDATE accounts SET balance_minor = balance_minor - ? "
        "WHERE id = 1 AND balance_minor >= ?",
        (AMOUNT_MINOR, AMOUNT_MINOR),
    )
    conn.commit()
    results[index] = cursor.rowcount == 1
    conn.close()


def run(variant: Callable) -> tuple[int, int]:
    path = fresh_db()
    barrier = threading.Barrier(THREADS)
    results = [False] * THREADS
    threads = [
        threading.Thread(target=variant, args=(path, barrier, results, i))
        for i in range(THREADS)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    return sum(results), balance_of(path)


def main() -> int:
    print("场景：账户余额 100.00 元，2 个线程同时各扣 60.00 元")
    print("正确的业务结果是：只能成功 1 笔，余额变成 40.00\n")

    rows = [
        ("A 先查后写（写回绝对值）", check_then_write),
        ("B 先查后改（增量写回）", check_then_increment),
        ("C 条件更新（一条 SQL）", conditional_update),
    ]
    for name, variant in rows:
        successes, balance = run(variant)
        if balance < 0:
            verdict = "错：余额被扣成负数"
        elif successes != 1:
            verdict = "错：多成功了一笔，等于白送服务"
        else:
            verdict = "对：另一笔被数据库拒绝"
        print(f"\n{name}")
        print(f"    成功 {successes} 笔 | 最终余额 {balance / 100:.2f} 元 | {verdict}")

    print("\n看懂了吗？关键不是余额数字，而是「成功笔数」。")
    print("· A 和 B 都在两个线程里读到了同一个旧余额（100），判断都通过；")
    print("· 数据库不会阻止你覆盖旧值，它只负责执行 SQL；")
    print("· C 把「够不够扣」和「扣钱」放进同一条语句，判断和执行不可分割，")
    print("  这正是 demo 里 app/services/orders.py 支付逻辑的做法。")
    print("\n再看一眼真实代码：app/services/orders.py 第 120-142 行（余额条件更新）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
