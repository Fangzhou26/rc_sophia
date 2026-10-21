# API 通知系统 MVP — Codex 开发说明

> 用途：将本说明交给 Codex 分阶段实现。目标是完成招聘作业要求的**最小可行实现**，并重点展示工程判断、系统边界、可靠性和对 AI 建议的取舍。
>
> **重要**：请先阅读全文，列出发现的设计矛盾/实现风险与实施计划，**不要第一步就一次性生成全部代码**。对本说明没有规定的细节，优先采用最简单、可测试的实现；如需改变核心设计，先解释原因。

## 1. 背景与目标

企业内部多个业务系统需要向第三方供应商 HTTP(S) API 发送事件通知。不同供应商的 URL、Headers 和 Body 不同。内部业务系统不需要等待外部响应，只希望通知任务被可靠接收并尽可能成功投递。

构建一个内部服务：**持久化接受通知任务，再由独立 Worker 异步投递，并具备有限重试、失败保留、进程崩溃恢复能力**。

作业强调的是 AI 辅助下的工程决策与复杂度控制，而非堆砌功能。必须保留真实 AI 会话记录供最终提交。

## 2. 已确定的工程决策（不要擅自改动）

| ID | 决策 | 理由/约束 |
|---|---|---|
| D1 | 有限重试，保留最终失败任务 | 最多 **5 次执行尝试（含首次）**；达到上限或不可重试的失败标记 `FAILED`，不静默丢失。|
| D2 | **Python 3.10 + FastAPI + SQLite + httpx + pytest**；API 与 Worker 独立进程 | 不引入 Kafka、Redis、Celery 或 PostgreSQL；目标是单机小规模 MVP。|
| D3 | **Lease 过期恢复** | 任务领取时设 `lease_until`（默认 60 秒）、生成 `lease_token`；避免崩溃后任务永久停留于 `PROCESSING`。|
| D4 | **指数退避 + Jitter** | 可重试失败延迟重试；基础延迟 10、20、40、80 秒，乘以 0.8–1.2 随机因子，最大延迟 300 秒。|
| D4b | 基础幂等支持 | 每条任务生成稳定 ID，重试时请求内容不变，外发携带稳定 `Idempotency-Key`；**不实现业务事件去重**，不声称可保证第三方恰好处理一次。|
| D5 | **精简可靠版** | 单 Worker、单张任务表、仅两个业务 API，无前端、无手动重投、无投递历史表。|

**精确定义**：这里的 at-least-once 是**对已接受任务的持续尝试语义**，直至收到 2xx 或明确进入终态 `FAILED`；由于重试有上限，**不能保证最终送达**。请求可能被第三方重复执行。2xx 视作 HTTP 层成功，不验证第三方业务是否完成。

## 3. 系统边界

**本版负责：**
- 校验并接收内部系统提交的 HTTP 通知任务。
- 数据库事务提交成功**之后**才向调用方返回 `202 Accepted`。
- 持久保存目标地址、HTTP Method、Headers、JSON Body、状态和重试信息。
- 独立 Worker 领取任务并发送 HTTP 请求。
- 按 HTTP 状态/异常分类决定完成、重试或终止。
- Worker 崩溃后利用 Lease 恢复处理。
- 提供任务状态查询与必要日志、自动化测试。

**本版明确不负责：**
- 确认第三方最终业务效果、跨系统事务、exactly-once processing。
- 供应商特定的认证刷新、Body 转换与业务映射。
- 批量通知、人工重新投递、失败告警平台、管理 UI、分布式部署。
- 同一业务事件被调用方重复提交时的 API 层去重。

**安全边界**：由于接收目标 URL 会涉及 SSRF，MVP 必须要求 HTTPS、限制目标域名在**配置的 allowlist** 中并禁用 HTTP redirect；不允许任意 URL。文档中坦诚指出：域名校验本身不能完全抵御 DNS 重绑定/网络层 SSRF；生产环境还需出口网络策略、DNS/IP 检查、调用方鉴权及凭据管理。不要把样例供应商 URL 当成可访问的真实生产地址。敏感 Headers 不应完整写入日志或状态查询。

## 4. 架构和进程

```text
Internal Business Systems
         |
         | POST /notifications
         v
      FastAPI ------------------> SQLite (durable notifications)
         |                            ^   |
         | 202 after commit           |   | atomically claim eligible task
         v                            |   v
      Caller                     Independent Worker
                                      |
                                      | HTTP POST (httpx, timeout)
                                      v
                               External Supplier API
                                      |
                                      v
                        SUCCESS / PENDING retry / FAILED
```

- API 服务与 Worker 为**两个独立启动的进程**，共享同一个 SQLite 文件（本机持久磁盘）。
- SQLite 配置 `busy_timeout`，可启用 WAL 以减轻读写争用。
- HTTP 请求**必须在数据库事务之外发送**，避免长期持有写锁。
- 本版正常运行时只启动**一个 Worker**；任务领取与写回仍应具备条件更新保护，以覆盖 Lease 过期时的竞态。
- 所有时间均以 **UTC** 进行存取和比较；建议以 ISO 8601 UTC 字符串或整数 Unix 时间戳统一表示，不混用。

## 5. API 设计

### 5.1 `POST /notifications`

示例请求：

```json
{
  "url": "https://crm.example.com/webhook",
  "method": "POST",
  "headers": {"Content-Type": "application/json"},
  "body": {"user_id": 123, "status": "paid"}
}
```

- MVP **只支持 `POST`** 外发，`method` 可省略且默认为 `POST`；若给出其他方法，返回 422/400。
- `body` 支持 JSON 对象/数组等合法 JSON 值，保存序列化数据；不支持 multipart 或任意二进制请求体。
- 对 URL 进行 HTTPS + hostname allowlist 校验；配置允许的供应商测试域名。校验 Headers 的类型，禁止调用方自定义/覆盖 `Idempotency-Key`，由服务统一生成；必要时拒绝 `Host` 等可能破坏请求语义的敏感头。
- 生成 UUID 风格任务 ID；初始状态 `PENDING`，`attempt_count=0`，`next_retry_at=now`。
- DB `INSERT` 成功且事务提交后返回 HTTP **202**：

```json
{"id": "<uuid>", "status": "PENDING"}
```

- 验证失败返回 4xx；数据库不可用、未能提交则返回 5xx，**不得假装已接受**。
- 不要在请求处理函数内发起对供应商的 HTTP 调用，也不要依赖 FastAPI `BackgroundTasks` 做唯一投递机制。

### 5.2 `GET /notifications/{id}`

- 找到任务返回 200，建议只暴露：

```json
{
  "id": "<uuid>",
  "status": "FAILED",
  "attempt_count": 5,
  "last_error": "HTTP 503",
  "next_retry_at": null,
  "created_at": "2026-10-21T12:00:00Z",
  "updated_at": "2026-10-21T12:05:00Z"
}
```

- 不存在返回 404。
- 不向查询者回显密钥或请求中的敏感 Headers/Body。

## 6. SQLite 数据表

唯一核心表 `notifications`：

| 字段 | 建议类型 | 说明 |
|---|---|---|
| `id` | TEXT PRIMARY KEY | UUID，稳定任务标识 |
| `url` | TEXT NOT NULL | 目标 HTTPS URL |
| `method` | TEXT NOT NULL | MVP 固定 `POST` |
| `headers` | TEXT NOT NULL | JSON 序列化 headers |
| `body` | TEXT NOT NULL | JSON 序列化 body |
| `status` | TEXT NOT NULL | `PENDING`, `PROCESSING`, `SUCCESS`, `FAILED` |
| `attempt_count` | INTEGER NOT NULL DEFAULT 0 | **领取/开始执行时**递增 |
| `next_retry_at` | UTC timestamp, nullable | 下次可尝试时间 |
| `lease_until` | UTC timestamp, nullable | PROCESSING 租约截止时间 |
| `lease_token` | TEXT, nullable | 每次领取生成新的随机 token |
| `last_error` | TEXT, nullable | 截断/脱敏后的失败摘要 |
| `created_at` | UTC timestamp | 创建时间 |
| `updated_at` | UTC timestamp | 修改时间 |

建议加 `status, next_retry_at` 的索引；Lease 查询可适当建索引。使用参数化 SQL，不拼接用户输入。

数据库持久性假设：文件放在持久磁盘；SQLite 事务提交提供本地恢复基础，但不等于磁盘/主机损毁时的跨机高可用。README 需声明此限制。

## 7. Worker 状态机和执行顺序

### 7.1 领取任务（原子）

Worker 周期性（例：每秒）进行短事务：

1. 查找可执行的 `PENDING` 且 `next_retry_at <= now`；或 `PROCESSING` 且 `lease_until <= now` 的任务。
2. 如发现 **租约已过期且 `attempt_count >= 5`**，将该任务置为 `FAILED`（例如 `last_error="lease expired after max attempts"`），不要再次领取。
3. 对符合执行条件且 `attempt_count < 5` 的任务，使用短事务 + 条件更新/CAS 原子领取：
   - 设置 `status=PROCESSING`
   - `attempt_count = attempt_count + 1`
   - `lease_until = now + 60s`
   - `lease_token = new_random_uuid`
   - `updated_at = now`
4. 提交事务后才向外发送 HTTP。并发竞争中如果条件更新未成功，应跳过并重新查询。

实现可以使用 SQLite `BEGIN IMMEDIATE` 来序列化写领取操作；避免先 SELECT、后无条件 UPDATE 的竞态。**不要在事务里等待外部 HTTP。**

### 7.2 HTTP 发送

- 使用 `httpx`，总请求超时 **10 秒**，`follow_redirects=False`。
- Headers 从保存内容读取，添加 `Idempotency-Key: <task.id>`，每次重试值相同。
- `url`, `method`, headers 和 JSON body 对**同一任务**保持不变；不根据重试次数改变通知业务数据。
- 只把 **2xx** 视为投递成功（包括 202：仅表示对方 HTTP 层接受）。

### 7.3 结果分类

| 返回/异常 | 结果 |
|---|---|
| 任意 `2xx` | 标记 `SUCCESS` |
| `408`, `429`, `5xx` | 可重试（若未耗尽） |
| 连接错误、超时 | 可重试（若未耗尽） |
| 其他 `4xx`（如 400/401/403/404/409） | 默认不可重试，标记 `FAILED` |
| 其他非 2xx 响应 | MVP 默认不可重试，记录摘要 |

- 可重试失败且 `attempt_count < 5`：改为 `PENDING`，设置 `next_retry_at = now + delay`，清空 Lease 字段。
- 可重试失败但已经第 5 次：标记 `FAILED`，保留 `last_error`。
- 不可重试失败：立即标记 `FAILED`，无进一步重试。
- 成功：标记 `SUCCESS`，清空调度/Lease 字段。
- 所有结果写回必须满足 `id = ? AND status = 'PROCESSING' AND lease_token = ? AND lease_until > now`；更新 0 行代表领取权已失效，不得覆盖较新状态。
- 如 DB 在发送成功后不可用，不要将失败的状态更新视作供应商未收到请求；Lease 到期后可能重复发送，这是既定投递语义。

### 7.4 指数退避 + Jitter

已开始 `n` 次尝试，在第 `n` 次可重试失败时计算下一次延迟：

```python
base_seconds = min(10 * (2 ** (n - 1)), 300)
delay_seconds = base_seconds * rng.uniform(0.8, 1.2)
```

因此第 1、2、3、4 次失败后的基础延迟依次为 10、20、40、80 秒；第 5 次失败为最终 FAILED。提供可注入随机数/时钟，以便测试不必真实等待数十秒。

对于 `429 Retry-After`：MVP 可以暂不专门解析，明确记录为后续增强；**不要假装支持**。若决定实现，需正确处理 delta-seconds/HTTP-date 并限幅，增加对应测试。

### 7.5 Lease 失败场景

- Worker **领取前**崩溃：任务留在 `PENDING`。
- Worker **领取后、发送前/过程中**崩溃：任务停留在 `PROCESSING`，Lease 到期后恢复领取。
- Worker **供应商已处理、更新本地状态前**崩溃：Lease 到期后可能重试，存在重复通知。
- 若连续 5 次领取后均崩溃：过期清理将任务转为 `FAILED`，不会无限领取；执行次数并不等于供应商实际收到的 HTTP 请求次数。
- `lease_token` 只防止过期 Worker 覆盖数据库状态，**不阻止**其已发出的外部请求产生重复操作。

## 8. 建议工程目录

```text
rc_<your_nickname>/
├── README.md
├── requirements.txt          # 或 pyproject.toml，二选一
├── app/
│   ├── __init__.py
│   ├── main.py               # FastAPI 路由
│   ├── database.py           # 初始化与 SQLite 操作
│   ├── models.py             # Pydantic 数据模型
│   ├── worker.py             # 独立 Worker、状态机
│   └── config.py             # 超时、allowlist 等配置
├── tests/
│   ├── test_api.py
│   └── test_worker.py
├── docs/
│   └── decisions.md          # 真实的设计取舍/约束
└── ai_conversations.zip      # 最终手动整理的真实会话，不能伪造
```

此结构是建议，不要为追求分层而引入无意义的抽象。请提供 API 与 Worker 各自的启动命令和环境配置示例。

## 9. 测试要求（自动化、无需真实第三方网络）

必须实现并运行以下测试，优先使用 `pytest` + mock transport / 本地假 HTTP 服务 + 可控时钟：

1. **成功接受**：合法通知写入数据库，提交完成后返回 202；初始 `PENDING`, `attempt_count=0`。
2. **入库失败**：模拟数据库异常，不得返回 202。
3. **成功投递**：Worker 外发一次，收到 2xx，任务 `SUCCESS`；同一任务的 `Idempotency-Key` 等于任务 ID。
4. **临时失败**：503 / timeout 能安排重试，`next_retry_at` 递增且 Jitter 在预期范围；随后 2xx 变成 `SUCCESS`。
5. **永久失败**：400 / 401 立即 `FAILED`，不安排重试。
6. **重试耗尽**：连续可重试错误后，最多领取 5 次，最终 `FAILED`。
7. **崩溃恢复**：模拟 `PROCESSING` 且 Lease 到期，重新领取并完成；Lease 未到期不可领取。
8. **租约竞争**：旧 token 的 Worker 不能覆盖新的 Worker 结果。
9. **耗尽且租约过期**：`attempt_count=5` 时正确标为 `FAILED`，不发第 6 次请求。
10. **API 验证**：非 HTTPS/不在 allowlist 的 URL 拒绝；查询不存在 ID 返回 404；重定向不会被跟随。
11. **进程重启持久化**：使用真实临时 SQLite 文件重新连接后任务仍存在。

测试不应仅仅验证函数调用次数，还要断言数据库最终状态和关键字段。优先关注可靠性行为，而不是形式化 100% coverage。

## 10. README / 设计文档的必答问题

README 要提供：
1. 问题理解和系统边界：做什么、不做什么及原因。
2. 架构图或文字数据流；API 样例、快速启动步骤。
3. 投递语义：为何选择 at-least-once 尝试 + 有限重试，而不是 exactly-once；失败最终如何处置。
4. 故障矩阵：进程崩溃、HTTP timeout、5xx、4xx、长时间不可用、数据库写失败、重复投递。
5. 核心决策和拒绝的过度设计：为什么不用内存 BackgroundTasks、Kafka/Redis/Celery、完整供应商适配框架、Dashboard。
6. 安全边界/限制：SSRF 防护仅基础措施；无完整调用方认证；SQLite 单机持久化，不是 HA。
7. 演进路径：PostgreSQL/分布式 Worker、Outbox/Queue、监控告警、失败重投、供应商配置、事件去重/幂等支持、生产级网络策略。
8. 可运行的测试命令和实际测试结果，禁止声称未经验证的功能。

## 11. Codex 执行方式（请严格分阶段）

**阶段 0 — 只做审查，不编码**
- 阅读本规范。
- 列出不清楚或有风险的地方，给出简明决策建议。
- 列出拟创建文件、执行步骤和测试策略。
- 不改变 D1–D5 的设计方向。

**阶段 1 — 骨架与 API**
- 创建依赖、SQLite schema、FastAPI 的 POST/GET。
- 完成接收持久化、输入校验的测试。

**阶段 2 — Worker**
- 实现原子领取、Lease、httpx 投递、分类、退避、状态更新。
- 完成单元测试和故障恢复测试。

**阶段 3 — Review 与修复**
- 在代码审查中重点检查：任务丢失、原子领取、Lease 竞争、超时、重试上限、SSRF、敏感信息日志。
- 修复前先提供复现测试；不要因为 AI 的风格偏好随意重构。

**阶段 4 — 文档与提交准备**
- README 和 `docs/decisions.md` 必须描述**实际实现**。
- 给出可复现启动和测试命令。
- 提醒用户**手动**导出 GPT、Codex、Copilot 的真实对话并打包为 `ai_conversations.zip`；不允许生成虚构聊天记录。
- GitHub repo 按作业要求命名 `rc_{your_nickname}`（替换占位符），确认最终仓库包含 ZIP。

## 12. 完成标准（Definition of Done）

- [ ] API 与 Worker 可分别启动且共享同一持久化 SQLite 文件。
- [ ] 创建任务成功后返回 202，数据库失败不返回 202。
- [ ] Worker 能异步发送 JSON POST 并正确识别 2xx、4xx、5xx、超时。
- [ ] 可重试错误最多 5 次执行尝试，指数退避 + Jitter 正确。
- [ ] `PROCESSING` 租约过期任务可恢复，达到上限时转为 `FAILED`。
- [ ] token 校验防止过期 Worker 覆盖当前状态。
- [ ] 重试时业务请求内容和 `Idempotency-Key` 稳定。
- [ ] URL allowlist、HTTPS、禁用重定向及基本 Header 限制生效。
- [ ] 自动化测试真实运行并通过，保留可查看的测试命令/结果。
- [ ] README 解释系统边界、风险、方案取舍和演进。
- [ ] GitHub 仓库与真实 AI 会话 ZIP 满足作业提交要求。

---

### 提醒 Codex

工程目标不是构建高吞吐、多租户的生产级通知平台，而是在明确约束内构建**逻辑闭环、故障可恢复、易于验证的 MVP**。发现实现范围不断膨胀时，优先回到系统边界，说明需要增加复杂度的确切故障场景。
