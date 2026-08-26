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

## 当前会话交接：Recorder生成收紧（2026-08-24）

本交接记录用户明确要求保存的收紧方案，供上下文丢失或新会话恢复时使用。目标不是继续
加协议，而是把Recorder生成链收紧到能闭环、能解释、能验证。后续会话必须先按这里恢复
任务边界：不要继续真实记事本业务生成；不要把内部预算、伪模式或旧入口重新做成用户主路径。

### 目标与不可删边界

用户主路径只有一条：录制/校正 -> 必要业务确认 -> 创建Job -> Copilot生成 -> 验证 -> 结果。
只有三类事能阻止生成：证据缺失或冲突、业务权威未确认、安全授权不足。50 KiB只作为效率
和发布指标，不是admission或inspect阻断理由；只有真实模型/API容量错误，或最小必要上下文
经过去重和按需分页后仍装不下，才失败为`framework_capacity_defect`。

必须保留的真实性和安全边界：原始evidence不可变、Timeline append-only、Request
revision/fingerprint、Decision Pack/Answers fingerprint、Plan/Manifest/Transaction分层、file
lease、PIC授权、scope校验、Plan-to-Code、Run Result provenance、Oracle/Matrix。删除这些会降低
准确性和安全性，不属于本轮收紧。

### 八阶段收紧方案

1. 恢复生成闭环：`context_budget`从admission硬阻塞移除；admission只检查Request身份和revision、
   selected Take、Evidence/Brief hard blocker、blocking Decision、PIC/scope/Contract、安全冲突；
   预算超限只警告并进入结果/评估效率项。
2. 明确`AIContextEnvelope`：它表示真正发给Copilot的内容。默认包含Job/request/profile id、
   compact Workflow、Brief、Decision answer摘要和fingerprint、AI capabilities、可用时的Plan
   Context、allowed query命令、design contract摘要、当前phase/claim/epoch。Job完整JSON、Workflow
   原始JSON和admission原始结构只作为backend identity，不进入默认上下文；被省略内容必须有稳定ID和
   查询命令，不能静默截断。
3. 收紧Profile：普通UI只保留`generation_first`/“专心生成”。`precision`在真正改变深度扫描、
   对抗验证或证据展开预算前隐藏；`legacy_script_maintenance`不出现在普通UI。无消费者的
   `repair_policy`、`investigation_policy`不要作为默认用户投影噪声。
4. 修Job生命周期：`current_job`只表示当前活动Job。Job终态后清空或转入`retired_jobs`，保留
   `last_job_result`；同一Request可以重新admission。`abort-job`后不能卡死；`switch-profile`只允许
   未claim Job。当前已提供显式`retry-job`和未进Transaction前的`retire-job`。
5. 隔离旧入口：普通帮助和Prompt只显示Job命令；旧Request命令隐藏或移动到legacy recovery；
   Generation Contract当前入口是Job路径；文档中的`/recorder-generate <request>`改为Workbench创建Job；
   `/recorder-adjust`退役或改为Job-bound repair后再公开。
6. 修projection/readiness混乱：`readiness.generation_ready`不能再被UI当成“可以生成”的最终依据，
   主按钮只看Workflow/admission/UserTask。current projection存在但损坏时，Review/readiness直接报
   projection损坏，不回退legacy root artifact；legacy fallback只允许无pointer旧Take或显式importer。
7. 统一结果语义：Job Result是用户看到的本次生成终态；Query不能重新计算终态覆盖Job Result。
   UI固定区分静态生成(Transaction)、真实运行(Run Result)、独立业务验证(Oracle/Matrix)、本次最终
   结论(Job Result)。字段要拆成`single_run_passed`、`oracle_passed`、`quality_passed`，不要再用一个
   `runtime_passed`表示所有东西。
8. 收紧反馈和知识：用户点“可以使用”不自动等于可复用Capability。accepted反馈至少区分
   `accepted_static_only`、`accepted_runtime_verified`、`accepted_oracle_verified`；未通过runtime/oracle
   的accepted只能advisory，不能进入强正向Capability候选。Capability质量从Job Result、Run Result、
   Oracle派生；旧`focused_execution`只作为legacy fallback。

### 当前实施状态

已实现并验证：admission和`inspect-job`的`context_budget`均为软警告；`inspect-job`返回明确的
`AIContextEnvelopeV1`并以它作为默认上下文计量，完整Job JSON只作为backend identity；Workbench普通UI只显示
`generation_first`；terminal Job可retire/re-admit；当前Generation Contract和Prompt入口改为Job路径；
legacy Request mutation命令隐藏为恢复兼容；当前非legacy Request缺`request_fingerprint`时fail-closed；
current projection损坏不静默fallback，Timeline owner修复路径例外；未验证accepted反馈只进入advisory；
Query/UI运行语义改用`single_run_passed`区分单次真实运行，新增`oracle_passed`和`quality_passed`
显式字段并保留旧`runtime_passed`兼容；Capability runtime
verification优先从JobResult阶段派生；RunResult同秒匹配稳定排序；readiness新增
`capture_generation_candidate`/`target_capture_generation_candidate`兼容别名，ReviewPanel内部不再把
`generation_ready`当最终admission结论；
accepted反馈写入`accepted_static_only`、`accepted_runtime_verified`或`accepted_oracle_verified`，只有
runtime/oracle verified反馈能进入强正向Capability候选。
显式`retire-job`可终止未进入Transaction的ready/design Job，`retry-job`通过当前admission创建新Job并
把旧terminal Job放入`retired_jobs`。`inspect-job`默认投影只显示profile identity、allowed queries和
validation stages；`repair_policy`、`investigation_policy`等未形成用户可见行为差异的策略字段只保留在
Job backend identity/registry中。

已完成：新readiness artifact只写`capture_generation_candidate`/`target_capture_generation_candidate`；
旧`generation_ready`/`target_generation_ready`只在历史artifact fallback、legacy importer和兼容测试中读取。
当前生产和maintainer evaluation读取面已集中到兼容helper，evaluation测试fixture也已迁移到
`single_run_passed`/`oracle_passed`/`quality_passed`新字段；`runtime_passed`/`independent_oracle_passed`
只作为旧artifact fallback和专门的兼容测试输入。

### 验证与现场状态

真实记事本录制smoke证明：Workbench可创建Job，`inspect-job`在默认上下文超50 KiB时返回exit 0、
`status=ready`、`next_action=start_generation_job`、`enforcement=warn_only`、`errors=[]`、`warnings`含预算提示。
一次误启动的真实Job已在Design前终止为`operator_aborted_after_probe`；未创建Transaction，未修改
`Bdd/steps`、`Bdd/page_obj`、`Bdd/locators`或`Bdd/data`业务资产。

验证基线：收紧相关宽回归425项通过/2 skipped；budget/job/v3聚焦149项通过；文档检查和
`git diff --check`通过；最终`framework_validation/tests`运行1571项，只有已知无关失败：缺失
`Bdd/test_features/APS_features/Console-SRS.22790.feature`、两个`BasePage.click`签名旧断言、一个Panel
目标选择环境问题；无残留python/ffmpeg进程。

## 下一阶段：无损生成体验收敛（2026-08-25）

一次真实 Notepad 静态生成证明 V5 Job -> Design -> Plan -> Manifest -> Transaction -> Job Result
主链能够闭合，但也暴露简单旅途的默认信息传输和机械实现交付成本过高。此阶段不是以缩短
JSON为目的，也不得因压缩减少 AI 的实现推理能力。唯一原则是：**保留 AI 作出任何 Design
决策所需的全部事实，只消除同一事实的重复表示，并把已经由 Plan/Manifest 确定的机械代码
形状提前交付。**

### 不可变边界

- 不减少或降级 AI 可获得的业务声明、完整有效 Action 顺序/命令/目标、Window 关系、值来源、
   locator 候选、ambiguity、跨 Step 因果约束或已有证据冲突。
- 完整 Brief、Evidence Context、Graph、Semantic Pack、Manifest 继续作为内容寻址的权威
   artifact；任何摘要都必须带稳定 `step_id`、`action_id`、`evidence_id` 和无损精确展开入口。
- 不弱化原始 evidence 不可变性、Timeline append-only、Decision、Plan、Manifest、file lease、
   PIC、scope、Plan-to-Code、Run Result provenance 或 Oracle/Matrix。
- AI 仍独立选择业务 operation、Step/Page owner、target、value authority、复用/新建和稳定
   公开业务名；系统不得以模板替代这些选择。

### 1. 事实等价的默认投影

`AIContextEnvelope`、`inspect-job`、`job-evidence` 和 `prepare-job` 改为输出共享表 + 引用的
规范化投影：每个事实只编码一次，Step/Action 通过稳定 ID 引用 Root、target、locator、值和
证据。默认摘要必须仍包含全部 Step、全部有效 Action 的有序命令形状、target/Root、跨窗口关系、
Feature 期望、冲突和未决 authority；仅原始逐事件细节、重复窗口快照、媒体路径、完整 UI tree、
重复 fingerprint 和后台审计字段移动到按需展开。

终端传输默认返回简短稳定摘要，而不是大 JSON：

```text
Step 4 输入 123456
   click text_editor (264, 68)
   send_text_keys text_editor: 123456{LEFT}
   observed text: 123456
```

`--step-id`/`--action-id` 无损展开对应事实，显式 `--full` 才输出完整 artifact JSON。摘要无法
支撑某个选择时，必须明确指出需要展开的 Step/Action/Evidence，不得让 AI 猜测。压缩实现要求
建立可逆性/决策等价测试：同一录制下，投影与完整 Brief 必须得到相同合法 Design/Plan，且现有
错误 target、value source、Window owner、PIC、scope、Data Table、runtime binding 负例仍失败关闭。
若无法在事实等价前提下压到 50 KiB，则保留完整事实并如实标记效率不达标。

### 2. Manifest 机械实现包

`prepare` 后由 Implementation Manifest 派生只读 `implementation_packet`。它不决定业务，
只提前交付已由 Plan/Manifest 确定且 Plan-to-Code 将机械验证的代码形状：AI 可编辑文件、固定
imports、Step decorator/函数签名、具名 `get_page` receiver、允许的 `$loc:` 引用、调用顺序、
冻结参数和值、Page 根声明和禁止写入边界。AI 只在这些已确定形状内实现业务代码。

Plan-to-Code 继续独立校验；失败只返回当前文件、失败调用、期望 packet 片段与源码差异。相同
源码快照不得重复验证并消耗一轮修复。Design contract 还应前置公开业务名规则和 Action relationship
的 scoped identity 规则，避免“提交后才知道”的纯语法型失败。

### 3. 键盘片段校正（独立切片，不与上述压缩混合）

连续文字输入和导航/编辑键仍保持为同一个不可变原始/有效 keyboard Action；**不得**为了 UI
将其拆成新的 evidence Action、重写原始事件或改变默认 Timeline 的紧凑密度。Timeline 在该
keyboard Action 行内提供一个默认收起的“键盘片段”可展开框：按顺序展示文字片段与导航/编辑键，
例如 `输入 123456` 与 `Left`。用户展开后可对片段执行明确的排除/恢复或要求重录；保存时只写
append-only Timeline edit，且有效投影必须可追溯回原 Action/事件范围。

只有导航/编辑片段影响后续编辑且与冻结 Feature 最终结果存在可机械证明的矛盾时，才提示
“校正录制证据”；它不是业务 Decision，不要求用户重新确认 Feature 已声明的事实，也不临时
引入脆弱的通用文本编辑模拟器。该专门切片需先定义支持的受限文本模型、未知时的 fail-closed
行为和真实用户旅程，再实施。

### 验收

- 简单旅途在不减少任何 Design 决策事实前提下，默认 Context <= 50 KiB；
- 一次 Design 提交、一次实现验证通过，不因 receiver、公开名或 Action ID 作用域等机械规则返工；
- 默认 CLI 输出可直接阅读，完整 artifact 仍可精确无损展开；
- 上述能力等价与全部相关负例保持 fail-closed；
- 用一条重新录制/纠正后的 Notepad 旅途完成静态、显式 Execution Profile、绑定 Run Result 与独立
   Oracle 验收。真实产品、Gantry/设备仍是用户提供的外部环境门，不得本机模拟替代。

### 已实施的第一切片（2026-08-25）

- Job CLI 默认传输已实现紧凑投影：`inspect-job` 输出状态、claim/epoch、目标统计、逐 Step
   Action 计数、ambiguity 计数、预算与查询入口；`job-evidence` 的默认/`--list` 输出按 Step
   聚合；`prepare-job` 输出 AI 可编辑文件、系统文件和机械实现包。`--full` 保留原完整 JSON，
   `--step-id`/`--action-id` 保留完整精确 evidence。真实 Notepad Job smoke 中，默认 inspect
   从约 54.7 KB 降到约 1.9 KB，完整 Envelope 仍可无损取得。
- `AIContextEnvelopeV1.2` 已实施事实等价 Brief 去重：完整内容寻址 Brief 不变；Envelope 仅省略
   `scenario_intelligence.specification` 与 `demonstration` 两个可由同一 Envelope 的
   `brief.target`、`brief.actions`、`brief.semantics` 重建的重复字段，并附全 Brief 路径、指纹、
   `inspect-job --full` 展开命令和受限 omission 声明。identity 校验拒绝未知遗漏、遗漏字段仍存在、
   指纹/指针不匹配或不正确展开入口。真实 Notepad Job 的默认 Context 从 52,987 bytes 降至
   50,993 / 51,200 bytes，状态变为 `within_target`，未删除任何 Design 决策事实。
- `implementation_packet` 已实现为 Transaction report 中从有效 Implementation Manifest 纯派生
   的只读投影：包含 AI 编辑边界、Page root skeleton、Step decorator/参数、具名 receiver、严格
   `$loc:` 目标、调用顺序与冻结参数/值。它不进入 Manifest identity、不授权新文件或业务选择，
   Plan-to-Code 继续为独立验证门。
- Timeline 已为 keyboard Action 增加默认收起的“键盘片段”详情框：将连续文字和导航/编辑键按
   顺序展示，例如“输入 12 / 左方向键 / 退格键”，不改变默认动作列表密度。片段级“忽略/恢复”
   已实施为 append-only `keyboard_fragment` edit：只记录原 keyboard Action 中被排除的 key-down
   event ID；物化从原始事件重建同一 Action ID 的有效 keys/event_ids/media_event_ids，并由现有
   undo/redo、projection、Graph、Semantic Pack、Request stale 路径重算。原始 Action/raw events
   不变，不能排除全部片段（应忽略整个动作）。**跨 Step 编辑语义冲突诊断和任何通用文本模型仍未
   实施；它们必须作为独立语义切片，并先定义受限模型、投影、回放与 fail-closed 负例。**
- 验证：CLI/Manifest/Job/Timeline 联合 263 项通过，键盘片段跨 Timeline/Application 136 项通过
   / 1 项条件跳过，Envelope V1.2 后完整 Recorder 1048 项通过 / 12 项条件跳过，完整
   framework_validation 1588 项通过 / 13 项条件跳过，framework smoke 通过，文档 27 项通过。

## 当前修复计划：Recorder 生成架构防漂移（2026-08-25）

真实 Notepad 生成和后续静态复盘暴露：内部 Plan/Manifest/Job Result 自洽不等于用户旅程
正确。后续在重新录制或真实软件验收前，先按
`ai/context/recorder-generation-architecture-repair-plan.md` 恢复上下文并执行修复。核心原则是：
AI 仍决定业务结构，系统冻结候选并验证证据，用户旅程一致性优先于内部 JSON 自洽。

优先级：当前工作区物化一致性门；证据支持的 ownership candidates；WindowView locator
active-root 收紧；Plan 语义质量门；CLI/Context 输出分层；端到端静态旅程回归。不要根据
`PopupWindowSiteBridge` 等技术类名硬编码业务 Page/View，也不要让 `parent_root` 成为无证据的
自由字段。