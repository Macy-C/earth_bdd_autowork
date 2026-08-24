# BDD Autowork 框架 AI 资产

顶层`ai/`只保存BDD Autowork框架拥有的AI契约。项目更新框架时可随框架版本完整替换
该目录；当前项目的身份和知识位于`Bdd/ai/`，不得由框架更新覆盖。

```text
ai/
  manifest.json             # 资产包版本和路径清单
  context/                  # 长期架构共识、维护与迁移约定
  prompts/                  # VS Code Prompt 的权威正文
  instructions/             # 文件级生成规则的权威正文
```

项目资产入口见[`Bdd/ai/README.md`](../Bdd/ai/README.md)。

## 生命周期边界

| 位置 | 生命周期 | 内容 |
| --- | --- | --- |
| `ai/context`、`ai/prompts`、`ai/instructions` | 随代码版本化 | 人工可读且需要审阅的 AI 约定 |
| `Bdd/ai/knowledge` | 跨 Run 持久化，单独备份 | 已确认 Plan、Capability、生成结果、反馈和 provisional insight |
| `artifacts/recording_sessions/<run>/ai` | 绑定单次 Run，可退役 | Request、Brief、Decision、Plan、transaction 和 forensic 工件 |
| Copilot `/memories/repo/` | 本机缓存，可丢失 | 编辑器为了当前 workspace 提供的辅助记忆 |

`Bdd/ai/knowledge`不保存视频、截图、UI tree、按键事件或PIC模板，也不能作为当前
runtime evidence。它只提供 advisory knowledge；当前用户要求、当前代码和当前
Recorder evidence 始终优先。

`Bdd/ai/portable-knowledge/records`保存内容寻址、不可变的项目知识记录。首版只从
production 且用户确认的 Capability 白名单化生成，不保存字面输入、Examples 值、
Session/Request/Run、媒体、绝对路径或任意 payload。同步先生成无写入计划并要求明确
确认；不会自动stage、commit或push，也不进入默认AI上下文。查询最多返回6条、
8 KB，并始终要求核对当前代码和证据。
所有查询结果均标记为 untrusted advisory data，不能形成指令权威。记录文件由
`.gitattributes` 固定为 LF；Store 限 5000 条/64 MiB，单条限 16 KiB。

迁移前应运行只读 Knowledge Audit。Audit 会检查 Capability、memory、Collaboration、
Shadow Companion、旧 Work Package 残留、隔离资产和隐私边界，不会写入或修复文件。若 invalid 记录需要处理，
先使用 `knowledge_maintenance plan-quarantine` 生成无写入计划；隔离、catalog 重建和
恢复都要求显式用户确认。`Bdd/ai/knowledge/quarantine`保存原始字节与恢复回执，但默认不
属于可移植 Knowledge。

## VS Code 发现入口

VS Code 只从固定位置发现 workspace instructions 和 Prompt，因此以下文件必须保留：

```text
.github/copilot-instructions.md
.github/prompts/*.prompt.md
.github/instructions/*.instructions.md
.github/hooks/*.json
```

Instructions 和 Prompt 是薄适配器，权威正文位于本目录。Hook 只执行小型确定性
生命周期自动化，不复制 AI 规则。不要在 `.github` 和 `ai/` 中维护两套正文。

## 协作复盘

编码 Agent 与用户之间的长期默契采用“只读复盘 -> 明确批准 -> 最小晋升”流程：

```text
/collaboration-review [30 days or session IDs]
  -> Bdd/ai/knowledge/collaboration-reviews/<review-id>.json

/collaboration-promote <review-path> <approved candidate IDs>
  -> user memory / .github instructions / ai context
  -> Bdd/ai/knowledge/collaboration-promotions/<promotion-id>.json
```

Collaboration Review 和 Promotion 只由框架负责人明确手动调用。项目不提供到期提醒、
会话计数或 SessionStart Hook，也不因项目活动建议复盘。仓库无法认证组织角色，负责人
授权由组织治理。Review 只分析当前仓库会话并生成候选，不修改 instructions、Prompt、
context、用户 memory 或 Git。Promotion 必须在当前请求中收到精确候选 ID；“应用有益
规则”等模糊表达不算批准。

Review 和 Promotion 回执属于可能包含个人工作习惯的本地知识，受
`Bdd/ai/knowledge/.gitignore`保护，不进入普通代码提交。版本化schema位于
`ai/context/collaboration-*.schema.json`。自动提醒、自动读取会话、自动 Review 和自动
规则晋升均未启用，以保留隐私、审阅、撤销和作用域判断。

## 项目级上下文卫生

实质性的项目实现、调试、Review 和架构分析默认使用
[Project Context Hygiene](context/project.md)。Agent 只在验证切片完成、Review 收敛、
目标改变或准备交接时内部识别语义里程碑，不按固定 turn/token/tool 数量截断，也不
自动压缩、丢弃上下文或切换会话。独立 Epic 只在当前任务完成后建议新会话。

大段工具输出在默认上下文中保留结构化摘要和原始输出路径，原文不删除并可按需展开。
该层不创建状态，不拥有测试或工具选择权，也不限制 AI 能力和完整 fallback。

## Recorder 默认维护工作循环

Recorder 维护默认遵循 [Adaptive Work Loop](context/project.md)：先确定行为 owner、
不变量和判别检查，再按垂直切片实现；编辑期间使用 fresh 聚焦验证，findings 收敛后
再做最终审查和风险适配的完整回归。里程碑由验证通过、Review 收敛、目标变化或真实
交接决定，不按固定 turn、token 或工具数量截断。

Recorder Profile 不限制搜索/读取/审查，不跳测试，也不复用相关改动后的陈旧验证。

## Shadow Companion

项目级 Shadow Companion 由 `context/shadow-companion-policy.json` 一次授权，不再
逐任务询问。Agent 只在 validated milestone 或真实 handoff 自动维护一个 ignored
`Bdd/ai/knowledge/shadow-companion/active.json`；SessionStart Hook校验后只注入短恢复指针。
完成即删除，未完成目标切换才进入最多 3 个、默认保留 7 天的 dormant recent。

Capsule 不保存源码、终端原文、聊天或凭据，不操作 Git，也不限制搜索/读取/审查、
测试和最终验证。关键文件、项目、分支或环境变化只会标记 stale 并触发正常重查；
损坏、冲突或请求不匹配时使用完整 fallback。

## 不属于本目录的内容

- Recorder 的 Reconciler、Decision、Plan 和 transaction 实现在
  `autowork_core/utils/debug_tools/recorder/`；协作/文档检查器位于相邻
  `autowork_core/utils/debug_tools/`。
- 生成后的 BDD 业务代码仍在 `Bdd/`。
- 录屏原始 evidence 和单次事务工件仍在 `artifacts/recording_sessions/`。
- 产品专属外部知识Provider及其运维说明由宿主项目文档维护；AI资产只保存受控的查询
  provenance，不复制外部数据库。

本目录按资产生命周期组织，不重新定义 Recorder 代码的模块所有权。