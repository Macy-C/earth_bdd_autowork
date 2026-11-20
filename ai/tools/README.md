# AI 辅助工具索引

本目录只拥有按能力归类的 AI 专属辅助，不聚合导出基础业务 API，
不提供重复的命令入口。确定性的基础业务仍位于其核心模块。

## 当前分类

| 分类 | 文件或位置 | 职责 |
| --- | --- | --- |
| 知识任务 AI 辅助 | [knowledge/artifact_polisher.py](knowledge/artifact_polisher.py) | 为 AI 提取受保护叙事槽位、校验并应用新副本 |
| 知识任务输入适配 | [knowledge/case_workflow.py](knowledge/case_workflow.py) | 解析本地文件或采集选择，不决定用户是否上传 |
| 包定义 | [__init__.py](__init__.py) | 无业务导出或转发副作用 |
| 基础知识能力 | [核心知识模块](../../autowork_core/knowledge/) | 采集导出、校验、上传和核心 CLI，不属于 AI 工具目录 |

## 新代码放哪里

- 知识任务的 AI 辅助放入现有 `knowledge/` 子包，任务步骤仍只在权威提示词中维护。
- 未来报告、脚本等能力只有确实需要 AI 专属 Python 辅助时，才增加对应子包。
- 如果现有函数或 CLI 已可直接调用，只增加 Skill 入口和任务正文，不创建重复 Python 包装。
- 业务算法、导出、合并、渲染、上传客户端等基础实现仍放在其核心业务模块。
- 不往工具根目录堆业务或转发文件，不为了分类创建空目录。

当前知识任务正文见 [Knowledge Assist](../prompts/knowledge-assist.md)。
全局目录边界见 [AI 资产索引](../README.md)。