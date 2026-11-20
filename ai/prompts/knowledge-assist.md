# Knowledge Assist

本 Skill 是现有知识能力的自然语言入口。采集、图文导出、校验和文件上传由核心模块独立提供；AI 只在用户需要时分析或润色，并按本次意图调用这些能力。不把测试通过、文件上传、远端解析和 Wiki 验收混为一件事。

## 先判定动作

| 用户要求 | 执行动作 | 完成后停止的位置 |
| --- | --- | --- |
| 只分析或校验 | 读取证据，返回分析或校验结果 | 不改文件，不上传 |
| 只润色 | 生成并校验受保护的新副本 | 返回副本路径，不上传 |
| 只上传或原样上传 | 校验并上传所选文件 | 返回文件上传结果，不润色、不自动审阅 Wiki |
| 润色后上传 | 先完成副本润色，再上传该副本 | 返回副本及上传结果 |
| 只问解析或摘要处理进度 | 复用已绑定文档 ID 和知识库，只读查询处理状态 | 不下载原件，不查 Wiki，不表示验收通过 |
| 完整核验远端文件、审阅或接受 Wiki | 使用原有完整状态或验收操作 | 不重传、不删除 |
| 动作无法从上下文确定 | 只问需要执行哪个动作 | 未明确前不修改或外发 |

文件名不是动作授权。`.polished.` 仅说明文件曾经润色；用户明确要求再次润色时仍可创建新副本，仅说上传时则不得擅自再润色。只有上传或远端操作需要目标知识库，纯本地任务不得索要知识库或执行远端预检。

用户指定文件就是不可替代的输入边界，不扫描采集、不重建运行案例、不覆盖或删除原件。内部 `resolve_case_request` 只确认本地选择，不决定动作或要求知识库。普通知识任务不修框架、不跑无关测试、不修改 Skill、配置和文档。

## 固定调用与状态分流

动作、文件和目标库明确后，按 Skill 调用卡直接执行已有 CLI，不先读取实现、历史会话或配置密钥。
`publish` 内部已预检、校验并回读远端字节；除用户单独要求这些检查外，不在外面重复
执行 `verify`、`preflight`，也不在成功回执后自动再查一次状态。

明确采集范围时直接 `build --capture-dir ... --compact` 导出；仅当选择不明确时才 `scan`，
不把明确文件改成全量采集搜索。只分析采集使用 `inspect --capture-dir ... --compact --read-only`，
只分析所选 Markdown 可用 `ai.tools.knowledge.artifact_polisher inspect --markdown ... --compact --read-only`。
只读模式不创建规格，内容只返回一次；诊断、失败/排除原因保留，需要的原文和图像证据按目标读取。
普通校验直接 `verify --markdown ...`，不绑定 AI 润色、远端预检或上传。

只查询解析/摘要进度时，使用 `python -B -m autowork_core.knowledge.document_sync status
"已绑定文档ID" --kb "明确知识库"`。该命令仅查处理状态，不校验本地案例或下载文件，
不代表 Wiki 验收。ID 未知时使用固定入口，不扫描回执目录或临时拼脚本：

```powershell
python -B -m autowork_core.knowledge processing-status --markdown "所选文件.md" --kb "明确知识库"
python -B -m autowork_core.knowledge processing-status --receipt "所选发布回执.json"
```

文件模式按发布器同一规则计算确切字节身份并读取对应回执；回执模式沿用其中绑定的目标库，
也可用 `--kb` 明确选择。两种模式均校验当前服务器、库和文档元数据绑定，只读取处理状态；
不做结构验收、不下载原件、不查 Wiki、不写文件。回执不存在、文件字节变化或绑定冲突时
返回明确失败，不补传、不自动改走完整状态，也不能凭标题相似猜测。
原有 `python -B -m autowork_core.knowledge status --markdown "所选文件" --kb "明确知识库"`
保留完整远端文件核验和 Wiki 检查，不将完整状态请求降级为轻量查询；解析失败时单列处理错误，并保留已接收和字节校验的事实。

案例发布恢复沿用同一文件、服务器和知识库。`status` 可只读核实已记录但结果未知的提交，只按记录中的精确目标、案例身份和文件字节认定唯一匹配；不上传、不打标签、不写回恢复记录。未匹配、多个匹配、来源变化或字节不符时停止，不用换名、改 ID、重新构建或盲目重传绕过。用户明确继续发布时，再运行原 `publish` 继续该记录：已拿到文档 ID 的只继续核验，已校验文件仅补后续标签，不再次上传文件。轻量 `processing-status` 不下载或核实未知提交，只如实报告未提交、明确拒绝、结果未知或已有 ID 的解析状态。

## 执行环境

- 复用当前任务中用户已指定或工作区已选择的现有 Python 解释器；只读确认路径存在后，直接用该可执行文件运行命令。
- 没有确认解释器，或多个现有解释器无法确定使用哪一个时，只问用户使用哪个现有解释器或提供其路径，不默认新建环境。
- 知识任务不调用环境配置、环境创建、Python 安装或包安装工具，不安装 `uv`、Python、依赖或扩展，不修改解释器选择、PATH 或环境设置。
- 缺少 `uv` 仅表示相关环境管理器不可用，不证明现有 Python 不可用。环境工具要求安装或配置才能继续时，停止该工具路径，不触发安装向导。
- 现有解释器确实缺少所需依赖时，明确报告解释器路径和缺失项，等待用户指示；不得把上传请求扩展为环境修复。用户明确要求环境维护时，才在另一个明确范围内执行该维护。

## 连接配置

发布连接位于 [Bdd/config.yaml](../../Bdd/config.yaml)：

```yaml
WEKNORA_SETTING:
	API_URL: "http://130.147.129.74:1101"
	API_KEY: "你的 WeKnora REST API Key"
	KNOWLEDGE_BASES:
		FRAMEWORK_DOCS: "BDD框架知识库_demo"
		ENGINEERING_CASES: "Earth工程案例库_demo"
		PRODUCT_DOCS: "Earth正式资料库_demo"
```

- `API_URL` 是 WeKnora 服务根地址，不是 `/api/v1` 路径，也不是 `/mcp/...` 地址。
- `API_KEY` 是 REST API Key；不出现在 `Settings.as_dict()`、运行日志、错误回执、Markdown 或聊天中。
- `KNOWLEDGE_BASES` 按角色绑定项目默认知识库：`ENGINEERING_CASES` 供本工作流默认使用（省略 `--kb` 时自动采用），`FRAMEWORK_DOCS` 供 `/document-sync` 使用，`PRODUCT_DOCS` 为产品正式资料库。三个角色不得指向同一个知识库；案例发布/审阅/退役预检会拒绝指向 `FRAMEWORK_DOCS` 或 `PRODUCT_DOCS` 的跨角色写入（只读 `processing-status` 不受此限制）。未配置默认角色库时，仍由用户请求或 `--kb "知识库名称或 ID"` 明确指定。
- 不读取 VS Code `mcp.json`，不从 MCP 地址推断服务器，不把 `mcp_...` 令牌当 REST API Key。
- `preflight` 仅用于请求自动发布时验证配置、可访问知识库、角色防呆和 REST 身份；采集和本地 Markdown 导出不依赖远端连接。

### 失败回执

每个失败必须返回 JSON，至少包含 `status`、`code`、`stage`、`message` 与 `next_action`。HTTP 失败还包含 `details.http_status` 和经过密钥脱敏的 `details.server_message`（若服务端提供）。报告必须明确本地源文件、旧版远端文档和清理动作是否保持不变；无法确认上传结果时报告 `upload_outcome_unknown`，仅按同一 ID 和稳定路径恢复，不得创建新的上传身份绕过结果未知。

## 能力调用与归属

确定性能力位于 `autowork_core.knowledge`：

- `case_export`：采集结束自动导出，及独立本地构建。
- `case_context`：场景身份、证据抽取与候选选择。
- `case_verifier`：结构与场景证据校验。
- `case_publish`、`weknora_client`：文件传输、回读、可选远端状态与验收。
- 基础 CLI 为 `python -B -m autowork_core.knowledge`，函数从对应核心模块调用；AI 工具只从能力子包调用，不提供混合聚合或转发入口。

AI 润色时，内部使用 `ai.tools.knowledge.artifact_polisher.inspect_markdown_for_polish` 提取可编辑段落，AI 只提交实际替换文本和审阅者标记，程序合并绑定字段后调用 `apply_markdown_polish`。本地输入适配位于 `ai.tools.knowledge.case_workflow`。标题和第 3/4 章叙事可改；动作 ID、控件、图片、代码、表格、来源指纹和原始字节受保护。仅润色的任务到校验副本并返回路径即结束，不调用发布器。用户不需要操作内部规格文件或 CLI 参数。

显式文件润色直接使用固定模块入口；现有函数同样保留：

```powershell
python -B -m ai.tools.knowledge.artifact_polisher inspect --markdown "所选文件.md" --compact
python -B -m ai.tools.knowledge.artifact_polisher apply --markdown "所选文件.md" --polish-spec "返回的规格.json" --source-sha256 "提取返回的原件哈希" --polished-by "vscode_agent" --replace "段落ID" "实际润色文本"
```

提取结果已经包含无内嵌图片的可编辑规格，不重复读取大文件或同一份规格。AI 仍负责真实
叙事润色，不用机械替换冒充完成。应用时仍校验原件字节、来源绑定、受保护令牌与新副本结构。
紧凑模式不重复输出默认 replacement 或保护哈希。重复 `--replace` 提交改动段落，未指定
段落保留原文；程序在内存合并，不改原规格或原件。旧完整规格调用方式仍保留。

上传时，核心发布器负责：

1. 读取案例身份和发布参数，固定本次选定文件的字节并现场计算 SHA256。内嵌 `Content Hash` 是历史说明，不是上传许可；不得因其过期改写文件、要求重新构建或更换路径。
2. 采集 ID 表示来源，服务器、目标库和实际提交字节区分发布回执。同一工件重试复用；原文与润色副本、不同目标库是不同提交。内容哈希只标识确切提交字节和校验传输，不用于语义或历史案例去重。
	发布记录在上传前保存到现有 `knowledge-publications` 位置，不保存密钥；请求前标记结果未知，拿到远端 ID 后立即记录已接收和校验待完成。记录无法保存时停止后续写入，保留已经取得的 ID 和阶段信息。旧的已完成回执仍可读取，不自动换身份或迁移远端文档。
3. 新发布直接进入知识库根级，只接受不含目录分隔符的上传文件名，保留内容摘要和采集案例 ID 后缀。上传后重新读取远端文档，验证知识库 ID、发布元数据、根级文件名和下载得到的原始字节 SHA256；已有回执则按回执绑定的原文档及原目录核验，不自动迁移或重传。空 ID 或未知冲突不得报告上传成功；实际字节不一致必须拒绝确认。
4. 文件回读一致后返回 `uploaded`，CLI 成功退出。远端解析状态附带报告；标签失败报告 `tagging_status: pending`，不撤销上传结果。普通上传不查询 Wiki；Wiki 不可用不改变文件上传结果。
5. 超时、断连、5xx、响应损坏或缺少有效 ID 不等于未上传，保留 `upload_outcome: unknown`。已收到 ID 而回读超时则保留 `accepted` 与 ID，返回 `verification_pending`。明确拒绝与未提交分别记录，不当作未知，也不因为标签或解析错误抹掉文件已接收的事实。
6. 同时报告 `upload_outcome`（接收事实）、`verification_status`（精确字节核验）、`tagging_status`（标签关联）和 `processing`（主解析、摘要及警告）。标签创建或关联超时也属于标签待补，不改成整体未上传；恢复仍核对原文档，不二次传文件。`completed` 只表示主解析完成，不证明所有图片、摘要或 Wiki 都正确。发布恢复不会自动删旧或替换版本，标签关联仍不提供跨客户端原子性保证。

文件上传不删除本地文件，不删除旧版远端文档。

场景归类统一使用小写、半角冒号的 `feature:<Feature 文件名去掉 .feature>`，从采集或文档记录中的 Feature 来源取值，不从 Markdown 文件名、Feature 标题或目录猜测，不截短。所有同名 Feature 场景使用同一标签；同一知识库中的同名文件会被归到同组，移动目录不改变标签，文件重命名不会自动迁移旧标签。标签仅用于归类，不决定案例身份、版本、去重或删除。

发布器补齐缺失的 `feature:` 标签，保留其他已有标签及额外标签，不能用空标签参数清掉 Feature 分类。旧输入中与来源文件名一致的 `需求:` 标签在本次发布参数中转换为 `feature:`；冲突分类在联网前报错，不改写所选文件。旧正文的归档路径不再恢复新发布目录，新导出只写建议上传文件名。手动上传时在根级选择或创建正文提示的同名标签；正文提示不代表前端已完成关联。程序上传必须回读确认标签；失败报告 `tagging_status: pending`，不为补标签重复上传。已有远端旧标签不批量改名或删除。

## 采集生成

运行采集保留不可变观察证据：`observations.json`、PNG 和来源快照。运行结束会自动为本次实际记录的每个 Scenario 导出独立 Markdown，不读 `CASE_FILE`，不调用 AI、Skill 或 WeKnora。失败和不完整记录照实标注；导出错误单独记录，不改变测试结果或删除证据。

用户可以直接使用自动生成的文档，无需 Skill。用户提供采集选择词时，先选择对应记录，优先复用已有 Markdown，不重新构建或重新润色。模糊关键词的默认候选仍是最新完整通过记录；不得把失败状态或缺图记录说成完整通过。

只请求分析时，只读观察与来源；只请求导出时，调用核心导出并返回本地路径；只请求润色时，沿用受保护的副本润色路径。只有明确请求上传，才进入发布。没有本地输出需求时，不为走流程创建规格或副本。

独立 `build` 不要求 AI 已润色；已有规格必须与证据匹配。导出后可针对同一份 `observations.json` 校验动作顺序和 PNG 指纹。现有不同字节的产物不得覆盖，AI 不得更改事实来迎合校验。

## Wiki 验收和退役

当远端解析为 `completed`，且 Wiki 已稳定后，发布器生成绑定以下内容的审阅 JSON：服务器地址、知识库 ID、文档 ID、文件 SHA256、`source_refs` 指向该文档的 Wiki 页面、各页面版本与内容哈希。AI 必须审阅当前页面后，把四项检查都标为 `true`：

正常审阅使用 `status --markdown ... --kb ... --review-output ... --compact`，程序保存完整
审阅文件，输出路径、文件 SHA256 和摘要，不再同时打印全文。AI 必须实际读取审阅内容
和必要来源证据一次；不能以摘要代替审阅。接受时可按以下方式提交判断，无需 AI 复制页面、
版本或哈希，完整记录由程序构建并沿用原校验：

```powershell
python -B -m autowork_core.knowledge accept --markdown "所选文件.md" --kb "明确知识库" --review "已读审阅文件.json" --review-sha256 "返回的审阅文件哈希" --check action_order --check control_attribution --check images --check inference_boundaries --notes "实际审阅结论" --compact
```

少一项、重复项、空结论、文件哈希不符或当前 Wiki 版本变化都必须拒绝，不自动勾选。
旧完整验收记录入口保持不变。`retire --compact` 仅省略已审阅正文，保留待退役 ID 和状态；
退役及清理仍需要用户独立明确请求，不在外层另跑重复核验，也不跳过核心即时检查。

- 动作时序与测试证据一致；
- 控件和页面归属一致；
- 图片和追溯引用完整；
- 推论边界没有把调用返回夸大为业务完成。

只有审阅 JSON 仍与当前远端版本一致时，`accept` 才记录接受，不启动删除。旧版退役需要独立明确的删除请求；任务仍在时状态是 `retirement_pending`，不是退役完成。

`clean` 仅能清理一个明确且已结束的采集 Run，且该 Run 中每个案例均为 `accepted`，无待退役版本。任一步失败保留本地文件、原始采集和远端旧版。