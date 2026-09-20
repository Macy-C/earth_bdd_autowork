# BDD Autowork 架构宪法

本文件仅存跨任务架构边界。用户请求、代码、测试和冻结 evidence 优先；其余 advisory。
细则在维护文档。

## 产品与职责

BDD Autowork 是 Windows 桌面 BDD 自动化框架：Feature 表达业务，Step 承担场景动作和编排，
WindowPage/WindowView 拥有稳定窗口与 locator，通用 actions 不承载产品语义。框架提供可信
事实、候选和安全边界，不把业务推理写成规则系统。

- AI 决定业务意图、operation、Step/Page/View 归属、复用、稳定业务命名、表格和运行时关系。
- 系统冻结事实、候选、权限、proof、写入范围和生命周期，并验证选择。
- Generation Job 开始前封存 Generation Capsule；AI 和中途 Transaction 只能消费 Capsule
  中的冻结生成输入，live workspace 只在最终 commit gate 和安全门中读取。
- 简单与复杂 Job 使用同一公开旅途：最多一次用户权威/授权 Answers 批次、一次有界 typed
  choice 批次、一次候选原生编辑和一个终态结果；AI-authority 业务歧义留在 Job 中解决。
- Workbench 主入口保持幂等：ready/running Job 继续，同一有效终态结果复用；显式
  “重新生成 Copilot 请求”在任意阶段都开启同一 Request 的新 attempt。
- Workbench 创建并复制 Copilot Job；用户权威/授权 Answers 在粘贴 `advance-job` 后、完整生成前由
  Job-bound option batch 一次性提交。AI 可判断的业务歧义不阻塞 Copilot，问题前外部文件审批为零。
- 完整 system-owned candidate 必须先在 Capsule 私有 staging 中通过 Python、locator、Step scope 与
  Plan-to-Code，再取得 Transaction/file lease 和原生编辑入口；失败终态化且 `Bdd/` 零变化，AI 不得手修。
- 仓库只投影紧凑候选索引和项目内只读 source files，不伪称拥有 VS Code 原子 WorkspaceEdit；宿主在
  一个 assistant turn 提交独立原生编辑，Hook 禁止 `--all`、chat resource 和外部 terminal-output 读取。
- 用户只决定缺失/冲突的业务真值、需其权威的证据修复、规格冲突和 PIC 授权；系统可追加唯一、
  验签的技术修复。已声明业务事实不重复询问。
- 没有有效授权分支的 PIC 候选由系统默认拒绝，不形成用户问题；终态失败先展示 owner 阶段和
  原因，SLA 只作为独立健康信息。
- 没有有效授权分支的 PIC 候选由系统默认拒绝，不形成用户问题；终态失败先展示 owner 阶段和
  原因，SLA 只作为独立健康信息。
- 不确定性先用代码、测试、证据或低风险检查消除；仍影响业务、范围、安全或不可逆操作时，
  说明选项与后果后再问用户，不猜测。

## Recorder 受控生成

Recorder 生成是独立功能域。修改其架构或协议前，先读取 [Recorder 设计](../../docs/维护/3.Recorder设计.md)、
`ai/instructions/bdd-generation.md`、当前 generation contract 及其 owner 测试。必须保持证据、用户权威、
写入范围、lease、provenance 与结果分层的失败关闭；具体 Job、Design、Plan、Manifest、Transaction 和运行
语义以当前模块代码、生成契约和测试为准。

## 所有权与维护

- `CaptureRuntime` 拥有 Hook、视频、暂停、观察、通知与 stop/abort；完整性不一致失败关闭。
- `RecorderWorkbench` 拥有录制期间窗口最小化/恢复；`OperationCoordinator` 是 UI/application 后台边界。
- `scenario_runtime` 只拥有通用启动、进程追踪、录屏和 Hook 顺序；宿主经 `Bdd.application` 提供准备/清理，
  不引入产品 Page、设备或资源标签。
- UI 只读取 Query Service DTO；投影、Request、编排和 Reconciler I/O 各有 owner，规则层不得反向调用
  application service。
- 普通 Recorder 只让用户录制、暂停、F9 检查、忽略误录、整段补录和回答业务问题；用户技术性
  Timeline 写操作已删除，当前协议只保存证据修正；系统验证过的修复仍可按受控协议追加。每项退役依赖等价替代、
  完整信任链和针对性成功/失败验证，不能借产品门重新开放。

## 验证与外部边界

首次编辑前定位 owner、可证伪假设、不变量与最便宜检查；每次实质编辑后先运行能证伪
本次假设的最窄验证。只有改动跨越公开契约、生命周期、并发或安全边界，聚焦结果矛盾/
不明确，相关环境或证据变化，或用户/仓库明确要求时，才扩大到相关套件、对抗审查或完整
回归。相关输入未变化时可复用同一里程碑内的新鲜结果；不得因无关编辑重复全量运行。宣称
架构阶段、跨模块契约或用户旅程完成前，再做风险相称的最终审查与回归。不要回滚无关改动。

长链路或架构任务先列出用户可观察的不变量、事实来源、失败边界和对应验证，再按一个
垂直切片完成owner修改与聚焦验证。测试、字节数、benchmark和本地artifact只是证据；若它们
没有直接测量用户目标，不得为了变绿而优化代理指标，也不得据此宣称端到端成功。协议字段和
版本应在行为稳定后一次收口，再运行昂贵回归与真实旅途；不要边改版本边反复重跑全套。

用户可见交付属于完成条件。后台写入文件、静态验证成功或生成结果指针存在，都不等于用户已
收到代码或结论；宿主必须实际呈现约定内容。宿主平台无法证明的命令发送时间、模型调用、并发
或对话展示只能标记为未测/不完整，不得由CLI参数、本地重放或替代Job伪造。

真实产品、Gantry、设备及依赖外部环境的 Oracle 属于外部环境门；未运行时如实标为未验证，不能由 mock、
support app、静态检查或历史结果替代。框架同步只运行 `framework_smoke`；宿主 `Bdd/` 发生改动才默认运行宿主测试。

## 上下文与恢复

Project Context Hygiene、Adaptive Work Loop 和 Shadow 只帮助呈现、恢复和验证节奏，不能限制搜索、
读取、推理、测试或 fallback，也不能按固定 turn/token/tool 截断、丢弃上下文或切换会话。Shadow Capsule
不是事实源；过期、冲突或目标不匹配时重新调查。

只在长期目标、所有权、信任边界或默认上下文改变时修改本文。资产生命周期、迁移、Run 退役、知识、
Review/Promotion 和 Shadow 细节见 [资产迁移与知识维护](../../docs/维护/5.资产迁移与知识维护.md)。
临时修复计划属于 Issue/PR；结束后保留代码、测试和维护文档中的长期结论，不保留计划上下文。
