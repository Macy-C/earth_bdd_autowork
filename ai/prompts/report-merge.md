# Report Merge

本 Skill 给现有最终报告能力增加自然语言入口，不替用户跑测试，不另写合并算法。
正常用法是：先生成最终报告，后续运行产生新报告，再把新结果回填进已有最终报告。
创建、合并、删除指定结果、单独渲染、预览和 AI 分析可分别使用，不能强制串联。

## 普通操作的固定调用路径

这是使用流程，不是开发排查。已有确认的解释器即可调用命令，不为执行一个操作重新研究核心源码、读取开发记忆或会话记录。只有命令返回异常且确实需要另立维护任务时，才进入代码排查。

删除、单独渲染和结构查询优先使用以下固定入口。`PYTHON` 表示当前已经确认的现有 Python 可执行文件，不是新环境或新依赖。全局选项必须位于动作前；用户指定目标时在动作前加 `--target <最终JSON路径>`，未指定时项目入口自动使用已有最终报告配置。

```text
PYTHON -B -m Bdd.final_report --json delete --feature-name "calc" --scenario "计算相加"
PYTHON -B -m Bdd.final_report --json --preview delete --feature-name "calc" --scenario "计算相加"
PYTHON -B -m Bdd.final_report --json render
PYTHON -B -m Bdd.final_report --json inspect
```

- 目标和范围已在用户请求中明确时，直接执行相应固定命令，不先单独读取配置、列报告或额外预览。命令内部完成范围检查、内存预演、写入前核对及结果回读。
- `status=completed` 且 `verified=true` 就简短返回实际范围和输出路径，不再运行另一段 JSON/HTML/附件哈希验证脚本，不再解释一次流程。
- `status=blocked` 时没有执行目标写入。范围不匹配会返回候选 `candidates` 和 `target_sha256`；只就真实歧义询问用户，不自行换到相似对象。确认后，原命令在动作前增加 `--expected-target-sha256 <返回的指纹>` 并改成已确认范围，直接重试，不重新设计脚本。
- `status=preview` 只说明预演结果，不继续提交；`status=inspected` 返回紧凑的身份和状态摘要，不返回 Steps、Base64 或附件内容。只有明确要求检查结构或分析时才单独调用 inspect。
- `status=failed` 可能已发生部分写入，按返回的 `stage`、`report_changed`、`html_exists` 和 `log_exists` 如实说明，不自动重试或回滚。
- JSON 快速入口仅支持 inspect、delete 和 render；创建、合并仍使用下文既有函数或普通 CLI，不把不支持的动作反复送入快速入口。不增设 AI 合并器，不修改原函数或普通命令的默认语义。

## 先确定用户要做什么

| 用户表达 | 执行动作 | 不得顺带执行 |
| --- | --- | --- |
| 生成最终报告；用这份报告创建最终报告 | 核心 `create`，返回 JSON、HTML 和日志位置 | 不把创建误作合并；不覆盖未明确允许替换的现有最终报告 |
| 把本次报告合到最终报告 | 回填源报告包含的执行结果，保留目标中其他结果 | 不重新创建最终报告，不只挑 passed 结果 |
| 只合并这个场景或 Example | 定位唯一 Feature 和 Scenario/Example，局部回填 | 不扩大为整个 Feature 或 Outline 替换 |
| 整组替换这个 Feature/Outline | 仅对明确指定的整组使用原整体替换语义 | 不从报告文件名或场景数量推断整体替换授权 |
| 从最终报告删除指定 Feature、Scenario、Outline 或 Example | 精确定位目标并预演，调用核心 `delete` 更新结果、HTML 和操作日志 | 不删除报告文件、原始运行报告、截图、录屏或 assets |
| HTML 丢失；重新生成 HTML；刷新最终报告页面 | 核心 `render`，从现有最终 JSON 重新渲染 HTML | 不创建或合并报告，不改 JSON、日志和附件 |
| 先看看会改什么；预览合并 | 只在内存预演并返回变更范围 | 不复制附件，不写 JSON、HTML、日志或临时报告 |
| 先看看删除会影响什么 | 只调用 `delete_result` 做内存预演，列出拟移除和保留的结果 | 不执行实际删除或写文件 |
| 分析失败原因；总结报告 | 只读报告和所需错误证据，返回分析 | 不合并，不改测试状态，不自动复跑 |
| 合并后分析 | 合并成功后分析实际结果 | 不用 AI 分析内容改写正式报告数据 |

只有动作、目标或冲突确实无法确定时提一个具体问题。路径、对象身份和文件状态可以查明的，先自行检查；用户明确要求合并、删除指定结果或渲染且范围清楚时，不重复索要确认。只说“删掉报告”无法确定是删除某条结果还是删除文件，必须询问，不自行选择。

## 报告路径：指定优先，已有默认其次

1. 用户明确给出的源 JSON 和最终 JSON 路径优先；指定 HTML 时可从对应 JSON 使用，不从 HTML 猜造测试结果。
2. 删除、渲染或结构查询未指定目标时，固定入口直接使用项目已有配置，不需要模型读脚本。创建或合并未指定路径时，读取 [Bdd/final_report.py](../../Bdd/final_report.py) 的 `DEFAULT_INPUT_REPORT_JSON`、`DEFAULT_FINAL_REPORT_JSON`，不修改这份项目脚本来设置本次参数。
3. 如果宿主缺少项目入口，使用核心的 `DEFAULT_SOURCE_REPORT_TEXT`、`DEFAULT_FINAL_REPORT_TEXT`。相对路径以现有项目根解析，不按任意终端目录猜测。
4. 不根据文件修改时间搜索“最新”报告。“本次报告”默认指项目配置的运行报告，不扫描全库挑文件。用户指定其他文件就使用该文件。
5. 源文件必须可读且为包含 `features` 列表的报告 JSON。创建或合并时，源、目标 JSON 必须不同，输出 HTML/日志不得覆盖输入或其他格式的产物。只分析一份报告时只读取该报告，不要求已有最终报告。
6. 创建时已有目标但用户未明确要求覆盖，询问覆盖还是另存。合并时最终报告不存在，说明缺少已有最终报告并询问是否创建，不能自动把 merge 改成 create。
7. 删除结果和单独渲染只需要已有最终 JSON，不检查本次运行报告是否存在。目标 JSON 缺失或无效时说明实际问题，不用 create 补造。渲染 HTML 的输出路径不得等于最终 JSON、日志或附件路径。

## 核心调用与合并范围

唯一实现是 [autowork_core/runtime/reporting/final_report.py](../../autowork_core/runtime/reporting/final_report.py)。
复用 `create`、`merge`、`preview_merge_report`、`merge_report`、`merge_report_file`、`delete_result`、`delete`、`render`、`final_report_paths`；无需新建 Python 包装、改配置或改现有函数默认值。
不要无参数运行项目脚本，因为它默认创建报告。所有调用都传本次已经确定的路径和范围。

- 日常回填使用 `preserve_unmentioned=True`，默认 `allow_add=False`。源中 A、B 更新后，目标 C 保留；Outline 逐 Example 回填，未复跑的行保留。
- 核心会在此模式跳过 `skipScope` 标记的未执行占位记录。失败结果仍照实更新，不把 passed 优先当作合并规则。
- 用户明确要求追加目标没有的对象时，才允许 `allow_add=True`。未匹配到目标时，不能为让调用通过而自动启用追加。
- 明确替换整个 Feature：用 `merge_report(source_data, target_data, feature_file=..., allow_add=False)` 预览，用 `merge_report_file` 传相同范围实际提交。不要调用会按源场景数量自动缩小范围的 `merge`，否则只有一个场景的源报告不会真正整组替换。
- 明确替换整个 Outline：明确 `scenario_name`，使用 `preserve_unmentioned=False`。普通回填或 Outline 替换的 preview 和 merge 必须传同一组 Feature/Scenario/Example、追加和保留参数。
- 多个 Feature 的本次结果可按回填模式处理；指定场景名在多个 Feature 中出现时，先根据路径和上下文唯一定位，否则列出候选询问。不模糊匹配后直接写入。
- 多份来源覆盖同一对象时，需要明确先后顺序或选择哪份，不能按时间或 AI 猜测决定。第一版默认一次处理一份源报告，不隐含保证多份报告整体原子提交。

## 删除指定结果

正常调用按上面的 JSON 命令执行以下检查；这些步骤由核心执行，不要求模型逐步读文件、拼脚本或多次发出工具调用。

1. 根据用户指定路径或现有默认值读取最终 JSON，定位唯一 Feature、Scenario/Outline、Example。用户说的是某个场景，不得因为未传场景参数而扩大为整个 Feature；删除一行 Example 必须包含其 Outline 和 exampleId。
2. 只在用户明确要求删除整个 Feature 或 Outline 时使用对应整组范围。重复名称、范围缺失或“清理失败项”等不明确请求先列候选询问，不自动删除所有 failed 结果。
3. 用 `delete_result(target_data, feature_file=..., scenario_name=..., example_id=...)` 做纯内存预演。说明移除项及保留项；删除最后一个 Example/Scenario 时，核心会同时移除空 Outline/Feature，必须把这项连带影响说明白。
4. 仅预览时到此停止。明确要求实际删除时，确认最终 JSON 未在预演后变化，调用 `delete(final_report_json=..., feature_file=..., scenario_name=..., example_id=...)`，参数与预演一致。
5. 回读核对实际结果与预演一致、其他结果保留、HTML 和删除日志已更新。原始运行报告及所有附件文件保留，不借删除结果执行物理文件清理。

## 单独重新生成 HTML

普通请求直接使用上面的固定 JSON render 命令，并信任其已核验的结果，不额外读取或重写报告。

只读取用户指定或默认的最终 JSON，使用 `render(final_report_json=..., html_report=...)`。
已有 JSON 是权威来源；HTML 可以按本次明确请求在对应输出位置重新生成，但不得反向修改 JSON 来修页面。
不调用 create、merge 或 delete，不生成新的合并日志，不复制或清理附件；无需运行报告、范围选择或 AI 分析。
返回 HTML 路径，说明最终 JSON 未修改。若附件已经缺失，单独说明，不能宣称渲染修复了媒体。

## 预览、执行和交付

1. 读取并核对报告，确定路径、范围、追加及保留模式。分析任务不进入合并；预览任务只到第 2 步。
2. 普通回填调用 `preview_merge_report(source_data, target_data, ..., allow_add=False, preserve_unmentioned=True)`；明确整 Feature 替换按上节使用 `merge_report`。均为纯内存操作，不能调用文件写入或附件快照函数来预览。展示拟更新的对象、状态变化，以及明确整组替换时会移除的对象。
3. 执行前确认本次源与目标仍是预览时的内容；变化时重新读取和预览，不提交过期判断。调用所选核心写入函数时使用相同范围和模式，源文件本身不修改。
4. 核对最终 JSON 的范围与状态是否符合预览、未涉及结果是否保留、HTML 和合并日志是否生成。核对所涉及附件可用性，不把缺失的截图或录屏说成已完整归档。
5. 返回实际动作、更新范围、保留结果和可点击的输出路径。只要求合并时不强制给 AI 长篇分析；明确要求分析时，再基于报告的错误证据给出结论及不确定项。

原正式数据只反映测试结果。AI 不修改 passed/failed、Steps、附件、耗时或断言来改善报告；不把“疑似原因”写成确定根因。
纯预览或分析不生成文件。完整报告很大时先取结构、范围和错误摘要，不把全部 Base64 或附件原始内容读进上下文。

## 失败与环境边界

使用当前任务已确认或工作区已选择的现有 Python 可执行文件；不确定就询问现有解释器路径。
不调用环境安装/配置工具，不安装 Python、uv、依赖或扩展，不创建环境，不改 PATH 或解释器选择。

校验、范围或缺文件问题在实际写入前说明。现有 JSON、HTML、日志不是整体事务：异常时复核已存在文件，说明完成了哪一步、哪一步失败，不声称全部没改或已自动回滚。
不在执行失败时自动重建最终报告、重跑测试或删除附件。删除只接入报告内的指定结果；自动复跑、上传、报告文件删除和媒体清理不属于本 Skill，原有这些能力的归属与入口不受影响。