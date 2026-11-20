# Copilot 接入目录

本目录保存 VS Code / Copilot 能发现和执行的入口，不是基础业务能力目录。
完整分工和当前能力索引见 [AI 资产目录](../ai/README.md)。

| 位置 | 用途 | 不应放入 |
| --- | --- | --- |
| [copilot-instructions.md](copilot-instructions.md) | 工作区通用边界、必要上下文入口 | 每个任务的完整操作步骤 |
| [instructions/](instructions/) | 按任务描述或文件范围加载的约束 | 报告合并、文件上传等业务实现 |
| [skills/](skills/) | Skill 描述、触发场景和任务正文引用 | 与正文重复维护的完整流程 |
| [prompts/](prompts/) | 手动 Prompt 命令的名称、参数提示、Agent 配置和正文引用 | 与 Skill 重复的同名命令 |
| [agents/](agents/) | 专用 Agent 的工具、执行边界和入口配置 | 通用函数实现 |
| [hooks/](hooks/) | 宿主事件适配和必要控制脚本 | 业务算法或独立运行状态源 |

## 为什么有两套 Prompts

本目录的 `.prompt.md` 是平台发现入口。`ai/prompts/` 中的 `.md` 是任务正文，
不直接注册成 VS Code Prompt 命令。

例如 [Collaboration Review](prompts/collaboration-review.prompt.md) 只有命令配置和
[正文链接](../ai/prompts/collaboration-review.md)，修改流程时只改正文。

## 新增入口的选择

- 现有功能需要自然语言判断和多步组合：增加 Skill，正文只保留一份。
- 仅需要一个手动调用的固定提示：增加 Prompt 命令，不必再增加 Skill。
- 只有工具权限、执行约束或交互方式需要隔离时，才增加 Agent。
- 只有必须在宿主事件上执行的确定性控制，才增加 Hook。
- 不要求每项能力同时拥有 Skill、Prompt、Agent 和 Hook。

[知识增强 BDD 生成入口](skills/bdd-knowledge-generate/SKILL.md)由现有本地 Coding Agent
执行，唯一正文在 [ai/prompts/bdd-knowledge-generate.md](../ai/prompts/bdd-knowledge-generate.md)。
它与案例导出/发布及 Recorder Job 分开；P1 只提供入口与流程，工具接通、新会话发现和真实生成效果另行验证。

现有 Recorder Generation Agent 及其内部 Skill 是已建立的专用入口，
本次目录整理不改变其 Job、权限或 Hook 协议。