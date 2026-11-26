# BDD Autowork 框架 AI 资产

顶层`ai/`只保存BDD Autowork框架拥有的AI契约。项目更新框架时可随框架版本完整替换
该目录；当前项目的知识库绑定位于[`Bdd/config.yaml`](../Bdd/config.yaml)（`WEKNORA_SETTING.KNOWLEDGE_BASES`），本地运行期与维护期知识位于`artifacts/ai/`。

```text
ai/
  manifest.json             # 资产包版本和路径清单
  context/                  # 协作复盘 Schema 与 Shadow Policy
  prompts/                  # 工作流的唯一正文
  tools/
    README.md               # 实现归属和工具清单
    knowledge/              # 知识任务的实际 AI 辅助实现
    __init__.py             # 包定义，不聚合业务 API
```

## 目录分工与唯一正文

| 层次 | 位置 | 职责 |
| --- | --- | --- |
| 平台接入 | [`.github/`](../.github/COPILOT.md) | 仓库总规则与 Copilot 的发现、命令和事件适配 |
| 任务正文 | [prompts/](prompts/) | 意图判断、能力组合、停止条件和任务级边界 |
| 策略与契约 | [context/](context/) | 协作复盘 Schema 与 Shadow Policy，不保存运行事实 |
| AI 辅助 | [工具索引](tools/README.md) | 按能力归类的 AI 专属内容编辑、上下文提取和输入编排 |
| 基础业务 | [autowork_core/](../autowork_core/) | 不依赖 AI 的采集、导出、报告、执行和外部服务能力 |
| 宿主适配 | [Bdd/](../Bdd/) | 项目原有脚本、配置和人工调用入口 |

一个任务只维护一份工作流正文。`ai/prompts/` 保存平台中立的任务规则，基础业务能力保留独立函数和 CLI；
`.github/` 仅提供 Copilot 的发现元数据与轻量调用适配（如高频固定 CLI 调用卡），不在入口复制业务细则。
后续若接入其他 IDE 或 AI 助手，复用同一份 `ai/prompts/` 正文和底层 CLI，按目标宿主实际能力单独新增薄适配并验证，不提前宣称未验证的平台兼容。
新增能力优先复用现有函数和入口；不建立额外注册中心，也不默认同时创建 Skill、Prompt、Agent 和 Hook。

普通 Feature/Scenario 生成使用 [bdd-knowledge-generate 正文](prompts/bdd-knowledge-generate.md)、
[`Bdd/config.yaml`](../Bdd/config.yaml) 中的知识库角色配置以及按需展开的脚本/动作指南和当前 API。

## 当前入口索引

| 能力 | 用户或宿主入口 | 唯一任务正文 | 实现归属 |
| --- | --- | --- | --- |
| 知识案例辅助 | [Knowledge Assist Skill](../.github/skills/knowledge-assist/SKILL.md) | [knowledge-assist.md](prompts/knowledge-assist.md) | 核心知识能力与 AI 润色辅助分开 |
| 知识增强 BDD 生成 | [BDD Knowledge Generate Skill](../.github/skills/bdd-knowledge-generate/SKILL.md) | [bdd-knowledge-generate.md](prompts/bdd-knowledge-generate.md) | 本地 Coding Agent 的普通生成流程 |
| 协作复盘 | [Review Prompt](../.github/prompts/collaboration-review.prompt.md) | [collaboration-review.md](prompts/collaboration-review.md) | 明确请求下的只读 AI 分析 |
| 协作规则晋升 | [Promote Prompt](../.github/prompts/collaboration-promote.prompt.md) | [collaboration-promote.md](prompts/collaboration-promote.md) | 明确批准边界内的规则维护 |
| 最终报告辅助 | [Report Merge Skill](../.github/skills/report-merge/SKILL.md)；[项目脚本](../Bdd/final_report.py) 独立保留 | [report-merge.md](prompts/report-merge.md) | [核心报告模块](../autowork_core/runtime/reporting/final_report.py)，AI 仅做意图组合及可选分析 |
| 框架文档同步 | [Document Sync Skill](../.github/skills/document-sync/SKILL.md) | [document-sync.md](prompts/document-sync.md) | [文档同步核心](../autowork_core/knowledge/document_sync.py) 与[后台更新任务](../autowork_core/knowledge/document_update.py)；统一刷新哈希，确认后保留并标记旧版、验证新版再删指定旧 ID；任务状态可查询、恢复和取消，普通上传不删除 |

## 工具分类原则

`tools/knowledge/` 保存当前知识任务的真实 AI 辅助。工具根目录不放业务实现、
聚合业务导出或临时转发层。只有确实有 AI 辅助代码时才建立子包，
不能把已有核心函数为了 Skill 再包装成一套业务实现。历史讨论文档用于解释背景；
当前入口以本索引、任务正文和代码为准。

[manifest.json](manifest.json) 是 AI 资产包的发现与交付清单，不是运行时工作流注册中心、
业务配置或执行状态源。新增入口时同步清单和本索引。

采集导出、证据抽取、文档校验、WeKnora 传输客户端和基础 CLI 位于
[`autowork_core/knowledge/`](../autowork_core/knowledge/)。它们不依赖 AI 或 Skill；
采集运行时直接调用核心导出，基础命令入口是
`python -B -m autowork_core.knowledge`，函数从对应核心模块调用。
Skill 根据用户意图组合能力，不能把润色、
上传和 Wiki 审阅绑定为必经流程。

## 生命周期边界

| 位置 | 生命周期 | 内容 |
| --- | --- | --- |
| `ai/context`、`ai/prompts`、`ai/tools` | 随代码版本化 | 人工可读且需要审阅的 AI 约定与确定性辅助工具 |
| `artifacts/knowledge-captures/<run_id>` | 绑定单次采集 Run，远端接受后可按整 Run 清理 | 运行观察 `observations.json`、去重 PNG 截图、AI 润色规格与生成的单文档图文案例 |
| `artifacts/ai/knowledge` | 本地维护期缓存 | Collaboration Review/Promotion 与 Shadow Companion 恢复状态 |
| Copilot `/memories/repo/` | 本机缓存，可丢失 | 编辑器为了当前 workspace 提供的辅助记忆 |

## Copilot 发现与控制入口

VS Code 与 Copilot CLI 从固定位置发现 workspace 资产，因此以下文件必须保留：

```text
.github/copilot-instructions.md
.github/skills/*/SKILL.md
.github/prompts/*.prompt.md
.github/hooks/*.json
.github/hooks/scripts/*
```

`.github/hooks`承载独立的Shadow Companion恢复检查。

## 协作复盘

编码 Agent 与用户之间的长期默契采用“只读复盘 -> 明确批准 -> 最小晋升”流程：

```text
/collaboration-review [30 days or session IDs]
  -> artifacts/ai/knowledge/collaboration-reviews/<review-id>.json

/collaboration-promote <review-path> <approved candidate IDs>
  -> user memory / .github instructions / ai context
  -> artifacts/ai/knowledge/collaboration-promotions/<promotion-id>.json
```

Collaboration Review 和 Promotion 只由框架负责人明确手动调用。项目不提供到期提醒、
会话计数或 SessionStart Hook，也不因项目活动建议复盘。仓库无法认证组织角色，负责人
授权由组织治理。Review 只分析当前仓库会话并生成候选，不修改 instructions、Prompt、
context、用户 memory 或 Git。Promotion 必须在当前请求中收到精确候选 ID；“应用有益
规则”等模糊表达不算批准。

Review 和 Promotion 回执属于可能包含个人工作习惯的本地知识，受
`.gitignore`（`artifacts/`）保护，不进入普通代码提交。版本化schema位于
`ai/context/collaboration-*.schema.json`。自动提醒、自动读取会话、自动 Review 和自动
规则晋升均未启用，以保留隐私、审阅、撤销和作用域判断。

## 项目级上下文卫生

实质性的项目实现、调试、Review 和架构分析遵循
[仓库规则](../.github/copilot-instructions.md)与[架构设计](../docs/维护文档/2.架构设计.md)中的
Project Context Hygiene 约定。Agent 只在验证切片完成、Review 收敛、
目标改变或准备交接时内部识别语义里程碑，不按固定 turn/token/tool 数量截断，也不
自动压缩、丢弃上下文或切换会话。独立 Epic 只在当前任务完成后建议新会话。

大段工具输出在默认上下文中保留结构化摘要和原始输出路径，原文不删除并可按需展开。
该层不创建状态，不拥有测试或工具选择权，也不限制 AI 能力和完整 fallback。

## Shadow Companion

项目级 Shadow Companion 由 `context/shadow-companion-policy.json` 一次授权，不再
逐任务询问。Agent 只在 validated milestone 或真实 handoff 自动维护一个 ignored
`artifacts/ai/knowledge/shadow-companion/active.json`；SessionStart Hook校验后只注入短恢复指针。
完成即删除，未完成目标切换才进入最多 3 个、默认保留 7 天的 dormant recent。

Capsule 不保存源码、终端原文、聊天或凭据，不操作 Git，也不限制搜索/读取/审查、
测试和最终验证。关键文件、项目、分支或环境变化只会标记 stale 并触发正常重查；
损坏、冲突或请求不匹配时使用完整 fallback。

## 不属于本目录的内容

- 协作/文档检查器位于 `autowork_core/utils/debug_tools/`。
- 生成后的 BDD 业务代码在 `Bdd/`。
- 产品专属外部知识Provider及其运维说明由宿主项目文档维护；AI资产只保存受控的查询
  provenance，不复制外部数据库。