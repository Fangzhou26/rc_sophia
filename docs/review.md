# 阶段 3 审查记录

审查日期：2026-10-22。范围为任务接收持久化、原子领取、Lease 竞争、超时、重试上限、URL/Header 校验与敏感信息输出。没有改变 D1–D5，没有做风格性重构。

## 已复现并修复

| 问题 | 复现及影响 | 修复 |
|---|---|---|
| Unicode 编码异常 | body 的字符串、对象键或数组项包含 JSON 转义的孤立代理字符时，模型接受，但数据库写入抛 UnicodeEncodeError，API 无法正常处理 | 序列化使用 ensure_ascii=True，使保存和发送的 JSON 字节可编码；保留解码后的业务值 |
| 校验位置回显输入 | 额外字段名、错误类型的 Header 键名会原样进入错误 loc，含敏感内容时被回显 | loc 仅保留固定顶层字段，删除所有用户控制的键名 |
| 不可发送的 Header 被接受 | Header 值首尾空格通过校验，但 HTTP/1.1 协议实现拒绝该值，任务收到 202 后仍无法投递 | 在接收时返回 422，不自动裁剪凭据 |

修复前先运行复现测试：

```bash
.venv/bin/python -m pytest -q tests/test_api.py -k 'surrogate or controlled_field or header_keys'
# 5 failed（修复前）
.venv/bin/python -m pytest -q tests/test_api.py -k 'edge_spaces'
# 3 failed（修复前）
```

## 可靠性复核

- 创建任务在事务提交后返回，独立连接可观察记录；模拟提交失败会回滚且不返回 202。
- 领取和过期清理在 BEGIN IMMEDIATE 事务内，HTTP 在提交后发送；测试中的独立写连接可在 HTTP handler 内取得写锁。
- 两个独立连接同时竞争时只有一个成功领取；旧 token、已到期 token 和已终结任务均不能覆盖新状态。
- 最多领取 5 次；第 5 次领取后崩溃也会过期转为 FAILED，不发送第 6 次请求。
- 新增真实子进程领取并提交后调用 os._exit 的测试：父进程观察到 PROCESSING，租约未到期不可领取，到期后新连接恢复并完成。该测试覆盖领取后、发送前退出，未模拟断电或磁盘损毁。
- HTTP 总超时测试使用阻塞 mock transport，验证取消并安排重试；不消费响应正文，响应会关闭。
- 成功发送后状态写回失败，保持 PROCESSING，Lease 到期后可能重复发送；这属于既定语义。
- 重试使用相同业务字节、URL、方法及 Headers；稳定 Idempotency-Key，不跟随重定向。
- Worker 自有日志不记录凭据或网络异常原文；入口将 HTTPX/httpcore 日志限制为 WARNING。状态查询和输入校验响应不包含请求正文或敏感键名。

## 验证结果与边界

```text
.venv/bin/python -m pytest -q
90 passed, 1 warning in 6.40s
```

Python 3.10.12，经授权在沙箱外执行，未访问真实供应商网络。warning 来自 Starlette 使用 AnyIO 已弃用的 BlockingPortal 别名。

本轮没有发现需修改的领取或 Lease 状态机缺陷。测试不代表生产安全认证：域名校验无法独立防止 DNS 重绑定；服务无调用方鉴权、请求限额或凭据加密；系统时钟大幅跳变可能影响 Lease 与调度；SQLite 仍依赖本机持久磁盘。既定有限尝试可能最终失败，重复通知仍可能发生。生产化能力继续保持在 MVP 范围之外。

## 最终验收追加修复：超长 URL

最终验收在原有 90 项测试之外复现：allowlist 内域名附加 66,000 字符路径后，POST 返回 202，HTTPX 抛 InvalidURL（不是 RequestError 子类），导致 Worker 主循环退出，任务留在 PROCESSING。

先增加两项回归测试，执行 `pytest -q -k oversized_url` 得到 2 failed、90 deselected，再修复：

- models.py 使用 HTTPX URL 解析器检查客户端可处理性，异常转换为校验错误；超长 URL 返回 422，数据库无新增记录。
- worker.py 单独捕获 InvalidURL，保存固定脱敏摘要并终止任务；此前已入库的数据不会使 Worker 退出，后续合法任务仍成功。

修复后完整命令 `.venv/bin/python -m pytest -q`：92 passed、0 failed、1 warning，耗时 4.96 秒。未扩大重试范围、修改 schema 或增加功能。
