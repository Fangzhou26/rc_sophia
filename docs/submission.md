# rc_sophia 提交清单

最终 GitHub 仓库名由用户确定为 **rc_sophia**。本目录就是项目根目录，无需为了匹配建议结构再嵌套同名子目录。

## 已完成

- [x] Python 3.10 下接收、持久化及查询通知。
- [x] API 与 Worker 独立启动，同一个 SQLite 文件。
- [x] HTTP 分类、最多 5 次领取、退避及 Jitter。
- [x] Lease 恢复、token 条件写回和最终失败保留。
- [x] HTTPS/域名 allowlist、禁用重定向及基本 Header 限制。
- [x] 测试真实运行：90 passed；见 README 和 review.md。
- [x] README 覆盖边界、启动、API、投递语义、故障矩阵、工程取舍、限制及演进。
- [x] 两个真实进程的启动与 API 重启持久化核验，无供应商网络访问。

## 仍需完成

- [x] 根据用户明确要求，从本次 Codex 会话的本地真实事件记录导出根目录 `ai_conversations.zip`，包括 Markdown、逐条 JSONL 和来源校验信息。仅提取用户消息与助手可见回复，不生成虚构记录。
- [x] ZIP 包含规格审查、分阶段实现、验收与 URL 修复期间的真实交互。导出截止时间见 ZIP 内 manifest.json；本轮最终确认及更晚消息未包含。已验证 ZIP 完整性及消息与来源逐条一致。
- [x] 已创建私有 GitHub 仓库 https://github.com/Fangzhou26/rc_sophia 。
- [ ] 向仓库提交源码、测试、文档和上述 ZIP。
- [ ] 从远端重新克隆检查：仓库中确实包含 ZIP，安装依赖后可运行测试。

本工作区 `.git` 为空且只读，`git rev-parse --show-toplevel` 返回“not a git repository”，本地无 Git 远端配置。当前远端仓库已创建，本地 ZIP 已导出，代码及 ZIP 尚未推送。

## 建议提交内容

```text
README.md
requirements.txt
.env.example
.gitignore
API_Notification_Service_Codex_Spec.md
app/
tests/
docs/
ai_conversations.zip       # 本次真实会话导出，已生成
```

不要提交 `.venv/`、`.env`、数据库文件及 WAL/SHM 文件、缓存或真实供应商凭据。默认数据库目录 data/ 已忽略；若自定义了数据库路径，也要保证其不进入版本库。真实会话 ZIP 未被 .gitignore 忽略。

若在另一个可写的本地仓库中整理提交，可使用以下步骤；替换尖括号占位符，先确认 ZIP 存在，再执行 Git 命令：

```bash
unzip -t ai_conversations.zip
git init
git add README.md requirements.txt .env.example .gitignore \
  API_Notification_Service_Codex_Spec.md app tests docs ai_conversations.zip
git diff --cached --stat
git diff --cached --check
git commit -m "Implement durable API notification service MVP"
git branch -M main
git remote add origin https://github.com/<your-account>/rc_sophia.git
git push -u origin main
```

以上是待用户执行的提交指引，不表示已经执行。GitHub 仓库应先创建为空仓库，访问权限由用户按作业要求选择；当前准备不包含发布授权或仓库可见性的推断。
