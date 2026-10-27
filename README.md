# AdFlow：多租户广告与交易原型系统

## 摘要

AdFlow 是一个面向国际化广告与交易平台场景的后端原型系统，旨在演示并验证多租户数据隔离、资金交易一致性、系统解耦、高并发查询与工程质量控制等核心架构问题的工程化解决方案。系统以 FastAPI + SQLAlchemy 为技术栈，默认采用 SQLite 与进程内组件，实现零外部依赖即可运行；同时通过 `docker-compose.yml` 提供 PostgreSQL + Redis 的生产级部署形态，业务代码无需修改。

主要特性：

- 零外部依赖启动：SQLite + 进程内缓存 + 线程 Worker
- 端到端演示入口：`python -m scripts.demo`
- 28 个自动化测试用例，覆盖行级隔离与独立库隔离两种模式
- 完整的设计决策记录（见 [docs/DESIGN.md](docs/DESIGN.md)）

---

## 一、设计决策概览

本系统在方案选型阶段对若干典型备选架构进行了对比与裁剪，关键决策如下表所示。各项选择均附带代价分析，详见 DESIGN.md。

| 备选方案                                   | 局限                                     | 本实现采用的方案                                                                                     |
| -------------------------------------- | -------------------------------------- | -------------------------------------------------------------------------------------------- |
| FastAPI + Redis + Celery + MySQL 全量中间件 | 部署依赖重，环境一致性难以保证                        | 默认 SQLite + 进程内缓存 + 线程 Worker，零依赖可运行；通过 `docker-compose.yml` 切换至 PostgreSQL + Redis，业务代码保持不变 |
| 幂等执行 = "先查询后插入"                        | 并发下存在 check-then-act 竞态，仍会产生重复订单       | `(tenant_id, key)` 唯一索引 + 事务内占位 + 请求指纹校验 + 409 语义                                            |
| 行级隔离与独立 schema 分别实现两套业务代码              | 业务逻辑分叉，维护成本高                           | 通过 `Database.engine_for()` 统一抽象，切换 `TENANT_ISOLATION` 配置即可更换隔离策略，Service 层零改动                |
| 以订单 CRUD 为核心                           | 未解决资金重复扣减、余额为负等一致性问题                   | 复式记账 + 余额条件更新（CAS）+ 余额与分录对账接口                                                                |
| Celery 直接派发任务                          | 写库成功而任务派发失败导致事件丢失；任务已派发而事务回滚导致处理不存在的订单 | 事务性 Outbox 模式 + 可重算消费者；Celery 仅作为 `dispatch_pending` 的可替换驱动                                  |

此外，本实现补充了三项在原始方案中未明确、但对系统正确性至关重要的设计：

1. **鉴权即隔离入口**：租户身份仅来源于 `X-API-Key`，不接受请求体或 URL 中传入的 `tenant_id`，从入口层杜绝越权请求。
2. **可观测性**：`X-Request-Id` 贯穿全链路、结构化访问日志、稳定错误码（`NOT_FOUND` / `IDEMPOTENCY_KEY_REUSED` / `INSUFFICIENT_FUNDS`），便于前端分支处理与告警接入。
3. **测试即证据**：28 个测试用例覆盖并发同键下单、跨租户越权、扣款回滚、分录平衡、缓存失效、Outbox 重投等关键路径。

---

## 二、快速启动

```powershell
python -m pip install -r requirements.txt
python -m scripts.demo        # 端到端演示，无需启动独立服务或外部中间件
```

输出节选：

```text
[STEP 03] 下单（幂等键 = 客户端生成的请求指纹）
   [OK] 订单 AD20261023F3C446 金额 199.99 USD，状态 PENDING_PAYMENT

[STEP 04] 模拟客户端超时重试：同一个幂等键再发一次
   [OK] 返回同一张订单，响应头 X-Idempotent-Replay=true
        - 没有唯一索引 + 状态机，这一步就会变成两张订单 / 两次扣款

[STEP 06] 支付：状态机 CAS + 余额条件更新（同一个事务）
   [OK] 订单状态 PAID
   [OK] 重复支付 -> HTTP 409 INVALID_ORDER_STATE（不会被扣两次）

[STEP 07] 复式分录：每笔资金变动两条分录，借贷相等
   PAYMENT  DEBIT   PREPAID      199.99 USD
   PAYMENT  CREDIT  REVENUE      199.99 USD

[STEP 08] 并发压测：8 个线程同时用同一个幂等键下单
   [OK] 8 个请求全部返回同一张订单，库里只多了 1 张（1 -> 2，耗时 10ms）

[STEP 09] 租户隔离：B 拿着自己的 Key 去访问 A 的订单
   [OK] HTTP 404 NOT_FOUND —— 查询条件里没有 tenant_id，也拿不到数据

[STEP 10] 缓存：同一个统计接口，第一次回源，第二次命中
   [OK] MISS 199.99 USD / 1 单，耗时 27.6ms
   [OK] HIT  199.99 USD / 1 单，耗时 4.9ms
```

测试执行：

```powershell
python -m pytest -q                                # 行级隔离模式：28 passed
$env:TEST_ISOLATION="schema"; python -m pytest -q  # 独立库模式：28 passed
```

---

## 三、服务启动与 OpenAPI 文档

```powershell
python -m scripts.seed        # 初始化 2 个租户 + 客户 + 充值 + 订单，密钥写入 data/api_keys.json
python -m uvicorn app.main:create_app --factory --port 8000
# 浏览器访问 http://127.0.0.1:8000/docs
```

Windows 一键启动：`.\run.ps1`

```powershell
$key = "adf_xxx"   # data/api_keys.json 中 northwind 租户的密钥
curl.exe -s http://127.0.0.1:8000/api/v1/stats/summary -H "X-API-Key: $key"
curl.exe -s -X POST http://127.0.0.1:8000/api/v1/orders `
  -H "X-API-Key: $key" -H "Idempotency-Key: 6f1c2c0e-1" -H "Content-Type: application/json" `
  -d '{\"customer_id\": 1, \"amount\": \"199.00\"}'
```

接口清单：

| 方法       | 路径                               | 说明                                       |
| -------- | -------------------------------- | ---------------------------------------- |
| POST     | `/api/v1/admin/tenants`          | 创建租户，返回一次性 API Key（由 `X-Admin-Token` 保护） |
| POST/GET | `/api/v1/customers`              | 客户建档 / 列表（游标分页）                          |
| POST     | `/api/v1/customers/{id}/deposit` | 预付金入账（借 CASH / 贷 PREPAID）                |
| GET      | `/api/v1/customers/{id}/account` | 余额 + 分录重算对账结果                            |
| POST     | `/api/v1/orders`                 | 下单（必须携带 `Idempotency-Key`）               |
| POST     | `/api/v1/orders/{no}/pay`        | 支付（状态机 + 余额 CAS）                         |
| POST     | `/api/v1/orders/{no}/refund`     | 退款（反向分录）                                 |
| GET      | `/api/v1/orders/{no}/ledger`     | 查询该订单的借贷分录                               |
| GET      | `/api/v1/stats/summary`          | 近 N 天汇总（缓存 + 单飞 + 版本号失效）                 |
| GET      | `/api/v1/stats/daily`            | 按天物化报表（Outbox 异步生成）                      |
| GET      | `/api/v1/stats/realtime`         | 今日实时看板（写路径计数，O(1) 读）                     |
| GET      | `/api/v1/stats/debug/cache`      | 缓存命中率                                    |

---

## 四、系统架构

```text
                     API Key ──▶ 中间件：Request-Id / 结构化访问日志
                                      │
                    依赖链：鉴权(Key→租户) → 滑动窗口限流 → 幂等执行器
                                      │
        ┌─────────────────┬───────────┴───────────┬──────────────────┐
        │  orders（交易）  │   customers（CRM）     │  stats（查询）     │
        └─────────────────┴───────────────────────┴──────────────────┘
                                      │
                   Service 层：订单状态机 / 复式记账 / 统计聚合
                                      │
        ┌─────────────────────────────┴─────────────────────────────┐
        │ SQLAlchemy 租户隔离                Cache 抽象                │
        │  row    : 自动追加 tenant_id       memory（默认）/ redis     │
        │  schema : 每租户一个库             cache-aside / 单飞 / 版本号 │
        └─────────────────────────────┬─────────────────────────────┘
                                      │  一个事务 = 订单 + 分录 + outbox + 幂等记录
        ┌─────────────────────────────▼─────────────────────────────┐
        │ Outbox Worker（线程，可替换为 Celery/Kafka）：重算日报 → 可重投 │
        └───────────────────────────────────────────────────────────┘
```

---

## 五、目录结构

```text
app/
  main.py            应用装配：生命周期、中间件、异常处理、路由
  config.py          全部配置来自环境变量，默认值保证零依赖
  db.py              连接管理 + 行级隔离自动过滤 + 跨租户写入守卫
  models.py          7 张表：租户/客户/账户/订单/分录/幂等/outbox/日报
  idempotency.py     幂等执行器（唯一索引 + 事务内占位 + 请求指纹）
  cache.py           缓存与限流抽象：Redis / 进程内两套实现
  security.py        API Key 哈希与租户解析
  deps.py            请求上下文、限流依赖
  services/
    orders.py        下单 / 支付 / 退款 / 充值（状态机 + CAS + 分录 + 事件）
    ledger.py        复式记账与对账
    stats.py         缓存聚合 + 实时计数
    tenants.py       租户创建与签发密钥
  routers/           admin / customers / orders / stats / health
  workers.py         事务性 Outbox 派发与消费者
scripts/
  demo.py            端到端演示（进程内，带断言）
  smoke.py           10 秒自检
  seed.py            为真实服务准备演示数据
  outbox.py          查看/手动派发异步事件
labs/
  lab_race.py        实验：并发扣款为什么会丢钱、会扣成负数
  lab_index.py       实验：有/无索引的 EXPLAIN 与耗时对比
  inspect_db.py      打开数据库看表结构与行数，可制造一条"不平的账"
tests/               28 个用例：隔离/幂等/交易/统计/outbox/限流
docs/
  DESIGN.md          设计取舍：选型依据与代价分析
```

---

## 六、关键机制与代码位置

| 机制        | 说明                                              | 代码                                               |
| --------- | ----------------------------------------------- | ------------------------------------------------ |
| 租户隔离（行级）  | ORM 事件为每条 SELECT 自动追加 `tenant_id` 过滤条件，业务代码无法遗漏 | [app/db.py](app/db.py)                           |
| 租户隔离（独立库） | `engine_for(tenant)` 按租户路由至独立库，切换配置即切换策略        | [app/db.py](app/db.py)                           |
| 越权防御      | `before_flush` 校验写入的 `tenant_id` 与当前上下文一致       | [app/db.py](app/db.py)                           |
| 幂等        | 唯一索引作为并发裁决，失败请求重试后读取胜出者响应并原样重放                  | [app/idempotency.py](app/idempotency.py)         |
| 资金安全      | 状态 CAS + 余额条件更新 + 复式分录，全部位于同一事务                 | [app/services/orders.py](app/services/orders.py) |
| 对账        | 账户余额必须等于分录重算值，不一致即暴露                            | [app/services/ledger.py](app/services/ledger.py) |
| 缓存        | cache-aside + 单飞防击穿 + 版本号失效                     | [app/services/stats.py](app/services/stats.py)   |
| 异步解耦      | 业务与事件同事务写入 Outbox，消费者通过重算保证幂等                   | [app/workers.py](app/workers.py)                 |
| 限流        | Redis ZSET 滑动窗口；无 Redis 时采用同语义内存实现              | [app/cache.py](app/cache.py)                     |

---

## 七、生产化演进方向

从原型到生产环境，以下模块需要进一步替换或增强：

- **数据库**：SQLite → PostgreSQL/MySQL；`JSON` 列、`func.date()` 方言、`SELECT FOR UPDATE` 语义均需适配
- **隔离**：schema 模式当前以"独立库"模拟（PG 原生方案为 `search_path` 切换 schema）；大客户隔离需考虑连接池按租户限流
- **异步**：线程 Worker → Celery/Kafka + 死信队列 + 积压监控；Outbox 需增加分区清理
- **一致性**：幂等记录与业务当前同库同事务；跨库场景需引入 Saga/补偿事务
- **可观测**：接入 OpenTelemetry、Prometheus 指标（P99、缓存命中率、Outbox 积压）
- **安全**：API Key → OAuth2/JWT + 租户级 RBAC；密钥轮换；请求签名
- **性能**：分页已采用游标；报表走物化表；下一步为读写分离 + 按月分区

---

## 八、参考资料

| 项目                                                                                            | 用途                                             |
| --------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| [fastapi/fastapi](https://github.com/fastapi/fastapi)                                         | 框架本体，路由/依赖注入/异常处理写法                            |
| [fastapi/full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template) | 官方项目骨架：分层目录、配置、测试、Docker 编排                    |
| [long2ice/fastapi-limiter](https://github.com/long2ice/fastapi-limiter)                       | 限流依赖写法（注：其采用固定窗口，本系统采用 ZSET 滑动窗口）              |
| [zhanymkanov/fastapi-best-practices](https://github.com/zhanymkanov/fastapi-best-practices)   | 工程规范：目录、依赖注入、异步、测试组织                           |
| [jestatsio/purview](https://github.com/jestatsio/purview)                                     | FastAPI + SQLAlchemy 的行级授权/多租户策略（对照本系统的自动过滤实现） |
| [Stripe: 幂等键设计](https://stripe.dev/blog/because-nobody-likes-being-charged-twice)             | 幂等协议的业界标准做法                                    |
| [SQLAlchemy 2.0 文档](https://docs.sqlalchemy.org/en/20/)                                       | `with_loader_criteria`、Session 事件、事务边界         |

---

## 九、已知限制

- 单币种统计（多币种需按币种分组或统一折算，`amount_minor` 已按币种精度处理）
- 租户级限流仅按 `(tenant, scope)` 计数，生产环境需补充接口维度与配额分级
- 未实现用户级 RBAC（当前仅提供租户级 API Key）
- Worker 与 API 同进程运行（便于演示）；生产环境必须拆分为独立进程，否则重启会丢失正在处理的事件
- 缓存降级至进程内时，多副本之间不共享计数（这是 `Cache` 抽象的明确边界）
