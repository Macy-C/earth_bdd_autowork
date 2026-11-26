# Copilot 接入目录

本目录保存 VS Code / Copilot 能发现和执行的入口，不是基础业务能力目录。
完整分工和当前能力索引见 [AI 资产目录](../ai/README.md)。

| 位置 | 用途 | 不应放入 |
| --- | --- | --- |
| [copilot-instructions.md](copilot-instructions.md) | 仓库唯一通用规则（架构红线、工作与审批底线、文档路标） | 每个任务的完整操作步骤 |
| `instructions/`（按需创建） | 仅在确需按 `applyTo` 文件范围生效的专项规则时创建 | 与通用规则重复的全局条款或业务实现 |
| [skills/](skills/) | Skill 描述、触发场景和任务正文引用 | 与正文重复维护的完整流程 |
| [prompts/](prompts/) | 手动 Prompt 命令的名称、参数提示、Agent 配置和正文引用 | 与 Skill 重复的同名命令 |
| `agents/`（按需创建） | 确需工具隔离时的专用 Agent 入口配置 | 通用函数实现或重复业务流程 |
| [hooks/](hooks/) | 宿主事件适配和必要控制脚本 | 业务算法或独立运行状态源 |

## 平台入口与工作流分离原则

- `ai/prompts/` 维护与具体 IDE / AI 助手解耦的唯一工作流正文；核心能力仍保留独立 Python 模块与 CLI 入口，不强绑定 Copilot。
- 本目录只放 Copilot 的发现元数据、参数提示及高频固定 CLI 调用卡；复杂分支、边缘恢复与状态规则统一回到 `ai/prompts/` 正文，不在入口重复维护两套业务规则。
- 例如 [Collaboration Review](prompts/collaboration-review.prompt.md) 仅保留命令配置与 [正文链接](../ai/prompts/collaboration-review.md)，修改流程时只改正文。

## 新增入口的选择

- 现有功能需要自然语言判断和多步组合：增加 Skill，正文只保留一份。
- 仅需要一个手动调用的固定提示：增加 Prompt 命令，不必再增加 Skill。
- 只有工具权限、执行约束或交互方式需要隔离时，才按需增加 Agent。
- 只有必须在宿主事件上执行的确定性控制，才增加 Hook。
- 不要求每项能力同时拥有 Skill、Prompt、Agent 和 Hook。

[知识增强 BDD 生成入口](skills/bdd-knowledge-generate/SKILL.md)由现有本地 Coding Agent
执行，唯一正文在 [ai/prompts/bdd-knowledge-generate.md](../ai/prompts/bdd-knowledge-generate.md)，并与案例导出/发布工作流保持独立。