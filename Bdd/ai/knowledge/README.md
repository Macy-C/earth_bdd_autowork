# Recorder 长期知识库

此目录由 Recorder 自动维护，保存已经从短期录屏中提炼出的自包含 AI 经验。

- `project-memory/events.jsonl`：append-only 经验、生成结果和用户反馈。
- `capabilities/catalog.json`：已确认 Capability 索引。
- `capabilities/capability-*.json`：可脱离来源 Run 检索的结构化能力。
- `collaboration-reviews/*.json`：编码会话只读复盘生成的规则候选。
- `collaboration-promotions/*.json`：用户明确批准后的规则晋升与验证回执。
- `shadow-companion/active.json`：当前项目工程 Epic 的单一 advisory 恢复 Capsule；只在有价值语义里程碑更新，不保存源码或终端原文。
- `shadow-companion/recent/*.json`：显式 archive 的 dormant Capsule，最多 3 个并按保留期有界清理；不会自动选择恢复。
- `work-packages/*`：旧 Shadow 1.0 兼容审计目录；新工作流不再创建，发现后应完成迁移或清理。
- `quarantine/*`：经用户明确确认后隔离的 invalid durable 记录、原 catalog 快照和恢复回执；原字节不改写，默认不进入可移植 Knowledge。
- `manifest.json`：运行时知识仓库版本与安全策略。

运行时文件默认不进入Git，因为可能包含产品业务信息；迁移时应使用受控存储复制
`Bdd/ai/knowledge/`。知识仓库不保存原始媒体，也不能证明当前runtime控件存在、唯一
或可操作。

不要手工修改或删除 quarantine。恢复时使用 `knowledge_maintenance restore`；需要
长期保留时应单独进入受控恢复归档，不能与普通 active knowledge 混为同一迁移集。

协作复盘只由框架负责人明确手动运行，不提供 SessionStart Hook、到期提醒或计数状态。
复盘文件只保存会话 ID、turn index 和短证据摘要，不应复制完整聊天、源码、秘密或
产品数据。它们同样默认不进入 Git；删除前应确认不再需要追踪规则来源。