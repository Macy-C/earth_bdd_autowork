---
feature: Bdd/test_features/earth/CT-NMP-SSRS-Console.1007.feature
scenario: Repeat a helical scan
step_file: Bdd/steps/CT-NMP-SSRS-Console.1007/scenario2.py
status: resolved
---

# Repeat a helical scan 待确认事项

## 1. 待确认业务问题

### Q1：螺旋扫描（Helical）模式下 `Cycles` 参数是否仍需读取与比对？
- **背景与影响**：Feature 的 Step 3 参数表列出了 `Length`、`Collimation`、`Cycles`、`kV`、`mAs`，但产品手册（IFU `offset=454`）定义 `Cycles` 为完整扫描旋转圈数且注明仅适用于轴向扫描（`Axial scans only`）。若 `Brain Helical` 协议在 `Show All -> General` 面板中不提供有效 `Cycles` 值，读取 `$loc:general_Cycles_Combobox` 会在运行时触发空值或控件异常。
- [ ] **方案 A（默认已采用）**：保持按 Feature 表格与同 Feature 轴向案例基线读取 `$loc:general_Cycles_Combobox`（`value.value`），并在 Step 3 比对 `cycles == cycles2`
- [x] **方案 B**：在螺旋扫描脚本中排除 `Cycles`：Step 2 不读取 `$loc:general_Cycles_Combobox`，Step 3 仅比对 `Length`、`Collimation`、`kV`、`mAs` 四项参数（保持 Feature 文件只读不改）
- **用户补充说明**：

### Q2：`Brain Helical` 协议是否默认勾选 `DoseRight Index` 并需在扫描前关闭以保持 `mAs` 一致？
- **背景与影响**：Step 3 要求首次扫描与 Repeat 扫描的 `mAs`（`$loc:DM_mAs_Combobox`）保持一致。轴向案例（`Brain Axial 420`）中 `DoseRight Index` 默认未勾选且轴向扫描不使用 3D 剂量调制；而产品手册（IFU `offset=1425, 1445`）说明螺旋扫描支持 `DoseRight Index` / `3D Dose-Modulation`，启用时会根据定位像自动重算推荐 `mAs` 并在扫描后更新为实际平均 `mAs`，取消勾选切换为手动模式则保持协议原始 `mAs`。手册未提供被测环境中 `Brain Helical` 协议的 `DoseRight Index` 默认勾选状态。
- [ ] **方案 A（默认已采用）**：保持 `Brain Helical` 协议默认状态，不点击 `$loc:DoseRightIndexDM_option`，直接读取并比对首次扫描前与 Repeat 后的 `$loc:DM_mAs_Combobox`
- [x] **方案 B**：在 Step 2 点击 `$loc:protocol2_option` 和 `$loc:ShowAll_button` 后、读取初始参数前，先点击 `$loc:DoseRightIndexDM_option` 关闭 `DoseRight Index`（切换为手动 `mAs` 模式）
- **用户补充说明**：

## 2. 非提问工程限制（Engineering Limits）

- **`EarthPlatformSimulatorPage` 静态预检未验证（`unverified`）**：`Bdd/simulator_client.py:75` 包含静态 `--resources` 预检不支持的模块/类执行结构，静态检查记为 `unverified`，需在实际模拟器运行环境中验证。
- **Step 局部定位器运行时边界**：Step 4 沿用同 Feature 已认可案例的局部 XPath 字典 `series201` / `series301`（`//DataGrid[@auto_id='DataGrid_1']//Text[@name='201']` 与 `'301'`），静态命名引用预检不覆盖局部字典在运行时的 UI 匹配与唯一性。

## 3. Sources & Bindings

- **Sources**：
  - `Earth工程案例库_demo`：`Earth__CT-NMP-SSRS-Console.1007__Repeat-an-axial-scan__515648b52aa1.md`（doc `759342c6-0129-45b3-9bac-8734fdd706c6`，offsets `0`, `8`, `16`, `24`；截图证据 F22 chunk `64`、F38 chunk `85`；Wiki `concept/repeat-an-axial-scan`、`concept/scan-parameter-consistency-verification`、`concept/axial-series-generation-rule`、`entity/scannpage`）
  - `Earth正式资料库_demo`：`D002293408 300016551971_D_CT Tenara_CT 5300_Incisive CT_IFU-en-US.pdf`（doc `0d90f628-1e2b-4187-82fc-c00e791b363c`，offsets `445`, `454`, `1005`, `1330`, `1425`, `1445`）
- **Step `$loc:` Bindings**：
  - Step 1（`NewPatientPage`）：`$loc:PatientId_Edit`、`$loc:LastName_Edit`、`$loc:FirstName_Edit`、`$loc:Adult_button`、`$loc:Reference_button`、`$loc:Head_button`、`$loc:Head_BrainHelical_protocol`、`$loc:Supine_Prone_button`、`$loc:posture_option1`、`$loc:Start_Exam_button`
  - Step 2（`ScanPage` / `EarthPlatformSimulatorPage`）：`$loc:protocol2_option`、`$loc:ShowAll_button`、`$loc:DoseRightIndexDM_option`、`$loc:geometry_Length_Edit`、`$loc:general_Collimation_Combobox`、`$loc:DM_kV_Combobox`、`$loc:DM_mAs_Combobox`、`$loc:GO_button`、`$loc:Repeat_button`、`$loc:protocol3_option`
  - Step 3（`ScanPage`）：Scenario 变量 `length`/`length2`、`collimation`/`collimation2`、`kV`/`kV2`、`mAs`/`mAs2`
  - Step 4（`CompletedPage`）：`$loc:PatientID_edit`、`$loc:completed_first_dataItem` 及局部 XPath `series201`（`201`）、`series301`（`301`）
