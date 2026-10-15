# 修复会话执行取消、子会话与历史生命周期的一致性

后台事件入库失败会让停止和释放长期卡住；前端对执行结果、子会话对象、活动历史以及归档/关闭状态的展示和可操作性又不一致。统一以持久化会话与 Turn 状态驱动操作和历史入口，避免用户无法结束任务、找回会话或判断下一步。

## 环境与证据原则

发现于 staging.sbx-agent.com，构建 `018a0e02370bcae3c3f17ec97aa91b105d232f8b`（2026-10-15）。这是包含多个验收子项的一个 Issue。组内标明 staging 实测、本地隔离复现或源码风险；后两者先补本地集成验证，再决定最终修复方式。原测试计算已释放，不能假设旧 session 仍可写。

## 接手范围与前置数据

control/application/ingest.py、execution.py、sessions.py、projections.py 与 persistence/queries.py；console/src/features/sessions/ConversationPane.tsx、worklog.ts、tabs/ChildrenTab.tsx、tabs/ActivityTab.tsx、SessionsPage.tsx；console/src/state/session-live.ts；console/src/shell/AppShell.tsx。

用隔离测试账号/工作区准备至少 51 条本地 session fixture，包含 open、archived、closed；准备含 ready ChangeSet 的父会话。使用 fake provider 或隔离 runtime 输出 PostgreSQL 不可表示字符，并准备运行中任务、排队追问和 test 子会话。原 staging 故障会话已关闭，不能用其已清理状态冒充重新复现。

## 串联复现

1. 先本地重放带异常字符的真实协议事件，验证入库错误不会使 watermark、cancel 和 release 永久互锁；检查失败原因和终结证据。
2. 运行任务时排队追问，取消前一个任务，再执行后续任务；检查每个 Turn 只有一份结果；在 Children 明确选择 No ChangeSet。
3. 完成并刷新会话，验证 Activity 历史和新事件按 seq 合并；浏览/搜索第 51 条及更早的会话。
4. 按 open → archived → open → closed 操作，验证筛选、页头和可执行 actions；关闭前展示具体后果，关闭后不再提供必然失败的 Retry。

## 修复顺序与边界

先解除异常事件对取消/释放的阻塞（AUD01），再以一致的状态和生命周期 eligibility 修复分组、历史和动作（AUD07/AUD10/AUD12/AUD13/AUD14/AUD18/AUD22）。最后重跑子会话完成及结果回传的原先受阻链路。

不修 G01 的文件、终端输入与 diff，也不修改 API key 权限。与 G01 共用 ConversationPane 时，本组负责 Turn 组织和 actions；Review changes 的 URL 由 G01 负责。长列表用本地 fixture，避免在真实账号批量制造 51 个云沙箱。

## 验收清单

- [ ] AUD01：异常输出得到明确的安全持久化表示或受控失败；取消与释放在有界时间内收敛，终结事件和 watermark 不被毒事件无限阻挡，UI 不泄露 turn.cancelling 原始键名。
- [ ] AUD07：排队、备注交错、取消、重试后，每个 Turn 的状态和结果仅出现一次，刷新后顺序一致。
- [ ] AUD10：明确选择 No ChangeSet 后保持为空；默认初始化、异步加载和 role 切换不覆盖用户意图。
- [ ] AUD12：刷新/重新进入后能读取有界历史 Events，并与实时流去重，可分页加载更早记录。
- [ ] AUD13：默认 Recent 排除 archived/closed；开启相应筛选才展示，归档页头明确显示 Archived。
- [ ] AUD14：第 51 条及以后的会话可分页访问和搜索，说明搜索范围；排序更新不会重复或跳过记录。
- [ ] AUD18：Close 之前明确目标及不可继续/释放后果，具有确认或定义清晰的恢复机制，并与可恢复 Archive 区分。
- [ ] AUD22：closed 会话的服务端 actions 不再包含 Retry，前端同时兜底只读；实际 API 仍拒绝新执行。

## 验证与完成条件

为毒事件、cancel/release 收敛、队列分组、历史分页及 lifecycle/action 矩阵增加回归。执行 make lint、make test、make console-check；用 make console-e2e 覆盖 lifecycle 与历史入口。完整 runtime/PostgreSQL 链路在隔离 Linux CI 中验证；此前 macOS 的 Linux prctl 失败不能作为产品回归结论。

每个子项均记录：复现结论、修复提交、自动化测试及复测结果（含构建 SHA）。尚未实际覆盖的场景不可标为通过。全部验收满足且无遗留计算/临时有效凭证后再关闭本 Issue；无需为组内每个 AUD 再建子 Issue。先阅读仓库 AGENTS.md，测试隔离 HOME/XDG 与云凭证。

## 附件映射

上传附件时只需选取本组对应的 AUD 目录。JPG 是实际截图，MP4 是固定每帧 3 秒的关键帧回放，不是连续录屏；缺少视频不代表该风险已经端到端验证。源码风险或辅助截图的限制见每项证据说明。

- AUD01：approved-cleanup-result.txt、final-resource-state.txt、recovery-only.jpg、server-child-state.txt、server-resource-state.txt、server-unicode-error.txt
- AUD07：prior-duplicate-completed.jpg、step-01.jpg
- AUD10：aud10-keyframes.mp4、step-01.jpg、step-02.jpg
- AUD12：aud12-keyframes.mp4、step-01.jpg、step-02.jpg
- AUD13：step-01.jpg
- AUD14：无 UI 故障附件，见下方源码/隔离复现记录。
- AUD18：aud18-keyframes.mp4、step-01.jpg、step-02.jpg、step-03.jpg
- AUD22：aud22-keyframes.mp4、step-01.jpg、step-02.jpg

## 旧 Issue 迁入的剩余验收

### #198：启动进度和用户可见延迟

[PR #200](https://github.com/soren-labs/sbx-agent/pull/200) 已合并预热镜像、分阶段 provisioning 计时及 allocation keepalive；其证据显示 3 次 lease bind 为 5.6/5.9/6.9 秒、125 次 job attempt 无 claim_expired。当前源码保留 keepalive，2026-10-15 针对 `tests/unit/executors/test_modal_allocation.py` 重跑 16 项通过。这些证明不能替代启动体验的剩余验收；PR #200 本身明确未覆盖 UI 进度和 p50/p95。

- [ ] LEGACY198-A：将真实的 queued / image resolve / sandbox launch / runtime connect / executing 状态及时反馈到 Console，不能仅显示最终模型执行耗时，也不能伪造模型流。提交后及时反馈以原 issue 的 ≤1 秒目标为参考，记录实际测量。
- [ ] LEGACY198-B：对受控 warm/cold 条件的多次 fresh session 记录提交→首次可见模型内容、总完成时间和各 provisioning 阶段；报告样本量、p50/p95 与条件。原 20–30 秒为校准目标，不是未经测量即可宣布达到的保证；不使用无界云压力测试。
- [ ] LEGACY198-C：复测有效 allocation 超过 60 秒时保持 claim、崩溃后按 operation ID 接管且不重复分配；保存清理结果。旧 93 秒故障本轮未再次复现，不能声称仍能触发。

### #199 / #205：已交付功能的回归约束与使用量疑点

Codex、Grok Build、Antigravity 的基础接入来自已合并的 #200–#211，既有真实验收记录分别见 [#199 证据](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/docs/reports/issue-199/pr-05/README.md) 与 [#205 证据](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/docs/reports/issue-205/README.md)。本轮仅重新验证三种来源的单会话短任务成功，没有重新登录或重跑完整并发验收。修复本组不能退化已有的独立登录、catalog/effort、每 Slot 单 Worker 和释放约束。

- [ ] LEGACY205-USAGE：在隔离环境用首次执行及 native resume 的真实/脱敏协议样本确认 Antigravity `result.usage` 是逐 Turn 还是会话累计；若累计，统一转换或准确标注，不能在会话汇总中重复累加。本疑点来自 #205 已有证据，本轮未重新测量，不预先认定当前统计必然错误。
- [ ] 回归使用现有受控测试；若运行 `tests/e2e_staging/owner_slots.py --run`，使用已授权的测试凭证和空闲测试 Slot，保留其请求 allowlist 和清理保障。#220 只交付了此客户端，不能把其合并误报为真实五 Slot 验收通过。

## 各子项的现场、定位和修复建议

### AUD01 工具输出中的不兼容字符会卡住事件入库与取消

- 优先级：P1。证据：staging 浏览器、服务日志和只读数据库查询。
- 现场：测试子会话 `sess_01a13f6ac90a7d16be69a9a366d7178b`。执行过程中，`execution.reconcile` 在更新 `message_parts` 时抛出 `psycopg.errors.UntranslatableCharacter: unsupported Unicode escape sequence`。一个 reconcile Job 已 failed，尝试计数为 211；停止后新 Job 仍反复报 `internal_error`。
- 实际行为：点击 Stop 后 Turn 保持 `cancelling`，无结束时间；Release compute 对应 Job 持续排队，lease 仍为 ready。页面仍显示 Working，消息区域显示原始键名 `turn.cancelling`，用户无法通过正常流程结束任务并回收资源。
- 原因定位：[ingest.py](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/ingest.py#L199) 将工具 input/output/error 直接写入 JSONB；[execution.py](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/execution.py#L982) 在同一批事件入库成功前无法推进；[资源释放](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/execution.py#L1149) 等待活跃 execution 消失。
- 修复建议：在统一持久化边界对 PostgreSQL 不可表示的字符进行明确、确定性的编码或替换，尤其是 NUL；对原始事件保留可审计的安全表示。错误事件不能无限阻挡 terminal/cancel 证据，应有明确的失败状态及可恢复路径。UI 展示后台失败原因与取消进度。
- 回归建议：工具输出含 NUL、控制字符和异常 Unicode；确认入库、watermark、重放、cancel 和 release 都能收敛，不重复执行已启动命令。
- 证据：服务端堆栈、Turn 与 Job 状态、释放等待状态。具体导致数据库拒绝的字符尚需进一步定位；当前未把它断言为已经确认的单一字节。
- 清理：经用户明确批准，仅终止本次故障测试沙箱；Modal 返回 termination_confirmed 为 true、observed_status 为 terminated。未手动改写数据库状态。后台随后将 Turn 标记为 cancelled、lease 标记为 lost，executor.release Job succeeded（146 次尝试）；保留了先前 failed 的 reconcile 证据。清理结果、最终资源状态。
- 清理后界面：


### AUD07 排队追问使同一 Turn 重复显示结果卡片

- 优先级：P2。证据：staging 实际复现。
- 复现：启动一个任务 → 在其执行期间发送排队追问 → 停止前一个任务 → 等待队列任务成功。
- 实际行为：同一个取消结果和 Retry 出现两次；队列任务也有一张空 Completed 和一张包含实际回答的 Completed。操作历史因此难以对应到用户请求。
- 原因定位：[blocksOf](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/worklog.ts#L292) 只合并相邻且 turn_id 一致的消息。排队用户消息与先前 Turn 的 assistant 消息交错，导致同一 Turn 分成多个块，每个块又渲染一遍 TurnStatus。
- 修复建议：稳定地按 Turn 身份组织执行结果，每个 Turn 只渲染一份结束状态；保留排队消息的接收时间及真正执行顺序，避免重排造成语义混乱。
- 回归建议：执行期间连续排队、备注穿插、取消后自动运行队列、重试与刷新后状态一致。


### AUD10 子会话无法选择 No ChangeSet

- 优先级：P2。证据：staging 实际复现及源码。
- 复现：父会话存在 ready ChangeSet → Children → ChangeSet 选择 No ChangeSet。
- 实际行为：控件立即重新选中第一个 ready ChangeSet。
- 原因定位：[ChildrenTab.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/ChildrenTab.tsx#L89) 用 `changesetId || ready[0]?.id || ""`，把明确选择的空值当成未初始化。
- 修复建议：用 null 表示尚未选择，用空字符串表示明确无对象；仅在初始化时应用默认 ChangeSet。
- 回归建议：research 无对象、手动清空、异步加载对象、切换 Role 后保留明确选择。

### AUD12 Activity 刷新后无法查看已经发生的事件

- 优先级：P2。证据：staging 实际复现及源码。
- 复现：已成功运行的会话，整页刷新后打开 Activity。
- 实际行为：Turn 仍在，Events 却显示 No events yet。页面实际上只包含当前浏览器订阅之后的新事件，容易使人误判历史证据丢失。
- 原因定位：[snapshot](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/state/session-live.ts#L142) 只装载 messages/turns/executor，并把 watermark 设为当前值；[ActivityTab](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/ActivityTab.tsx#L35) 仅使用内存 state.events。
- 修复建议：Activity 首次打开时请求有界的历史事件页，并按 seq 与后续流合并；提供加载更早记录和过滤，避免重放全量流进入所有面板。
- 回归建议：已有历史但没有新事件的会话、刷新、重新登录、事件分页、流与历史页的去重。

### AUD13 会话列表的归档与关闭筛选无实际隐藏效果

- 优先级：P2。证据：staging 实际复现及源码。
- 复现：归档或关闭本轮测试会话，保持默认未选 Show archived/Show closed。
- 实际行为：这些会话依然出现在 Recent 列表。Archive 后会话页头显示 Idle，直到用户留意正文横幅才知道已归档。
- 原因定位：[AppShell.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/shell/AppShell.tsx#L100) 基础查询不传 lifecycle；[sessions.page](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/persistence/queries.py#L20) 将 null 解释为所有状态。后续额外查询再合并没有实现排除。
- 修复建议：默认显式请求 open；启用相应选项时再并入其他生命周期，或统一通过服务端多状态过滤。页头明确展示 Archived，而不是仅显示 activity 的 Idle。
- 回归建议：默认、仅归档、仅关闭、二者同时显示；归档后立即更新；清晰区分 lifecycle 与 activity。

### AUD14 用户无法访问第 51 个以后的会话

- 优先级：P2。证据：源码确认，未创建大量测试会话。
- 原因定位：[SessionsPage.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/SessionsPage.tsx#L55) 固定 limit 50 且不消费 next_cursor；侧栏也把结果 slice 为 50，搜索只在该局部列表内完成。后端已有 cursor 分页。
- 影响：会话增加后，旧会话从产品入口和搜索中消失，用户必须记住直接 URL。
- 修复建议：主列表增加 cursor 分页或加载更多；全局搜索使用服务端搜索，或明确标示“仅筛选最近会话”并提供完整历史入口。
- 回归建议：至少 51 条记录、不同生命周期混合、更新排序、搜索第 51 条记录、分页去重。

### AUD18 关闭会话没有不可恢复提示

- 优先级：P2，交互设计。证据：本轮测试会话实际关闭。
- 实际行为：Session actions 中点击 Close session 立即执行，菜单没有解释其与 Archive 的区别，也没有给出撤回入口；关闭后会话永久不再接受消息。
- 修复建议：保留 Archive 为可恢复默认操作；Close 明确说明停止任务、释放计算及不能继续的后果，并提供有对象名称的确认，或提供产品定义的恢复机制。
- 回归建议：键盘误触保护、运行中关闭的取消状态、关闭后的只读状态、与 Archive 的区别。

### AUD22 关闭后的会话仍显示 Retry，但执行必然失败

- 优先级：P2。证据：staging 实际复现。
- 步骤：通过 API 取消 Turn，关闭 session，打开 Console；历史取消卡片仍有 Retry。点击后返回 `invalid_transition: session is closed; new execution requests are rejected`，request ID 为 `req_108e92cdc4f64031`。
- 定位：[projections.py](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/projections.py#L47) 只依据 Turn 状态生成 actions，没有考虑 session.lifecycle；[ConversationPane.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/ConversationPane.tsx#L84) 据此直接渲染 Retry。
- 建议：由服务端结合 session 生命周期计算允许的 actions；关闭后清除 Retry，UI 同时用只读状态兜底。若产品支持 continuation，可提供明确的新建关联会话入口。
- 回归：取消/失败 Turn 在 open、archived、closed 状态下的按钮和 API eligibility 保持一致。
- 证据：关闭后 Retry 的反馈截图。
