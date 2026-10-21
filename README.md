# API 通知服务 — rc_sophia

为内部业务系统持久化接收供应商 HTTP 通知，由独立 Worker 异步发送。调用方只等待本地任务提交，不等待供应商响应。代码及专项审查已完成；最终仓库和真实会话归档的待办见 [提交清单](docs/submission.md)。

## 边界与架构

```text
内部调用方 -- POST /notifications --> FastAPI -- 提交 --> SQLite
内部调用方 <-- 202（提交后）---------- FastAPI              |
内部调用方 -- GET /notifications/id -> FastAPI              |
                                                          v
                                 独立 Worker -- 领取任务/设置 Lease
                                      |
                                      +-- 事务外 HTTPS POST --> 供应商
                                      |
                                      +-- 条件写回 --> SUCCESS / PENDING / FAILED
```

面向单机、小规模通知场景，只提供接收和查询两个业务接口，不包含前端、批量通知、供应商认证刷新、Body 转换、投递历史表、人工重投、告警平台或跨系统事务。不确认供应商业务处理结果。API 层不做业务事件去重。

## 启动

需要 Python 3.10。在项目根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export NOTIFICATION_DB_PATH="$PWD/data/notifications.sqlite3"
export NOTIFICATION_ALLOWED_HOSTS=crm.example.com
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另开终端，设置相同配置并启动唯一一个 Worker：

```bash
export NOTIFICATION_DB_PATH="$PWD/data/notifications.sqlite3"
export NOTIFICATION_ALLOWED_HOSTS=crm.example.com
.venv/bin/python -m app.worker
```

API 和 Worker 是独立进程，通过同一个本机 SQLite 文件交接任务。两者均可初始化数据库。Worker 每秒轮询，串行投递，空闲或数据库错误时等待一秒再查询。停止后重启 Worker，未完成的 PROCESSING 任务在 Lease 过期后恢复。

`.env.example` 仅作为配置参考，不会自动加载。allowlist 默认为空，拒绝所有目标；多个域名以逗号分隔，精确匹配，支持 ASCII 域名或 punycode，不支持通配符。数据库路径在启动时解析为绝对路径，后续 Worker 应使用同一路径。

| 环境变量 | 默认值 | 作用 |
|---|---|---|
| NOTIFICATION_DB_PATH | ./data/notifications.sqlite3 | 本机持久化数据库文件 |
| NOTIFICATION_ALLOWED_HOSTS | 空 | 逗号分隔的目标域名；API 与 Worker 配置一致 |
| NOTIFICATION_BUSY_TIMEOUT_MS | 5000 | SQLite 锁等待毫秒数，非负整数 |

Lease 60 秒、总投递超时 10 秒、轮询 1 秒、最多 5 次领取为当前固定值。

## API 示例

```bash
curl -i http://127.0.0.1:8000/notifications \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://crm.example.com/webhook","body":{"user_id":123}}'
curl http://127.0.0.1:8000/notifications/TASK_ID
```

创建成功的 HTTP 状态为 202，响应示例：

```json
{"id":"<uuid>","status":"PENDING"}
```

将返回的 id 替换上面的 TASK_ID。查询成功响应示例（数字为 UTC Unix 秒）：

```json
{"id":"<uuid>","status":"SUCCESS","attempt_count":1,"last_error":null,"next_retry_at":null,"created_at":1792598400,"updated_at":1792598401}
```

查询时任务可能已经被 Worker 领取或完成，因此不保证仍为 PENDING。

样例供应商域名仅用于说明，不代表真实可用端点。只接受 HTTPS、443 端口和 POST；body 必填，支持标准 JSON 的所有值，包括显式 null。查询返回调度与状态字段，时间统一为整数 Unix 秒（UTC），不返回 URL、Headers 或 Body。输入校验失败返回 422，任务不存在返回 404，运行期间数据库错误返回 503。初始化数据库失败将阻止服务启动。

目标 URL 还必须通过 HTTPX URL 解析，包括其长度限制；无法由客户端处理的 URL 在入库前返回 422。历史数据中已有的无效 URL 会由 Worker 标记为 FAILED，错误摘要为 `Invalid destination URL`，不会中断 Worker 或继续重试该任务。

## 安全与持久化限制

写事务提交后才返回 202。采用 SQLite WAL、FULL 同步和默认 5000ms 锁等待；数据库必须位于本机持久磁盘。这不提供磁盘损毁保护或跨机高可用。调用方未收到响应后重新提交可能产生独立任务，本版不做业务事件去重。固定依赖版本用于复现此次作业，不代表依赖安全性审计；生产部署需维护更新。

本版无调用方鉴权，只适合受控内部开发环境；域名 allowlist 不能完整抵御 DNS 重绑定，生产仍需出口网络控制及凭据管理。数据库保存的供应商凭据需要通过文件访问权限保护。API 不记录请求内容，验证错误也不回显原始输入。

## 投递语义与重试

Worker 领取时递增 attempt_count，并设置 60 秒 Lease 及随机 token，提交后才发 HTTP。外发 POST 使用保存的 JSON 字节、稳定任务 ID 作为 Idempotency-Key；默认 Content-Type 为 application/json。总投递时限 10 秒，同时配置 HTTPX 阶段超时；禁用重定向和环境代理，不读取供应商响应正文。Worker 发送前再次检查当前 allowlist；域名已移除则 FAILED。

2xx（包括 202）仅代表 HTTP 层成功。408、429、5xx、网络连接/远端协议错误、超时允许重试；其他 HTTP 响应和不可重试请求错误直接 FAILED。最多领取 5 次，首次执行计入上限。第 1–4 次可重试失败的基础延迟为 10、20、40、80 秒，乘 0.8–1.2 Jitter，再四舍五入为整数秒，最终限幅 300 秒。本版不解析 Retry-After。

所有结果写回要求状态仍为 PROCESSING、token 匹配且 Lease 未过期。终态和待重试状态清空 Lease；终态还清空调度时间。过期且已领取 5 次的任务直接 FAILED，不会发送第六次。FAILED 保留在数据库中供查询，无自动告警或手动重投接口。

这是持久化接受、有限尝试且可能重复的投递机制，不保证最终送达或 exactly-once。领取次数不同于供应商收到请求次数。供应商已处理后崩溃、或本地写回失败，都会在 Lease 恢复后产生重复请求；旧 token 只保护本地状态，不能撤销外部请求。API 重复提交也会创建不同任务。固定 Idempotency-Key 是否防止重复业务操作，取决于供应商支持。

## 故障行为

| 故障或结果 | 系统行为 |
|---|---|
| 接收时数据库写入/提交失败 | 返回 503，不返回 202；启动初始化失败则服务不启动 |
| Worker 领取前崩溃 | PENDING 留在数据库，重启继续领取 |
| 领取后、发送前或过程中崩溃 | PROCESSING 等待 Lease 到期后恢复；每次领取消耗一次尝试 |
| HTTP timeout / 连接错误 / 408 / 429 / 5xx | 未耗尽时退避重试；第五次失败后 FAILED |
| 其他 4xx 或 3xx | FAILED，不跟随重定向 |
| 供应商已处理，本地写回前崩溃或数据库不可用 | Lease 到期后可能重复发送；固定幂等键交由供应商识别 |
| Lease 已到期的旧 Worker 返回结果 | 拒绝写回，不覆盖当前状态；外部请求仍可能已经生效 |
| 连续五次领取后崩溃 | Lease 到期清理为 FAILED，不发第六次 |
| 供应商长时间不可用 | 最终 FAILED，保留摘要供查询，无自动告警/重投 |
| 数据库运行期不可用 | API 返回 503；Worker 记录固定摘要并等待后重试数据库操作 |
| API 返回前连接断开，调用方重新提交 | 原提交可能已经落库；再次提交生成新任务，可能重复通知 |

## 工程取舍与演进

选择 SQLite 持久化与独立 Worker，便于直接验证提交、崩溃恢复和并发写回。内存队列或单独依赖 BackgroundTasks 无法保留进程退出后的任务。当前规模不需要 Kafka、Redis、Celery、分布式 Worker 或多表投递历史；标准库 SQL 足够，无需 ORM、通用 Repository 或迁移框架。供应商适配框架和 Dashboard 不解决本轮可靠性目标，因此不纳入。具体决策见 [docs/decisions.md](docs/decisions.md)，审查及复现记录见 [docs/review.md](docs/review.md)。

后续按实际需求引入 PostgreSQL 和多个 Worker；业务数据库与通知接收需要原子性时评估业务 Outbox 与队列；增加监控、失败告警及受控重投；抽取供应商配置与认证管理；补充 API 事件去重和供应商幂等协议。生产部署另需调用方鉴权、请求限额、凭据管理、网络出口策略及备份恢复方案。

## 验证

运行全部测试（无需真实第三方网络）：

```bash
.venv/bin/python -m pytest -q
```

阶段 3 实测（Python 3.10.12）：90 passed，耗时 6.40 秒。覆盖接收提交、输入校验、独立连接竞争领取、重试分类与上限、Lease 恢复与旧结果拒绝、成功后数据库写回失败、请求内容稳定、总超时和不消费响应正文。另覆盖真实子进程领取后直接退出恢复、Unicode 转义序列、校验错误键名脱敏和 Worker 日志脱敏。当前执行环境的沙箱会阻塞 TestClient 的跨线程事件循环，测试经授权在沙箱外运行；测试未访问第三方网络。有 1 条 Starlette 对 AnyIO BlockingPortal 别名的弃用警告，不影响测试结果。

阶段 4 核验（2026-10-22）：`python -m pip check` 无依赖冲突；使用临时数据库实际启动 API 和 Worker 两个进程，通过本机 HTTP 确认创建返回 202，并在重启 API 后查到同一任务。核验在创建任务前停止 Worker，没有向示例供应商发送请求；真实供应商端到端投递未验证。未修改代码，因此沿用上面的完整测试结果。

最终验收后修复（2026-10-22）：先复现超长 URL 被接受及 Worker 因 InvalidURL 退出，新增两项回归测试后均失败；修复输入校验与 Worker 异常分类后，完整执行 `.venv/bin/python -m pytest -q`，结果为 **92 passed, 1 warning in 4.96s**。验证新无效请求不入库、历史无效任务终止且不影响后续任务，警告仍为上述第三方弃用提示。
