对，我刚才那种“优化点列表”还不够合格。稳定方案必须先回答三件事：

```text
会不会影响现有功能？
副作用是什么？
靠什么输入证明这个方向不是猜？
```

我刚才重新看了代码和测试，结论是：**现在不能直接上任何会改变 F10 语义的优化；第一刀只能做非行为观测，然后按数据决定 owner-local 小改。**

**已经确认的权威事实**
当前产品真实链路是同步闭环，不是 D4 异步链路：

```text
F10 / finish_step
-> runtime.stop()
-> window evidence after
-> write_take
-> TimelineStore.initialize(actions)
-> materialize()
-> ProjectionStore.publish()
-> publish_compilation_from_projection()
-> evidence-compilation.json
-> validate_ai_bundle / Request / Brief / Job
```

依据：

- `session.py:1124`：`_close_active()` 里 F10 后同步 `runtime.stop()`、写 Take、`TimelineStore(...).initialize(actions)`。
- `timeline.py:537`：`materialize()` 同步构建 effective actions/events/locators/media/evidence graph/semantic pack。
- `timeline.py:756`：随后同步 `publish_compilation_from_projection()`。
- `bundle_validator.py:268`：生成准入依赖 `evidence_compiled` 和 `evidence_generation_allowed`。

所以能保证不影响功能的前提是：**不改这条链的产品语义**。

**不能做的“优化”**
这些不是稳定优化，是重构或安全退化：

```text
不能把 F10 改成只保存 raw facts 就返回
不能让 evidence-compilation 后台接管主路径
不能把 window evidence 超时降级放行
不能恢复默认 tree process timeout/spawn
不能把耗时观测写进 evidence identity
不能降低 tree depth/nodes 默认值来赌不会影响 locator
```

依据很具体：

- `test_lifecycle.py:1050` 明确要求 window evidence 迟提交时拒绝 Take，防止 late artifact。
- `test_lifecycle.py:2326` 明确要求 finish 后已有 complete projection 和 terminal compilation。
- `evidence_compilation.py:148` 的 input fingerprint 包含 `take.json`、`current-projection.json`、`evidence-acquisition.jsonl`、`ui/`、`windows/`、截图和 frames；所以把 timing 写进这些地方会污染证据身份。
- `test_lifecycle.py:932` 已经锁定不再启动 background discovery。
- `test_lifecycle.py:1394` 已经证明 event target watchdog 失败后，本 Take 后续 target capture 会 `skipped_by_health`，这类源头降风险已经存在。

**稳定优化方案**
第一阶段只做一个东西：**F10 同步链路非权威耗时诊断**。

目标不是“加日志好看”，而是拿到足够输入，避免继续猜：

```text
runtime.stop 总耗时
hook stop
raw journal seal
frame stop
worker stop
window evidence stop
after-window tree/screenshot
write_take
timeline materialize
evidence compilation
validate_ai_bundle
```

副作用：

```text
会多一个诊断输出。
会有极小 perf_counter 开销。
如果写错位置，会污染 evidence fingerprint 或导致旧 Request stale。
如果 UI 直接消费它，会把维护诊断变成用户产品状态。
```

控制方式：

```text
只写非权威 diagnostics，不写 take.json。
不写 evidence-acquisition.jsonl。
不写 ui/、windows/、screenshots/、frames/。
不改 current-projection.json。
不改 evidence-compilation.json。
不进入 readiness / Request / Brief / Job。
普通 UI 不展示；只给维护排障用。
```

这就是为什么第一刀可以保证不影响功能：它不参与产品决策链。

第二阶段才按观测结果做 owner-local 优化：

```text
如果慢在 window evidence：
只看 InputCaptureEngine / FeatureRecordingSession 的业务窗口证据队列。
不改 Request/Brief/Job。
不放宽 late commit fail-closed。
只减少非必要进入队列或非必要截图/tree。

如果慢在 tree capture：
只看 session._capture_tree / tree_snapshot。
不改默认 spawn timeout。
不全局降低 depth/nodes。
只对已证明慢的窗口类型或阶段做范围收窄，并用 locator 回归证明不丢证据。

如果慢在 materialize/compiler：
只看 TimelineStore / EvidenceCompiler。
不改 F10 语义。
优先避免重复 materialize 或重复读大文件；ProjectionStore 已有同 source revision 复用能力：[projection_store.py](autowork_core/utils/debug_tools/recorder/projection_store.py#L93)。
```

**证明方式**
第一阶段完成后必须证明这些不变：

```text
raw-events.jsonl 不变
capture-completion.json status 仍 complete
current-projection.json 字节不变或 fingerprint 不变
evidence-compilation.json 字节不变或 result_fingerprint 不变
validate_ai_bundle 结果不变
F10 成功语义不变
```

对应测试应覆盖：

```text
finish 后 complete projection / terminal compilation 仍成立
shadow/report 不改权威文件
window evidence late commit 仍 fail-closed
event target timeout 仍保存 Take 且进入 weak target review
no background discovery 仍成立
```

所以更精确的回答是：**现在不能先“优化实现”，要先加不参与证据身份的 F10 timing diagnostics，拿真实慢点作为输入；然后只在对应 owner 内做小改。没有 timing 输入就直接调 tree/window/compiler，就是猜。**

这不是兜底，也不是 D4。它是稳定优化的前置证明层：先证明问题在哪，再动最小 owner。