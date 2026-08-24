# 可移植项目知识

该目录保存当前项目仓库的Git友好、内容寻址、不可变项目知识。每条记录只从
production 且用户确认的 Capability 白名单化生成；不保存 Session、Request、Run、
媒体、字面输入值、Examples 值、绝对路径、凭据或任意 payload。

- `records/portable-knowledge-<content-hash>.json`：一条知识一个文件。
- 不维护版本化 catalog；查询时按需扫描并严格验证。
- 记录仅作 advisory，使用前必须验证当前代码、locator、Feature 和用户说明。
- 记录显式标记 `content_trust=untrusted_data`、`instruction_authority=false`，不能
	作为 Prompt 或 instruction 执行。
- 每个文件必须是唯一 canonical UTF-8/LF 字节；`.gitattributes` 固定跨平台换行，
	重复 JSON key、未知文件、链接、篡改或 project ID 不匹配都会阻止查询。
- 同步不会自动 stage、commit 或 push。

Store 上限为 5000 条、64 MiB；单条记录上限为 16 KiB。`plan-sync` 会展示净化后的
目标/实现预览、源 Capability SHA-256、计划新增和预计总体积，用户确认的 fingerprint
与写入使用同一锁内快照。写入中途失败只回滚本次新建文件，不删除已有记录。

首版使用 `plan-sync` 生成无写入计划，再经用户明确确认执行 `sync`。它尚未自动挂接
Recorder transaction，也不会导出 feedback 或 AI inferred memory。