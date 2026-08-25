# AI 协作上下文

本文是 BDD Autowork 的版本化 AI 启动上下文。它保存长期有效的目标、架构边界
和验证习惯，使新机器、新工作区或新的 AI 会话无需依赖本地 Copilot 记忆即可
继续维护项目。

本文件不替代代码、测试和详细设计文档。发生冲突时，优先级为：

1. 用户本次明确要求。
2. 当前代码、测试和当前 Recorder 证据。
3. 本文件及仓库内适用的 instructions、Prompt 和设计文档。
4. 用户确认的 Recorder 项目知识与历史生成结果。
5. Copilot 本地记忆、聊天摘要和未经确认的 AI 推断。

## 项目目标

BDD Autowork 是 Windows 桌面应用 BDD 自动化框架。Feature 描述业务；Step 负责参数、
场景特有线性动作、业务期望和跨窗口编排；WindowPage/WindowView 拥有窗口与 locator，
只承载已经稳定复用的业务能力；通用 actions 不写产品业务含义，locator 和 data 优先配置化。

框架代码给 AI 提供可靠证据、紧凑候选和安全边界，而不是用大量规则替代 AI 的
完整实现推理。设计保持轻量、文件化、可审计，并服务于长期迭代，而不是只让
单次生成通过。

## 协作方式

- 从用户真实工作流和整体架构判断问题，不以局部方法能通过作为完成标准。
- 优先修复行为所有者和生命周期根因，避免在多个调用点重复补丁。
- Runtime定位/等待收敛必须保持Feature、Step、Page、locator/data YAML和`BasePage`公开API零修改兼容；按`docs/维护/2.架构设计.md`的`ResolvedTarget + WaitPolicy + probe_once`阶段迁移，每个切片可独立回退，不能要求宿主脚本增加等待或改写locator来配合底层重构。
- UI 测试尽量复现真实控件树、全局热键、线程和窗口生命周期；直接调用内部方法
  不等于覆盖用户操作。
- 主动识别重复协议、重复 UI 和不必要的代码规模，但不同信任边界上的重复校验
  不能只为减少行数而删除。
- 评估 AI 成本时关注默认注入的 artifact、查询结果和 token，不单看源码行数。
- 规则用于证据、候选和安全门，不演变为替 AI 作出全部业务判断的专家系统。
- 新增或公开 `BasePage` 通用动作时，必须同步在 `AI_CAPABILITIES` 中显式且唯一分类：
  `plan_enabled=True` 表示开放给 AI Plan，`plan_enabled=False` 表示只作为生成实现可调用的
  框架辅助 API，`debug_only=True` 表示仅调试使用，`ai_exclusion=<policy>` 表示由安全策略
  明确禁止。完整维护责任是通用Runtime实现、`BasePage`包装、Registry分类/动作知识、动作
  参考和风险相称的Runtime、Plan、Plan-to-Code测试；不得依赖默认值、Prompt白名单或遗漏
  注册来表达“不开放”。
- 面对不确定性先给出可证伪假设并用最小检查验证；发现假覆盖时修正测试模型和
  实现，而不是保留一个看似通过的数字。
- 实质性模拟、原型或用户体验优化开始前，先明确要判别的用户问题或风险，以及真实
  规模和状态假设；验收后必须总结发现、根因、设计上限和未覆盖风险，不能只报告通过数
  或视觉完成。隐藏必要操作、删减必要含义或换一种形式重复同一信息都不算优化；产物
  没有回答新的用户问题时，停止继续打磨并更换方向。
- 修改产品专用的启动方式、工作目录、窗口可见性/焦点、就绪条件或设备联动前，先冻结
  原有可观察语义作为不变量。代码与测试一致只证明实现自洽，不证明产品契约正确；应以
  真实产品或可区分语义的忠实目标验证，无法验证时明确标记未验证。只有产品意图已确认
  时才同步更新旧测试。
- 回答框架使用、参数或排障问题时，先从 `docs/1.快速开始.md` 按编号找到权威正文；
  README、user guide 和导航摘要不能作为另一套独立规则维护。

## 项目级上下文卫生

实质性的项目工程工作默认使用 Project Context Hygiene，包括实现、调试、代码审查和
架构分析。简单问答、一次性命令和无需展开的窄任务不增加流程；对窄任务，完成任务
本身就是唯一里程碑。该层只优化上下文呈现与任务边界，不选择代码、工具、测试或
结论，也不建立持久运行状态。

语义里程碑只在已验证切片完成、当前 Review 收敛、工程目标改变或准备交接时由 Agent
内部识别，不要求用户逐次确认。它不能按固定 turn、token、工具或文件数量触发，不能
自动压缩、丢弃上下文或切换会话。当前任务完成后若出现独立新 Epic，Agent 可以建议
开启新会话；用户继续当前会话时仍使用完整能力。

长工具输出在默认对话中只保留状态、数量、耗时、首个可行动错误、重跑命令和原始
输出路径。原始输出不删除，信息不确定、错误存在或细节影响判断时必须按需展开。这是
可逆的呈现优化，不限制搜索、读取、子 Agent、验证或其他工具，也不允许隐藏失败。

### Shadow Companion

版本化 `shadow-companion-policy.json` 是项目级授权：实质工程任务不再逐任务询问是否
使用 Shadow。Agent 只在聚焦验证通过、Review findings 收敛或真实 handoff 等有价值
语义里程碑，自动维护ignored`Bdd/ai/knowledge/shadow-companion/active.json`；普通读取、
搜索、每次编辑和简单问答不写状态。Hook 不推断业务语义，只在 SessionStart 校验并
注入 active Capsule 的短恢复指针。

Capsule 只保存 project ID、Git branch/HEAD、Python 平台、目标、working hypothesis
及其反证方式、不变量、已完成项、下一检查、短 evidence facts、关键文件 SHA-256 和
最近验证摘要。禁止源码、终端原文、私有绝对路径、凭据和 Git 操作。所有 facts 和
validation 都保守绑定整组关键文件；任一文件、分支、项目或环境变化都会标记 stale，
但绝不禁止重读、搜索、审查或测试。

Agent 使用以下生命周期，不需要用户审批：

- `status`：先检查是否已有 active Capsule；当前请求与 goal 不一致时不得自动采用。
- `start`：仅在没有 active 且首次出现有价值里程碑时创建；validated milestone 必须
  绑定 passed validation，未验证 handoff 必须明确使用 `handoff` reason。
- `milestone <capsule-id>`：以完整短快照更新同一 Epic；不得创建 Phase Capsule。
- `complete <capsule-id>`：任务与 fresh final validation 完成后直接删除 active。
- `archive <capsule-id>`：目标切换但旧工作未完成时转为 dormant；recent 最多 3 个、
  默认保留 7 天。`resume <capsule-id>` 只能显式选择，多个候选永不自动挑选。

入口为 `python -B -m autowork_core.utils.debug_tools.shadow_companion <command>`。
`start`/`milestone` 只接收短结构化字段和 workspace-relative `--key-file`。active 冲突、
状态损坏、项目/分支不匹配或内容过期时，失败方向必须是少恢复并重新调查，不能覆盖、
猜测或限制 AI。Knowledge Audit 对 Store 只读检查；SessionStart 只做有界 recent 清理
和指针注入，不生成摘要或修改工程文件。

## Recorder 默认维护工作循环

Recorder 维护在项目级上下文卫生之上使用 Adaptive Work Loop。它是当前 Agent 的
advisory 工作节奏，不是新命令、运行时服务、持久缓存或任务管理系统，也不适用于
正常 `/recorder-generate` transaction。它只减少无信息增量的重复工作，不能减少
AI 可用的搜索、读取、推理、审查、验证或完整 fallback。

1. 路由一次：首次编辑前确定一个可证伪的行为所有者假设、必须保持的不变量和最便宜
  的判别检查。协议、迁移、并发、生命周期或信任边界变化先做一次架构/威胁检查，
  把适用风险转成负向测试；不要在每个补丁后重新做同一轮宽泛审计。
2. 垂直切片：只实现能够由当前判别检查验证的最小完整行为。第一次实质编辑后立即跑
  聚焦检查；失败支持当前假设时修复同一切片，推翻假设时只向真正 owner 移动一跳。
3. 新鲜度：同一语义里程碑内，相关文件、依赖、环境和外部状态没有变化，也没有新
  矛盾证据时，可以复用刚得到的读取事实或验证结果。发生相关编辑、外部变化、未知
  状态、Review finding、用户要求或 repository gate 时，旧结果立即失效并重新验证。
  最终完成声明必须基于 fresh validation，不能使用跨里程碑缓存。
4. 收敛：聚焦验证通过、当前一组 Review findings 关闭、行为 owner/目标改变，或准备
  暂停/交接时形成语义里程碑。里程碑由证据状态决定，不能用固定 turn、token、工具
  或文件数量硬切；复杂调查可以继续完整展开。
5. 最终验证：编辑期间优先聚焦检查；默认在 findings 收敛后做一次最终对抗审查，再
  做一次风险适配的完整回归。跨切片契约、环境变化或新矛盾证据可以随时提前升级或
  重跑，不得为了减少额度跳过用户、仓库或风险要求的验证。

项目级上下文卫生和 Shadow Companion 负责输出呈现与恢复；Recorder Profile 只增加
领域验证节奏。Shadow 永远不是当前会话工作循环或完整调查的前置条件。

## Recorder 主链

当前生成主链只有一条：

```text
CaptureSession
  -> Evidence Graph 2.6 / Evidence Context 2.3（兼容重建旧 Graph、读取 Context 2.0-2.2）
  -> immutable business RequestV3 + Execution Profile 1.0
  -> Semantic Pack 6.1（兼容读取 5.0-6.0；typed Observation Intent + semantic control state）
  -> compact Generation Brief 4.4（Canonical Action 1.0 + Scenario Intelligence 1.1 + agent_tasks）
  -> Decision Pack 5.8 / Answers 5.1（用户权威 outcome + 白话呈现）
  -> Generation Profile 1.0 + admission -> immutable GenerationJobV1
  -> GenerationDesignV1（业务概念、operation、值权威、table/reuse/runtime关系）
  -> deterministic Design + Proof Compiler
  -> GenerationPlanV4.2（系统派生证明 + runtime binding + Contract/Job lease；兼容读取 V3.0-V4.1）
  -> AI Plan Context 1.1（含value provenance的内容寻址再生成投影；不落盘）
  -> GenerationTransactionV3 + ImplementationManifestV1.7（Job子租约、AI/system文件所有权、预检ledger、恢复基线）
  -> AI implementation + Plan-to-Code -> Code Manifest 1.0
  -> GenerationJobResultV1（static/runtime/oracle owner结论的内容寻址终态投影）
  -> distilled Bdd/ai/knowledge
```

关键约束：

- 原始录制 evidence 不可变，时间线修订 append-only。
- Run 内 `ai/workflow/<request-id>.json` 是唯一工作流运行状态源。
- Request 只保存事实并绑定 revision；Decision Answers 只约束 Intent/Plan，不能用自由
  文本直接修改 Plan。
- Brief 的 typed ambiguities 是冻结问题基线。规则只声明事实、允许 outcome 及其
  `ai/user/evidence` 权威；AI 只能在 Plan 中选择声明过的 AI outcome，用户 outcome
  只能来自 Answers，evidence-required outcome 只能修复或补录。
- Plan 提交后不得反向重建 Brief 或消除原始风险。Workflow State 通过“冻结
  ambiguities - Answers - validated Plan”投影当前状态；Brief、Decision 和 Plan
  各自保持单一职责。
- `prepare`从已验证Plan、Brief和generation input snapshot确定性派生
  `ImplementationManifestV1.7`。它分开`ai_editable_changes`、`system_owned_changes`和只读复用文件，并投影Step decorator、模板pattern与函数参数、
  operation顺序、严格`$loc:`目标和动态输入绑定、Page method receiver与输入来源、locator ensure patch、package marker和受保护路径；
  Transaction在committed file lease内用可恢复journal原子物化locator/PIC patch与package marker；AI只写实现body。正式`design`在持久化前执行完整Design编译与Plan校验；
  `validate-design`保留为提交失败后的只读结构化诊断，不在成功路径重复执行。`validate-implementation`复用正式Plan-to-Code校验并把每次技术修复追加到哈希ledger；`finish`要求最新valid且源码快照未漂移，并由实际diff生成系统receipt。
  Job-bound `abort`用于技术修复耗尽或操作者明确放弃：Transaction归档AI草稿，按prepare时
  封存的Manifest目标基线恢复AI文件、回滚系统物化并释放file lease，然后把当前Job终止为
  `failed/aborted`；新尝试必须重新admission创建新Job。只有历史V3 running Transaction在完整
  清理后恢复Request `ready`。`aborting`中断由对应prepare入口幂等收敛，任何scope、baseline、
  journal或lease异常都失败关闭；Manifest 1.7之前缺少基线的running事务不声明安全abort。
  Transaction同时冻结generation roots外的项目guard快照（排除运行产物与打包模型），
  未申报的scope外新增、修改或删除均失败关闭。
- Reconciler 不生成 operation 草案。Brief 4.4 的 `agent_tasks` 只列 Step、Action 类型、
  ambiguity ID 和可用调查工具；它不是实现建议。AI在GenerationDesign中选择业务概念、
  operation、冻结target Action、值权威、同Step Action关系及table/reuse/runtime关系；
  `activation_for`和`transport_for`保留两端物理operation，只有经过同Step、顺序、
  辅助动作、同target和Decision门验证的`absorbed_by`可覆盖来源Action。编译器从冻结
  Brief派生Scenario Model、owner/path、`value_provenance`、`action_ids`、`evidence_ids`
  和`target_fingerprint`，并拒绝AI伪造机械证明字段。
- Brief 3.9 仅为发生过矫正的 Action 投影有界 `correction`：补录来源类型与原 Action
  ID，或最多 12 个 merge 来源 ID、总数及截断标志。补录路径、edit ID、媒体、时间戳
  和完整 merge 快照仍只保留在 effective evidence，普通 Action 不增加该字段。
- Semantic Pack分开保存Feature/Examples声明、typed录制意图、运行时观测和确定性派生
  候选。按键序列只能形成输入候选；新F9 note为空，`ObservationIntent`只可引用受控
  expected source，业务含义不能成为参数。实现方式与技术角色由AI在Plan中选择，Decision
  不询问用户技术偏好。
- Evidence Graph 2.6为每个Action确定性投影Canonical Action 1.0，强类型分离
  `command`、`observed_after`与`business_expectation`。Canonical Action只保存后端无关的
  结构化按键、点击、目标和前后状态；只有AI在Design中选择`send_text_keys`后，Plan编译
  适配器才把按键事实转换为当前pywinauto参数。运行时结果不能回填命令，也不能自动升级为
  业务期望。Feature/Rule/Examples/Data Table已声明的业务事实直接具有业务权威，不重复询问。
- replacement-text候选要求完整平衡的key-down/key-up序列；截断、孤立或重复按键事件
  只保留原始事实，不得重建输入值。
- 明确的窗口标题Observation保存为`observed_window_titles`；只有typed Intent声明
  `window_title`、关系和Feature/Examples/Data Table来源且冻结标题匹配时，才形成绑定Root
  的文本断言候选。pre-5.8 note转换只在已验证旧Semantic artifact存在且Take/Run未声明
  2.0模型时启用。Plan可通过
  `locators[].evidence_name`把机器化录制名称映射为业务名称，但映射目标必须属于当前
  operation覆盖Action的冻结Root或locator。
- Window owner分开保存内部`evidence_root`和公开`public_name`。录制Root、owner ID与
  target fingerprint继续绑定证据；新建Page包、Page类、locator包和公开Root只从AI选择的
  稳定业务名派生。内部机器hash不得进入公开业务API，不同Root不得映射为同一公开名称。
- 新 RequestV3 以 `request_fingerprint` 封存完整 canonical payload；`running`
  transaction 可继续使用冻结的外部 evidence revision，但不得接受 Request 文件篡改。
- 新Request使用`business-v1`身份，只绑定Feature/Scenario/Step规格、选中Take、Evidence、
  Annotation、目标相关Memory revision和Execution Profile；不复制Framework Contract，也不
  因Contract/API变化更换Request或重复Decision。GenerationPlanV4.2在提交Design时冻结当前
  Generation Contract lease并纳入Plan指纹；Contract变化只退休当前Plan并要求AI基于同一
  Request/Decision重新Design。prepare和finish均重验lease，运行中变化失败关闭。
- Execution Profile 1.0默认`not_configured/static_only`。只有显式`attach_existing`或带
  明确应用命令的`launch`允许框架运行；`external_manual`由外部执行者负责。生成Runner在任何
  应用Hook前验证Request和完整Transaction provenance，禁止继承全局启动配置或猜测attach。
- 默认 AI 上下文由`AIContextEnvelopeV1.1`统一承载 compact Workflow、Brief、AI capabilities、
  可用时的revision-bound Plan Context、必要的Decision指针和GenerationDesign契约；完整Job、
  Workflow和Plan只作为backend identity。`generation_workflow evidence`按Evidence、Step或
  Action展开冻结事实，`compare-takes`只读比较当前Request目标Step的多个Take，
  `decision-media`只按已有Decision问题展开验签媒体；完整Graph、Semantic Pack 和媒体
  仍按疑点展开，不要求每次全量读取。Decision媒体是解释而非业务真值、target/locator
  授权或Answers状态，`blocked`/`stale` Request和不可信帧不得产生可用媒体投影。AI Context
  Budget 1.3超过50 KiB只写入`warn_only`性能警告和评估效率项，不阻止admission或`inspect-job`；
  不能靠截断事实满足预算，真正容量不足才以`framework_capacity_defect`终止。
- 新Design先读取`generation_workflow design-contract`，再通过正式Job-bound `design-job`命令一次提交；正式命令在Plan持久化前执行全部编译和校验。只有提交被拒绝时才调用只读诊断获取结构化问题分类。Generation Contract 6.19
  显式公开Design 1.1的table use、Step/Page reuse和runtime producer/consumer形状；它不
  要求AI提交Plan AST、owner/path、locator、proof或用户Decision结果，并把Design contract
  版本/指纹纳入Plan 4.2 Contract lease。`intent-contract` 1.2和历史`adjust`仅保留兼容读取；
  Contract变化不改变业务Request或Answers，只要求重新编译Design/Plan。
- Scenario Intelligence 1.1 使用覆盖保持型压缩：阻塞 gap引用的Action和每个有录制动作的
  目标Step至少保留一个episode，每个含gap的Step至少保留一个轻量gap索引；被省略的
  Action、gap与结构化Observation必须在`coverage_manifest`中列出稳定ID和展开来源，
  不能只给truncated布尔值。
- Brief 的 `scenario_intelligence` 是纯投影，不是第二事实源：`feature_declared`
  specification 来自当前 Feature/Rule/Background/Outline/Examples，`runtime_observed`
  demonstration 只表示本次 Take 可证明的动作，`code_verified` references 仍须结合质量
  画像判断，`environment_dependencies` 只描述当前 Scenario tags 激活的 scoped lifecycle
  回调及数据键且禁止自动生成副作用，gaps 保留原 ambiguity/conflict 路由。各类来源不得
  互相冒充。
- AI 必须在 PlanV4.2 中声明 `scenario_model`，逐一覆盖目标 Step、保留
  Given/When/Then 角色、命名候选状态与前向 transition，并用上述权威分层引用支撑每项
  结论。完整且仅含一个目标Step、无Data Table时可声明`mode=single_step_intent`，只保留
  Step角色、理由和support，不编造states/transitions；部分范围、多Step或表格Step仍必须
  使用完整状态模型。框架只校验范围、顺序、引用和因果形状，不推导业务状态。
- Decision Pack 5.8 的机器option和Plan patch保持结构化；`presentation`按“Step、观察、
  不确定性、建议依据、选择后果”白话展示。业务真值和PIC授权不预选。展示文案与option
  一起进入Pack fingerprint，保证用户所见即所签；自由文本仍不能直接修改Plan。
- 当前生成体验已引入正式`Generation Profile`，而不是把行为差异散落到Prompt补丁中。首版
  registry包含`generation_first`（专心生成，默认）、`precision`（专心精确）和合法但
  `start_allowed=false`的`legacy_script_maintenance`占位。Profile是生成编排策略，不进入
  Request事实或fingerprint，也不得改变Decision、Evidence、Plan或Transaction的权威边界；
  它与作为Request业务身份及运行授权输入的`Execution Profile`是两个独立协议。
- 用户界面的`Evidence Readiness`由Generation admission统一投影，不新增第四套readiness
  状态或事实文件。Admission在AI开始前重验当前Request/revision、Brief覆盖与ambiguity路由、
  Decision Pack/Answers、`evidence_required`、Generation Contract、Context Budget和冲突中的
  Job/Transaction；会话`readiness.json`仍只拥有采集健康，Workflow State仍拥有运行状态。
  Decision media只解释已有问题，不是业务真值；除被现有证据协议明确要求的内容寻址媒体外，
  其可用性不能自行放行或阻断Job。
- 只有admission通过且所有已知blocking Decision已由Workbench一次性回答，系统才在Design前创建不可变、
  内容寻址的`Generation Job`并把其路径作为AI入口。Job只保存Profile策略指纹及Request、Brief、
  Decision、Contract和允许查询/验证边界的验签引用；不复制Evidence/候选事实，不预猜AI尚未
  决定的精确文件，也不替代Plan或Manifest。AI不能切换Profile、回答Decision、修改Request，
  或在同一Job内重开admission；仍只提交GenerationDesign，系统继续确定性编译Plan/Manifest。
- Workflow State 4.0继续是唯一运行状态源，只保存当前Job和终态结果指针，并以单调epoch对Job
  mutation执行CAS。Plan必须绑定Job lease，Transaction再冻结并重验Job/Plan lease；Profile或
  Contract改变会退休未认领Job/Plan，running Job在下一安全门按冻结lease完成或失败关闭，但都
  不改变业务Request或仍然有效的Answers。`generation_first`限制为Job命名范围内的按需调查和局部技术
  收敛；`precision`可扩大证据、代码和对抗验证深度，但共享同一Context Budget、安全门、
  权威边界和“一个Job内不追加用户问题”规则。
- Job开始后的Design/Plan-to-Code/编译/locator引用问题可在不改变业务权威和Job scope时局部
  修正；重复无进展或不能安全继续的stale、证据缺失、漏检用户权威、越权、framework defect、
  runtime或Oracle失败必须结束Job。漏检用户权威属于admission/Reconciler缺口，不能用AI自由
  文本临时生成新Decision。Job级终态结果只验签引用Plan、Transaction、Run Result和Oracle等
  owner结论并投影category/next action，不回写或覆盖这些事实；Job-bound Transaction只结束
  静态stage，是否等待runtime/oracle由Job completion policy决定。普通Workbench/Prompt只发
  Job路径；旧Request mutation CLI已退役，只读查询和已有V3 running Transaction收口仍兼容。
- 收紧方向：用户主旅途必须优先于内部审计便利。50 KiB默认上下文预算是效率/发布指标，
  不是admission硬阻塞；有效录制只因证据缺失或冲突、业务权威未确认、PIC/scope/Contract等
  安全门失败而被阻止。`inspect-job`默认返回实际发送的`AIContextEnvelopeV1`紧凑投影，内嵌
  compact Brief、Decision摘要、Design Contract摘要、allowed query和claim/epoch；Job完整JSON、
  Workflow和admission原始结构是backend identity，不重复进入默认上下文预算。当前只公开真实可用
  的`generation_first`；`precision`在有执行消费者前不作为普通用户模式。Job终态后必须能显式
  retire并对同一Request重新admit；历史Request命令、legacy Timeline技术编辑和legacy projection
  fallback只保留在兼容/恢复路径，不污染默认主链。Job Result是用户终态投影，Query不重新解释
  终态；Runtime单次通过、独立Oracle通过和最终Quality Gate通过必须分字段表达，不能互相
  兜底。Capability runtime verification优先从Job Result/绑定Run Result/Oracle阶段派生，旧
  Transaction focused_execution只能作为历史fallback。Run Result同秒匹配必须稳定排序，CLI帮助
  只暴露当前Job入口，退役Request mutation命令仅保留为显式恢复兼容。未验证accepted反馈只能
  作为advisory，不能升级为强正向Capability候选。
- Review中的业务确认现场由Query层按问题的Action/Evidence锚点动态投影动作前后帧并在
  内存中标记目标；帧路径限制在Take/Session/Project内，内容摘要在投影与实际预览/打开时
  都要复核。图片不写入Decision Pack、不改变Answers/Plan，也不能替代业务真值；无可信
  动作媒体时明确降级为文字/规格对比，不使用无关Step边界图。
- PIC用户授权不能被同一Action的AI实现ambiguity覆盖；授权Region必须引用该operation
  所属Window owner的唯一Root。Request业务事实、模板或audit变化后旧授权失效；纯Framework
  Contract变化保留Answers，但旧Plan保持历史只读并要求Plan 4.2重新绑定当前lease。
- PlanV4.2把`external_ai`、`deterministic_surrogate`、`human_authored`或`legacy_import`
  来源写入artifact并绑定fingerprint。质量门只有在Request、Plan、Transaction和正式
  内容寻址Run Result身份闭合、目标Step真实通过且来源为`external_ai`时才报告
  `ai_quality_passed`；synthetic Plan最多报告`protocol_e2e_passed`。
- 生成结果机器投影分开记录`semantic_selection/design/implementation/transaction/runtime/oracle`，缺少权威证据保持
  `not_evaluated`，不能由Transaction或Behave通过兜底。`stage_timing_ledger` 1.0固定同一组六阶段键，只给实际执行过的Design派生、实现预检和Transaction收口写起止时间与耗时；Runtime/Oracle没有绑定证据时不伪造耗时。系统从最终实际Python源码及本地依赖AST派生plugin、dynamic dispatch、外部可变状态和并发风险；标准场景使用绑定Run Result，高风险场景必须提交不同运行实例的完整矩阵。独立业务Oracle只能来自受保护项目注册或框架注册，由框架绑定具体PID/HWND、进程身份、目标Scenario/Step、环境快照和Run Result后执行。
  当前协议为Runtime Risk Policy 1.2、Oracle Registry/Runtime Matrix 1.1和Generation Quality Gate 1.2。Risk Policy只扫描Manifest业务源码及其项目依赖，在共享`autowork_core`公共API边界停止；框架内部动态适配由Contract/Runtime所有，不能把普通BasePage调用升级成业务高风险矩阵。历史Risk Policy 1.1与Quality Gate 1.1只读兼容，不可作为当前Contract质量升级证据。
- 正式Run Result 1.3可为失败Step保存结构化Runtime Diagnostic，并兼容读取1.1/1.2。
  Diagnostic区分未找到、状态超时、视觉未命中和backend错误，保留等待预算、探测次数、
  候选数、Root与cause；公开Page API和原异常类型不变。只有在Behave显式绑定一个已完成Transaction、完成报告与
  Request/Plan身份有效、且当前全部生成根代码快照仍与Transaction一致时，才写入
  `generation_provenance`。Query和质量门按完整provenance精确匹配；同Feature/Scenario
  的其他Transaction或无绑定1.1/1.2结果只作为历史报告，不能显示为当前已验证。
- `inspect` 不开启事务；`prepare` 冻结 Request revision、Brief、Plan 和 policy；
  `finish` 校验真实文件变化、Step scope、PIC、证据与 Plan-to-Code 一致性。
- Plan-to-Code只证明调用顺序、locator/owner、值和provenance符合Plan，不模拟pywinauto
  后端如何解释控件文本、状态或动作。运行时适配器语义由共享Action契约测试与真实
  Behave Run Result负责；不要把Value/Name等后端读取规则复制进Plan校验器。
- Evidence Graph为每个Action最多冻结4个已验证、内容寻址的结构locator候选；Brief有界
  投影候选ID/稳定依据，完整表达式按Action展开。AI可在Design中选择candidate ID，系统在
  Design、Plan和Manifest边界重复校验Action、Root、唯一性与target match；用户不参与技术选择。
- `failed` 状态的`inspect`只从Workflow绑定的`last_result`投影原Intent、Plan和事务报告；
  路径、transaction/request/status、revision、Plan、Intent或终态报告内容指纹任一不一致
  都拒绝修订上下文。外部AI可显式提交一次修订Intent，但Recorder不自动循环模型。
- Evidence Context 2.3加载时重算事实内容指纹；2.0-2.2无指纹artifact只保持历史只读
  兼容，带指纹旧artifact仍必须匹配。Request声明的Context指纹不能替代内容自校验。
- 多 Step 动作身份是 `(step_id, action_id)`，不同 Take 的本地 action ID 不得跨
  Step 借用证据、locator 或实现文件。
- 当前生成作用域是一个录制 Session 中的 Scenario 目标 Step 集：Step 仍是采集和
  修复单元，Request/Brief/Plan/Transaction 聚合全部目标 Step。显式部分范围必须
  保存 excluded Step 并标记 incomplete；Outline 的 `logical_template_id` 只关联
  参数化模板，不能跨 Examples 行借用 runtime evidence。
- 相邻 Step continuity 与属性断言只能从已冻结的 Semantic Pack 派生为 advisory
  candidates；不得为此增加高频截图、UI 树或新的原始监听，也不得让候选绕过
  Decision、Plan、evidence coverage 或 finish 校验。
- V5 保持文件化和 fail-closed，不引入数据库、服务、队列或常驻守护进程。
- 跨机器录屏使用版本化 Recorder 便携包：导出只读保存 Run 原始/有效证据、补录、媒体
  和 PIC，剥离 Request/Workflow/Plan/Transaction；导入先校验逐
  文件 SHA-256 和身份，再重定位、重建投影/catalog，并在目标机器物化新 Request。
- Step Data Table 始终作为 Request 中的原始业务数据，不由表格形状自动决定循环。
  Reconciler 结合 Step 文案、录制 actions、现有代码和已确认经验生成 Table Usage
  候选；AI 在 PlanV4.2 中选择用法，含糊且需要用户业务权威时只在 Decision Pack 询问
  一次。Plan 必须声明 `each_row`、`whole_table` 或 `scenario_state`、数据形状、消费
  owner、列角色和顺序/重置约束。Step 解析 `context.table`；场景特有逐行/整表消费可
  留在 Step，稳定复用循环才由 Page Object 承载，跨 Step 数据显式写入 Scenario Context。框架不提供专用
  Matrix Runtime，也不复制 Feature 表格为第二套持久协议。
- 运行时元素值通过`BasePage.save_text()`或`save_attr()`写入
  `ScenarioRuntimeState.variables`，并由`get_variable()`显式读取；变量规则由
  `actions/variable_actions.py`拥有，BasePage只解析locator并转发。变量不进入静态data、
  不跨Scenario保留，默认拒绝空值和覆盖。Plan 4.2允许有F9 ObservationIntent证据的
  `save_text`/`save_attr`用`result_binding`生产值，后续operation用
  `source=runtime.<binding>`消费；`get_variable`只作为实现辅助，`set_variable`不向Plan
  开放。AI决定业务关系，系统只验证同Scenario引用、唯一性、顺序、Feature声明冲突和
  Plan-to-Code闭包，不按值相同、Step相邻或continuity候选自动连线。

详细协议见 [Recorder 设计](../../docs/维护/3.Recorder设计.md) 和
[框架结构](../../docs/维护/2.架构设计.md#recorder-v3-证据与生成架构)。

## 生成产品目标与简单旅途门

本功能的目标是让AI根据Feature、录屏证据和现有项目代码，生成正确、可运行、可维护的
自动化测试脚本。AI自由度、安全门、投影层和速度预算都只是实现手段，不是产品目标。

- 系统硬限制只负责可机械判断且后果严重的边界：证据不可篡改、写入范围、PIC授权、
  Execution Profile、Plan/Transaction/Run Result来源和代码快照。
- AI负责operation、Step/Page归属、现有资产复用、locator、业务命名和完整实现推理。
- 只有业务事实缺失或冲突、证据缺失或矛盾、以及PIC授权才询问用户。Feature/Rule/
  Examples/Data Table已明确的业务事实不得重复确认。
- 当前Contract无法表达正确实现，或需要修改`autowork_core`、`framework_validation`及框架
  Prompt/测试时，普通生成任务必须abort并报告framework defect，不能在同一任务维护框架。
- 默认上下文只提供紧凑Brief、按需证据、当前代码候选和能力契约；删除重复投影、重复读取、
  重复确认和重复事务来提速，不能通过截断事实、跳过安全校验或弱化真实运行换速度。

`framework_validation.evaluations.simple_path_evaluation`是只读发布门，不进入生产协议，也不
替AI选择实现。它对fresh简单旅途同时评估正确性、用户成本、可维护性、安全、执行真实性
和效率：默认要求一个business Request、一个Plan、一个Transaction、零重复业务问题、稳定
公开命名、仅业务文件、默认上下文不超过50 KiB、Transaction不超过180秒、Request到静态
完成不超过300秒。有运行授权时还要求绑定Run Result和框架注册的独立业务Oracle；无授权时
只允许`static_validated/runtime_not_run`。没有签名回执的模型调用次数和整机无关进程数明确
标记`not_measured`，不得自报为通过。

2026-08-22的历史Notepad旅途是失败基线：4个Request、2个Plan、2个Transaction（一次
abort），完成事务约305秒，公开路径含内部hash，两个绑定Run Result均失败且没有独立Oracle。
这些事实证明旧链存在问题，不能作为新方案验收。当前开发机可以做Notepad真实窗口验收，
但本次架构实现尚未产生fresh external-AI + 显式Execution Profile的新链结果；在该结果和
独立编辑器文本Oracle出现前，简单旅途发布门保持未通过。Gantry/真实设备仍是外部环境门，
不得在本机模拟替代。

## 所有权边界

- `CaptureRuntime` 唯一拥有 Hook、视频、暂停、观察、通知和 stop/abort 生命周期。
- `CaptureRuntime`同时拥有可选悬停解析worker的启停与visual-only通知；每个MouseMove
  直接提交Recorder同款`ElementFromPoint`最小控件解析，忙时只保留最新坐标，不使用dwell、
  定时采样、点击确认、控件树缓存或第二目标路径。结果通过Win32消息直接绘制点击穿透边框，
  不进入raw/canonical evidence，也不建立窗口scope。只有边框可从mss/ffmpeg桌面捕获中
  排除时才显式启用；视觉链失败不能影响Hook、F9、F10、Take完整性或录制成败。
- Hook ingress 只分配稳定事件 ID 并投递内存队列；raw journal、窗口证据、边界帧和
  事件富化使用独立进程内 lane。F10 必须封存 `raw-events.seal.json`，并由
  `capture-completion.json` 证明 raw/canonical event ID、数量和顺序一致；任一 owner
  线程未停止、迟到回调或集合不一致都失败关闭，不能提交部分 Take。
- Recorder 实时 target inspection 不执行 Child/XPath Root 唯一性回查。动作首事件只
  采集目标、有限局部上下文和 deferred 候选，提交事件只刷新必要状态；结构 locator
  在结束后按事件 HWND 使用完整、未截断窗口树离线验证，跨窗口树不能互相证明唯一。
- `RecorderWorkbench` 唯一拥有主录制与补录期间的窗口最小化和恢复。
- `OperationCoordinator` 是 UI/application 后台任务的统一执行边界。
- `scenario_runtime`只拥有`APP_PATH`启动、进程追踪、录屏和通用Hook顺序；它通过约定式
  `Bdd.application`调用可选`prepare_scenario(context, scenario)`和
  `cleanup_scenario(context, scenario)`，不认识产品Page、设备或资源标签。空宿主实现是
  no-op；`APP_PATH=runtime`时另要求宿主提供`start_application()`和`stop_application()`，
  缺失即失败关闭。项目准备失败后Behave仍执行`after_scenario`，先释放宿主资源再清理
  APP_PATH进程。
- 当前宿主的应用、资源标签、Page就绪、设备集成和运维资产由`Bdd/README.md`及其项目
  文档维护；框架上下文不复制产品名称、固定路径、进程清单或设备恢复步骤。
- 主 Take 与 Supplement 复用采集内核和状态界面，只拥有不同 artifact sink。
- Timeline 是内部append-only证据协议和实现名称；普通用户界面只称“录制内容”或
  “检查与补录”，用户不需要理解Timeline、Take、revision、物化或readiness。补录片段
  只有通过 `insert_supplement` 编辑才进入effective evidence。
- Recorder默认路径只呈现六项用户任务：正常录制、暂停准备、F9检查、忽略误录、补上
  缺失动作和回答业务问题。动作类型、技术角色、binding和动作组合的目标owner是AI，
  但技术修复入口只能在对应结构化替代和复杂验收通过后逐项退役；隐藏或删除控件本身
  不算系统/AI完成接管。底层Timeline协议继续兼容历史类型、角色、binding、merge和
  逐Action补录编辑；新修复仍遵守append-only/revision门。
- 默认补录流程必须先冻结插入锚点，完成片段后再让用户一次确认整段Action顺序；确认
  才追加`insert_supplement`，取消只保留片段。Phase 0临时兼容工具可从已保存片段逐Action
  插入，但必须为每条写入记录结构化路径尚未覆盖的原因。
- 系统只接管确定性派生和校验；AI接管有冻结候选及Plan/Transaction门禁的技术推理；
  业务真值、pause/窗口归属、表格用法、规格冲突和PIC授权仍由用户决定。
- Capture 在录制期持有长期 Run 写租约；完成录制立即释放。Timeline mutation 和导出
  只在各自完整文件事务期间持有短期 `.recorder.lock`，并按 expected revision 或快照
  边界失败关闭，不能在检查与文件读取之间释放租约。
- UI 只读取 Query Service DTO，不直接解析 JSON/YAML artifact 布局。
- `session_projection.py`、`request_repository.py`、`workflow_service.py` 和
  `reconciliation_repository.py` 分别拥有投影、Request、编排和 Reconciler I/O；
  规则层不得反向调用 application service。

### Recorder 说明模型与技术能力迁移门

Phase 1/2/3已删除共享说明槽：Step业务说明、F9意图、F7边界、F10总结、取消原因和跳过原因
均有独立生命周期。说明模型只允许以下typed authority：

- `StepUserContext`已绑定`step_id`，表达Given前置、When业务动作/约束或Then期望逻辑；
  它通过`recording-annotations.jsonl`append-only保存，受expected revision、Run锁和Step
  scope约束，进入Request身份/revision与Brief。与Feature显式冲突时路由无预选Decision，
  不能覆盖Feature或直接修改Plan；旧Request只在当前revision为0时兼容缺失annotation字段。
- `ObservationIntent`已绑定一次F9的`step_id/take_id/event_id/action_id`，保存检查重点、
  关系、受控expected reference和可选业务含义；只影响该Observation，快速双F9和未处理
  Intent时F10均被拒绝，采集失败不创建Intent。
- Pause只保存状态边界与差异，归属通过结构化Decision确认；新录制的F7不消费或保存文字，
  旧Run Pause note仅只读兼容。
- F10只封存Take；可选总结只存Run manifest审阅状态并显示于Take选择器，不进入`take.json`、
  AI context、Request、Semantic Pack、Brief或Plan。
- 取消/跳过使用独立原因，同样只属于审阅状态；Shift+F11使用固定原因且不弹窗。新2.0
  artifact不再写通用`note`，旧Run note只读迁移且不改原文件。自由文本不能充当`window title contains`
  等技术命令；新录制使用结构化subject/relation/expected reference。

迁移必须严格按阶段推进，不得因一个support app或全量单测通过而跳阶段：

0. **临时兜底**：结构化替代和复杂验收完成前，技术修复能力不得静默丢失，也不得转嫁给
  普通用户。当前实现已没有type、role、binding、merge或逐Action补录的技术写UI，测试
  明确锁定默认界面只允许检查误录与整段补录；历史技术编辑仍只读兼容。由于真实产品门
  尚未闭合，这一状态是待验收的迁移缺口，不是已授权退役：若真实产品暴露尚无typed替代
  的必要能力，生成必须失败关闭，维护者应先完成进入完整信任链的结构化替代；发布前确实
  无法替代时，才恢复与普通路径隔离、记录未覆盖原因的临时维护工具，不能要求用户理解
  Timeline或填写技术属性。
1. **Step Context（已实现，待L0-L4累计验收）**：append-only、revision-bound的Step业务
  说明已纳入Request指纹/revision、Brief authority、Feature冲突Decision、Bundle校验和
  便携包；F7/F9/F10不消费它。
2. **Observation Intent（已实现，待L0-L4累计验收）**：F9先采集真实目标，再绑定一次性
  typed focus/relation/expected source；新note暗号无效，pre-5.8旧artifact受限迁移；主Take、
  Supplement、Semantic 5.8、Request/Brief、Bundle和便携链已闭合。完整、未截断且内容寻址
  的集合receipt可形成有序断言候选；`get_collection_items`与`assert_collection_equal`只读取
  结构化集合的直接子项、排除ScrollBar chrome并限制最多200项。receipt篡改、截断、目标不唯一
  或超过上限均失败关闭，不能用Decision放行。
3. **Pause/F10解耦（已实现，待L0-L4累计验收）**：F7只管理pause evidence；F10只封存
  Take并可保存review-only总结；取消/跳过各自拥有review-only原因。typed字段有2000字符
  API边界，旧note只读迁移，新生成链不消费这些审阅文字。
4. **信任链闭合（已实现，待L0-L4累计验收）**：Request/Brief冻结不含自由文本的
  Annotation snapshot；AI Intent必须覆盖每个目标Step，系统为各Step派生精确
  `annotation_ids`全集及scope、revision、authority和content fingerprint写入Plan trace；
  兼容输入若显式提供IDs则必须完全一致。Transaction prepare/finish冻结
  并重算Annotation lease；缺失、额外、stale、跨Step/Take/event、proof注入和自由文本
  Plan mutation均失败关闭。Request语义ID、source-backed fingerprint和effective Action
  过滤均参与对抗校验。
5. **复杂与实机验收**：在现有UJ之外覆盖Background+Outline+多Step、多个业务窗口、
  模态、动态集合、拖拽/滚动、OCR/PIC、Canvas、日志/领域Provider、长异步假就绪、
  崩溃恢复、设备联动和多补录锚点；每类必须有成功、对抗和失败关闭变体，并完成
  external-AI Intent、Transaction及真实Behave Run Result。最后还需代表性真实产品
  Feature验收，support app不能替代产品契约。

当前L3复杂组合门必须同时满足组合测试能力和对应external-AI成功链的最小语义锚点，不能
用任意`ai_quality_passed`产物替代组能力。G1、G2、G3、G4和G6已闭合；G3同一真实Take的
受控PIC、真实drag和冻结scroll已进入用户Decision、external-AI Plan、Transaction、真实
Behave与质量门。G6以普通F9自动采集Canvas区域文字，冻结Receipt/Root/Window/Region，
由Feature声明期望，经external-AI Intent、Plan、Transaction和真实PaddleOCR Behave闭合；
篡改、重绑、低置信、空结果或截断均失败关闭。通用框架矩阵只拥有G1、G2、G3、G4和
G6；原G5 Gantry/真实设备链属于宿主外部产品门，不得由框架评测硬编码`Bdd.tests`。
该产品门及L4仍未闭合，因此任何Timeline技术能力退役均未获授权。

任一旧技术能力只有同时满足以下门槛才可退役：存在等价typed替代；替代进入完整信任链；
相关普通与复杂旅途通过；至少一个对应真实产品Feature通过；成功与失败关闭均有证据；
旧Run仍可只读兼容。未满足时恢复或保留临时兜底，不得用界面简化掩盖功能缺口。

### Feature目录工作台与跨项目交付

Recorder是Feature实现过程中的可选工具，不是Feature生命周期。用户可以直接手写脚本；
也可以选择本地录制后AI生成，或把已有录制资料跨工程导出/导入。录制工程和生成工程安装
同一模块，但不建立强制顺序、两套产品或角色权限。

- 首页选择项目内Feature目录并递归加载`.feature`，按Feature -> Scenario/Examples展示。
  用户可通过移动Feature文件整理当前目录；底层Run目录始终由系统拥有，不能手工移动。
- Feature使用单一`# recorder-feature-id: feature-<hex>`持久身份。首次实际加载录制时以
  旧路径派生ID原子初始化，保证既有Scenario/Step身份不变；扫描目录不得批量改写文件。
  移动后ID保持，`source_relpath`可变化。重复/非法ID失败关闭；ID只表达血统，不证明内容
  相同。移动后若要继续生成，仍须受控更新source binding，不能仅凭ID复用旧Request。
- Feature目录只投影三个独立事实：录制覆盖、最近导出、真实问题。“无录制”和从未导出
  都是中性事实；解析、重复身份或录制损坏才是问题。Workflow、Transaction和Run Result
  只属于具体Run，不能派生Feature目录总状态。
- 变化判断分层：源码SHA表示字节相同；版本化完整业务投影/指纹用于解释Scenario、
  Background、Examples、Step、Data Table变化；真实运行和用户权威判断产品变化。业务
  指纹只用于管理提示，不得自动确认无需维护、覆盖Feature、复用Request/Plan或替代运行。
- 一个Scenario/Examples实例的Run仍是最小证据单元；一个Feature录制资料包聚合该Feature
  当前已有的零散录制覆盖并携带Feature快照、来源、逐文件SHA和交付清单。至少有一个
  当前有效Run才创建资料包，但不要求所有Scenario录制。多Feature导出保持每个Feature
  为独立交付单元。包不携带源工程`ai/`生成状态、代码、Plan或Transaction。
- 生成工程导入前可没有Feature；导入时预览并把Feature安装到清单声明的项目相对路径。
  目标不存在则创建，SHA相同则复用，ID相同但内容不同必须展示差异并明确处理，业务结构
  不同的Run不能进入可生成状态。同session ID且内容相同可跳过，不同则拒绝；关键失败
  整体回滚。导入后重建projection、catalog和Request，再沿现有生成主链继续。
- Feature级记录分别保存最近导出和最近导入，导入不能覆盖用户关心的导出事实。记录绑定
  Feature ID、源码SHA、session ID、更新时间和包SHA，用于“最近导出/导出后有变化”；
  它不是状态或证据权威。
- 当前普通导入导出只位于“Feature与录制”页；历史页只管理Run。底层Run便携包继续由
  API/CLI支持维护、备份、诊断和兼容，不作为第二个普通业务入口。多Feature先在目标卷
  完整生成再以不覆盖方式发布；辅助记录失败只警告，不回滚已经完成的主交付。
  补录回传和批量生成仍后置。保持文件化，不增加数据库、服务、队列、复杂标签或独立
  归档系统。

## AI 资产生命周期

- 顶层 `ai/context`、`ai/prompts` 和 `ai/instructions` 是版本化权威内容。
- `Bdd/ai/portable-knowledge/records`是当前项目仓库的内容寻址不可变知识投影；首版
  只接受 production user-confirmed Capability，不自动发布、注入上下文或操作 Git。
  查询有界且结果仍须以当前代码、证据和用户说明复核。
- `Bdd/ai/knowledge`是跨Run的自包含advisory knowledge，自动保存确认Plan、
  Capability、生成结果和用户反馈，不保存原始媒体。
- `knowledge_audit.py` 只读检查 durable knowledge、Collaboration、Shadow Companion、
  旧 Work Package 残留、隔离资产和隐私边界；audit 不初始化、迁移、修复或删除
  knowledge。
- `knowledge_maintenance.py` 只在用户明确确认后重建派生 catalog，或把 audit 已判定
  invalid 的 Capability 原字节隔离并留下可恢复回执；quarantine 默认不迁移。
- `.github` 保留 VS Code 自动发现所需的薄适配器，以及小型、可审计的确定性生命周期
  Hook；权威规则和 Prompt 正文仍位于 `ai/`。
- `artifacts/recording_sessions/<run>/ai` 是单次 evidence/transaction 工作区，随 Run
  退役，不是长期知识库。
- Copilot `/memories/repo/` 是可丢失缓存，不是事实源。
- 编码协作默契通过 `/collaboration-review` 生成只读候选；只有用户在当前请求中
  明确批准候选 ID 后，`/collaboration-promote` 才能最小修改 user memory、
  `.github` 或 `ai/context`。Review 本身不能改变规则。
- Collaboration Review 和 Promotion 只由框架负责人明确手动调用；SessionStart、
  会话计数、时间阈值和项目活动均不得触发提醒、Review 或 Promotion。仓库不认证
  组织角色，负责人授权属于仓库外治理，不得为此重新引入 Project Identity 或 Doctor。
- Shadow Companion SessionStart Hook 只校验 active Capsule、清理过期 dormant 状态
  并注入短恢复指针；它不读取聊天、不生成工程摘要、不选择 Epic，也不修改源码、
  Prompt、instructions、用户 memory 或 Git。当前请求不匹配时 Agent 必须忽略指针。

保留一个 Run 时不得选择性删除其 evidence；知识成功提炼且不再需要复现、审计或
重新生成后，可以整 Run 退役。详细策略见 [连续性与保留](continuity.md)。

## 生成原则

- 先搜索现有 Step、Page Object、locator、data 和 confirmed Capability。
- 规则只提供 evidence、candidate 和 safety gate；AI 负责业务意图、代码归属和
  完整实现推理。
- 精确 Gherkin pattern 只证明 Step 候选可执行且文案匹配，不能自动证明函数体覆盖
  本次录制的全部 action。行为复用必须由 Plan 显式绑定冻结候选和 action 集。
- 长期顶层 Root 和可复用目标写入 locator YAML。生成代码不得新增 `set_root()`、
  inline locator 字典或未经授权的 PIC。
- 定位优先级为 Child -> XPath -> OCR + Region -> POS。PIC 仅在结构化定位失败、
  模板审计通过且用户明确授权后使用，并在 prepare/finish 双重校验。
- Runtime结构定位的`name/title/text`统一表示Accessible Name，Root、Child和XPath共用
  严格属性读取；空Name不回退正文，backend属性错误不伪装成未找到。legacy default只做
  auto_id/Accessible Name精确查询，隐式OCR fallback已退役；新生成调用必须使用显式
  `$name`/`$loc:name`，OCR/PIC必须显式声明Region。
- UIA 的 Document、Canvas、Pane 等结构角色不能单独证明目标较弱；应结合唯一
  locator 回查、runtime identity、状态和媒体结果判断。
- 不得为了让工作流通过而削弱 Request、projection、Decision、Plan、transaction、
  scope、PIC 或 Plan-to-Code 校验。

## 学习与外部知识

Recorder 项目知识是 append-only advisory evidence。用户明确确认的 Plan 可以发布
Capability；AI 自动生成结果的首次 accepted 只记录候选，第二个来自不同 transaction、
Request 和录制 Session 的同构结果再次 accepted 后才发布 confirmed Capability。机器计划
不能伪装成用户确认。当前用户说明、
当前代码和当前录制证据始终高于历史经验。

Request 只绑定当前 target 可见的结构化经验 revision，而不是整个 journal hash；
无关 Feature/Step 的事件不得让 Request 抖动。Memory Context、Request ID 和 Brief
使用同一次快照；Brief 只暴露有界、白名单化的 `memory_digest`。Plan 可选
`memory_trace.applied/dismissed`，引用只能来自冻结 Brief，transaction 报告只从
冻结 Plan 派生使用/拒绝记录，不能接受外部自报。

相关经验在事务前变化会让旧 Request stale；`running` transaction 继续使用冻结的
Brief/Plan，完成报告仍可审阅和反馈，再次 prepare 时才要求新 Request。纯 freshness
读取不得迁移或写 knowledge；已绑定 revision 却无法复核时在事务前 fail-closed。

宿主可按需使用外部产品知识Provider补充候选，但它不是runtime evidence，不能证明当前
控件存在或唯一，也不能授权PIC。正式引用时必须冻结provider/database fingerprint、
query、result IDs和结果hash；Provider不可用时V5正常降级。具体Provider和产品语义只在
宿主项目文档中维护。

## 验证习惯

- 修改前从最接近行为的实现、失败测试或调用点建立可证伪假设。
- 第一次实质编辑后立即运行最窄行为验证，再继续相邻改动。
- 不回滚无关 working-tree 修改。
- 框架测试入口：

  ```powershell
  python -m unittest discover -s framework_validation/tests -p "test_*.py"
  ```

- 仅同步已发布框架到宿主项目时，不运行上述全量测试；只运行：

  ```powershell
  python -B -m autowork_core.utils.debug_tools.framework_smoke --project-root .
  ```

- 当前宿主项目测试入口：

  ```powershell
  python -m unittest discover -s Bdd/tests -p "test_*.py"
  ```

- 选择项目支持的 Python 3.11 环境，不在版本化文档中固化机器解释器路径。
- 2026-07-29 的已知检查点为 Recorder `169/169`；这是历史记录，不是固定数量。

## 维护本文件

Recorder 主链、模块所有权、信任边界、AI 默认上下文或迁移方式变化时，实施变化
的人或 AI 同步本文。用户不需要在每次录制、生成或普通缺陷修复后手工维护。
不要记录临时调试结论、机器绝对路径、未确认猜测或完整聊天内容。