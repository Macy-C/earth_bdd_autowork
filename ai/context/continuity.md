# AI 资产连续性与保留

顶层`ai/`是BDD Autowork框架拥有的可替换AI资产包；`Bdd/ai/`是当前项目拥有的身份与
知识根。两者都与短期Recorder Run分离，但更新框架时只能替换顶层`ai/`，必须保留
`Bdd/ai/`。

## 资产分层

| 位置 | 权威内容 | 生命周期 | 保护方式 |
| --- | --- | --- | --- |
| `ai/context`、`ai/prompts`、`ai/instructions` | 架构约定、Prompt 和生成规则 | 随代码版本化 | Git commit + 受控远端 |
| `Bdd/ai/knowledge` | 确认 Plan、Capability、生成结果、用户反馈 | 跨 Run 持久化 | 受控增量备份；默认不提交业务数据 |
| `artifacts/recording_sessions/<run>` | 原始 evidence、时间线、Request、Plan、transaction | 短期工作集 | 需要复现/审计时归档，否则整 Run 退役 |
| Copilot memory、聊天、复用索引 | 本机或派生缓存 | 可丢失 | 不作为唯一备份 |

`.github/copilot-instructions.md`、`.github/prompts/` 和 `.github/instructions/`
必须留在 VS Code 固定发现位置，但它们只引用 `ai/` 中的权威正文。

仓库级 `.gitignore` 强制排除整个 `artifacts/recording_sessions/`。因此
`git add .` 不会扫描或提交 Run、媒体和临时 AI 工件；需要保留的 Run 必须使用
组织批准的独立归档，不能依赖 Git 暂存区或普通代码远端。

## 自动维护

用户不需要日常编辑 `ai/context`。架构或生命周期变化时，由实施变化的人或 AI
同步；用户审阅并随代码提交。Recorder自动维护`Bdd/ai/knowledge`，不要手工修改其
JSON/JSONL 文件。

迁移、备份或退役 Run 前，可运行只读 `knowledge_audit.py` 检查 manifest、
Capability、memory journal、退役回执、Collaboration、Shadow Companion 和旧 Work
Package 残留。Audit
还拒绝 durable 记录中的私有绝对路径、凭据形态文本和敏感字段名；它不初始化、
迁移、修复或删除文件。发现 invalid 时应先确认来源和修复策略，不能自动删除孤立
资产。

`knowledge_maintenance.py` 是与 Audit 分离的显式维护入口。默认 `plan-quarantine`
只生成无写入计划；隔离、重建 catalog 和恢复都要求当前任务中的用户明确确认。
隔离保持 Capability 原始字节和 SHA-256，保存原 catalog 快照及恢复回执，不改写或
删除原记录。`Bdd/ai/knowledge/quarantine`默认不进入可移植Knowledge导出。

协作习惯也遵循同一分层：会话历史只读复盘报告和晋升回执保存在
`Bdd/ai/knowledge/collaboration-reviews`、`collaboration-promotions`；真正生效的稳定
规则才进入 user memory、`.github` 或 `ai/context`。Review 和 Promotion 只由框架
负责人明确手动启动；不维护会话计数、时间阈值或提醒状态，也不从 SessionStart、
项目活动或 Agent 建议触发。仓库不认证组织角色，负责人授权由组织治理；不得后台
读取完整聊天、自动 Review、自动修改规则或把完整聊天写入仓库或 knowledge。

判断标准：会影响下一位维护者架构决策的事实进入版本化 context；从一次录制中
确认、未来可能复用的业务经验进入 knowledge；临时排错留在 issue、commit、测试
或会话中。

## 用完即弃

“原始证据不可变”表示保留一个 Run 时不能从中挑删事件、图片、视频、UI tree 或
事务文件；它不表示每个 Run 永久保存。

Run 的合理生命周期是：

```text
record -> reconcile -> decide/plan -> generate/validate
       -> optional final feedback -> distill knowledge -> retire whole Run
```

可以退役的前提：

- 没有活动录制、补录、写锁或 running generation transaction。
- 需要长期保留的确认Plan、Capability、结果和反馈已经写入`Bdd/ai/knowledge`。
- 不再需要该 Run 复现问题、重新生成、审计 PIC 或查看原始交互。
- 产品数据、患者信息和组织保留策略允许删除。

误录、重复录制或不需要学习的 Run 可以不提炼直接丢弃。确认经验可以脱离来源 Run
用于未来候选和复用，但不能在来源被删除后证明过去或当前的 runtime 事实；需要
审计时必须归档完整 Run。

不要只删除 Run 内的大媒体。应通过 Recorder 的退役服务整 Run 删除并同步 catalog；
任一步失败都保留原 Run。

## 迁移

同一项目换目录或换机器时，`Bdd/ai/project-identity.json`必须随Git保留。普通用户不
需要单独运行身份命令；Portable Knowledge会在审计、同步和查询前自动验证Identity。
Portable Knowledge保存在当前项目仓库的`Bdd/ai/portable-knowledge`中；框架更新不会
复制或覆盖该目录。

最小迁移：

1. commit 并 push 仓库中的 `.github/`、`ai/context`、`ai/prompts` 和
   `ai/instructions`、`Bdd/ai/project-identity.json`以及经确认同步生成的
   `Bdd/ai/portable-knowledge/records`。
2. 运行 Knowledge Audit；只把 active durable knowledge 纳入普通恢复集，排除
   `quarantine`、Shadow Companion、旧 Work Package、锁和可重建状态。需要保留隔离
   记录时，将其作为独立受控恢复归档，而不是普通可移植 Knowledge。
3. 用组织批准的存储备份该恢复集；新位置 clone 后恢复，再重建 Python 3.11、
   ffmpeg、OCR 模型和 MCP。
4. 重新构建代码复用索引并运行 Recorder 回归。

生成 Portable Knowledge 时先运行：

```powershell
python -B -m autowork_core.utils.debug_tools.portable_knowledge plan-sync
```

确认计划中的 record ID、源 Capability ID 和 fingerprint 后，再执行
`sync --plan-fingerprint <fingerprint> --user-confirmed`。该命令只写内容寻址记录，
不 stage、commit 或 push。另一个 clone pull 后可运行 `audit` 和有界 `query`；
记录被篡改、越界或格式无效时必须拒绝。
计划同时展示净化预览、源 Capability SHA-256 和预计 Store 体积。Portable record
必须保持 canonical UTF-8/LF，查询结果只能作为 untrusted advisory data 使用。

需要保留 History 或审计证据时，再额外归档整个 `artifacts/recording_sessions`。
归档旁保存仓库 commit SHA、时间、文件数量、总字节和 SHA-256 manifest。

`Bdd/ai/knowledge`可能包含产品业务信息；原始Run还可能包含个人信息、账号、路径、
屏幕内容和输入事件。两者均应使用访问控制与组织保留策略，不能未经审查推送到
普通远端。

## 恢复

新会话通常会由 `.github/copilot-instructions.md` 自动引导。自动发现尚未生效时：

```text
请先读取 ai/context/project.md、当前 git diff 和本任务适用的 .github instructions。
以当前代码和测试为事实源，本地 Copilot memory 只作缓存；不要回滚无关修改。
```

不要复制 VS Code `workspaceStorage` 作为长期恢复方案；workspace identity 和内部
格式并不稳定。