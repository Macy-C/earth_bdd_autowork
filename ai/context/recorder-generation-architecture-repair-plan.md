# Recorder 生成架构修复计划

日期：2026-08-25

本文记录本轮真实 Notepad 生成复盘后的架构修复方案。它的作用是给后续 AI 或维护者恢复上下文，避免把局部测试通过误认为用户旅程闭合，也避免因为忘记上下文而新增错误抽象。

本文不替代当前代码、测试和 `ai/context/project.md`。发生冲突时，以当前代码、测试、真实 Recorder 证据和 `project.md` 为准。

## 最终目标

Recorder 生成链的目标不是“录完能生成代码”，而是让测试人员完成一条可信旅程：

```text
录制业务流程
-> 只纠正证据问题
-> AI 理解业务层级和动作意图
-> 框架提供可靠事实、候选和安全边界
-> AI 生成可维护 Step/Page/View/locator
-> 静态验证、真实运行、独立 Oracle 分层闭合
```

架构评价标准必须从人的旅程出发：用户看到的流程、校正状态、生成结构、当前工作区文件和验收结论必须一致。内部 Plan/Manifest 自洽不等于用户旅程正确。

## 当前中肯判断

主架构方向正确，不应推翻：

- Request/Brief/Decision/Design/Plan/Manifest/Transaction 分层是必要信任边界。
- 原始录制 evidence 不可变、Timeline append-only 是正确基础。
- AI 提交 `GenerationDesign`、系统编译 Plan/Manifest/证明字段的方向是正确的。
- `WindowPage/WindowView` 是正确业务抽象，不应因为 popup HWND 新增业务 Page 类型。
- static/runtime/oracle 分层是正确验收模型。

当前主要缺口不是“层太多”，而是若干边界没有完全贯彻用户旅程：

- 有些判断只有 AI 自由文本/字段表达，缺少冻结候选约束。
- 有些验证只证明 code 符合 Plan，没有证明 Plan 符合人的业务结构。
- 有些 UI/CLI 输出把人看的回执、AI 用的实现包、审计 report 混在一起。
- 历史 Job Result 和当前工作区文件状态可能分离，但 UI/CLI 没有明确表达。

## AI 应该保留的判断空间

不要把框架做成替 AI 决策的专家系统。AI 仍应决定：

- Step 的业务意图和 Given/When/Then 角色解释。
- 哪些动作是业务动作、聚焦/菜单打开/运输动作或可吸收动作。
- operation 选择，例如 `click`、`send_text_keys`、`assert_text_equal`、语义控件操作。
- 值权威来源：Feature 文案、Examples、Data Table、recorded action、runtime producer。
- Step inline 还是 Page/View method。
- Page/View 的稳定业务命名。
- 是否复用已有 Step/Page/View method。
- 弹层、菜单、子页面是父 Page 的 View，还是独立业务 WindowPage。
- 证据不足时终止并要求修复 evidence。

框架不应写死“某 class_name 一律是 View”或“某控件一律是某 operation”。

## 框架必须限制的内容

框架限制事实、权限、证明和安全边界：

- AI 不能编造不存在的 Action、Root、locator、值来源、Decision 结果或 proof 字段。
- AI 不能把没有冻结证据关系的两个 root 声明成父子。
- AI 不能把用户已忽略的片段放回 effective 生成链。
- AI 不能把 `view_owner=file_menu` 的 operation 写成 `get_page(context, FileMenuPage)`。
- AI 不能写 Manifest 未授权文件。
- completed/static_validated 不能被展示成“当前工作区仍可验收”，除非当前文件与 terminal snapshot 匹配。
- static 通过不能被描述为真实软件运行或 Oracle 通过。

正确形态是：系统冻结候选，AI 选择候选，编译器验证选择。不是系统替 AI 选，也不是 AI 任意声明。

## 已验证问题

### P0：当前工作区一致性缺口

现象：用户撤回 Bdd 生成文件后，Workflow 仍可显示历史 `completed/static_validated`，但当前磁盘上没有对应文件。

这在审计上不是历史 report 被篡改，但在用户旅程上会误导用户进入真实验收。

结论：需要 `current workspace materialization` 检查。latest Job Result 是历史结论；当前工作区是否仍匹配 report 必须单独投影。

### P0：父子窗口归属缺少证据候选约束

现状：`GenerationDesign.window_ownership` 已能用 `parent_root` 表达 child root 属于父 `WindowPage` 的 `WindowView`，但这个关系仍主要由 AI 声明。

风险：AI 可以写出看似合理但没有证据支撑的父子关系。

结论：`parent_root` 必须改为引用冻结的 ownership candidate，而不是自由字符串判断。

### P1：Plan 语义质量门不足

现象：旧生成中 `FileMenuPage(WindowPage)` 可以通过 Plan-to-Code，因为它符合错误 Plan/Manifest。

结论：Plan-to-Code 只证明实现符合 Plan。还需要一层用户旅程/owner 语义检查，抓“明显反直觉但内部自洽”的模型。

### P1：WindowView locator 放宽需要收紧

现状：为了支持父 Page 拥有的菜单/popup root，窗口 locator package 允许 View locator 文件声明额外 top-level root。

风险：规则如果不收紧，View YAML 可能包含多个无关 top-level root，重新引入跨窗口混乱。

结论：View locator 文件最多允许一个 active root，并且必须等于 Plan 中 `views[view].active_locator`，且绑定该 View 的 `evidence_root`。

### P1：Review Projection 与 Effective Projection 必须成为通用模式

键盘片段问题说明：用户校正状态不能从 effective action 反推。Review 是用户理解，Effective 是生成事实。

结论：以后 target repair、ignore、supplement、merge 等校正路径都要明确其 Review/Efffective 数据源，不能混用。

### P2：CLI/Context 输出受众仍需进一步分层

`prepare-job` 默认输出已经不再内嵌完整 packet，但 `validate-job-implementation`、`finish-job` 等 compact 输出仍有复用 prepare 形状的迹象。

结论：inspect、prepare、validate、finish 应有各自回执；packet 是 AI 实现输入，report 是审计，不应在默认人类输出里混杂。

### P2：默认 AI Context 仍有简单旅途超预算风险

Notepad admission 仍可能出现默认上下文超过 50 KiB 的 warn-only 情况。

结论：这不是 correctness blocker，但会放大 AI 漂移风险。继续做事实等价的结构化压缩，而不是删事实。

## 完整修复方案

### 阶段 1：当前工作区物化一致性门

目标：历史 report 和当前工作区状态分开显示，避免“报告完成但文件已不存在”误导用户。

落地：

1. 在 Query/Workflow 层增加 `workspace_materialization` 投影。
2. 对 latest completed Job Result 读取其 transaction report 的 `implementation_snapshot`。
3. 对比当前磁盘文件存在性、sha256、size。
4. 状态枚举：
   - `matches_report`
   - `missing_files`
   - `modified_files`
   - `extra_generation_files`
   - `report_unavailable`
5. Review UI / CLI inspect 显示历史结果和当前工作区状态：

```text
历史结果：completed/static_validated
当前工作区：materialization_missing
下一步：重新生成或恢复生成产物
```

验收：

- 删除一个生成 Step 文件后，UI/CLI 不得继续表示“当前代码可验收”。
- 重新生成后状态回到 `matches_report`。
- 不改写历史 report，不把历史结果倒改为失败。

### 阶段 2：证据支持的 ownership candidates

目标：AI 仍选择父子归属，但只能选择冻结证据支持的候选。

落地：

1. Reconciler 在 `window_ownership` 中投影 `ownership_candidates`，不生成 operation 草案。
2. 候选结构：

```json
{
  "candidate_id": "window-view-candidate-...",
  "kind": "child_view",
  "parent_root": "notepad_window_...",
  "child_root": "popup_window_...",
  "opener_action_id": "action-open-menu",
  "child_action_ids": ["action-new-tab"],
  "evidence": {
    "opened_by_parent_action": true,
    "parent_action_root": "notepad_window_...",
    "child_action_roots": ["popup_window_..."],
    "order": "opener_before_child"
  },
  "confidence": "evidence_supported"
}
```

3. `GenerationDesign.window_ownership` 对 child view 不再自由写 `parent_root`，而是引用 `ownership_candidate_id`。
4. 编译器验证候选：
   - parent/child root 均存在于 Brief windows。
   - opener action 属于 parent root。
   - child actions 属于 child root。
   - child actions 发生在 opener 后。
   - 同 Step 或有明确 continuity 证据。
   - candidate fingerprint/ids 与 Brief 匹配。
5. 编译输出仍是现有 Plan：`owner.views` + operation `view_owner`。

验收负例：

- 无候选却声明 child view -> fail。
- candidate parent_root 被篡改 -> fail。
- child action 早于 opener -> fail。
- opener action 不属于 parent root -> fail。
- child root 被两个 parent 候选同时选中 -> fail，除非 Design 明确选择唯一候选且无冲突。

### 阶段 3：Plan 语义质量门

目标：防止错误模型内部自洽但不符合用户旅程。

落地：

1. 新增 `validate_generation_semantic_shape(plan, brief)` 或纳入现有 plan validation 的独立区段。
2. 不替 AI 决策，只检查明显的反常形状：
   - 同 Step 中父 action 打开 child root，child action 立刻使用 child root，但 Plan 把 child root 建成独立 `WindowPage` 且无理由。
   - `view_owner` operation 在 Step 中被 `get_page(ViewClass)` 实现。
   - opener/child sequence 被拆成不相干 Page owner，且没有 Step orchestration 理由。
3. 失败类型区分：
   - `semantic_shape_error`：证据与模型矛盾。
   - `semantic_shape_warning`：可能合理但需要 AI reason。

验收：旧 `FileMenuPage(WindowPage)` 形状应被至少 warning，若有 child-view candidate 且 AI 未说明独立 Page 理由则 fail。

### 阶段 4：隔离的 child-root WindowView runtime

目标：支持父 Page 拥有的 popup/menu root，但不放开任意多 root。

落地：

1. 保持通用 `compile_window_locator_package(root_data, view_data)` 语义不变：父
  WindowPage 包恰好一个 root，普通同窗口 View 不能声明 top-level root。
2. evidence-backed child View 在 Plan 中声明：

```json
{
    "ownership_candidate_id": "window-view-candidate-...",
  "active_locator": "file_menu_window",
    "root_locator": "file_menu_window",
    "evidence_root": "popup_window_..."
}
```

3. 编译规则：
    - `WindowPage` 的普通 locator 包仍恰好一个父 root。
    - 显式 `root_locator` 的 View YAML 独立作为窗口包编译，必须恰好一个
      top-level root，且等于 Plan 的 `root_locator` 和 `active_locator`。
    - 该 View 的 child locator 只能引用自己的 root；不能引用父 root 或未知 root。
    - 无 `root_locator` 的普通 View 继续并入父包，旧行为不变。
  4. `WindowPage.load_owned_window_view()` 只把独立 View 包合并到该 Page 实例的
    私有 locator 表，`WindowView` 仍复用现有 `_switch_root()` 和 Page property。
  5. Resource preflight、Manifest packet 和 Plan-to-Code 使用同一隔离规则；不新增
    业务 Page/View 类型，也不放宽 common compiler。

验收负例：

- View YAML 两个 top-level root -> fail。
- active root 与 Plan 不一致 -> fail。
- child locator 指向未知 root -> fail。
- 无 `root_locator` 时保持旧同窗口 View 约束。

### 阶段 5：Packet 作为人可读实现契约

目标：packet 明确告诉 AI 该怎么写，不靠 AI 猜 receiver。

落地：

1. `implementation_packet.pages` 区分：

```json
{
  "base_class": "WindowPage",
  "receiver": "notepad",
  "views": [
    {
      "receiver": "file_menu",
      "class_name": "FileMenuView",
      "path": "Bdd/page_obj/notepad/file_menu.py"
    }
  ]
}
```

和：

```json
{
  "base_class": "WindowView",
  "receiver": "file_menu",
  "parent_receiver": "notepad",
  "locator_file": "notepad/file_menu.yaml",
  "active_locator": "file_menu_window",
  "root_locator": "file_menu_window"
}
```

2. operation 必须包含：

```json
"receiver_expression": "notepad.file_menu"
```

3. Plan-to-Code 对 `view_owner` operation 拒绝 `get_page(context, FileMenuPage)`。

验收：Notepad 菜单 Step 必须出现 `notepad.file_menu.click("$loc:new_tab")`，不能出现 `FileMenuPage`。

### 阶段 6：CLI/Context 输出分层

目标：减少 AI 漂移和人类误解，默认输出只显示当前动作需要的摘要。

落地：

1. `inspect-job`：状态、下一步、预算、Step/Action 计数、展开命令。
2. `prepare-job`：transaction id、AI 文件、system 文件、packet ref、packet summary。
3. `validate-job-implementation`：验证状态、失败 operation、源码位置、期望 packet 片段。
4. `finish-job`：终态、changed files、static/runtime/oracle 状态、workspace materialization 状态。
5. `--full` 只作为审计出口。

验收：默认 prepare 不含完整 packet；validate 不返回 prepare 风格的空 packet ref；finish 不伪装成 prepare。

### 阶段 7：端到端静态旅程回归

目标：覆盖用户真实旅程，不只测函数。

测试旅程：

```text
Notepad evidence: click File -> click New tab -> input 123456 + Left -> delete -> assert
Timeline: ignore Left
Request/Brief: effective command excludes Left
Design: File menu child root chooses evidence-backed child view candidate
Plan: window_owner=notepad, view_owner=file_menu
Manifest: notepad/page.py + notepad/file_menu.py + view locator YAML
Packet: receiver_expression=notepad.file_menu
Implementation: no FileMenuPage/get_page(FileMenuPage)
Finish: static_validated
Workspace materialization: matches_report
```

失败条件：

- `Left` 从 Review UI 消失。
- `FileMenuPage(WindowPage)` 出现。
- `parent_root` 未引用冻结 candidate。
- completed report 与当前磁盘不一致但 UI 仍显示可验收。
- prepare 默认输出完整 packet。
- `WindowView` locator 文件含多个 active roots。

## 落地顺序

1. 当前工作区物化一致性门。
2. ownership candidate 投影与 Design 引用。
3. WindowView locator active-root 收紧。
4. Plan 语义质量门。
5. CLI/Context 输出分层补齐 validate/finish。
6. 端到端静态旅程回归。
7. 用同一份录制/校正重新生成 Notepad Bdd 文件。
8. 配置显式 Execution Profile 后再做真实软件 Run Result + Oracle。

## 后续 AI 恢复规则

后续会话处理 Recorder 生成前，先读本文和 `ai/context/project.md`。不要根据聊天记忆推断当前目标。

如果看到以下情况，必须先停下来做架构判别，而不是继续生成：

- latest Job Result completed，但当前 Bdd 文件不存在或 hash 不匹配。
- AI 想新增 Page/View 类型来解释已有 `WindowPage/WindowView` 父子模型。
- Design 想自由声明 parent_root，但 Brief 没有冻结 candidate。
- 默认 CLI 输出又把完整 packet/report 塞进人类回执。
- UI 从 effective evidence 重建用户 Review 状态。

一句话恢复原则：

```text
AI 决定业务结构；系统冻结候选并验证证据；用户旅程一致性优先于内部 JSON 自洽。
```