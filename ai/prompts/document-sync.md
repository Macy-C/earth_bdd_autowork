# Framework Document Sync

本工作流支持本地 `docs/` 全量目录递归哈希刷新、`docs/` 与远端知识库的目录级对齐检查与同步
（保留 `说明文档/...` 与 `维护文档/...` 相对层级，支持归档移动、缺补、更新与多删）、
普通 Markdown 上传、确认后的异步更新及状态查询。复用核心
`autowork_core.knowledge.document_sync` 及现有 REST 客户端，不调用案例发布器，不要求
Feature/Scenario，不绑定润色、Wiki 验收或发布台账。

## 先确定动作

| 用户意图 | 允许动作 | 不能隐含执行 |
| --- | --- | --- |
| 刷新哈希 | 只改选中文档（默认 `docs/**/*.md`）的本地哈希行 | 上传、更新远端 |
| 目录检查/同步 (`sync-plan` / `sync-apply`) | 先刷新本地哈希并列出 `in_sync`、`to_move`、`to_upload`、`to_update`、`to_delete`；确认后执行 | 未确认移动、上传、替换、删除远端文档 |
| 单文件/指定集合检查 (`compare` / `compare-many`) | 刷新并比较；普通检查有已核实差异时询问后续选择 | 未确认上传、替换、重解析 |
| 上传 (`upload`) | 刷新本地哈希，保留 `docs/` 下相对目录提交选中文件 | 替换、删旧版、自动改名绕过冲突 |
| 查询处理状态 (`status`) | 按明确文档 ID 与库只读查询一次 | 上传、重试、重解析 |
| 更新已有文档 (`update`) | 刷新比较；不一致确认后上传新版（对齐目标目录）、标记旧版，后台验证并删指定旧 ID | 未确认写远端、先删旧再上传、按标题批量删除 |
| 查询更新任务 (`task-status`) | 只读本地任务状态、阶段、历史、异常与 worker 存活信息 | 推进任务、上传或删除 |
| 恢复/取消更新任务 (`task-resume` / `task-cancel`) | 明确任务 ID 后恢复原任务或停止后续清理 | 改换目标、重传未知提交、撤销已提交的远端删除 |

刷新、检查、目录对齐检查 (`sync-plan`)、上传、更新统一先刷新选中文档的本地哈希字段；
这是必要准备步骤，不另问一次刷新许可。只写哈希行，不改正文。状态查询仍完全只读。

## 目录级闭环检查与同步（默认主入口）

用户说“检查或同步下 `docs/` 文档”“看看知识库文档有没有变化”时，默认以 `docs/` 下所有
Markdown 文档（递归扫描 `说明文档/*.md`、`维护文档/*.md` 等）为范围，保留相对 `docs/` 的
目录层级（`folder_path` 为 `说明文档` 或 `维护文档`，绝不带 `docs/` 前缀，也不篡改标题冒充目录）：

1. 先运行只读远端计划命令（内部自动先刷新所有选中本地文档的 `文档哈希（SHA256）`；若 `Bdd/config.yaml` 已配置 `WEKNORA_SETTING.KNOWLEDGE_BASES.FRAMEWORK_DOCS`，可省略 `--kb`，且预检会拦截指向 `ENGINEERING_CASES` 或 `PRODUCT_DOCS` 的跨角色误操作）：

   ```powershell
   python -B -m autowork_core.knowledge.document_sync sync-plan --kb "目标知识库名或ID"
   ```

2. `sync-plan` 会一次性拉取远端文档列表并与本地 `docs/**/*.md` 对齐，输出分类：
   - `in_sync`：路径（`folder_path/file_name`）与内容哈希均一致。
   - `to_move`：远端文档位于旧目录（如根目录 `""`），但文件名唯一且内容一致（`document_sync_hash`
     或原始文件 MD5 `file_hash` 匹配），只需调用 `POST /api/v1/knowledge/folder` 移入目标目录（如 `说明文档`），无需重传重解析。
   - `to_upload`：远端缺失的本地文档，将携带 `relative_path`（如 `维护文档/1.维护者指南.md`）直接上传到对应目录。
   - `to_update`：同名文档内容已变化（无论当前在根目录还是子目录），确认后通过后台安全更新任务将新版直接上传到 `target_folder_path`，旧版打状态标签并在新版解析验证通过后删除。
   - `to_delete`：全量 `docs/` 检查时，远端存在但本地 `docs/` 下已不存在的多余文档。
   - `active_tasks` / `conflicts`：已有未完成更新任务或同名冲突项；存在 `conflicts` 时阻断自动执行。
3. 若全部 `in_sync`，直接告知“远端目录与内容均已是最新状态”并结束；若用户明确“只检查”，展示分类清单后结束。
4. 若存在变更项（`to_move` / `to_upload` / `to_update` / `to_delete`），向用户展示汇总清单并一次性询问确认：
   - **执行完整同步**（移动归档 + 上传缺失 + 更新差异 + 删除远端多余文档）
   - **仅同步新增/变更/目录移动，保留远端多余文档**（加 `--no-delete`）
   - **暂不同步**
5. 用户确认后执行：

   ```powershell
   python -B -m autowork_core.knowledge.document_sync sync-apply --kb "目标知识库名或ID" --confirm
   ```

   （若用户选择保留远端多余文档，追加 `--no-delete`。）
   `sync-apply` 按顺序执行：`to_move` 批量移目录 $\rightarrow$ `to_upload` 上传新文档 $\rightarrow$ `to_update` 启动后台替换任务 $\rightarrow$ 在前三步无错误时对 `to_delete` 提交删除。完成后直接报告各动作结果及 `to_update` 的后台 `task_id`。

## 输入与本地命令

使用用户已有的解释器，从项目根目录执行
`python -B -m autowork_core.knowledge.document_sync`。未指定路径时，默认递归选择仓库
`docs/` 下所有 Markdown 文档（`docs/**/*.md`）；也可传入具体文件路径或子目录路径。

```powershell
python -B -m autowork_core.knowledge.document_sync inspect
python -B -m autowork_core.knowledge.document_sync refresh
python -B -m autowork_core.knowledge.document_sync refresh "docs/说明文档/3.动作参考.md"
```

`inspect` 是底层只读诊断，返回当前 `local_hash`、`declared_hash`、`relative_path`、
`folder_path` 和 `needs_refresh`。`refresh` 只写本地头部的“文档哈希（SHA256）：”一行；
缺少字段时自动添加，保留其余正文、编码标记和原有换行。不要手算或人工编造哈希。

计算使用 SHA256：排除文档头部哈希字段的整行，忽略 UTF-8 BOM，将 CRLF/CR 统一为 LF。
不删除其余空白、不改变代码缩进，不排除代码示例或正文中的相同字样。头部到第一个
二级或更深标题之前为止；重复字段、非法编码等错误应报告，不自动改正文。

## MCP 头部检查与 compare-many（按需使用）

当仅通过 WeKnora MCP 只读检查指定文档头部字段时，仍可使用 `compare` 或 `compare-many`：

```powershell
python -B -m autowork_core.knowledge.document_sync compare-many --kb "已明确知识库" --compact --item "docs/说明文档/3.动作参考.md" "已匹配文档ID" "远端字段的64位十六进制值"
```

## 单文件更新比较与确认 (`update`)

1. 对单个已有文档直接执行更新比较或确认时，可使用 `update` 子命令：

   ```powershell
   python -B -m autowork_core.knowledge.document_sync update "docs/说明文档/3.动作参考.md" --kb "用户指定的库名或ID" --document-id "已核实的远端ID" --remote-hash "远端字段的64位十六进制值"
   ```

2. `in_sync`、`requires_confirmation=false`：报告“远端目前是最新状态”，不提示更新，
   不上传、不重解析。如果服务端另有解析失败状态，单独报告，版本一致不代表处理可用。
3. `needs_confirmation`、`requires_confirmation=true`：展示本地路径、库、远端 ID
   和两边哈希，只确认不一致项。确认说明必须覆盖：上传新版（自动对齐 `docs/` 下相对目录）；创建或复用同库状态标签并关联旧项；新版验证
   成功后删除该旧 ID；新文档 ID 改变，旧引用不自动迁移；并存期间两版可能都被检索。
   检查后的选择已包含这些说明并明确所选项时，不再重复弹出确认。
5. 远端缺失不等于已有项可以更新；报告缺失并询问是否改为上传。无法读取则报告
   “无法核验”，不是“需更新”。不带 `--confirm` 的比较命令保持 `remote_written=false`。
6. 用户明确确认后，传入刚展示的本地哈希和远端哈希，不以新计算值替换过期确认：

   ```powershell
   python -B -m autowork_core.knowledge.document_sync update "docs/3.动作参考.md" --kb "已确认的库名或ID" --document-id "已确认的旧文档ID" --remote-hash "已确认的远端64位哈希" --expected-local-hash "已确认的本地64位哈希" --confirm
   ```

   远端头部确实缺字段时省略 `--remote-hash`；不得用它掩盖读取失败。核心重新校验
   本地和远端原始 Markdown，变化即返回 `confirmation_expired`，先重新比较确认。
   删除前验证需要 REST `retrieve` 和 `ingest` 权限；普通检查仍不下载原文件。
7. 立即报告任务 ID、当前阶段、新旧 ID（已取得的部分）、旧版是否保留及下一步。
   不在聊天中等待解析或反复调用 `status`，后台任务自动接续。`status=running` 仅是
   已提交；只有 `status=completed` 且旧项删除已核实，才报告“更新完成”。

## 后台阶段与异常说明

任务记录位于 `artifacts/document-updates/<task_id>/task.json`，记录目标、原标题、双方
指纹、阶段和历史；不保存 API key。`worker.log` 保留后台输出。复用现有解释器启动
独立进程，正常退出命令不会等待解析；同一旧 ID 的重复请求返回已有任务，不重复上传。

| 阶段 | 必须向用户说明 |
| --- | --- |
| `uploading` | 正在提交新版，旧文件及标题尚未改变 |
| `preparing_new` | 新 ID 已取得，后台核对来源并继承旧标题、目录、标签和自定义元数据 |
| `marking_old` / `waiting_parse` | 关联“旧版保留·新版处理中”状态标签并回读验证；旧文件、标题和文件名保持不变 |
| `deleting_old` | 新版主解析完成且已启用、原始文件字节和哈希正确，再核对旧版未被人工修改 |
| `delete_submitting` / `delete_pending` | 将本任务状态标签切换为“待删除”，删除已提交或结果待核实，不是完成 |
| `completed` | 已确认旧 ID 不存在、目标库仍可读、新版仍验证可用；更新完成 |

已确认更新的破坏性门槛比普通标记检查更严格：核心下载新旧原始 Markdown 以核对
来源字节；不能只信回传的 metadata 哈希，也不能用 Wiki 或摘要替代原始来源。
删除只针对任务保存的旧 ID，不根据“待删除”标签、历史标题前缀或文件名猜测。标签标记
不隔离检索，也不会改标题或文件名。旧版的原有业务标签仍保留，新版只继承这些原有标签，
不继承本任务状态标签。取消或失败只解除本任务添加的状态标签关联，不删除知识库标签定义。

新任务使用 `marker_strategy=tags`，查询时展示 `old_status_marker`、`marker_verified_at`
和 `old_marker_restored`。必须同时有标签定义、文档关联及详情回读，才可报告“状态标签
已关联并验证”，不能把创建标签定义当作已标记文档。官方列表/卡片有标签展示入口；
标签较多时可能折叠，需刷新或展开标签区域。API 回读不等于已实测用户当前浏览器界面，
未做界面验收时不能声称用户一定看到。标签关联无权限或无法验证时暂停，不继续删除。

历史任务没有 `marker_strategy=tags` 时仍按原任务的标题记录核验，查询中的
`old_title_restored` 只表示该历史路径；不自动把历史任务切换成新的标签写入范围。

| 异常 | 必须报告的事实与处理 |
| --- | --- |
| 上传拒绝 | `upload_rejected`；旧文件及标题不动，报告原因，不自动再上传 |
| 上传超时、断连或响应损坏 | `upload_outcome_unknown`；暂停，旧版不动；恢复时按任务标记和精确文件字节查找原提交，不重传 |
| 标签创建/关联失败或无法回读 | 暂停，不删旧版，不宣称标记已成功；定义已创建但未关联时单独说明 |
| 新版解析失败 | 保留旧版；只清理本任务添加的状态标签，显示 `old_marker_restored`；失败新版保留供排查 |
| 字节/哈希不符、本地变化、人工编辑旧项 | 暂停删除，报告冲突；标签或标题有外部变化时不覆盖，保留现状并说明无法自动清理 |
| 未知解析状态或新版未启用 | 暂停，不当作已就绪 |
| 删除失败、断连、无任务 ID或无法确认完成 | 新版保留，报告“两版并存/删除待核实”；不重复上传，不盲目重发 DELETE |
| worker 启动失败、中断、断网或等待超时 | 任务保留，显示 `paused` / `interrupted` 与具体原因；检查后显式恢复，不静默丢失 |
| 主解析完成但摘要失败 | 单列警告；不声称全部增强任务或 Wiki 均成功 |

删除若明确被 400/401/403/405/413/422 拒绝，不视为已提交；修复原因并显式恢复后，
重新验证新旧项才可执行。超时、5xx、断连或缺任务 ID 仍属于结果未知，恢复只核查，
不会自动重发。404 还须核对目标知识库仍可读取，不能把权限丢失当作删除成功。

任务锁只防止本机重复执行，不会锁住其他客户端。公开删除接口没有本任务可用的条件
删除事务，因此不能保证跨客户端原子替换；应避免同时编辑同一远端文档，检测到变化
即暂停，不能宣称已经锁住整个知识库。

默认后台每 15 秒检查一次，单次 worker 等待上限 1 小时；到期暂停并保留进度，不自动
删除。电脑休眠、关机或进程终止时不能保证继续执行，恢复后需显式接续任务；不配置
系统服务或开机计划任务。没有自动向当前聊天推送最终结果的通道，需通过任务状态查看
历史和最终异常，不承诺主动聊天通知。

```powershell
python -B -m autowork_core.knowledge.document_sync task-status "返回的任务ID"
python -B -m autowork_core.knowledge.document_sync task-status "返回的任务ID" --summary
python -B -m autowork_core.knowledge.document_sync task-resume "已核对的任务ID"
python -B -m autowork_core.knowledge.document_sync task-cancel "要停止的任务ID"
```

`task-status` 默认完整返回；普通进度查询可用 `--summary`，仅省略历史数组，并明确返回
`history_count`、`history_omitted`。当前异常、警告、阶段及 worker 状态仍保留；用户要求
历史或调查失败过程时不用摘要。不聊天轮询，也不把摘要当成完整历史验收。
`task-status` 只读，不推进删除。恢复继续同一任务，仍校验服务地址、ID、文件指纹，
不会自动重传或重新解析；先处理实际错误，再恢复。取消停止后续清理并尝试清除本任务状态标签，
不删除新版；已提交的异步删除无法用取消命令撤销，必须查询并说明现状。仅改标题不
能恢复已删除的旧文档。任务记录和日志不能当作“已自动回滚”的证据。

## 明确上传与状态查询

普通用户目标是把选定指南交给指定知识库处理。用户明确文件和库后，Skill 负责检查
并刷新本地字段、调用上传、回读一次处理状态并逐文件报告 ID 和结果；用户不必手拼 HTTP。
仅当出现同名冲突或用户要替换旧版时，才保留明确的人工选择。

REST 配置沿用 `Bdd/config.yaml -> WEKNORA_SETTING.API_URL/API_KEY -> Settings`。
API_URL 是服务根地址，API_KEY 是 REST Key，不是 MCP Token；不读取编辑器秘密配置，
不显示密钥、不安装依赖或更改环境。能列出知识库不证明有上传权限，实际响应才是证据。
缺配置或 MCP 不可用时分别报告受影响动作，不把两种连接方式混用。

固定调用卡，替换占位的文件、库和 ID 后执行：

```powershell
python -B -m autowork_core.knowledge.document_sync upload "docs/3.动作参考.md" --kb "用户指定的库名或ID"
python -B -m autowork_core.knowledge.document_sync status "已返回的文档ID" --kb "用户指定的库名或ID"
```

1. 上传自动先 `refresh` 选定文件，不因字段缺失或过期再询问刷新许可。核心
   `upload_document` 也自动刷新，因此直接调用 `upload` 同样上传带最新字段的文件。
2. 核心完整分页列出目标库并精确匹配文件名。同版本可返回已有 ID 及处理状态；
   手动上传的历史文件可能没有本工具附加的 `document_sync_hash` 元数据，不能据此
   断言不同或跳过冲突。必要时沿用只读 MCP 头部核验；已确认同版本则直接查询该 ID，
   不为了获得元数据而重传。
3. 同名异版/无法核验时返回 `needs_confirmation`，尚未 POST。仅当用户对具体文件
   明确选择“另增一份并保留旧版”，才对该文件加 `--allow-duplicate`；不能在整个
   混合批次盲加该参数。多个同名条目不自动选最新，也不自动改名。用户选择更新时
   转入上面的更新比较与确认，不用元数据更新或 reparse 冒充文件更新。
4. 核心固定本次源字节，使用 `POST /api/v1/knowledge-bases/:id/knowledge/file`。
   不强制多模态、不修改库的处理配置。上传前文件变化则不发送；提交后变化会单列
   `local_changed_after_submission`，不能声称远端与变化后的本地一致。
5. 成功响应须有有效 ID，上传后只做一次详情 GET。`upload_outcome=accepted` 表示
   服务端已接收，不等于解析完成。明确 `409 duplicate_file` 也必须回读核验 ID、库、
   文件名和版本标记；其他 409 不能算去重成功。确认已有项返回 `already_present`，
   若该项解析失败仍须报告失败，不能说“无需处理，成功”。
6. 超时、断连、5xx、无有效 ID 或损坏响应可能已经落库，报告 `upload_outcome_unknown`，
   不盲目重传。保留候选 ID（若有），先只读核查库列表/文件头；核查不清时告知用户。
   401/403 等拒绝则报告失败。已接收但详情读取失败时保留 `accepted` 和 ID，另报
   `status_unavailable`；不能说未上传。不要在 AI 对话里循环轮询或重试。
7. 批量结果逐文件保留，不把部分成功改写为全部成功。查询状态只发 GET，不触发
   上传、自动 reparse 或删除。库绑定不符、权限和读取错误均是无法核验。

## 状态与结果

| 返回 | 应向用户说明 |
| --- | --- |
| `parse_status=pending/processing/finalizing` | 已接收/已有，尚在处理中，附 ID 供后续查询 |
| `parse_status=completed` | 主处理完成；不保证摘要、问题、图谱或 Wiki 都成功 |
| `summary_status=failed` | 摘要子任务失败，与主解析状态分开报告 |
| `parse_status=failed` | 文件可能已接收，但处理失败，附返回的错误信息 |
| `parse_status=deleting` | 正在删除，不当成可用，不自动重传 |
| 未知状态或缺少状态 | 保留原状态并报告未知，不强行映射成功 |
| `upload_outcome_unknown` | 本次提交结果未知，先核查，不能自动重试 |

退出码 `0` 表示版本一致或正常返回已完成/处理中结果，并非解析全部成功；`2` 表示
版本不同、需确认或非就绪状态；`1` 表示失败、结果未知或无法查询。判断时同时读取 JSON 中
`upload_outcome`、`parse_status` 和 `summary_status`，不只看退出码。
任务命令退出码 `0` 可表示后台仍运行或已取消，不等于更新完成；`paused`、`failed`、
`interrupted` 返回 `1`。每次反馈任务阶段、错误、警告及旧版删除状态，不只展示成功计数。

这是上传版本标记检查，不是远端正文完整性、解析质量或 Wiki 验收。约定仓库是维护源，
远端通过带新字段的文件更新；有人在远端改正文却不改字段时，本检查无法发现。
不因此增加全文下载、额外数据库、自动发布或历史版本治理。