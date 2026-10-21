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

## 归档与提交状态

- [x] 根据用户明确要求，从本次 Codex 会话的本地真实事件记录导出根目录 `ai_conversations.zip`，包括 Markdown、逐条 JSONL 和来源校验信息。仅提取用户消息与助手可见回复，不生成虚构记录。
- [x] ZIP 包含规格审查、分阶段实现、验收与 URL 修复期间的真实交互。导出截止时间见 ZIP 内 manifest.json；本轮最终确认及更晚消息未包含。已验证 ZIP 完整性及消息与来源逐条一致。
- [x] 已创建私有 GitHub 仓库 https://github.com/Fangzhou26/rc_sophia 。
- [x] 向私有仓库 main 分支提交源码、测试、文档和上述 ZIP；源码提交为 b7a3016。
- [x] 从远端重新克隆，逐文件比对上传内容，验证 ZIP 完整性及会话 JSONL 的 SHA256。使用已有 Python 3.10 项目环境执行完整测试，92 passed、0 failed、1 warning，耗时 5.69 秒。本次复核复用已安装依赖，未重新安装。

原工作区 `.git` 为空且受保护，上传通过独立临时检出完成；远端为 https://github.com/Fangzhou26/rc_sophia ，代码及 ZIP 已推送。仓库当前为私有，如评审需要访问，需按作业要求授予权限或调整可见性。

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

以上保留为其他环境的提交参考；本项目已由用户授权完成上传，无需重复初始化或推送首个提交。
