# 修复工作区文件编辑、终端重试与变更审阅链路

用户在工作区创建或编辑文件、运行终端命令、查看变化和进入审阅时，会遇到新建入口失效、截断内容可覆盖原文件、命令可能因重试执行多次，以及终端和变更视图丢失状态。将这一条工作区操作链作为一个修复单元。

## 环境与证据原则

发现于 staging.sbx-agent.com，构建 `018a0e02370bcae3c3f17ec97aa91b105d232f8b`（2026-10-15）。这是包含多个验收子项的一个 Issue。组内标明 staging 实测、本地隔离复现或源码风险；后两者先补本地集成验证，再决定最终修复方式。原测试计算已释放，不能假设旧 session 仍可写。

## 接手范围与前置数据

console/src/features/sessions/WorkspacePanel.tsx、tabs/FilesTab.tsx、tabs/TerminalTab.tsx、tabs/ChangesTab.tsx、ConversationPane.tsx 中的审阅链接；console/src/state/session-events.ts；control/api/live.py、control/application/live.py；runtime/daemon/worktree.py。

新建无业务数据的隔离 session，准备已有小文件、新路径、超过 1,000,000 字节且带尾部标记的文件。用测试终端创建一份 marker；准备新增、修改、删除及多 hunk 的 ChangeSet。桌面使用 1440×900，并检查窄屏入口。故障重试与大文件覆盖先在本地隔离环境执行。

## 串联复现

1. 先用包含尾部标记的临时大文件验证 read.truncated → write 的数据完整性保护，并完成不存在路径的创建流程。
2. 打开 Changes 形成缓存，切到 Terminal 执行计数器递增和创建 marker；离开终端后返回，检查输出、连接和文件变化。
3. 在本地代理/测试 transport 中模拟命令已执行但响应丢失，以同一 Idempotency-Key 重试；检查最终计数为 1，并测试同 key 不同 payload 的冲突。
4. 捕获 ChangeSet，查看新增/删除/重命名的状态及 diff 行号；分别从 overview、files、terminal、changes 点击 Review changes。

## 修复顺序与边界

先实现服务端文件完整性和终端幂等保护（AUD02/AUD16），再修复新建、连接保存、查询失效和审阅 UI（AUD04/AUD09/AUD05/AUD08/AUD11/AUD19）。

不改项目发布模型、会话列表生命周期策略或 API key 签发。ConversationPane 仅改 Review changes 路由；不要同时重写 G02 的 Turn 分组与 action eligibility。终端幂等在 live 路由贯穿实现，不通过全局禁止所有 mutation 重试来绕过。

## 验收清单

- [ ] AUD02：截断/脱敏视图不能静默整文件覆盖；保存失败时原文件全部字节与尾标记不变；完整小文件仍可正常编辑。
- [ ] AUD04：不存在路径可输入并保存，使用 absent 前置条件；并发已创建返回可理解冲突，其他错误不伪装成 missing。
- [ ] AUD05：agent 或终端创建/删除文件后，Live changes 不需整页刷新即可正确更新，并有明确刷新动作。
- [ ] AUD08：终端正确处理颜色、回车、宽字符及跨 chunk 控制序列，不显示原始 ANSI 噪声。
- [ ] AUD09：同一 session 切换面板不丢失仍存活的终端入口；重连与关闭语义明确，不静默新增终端。
- [ ] AUD11：所有 session 子路由的 Review changes 都进入同一合法 changes 路由，无嵌套 404。
- [ ] AUD16：同一输入命令在响应丢失/503 后只执行一次；同 key 不同内容冲突；无法保证幂等时不能静默重发。
- [ ] AUD19：A/M/D 与实际 patch 一致，文件头不占代码行号，多 hunk 和无尾换行标记正确。

## 验证与完成条件

补充实际覆盖完整性、重试副作用计数、状态保留和路由矩阵的测试。后端修改执行 make lint 与 make test；前端执行 make console-check，工作区交互纳入 make console-e2e。runtime 相关测试在 Linux 环境执行。

每个子项均记录：复现结论、修复提交、自动化测试及复测结果（含构建 SHA）。尚未实际覆盖的场景不可标为通过。全部验收满足且无遗留计算/临时有效凭证后再关闭本 Issue；无需为组内每个 AUD 再建子 Issue。先阅读仓库 AGENTS.md，测试隔离 HOME/XDG 与云凭证。

## 附件映射

上传附件时只需选取本组对应的 AUD 目录。JPG 是实际截图，MP4 是固定每帧 3 秒的关键帧回放，不是连续录屏；缺少视频不代表该风险已经端到端验证。源码风险或辅助截图的限制见每项证据说明。

- AUD02：无 UI 故障附件，见下方源码/隔离复现记录。
- AUD04：aud04-keyframes.mp4、step-01.jpg、step-02.jpg
- AUD05：aud05-keyframes.mp4、prior-after-reload.jpg、prior-before-reload.jpg、step-01.jpg、step-02.jpg、step-03.jpg
- AUD08：aud08-keyframes.mp4、step-01.jpg、step-02.jpg、step-03.jpg
- AUD09：aud09-keyframes.mp4、step-01.jpg、step-02.jpg、step-03.jpg
- AUD11：aud11-keyframes.mp4、step-01.jpg、step-02.jpg
- AUD16：无 UI 故障附件，见下方源码/隔离复现记录。
- AUD19：aud19-keyframes.mp4、step-01.jpg、step-02.jpg

## 各子项的现场、定位和修复建议

### AUD02 保存被截断的文件会静默丢失尾部

- 优先级：P1。证据：源码及本地临时文件隔离复现；未在用户实际文件上操作。
- 复现：生成 1,000,019 字节 UTF-8 文本，尾部为独立标记；读取后将返回内容和返回 digest 原样写回。
- 实际结果：读取返回 `truncated=true`、1,000,000 字节，写入成功；原文件缩短为 1,000,000 字节，尾部标记丢失。
- 原因定位：[worktree.py](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/runtime/daemon/worktree.py#L172) 截断内容但返回原文件完整 digest；[FilesTab.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/FilesTab.tsx#L170) 不检查 `truncated`，可直接保存截断后的内容。版本检查因此无法阻止这类覆盖。
- 修复建议：截断或脱敏后的视图默认只读，并明确解释原因；若需要编辑大文件，应先获取完整内容或使用范围补丁，并在服务端验证编辑基于完整内容，不能只依赖原文件 digest。
- 回归建议：超过读取上限的文件、恰好处于上限的文件、含多字节字符的边界、脱敏视图；保存前后验证完整字节及尾部不丢失。

### AUD04 新建文件入口无法完成新建

- 优先级：P1。证据：staging 实际复现。
- 复现：活跃会话 → Files → Open or create file 输入不存在的 `ux-audit-new.txt` → Open。
- 实际行为：页面同时显示“File does not exist yet; saving creates it”和 Not found，但没有文本输入区、Edit 或 Save；用户无法完成该功能。请求标识 `req_6a0e10bd0e2e4917`。
- 原因定位：[FilesTab.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/FilesTab.tsx#L168) 的 editing 初始为 false，而 Edit 按钮仅在 file 已存在时渲染。另一个问题是 `Boolean(file.error)` 把所有错误都解释成 missing。
- 修复建议：仅把明确的 not_found 视为可新建，直接进入空白编辑器；权限、连接或网络错误应显示各自恢复动作。按预期使用 `expected_digest: absent` 防止覆盖并发创建的文件。
- 回归建议：新文件创建成功；嵌套目录；已存在文件；权限错误；并发创建冲突。


### AUD05 Live changes 使用旧缓存导致改动漏显示

- 优先级：P2。证据：staging 实际复现，已对照整页刷新。
- 复现：打开 Changes 形成初始缓存 → Terminal 执行 `touch ux-audit-terminal.txt` → 回到 Live changes。
- 实际行为：列表只显示之前的 `ux-audit-existing.txt`；整页刷新后才显示两个文件。此前 projectless 的冒烟会话中也出现新文件已存在、Live changes 却显示 No uncommitted changes 的现象。
- 原因定位：[WorkspacePanel.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/WorkspacePanel.tsx#L50) 预先请求 live changes；[ChangesTab.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/ChangesTab.tsx#L287) 无轮询或手动刷新；[事件失效规则](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/state/session-events.ts#L79) 未在 Turn 结束时刷新 changes。终端内直接产生的 Git 变化也没有对应失效事件。
- 修复建议：激活该面板时刷新；提供明确的 Refresh；Turn 结束及恢复连接后更新相关查询。活跃面板可低频轮询，避免“Live”显示静态缓存。
- 回归建议：agent 新增文件、Terminal 新增/删除文件、无 repository 的会话、在运行期间提前访问过 Changes 的情况。
- 澄清：源码的 Git observation 支持 untracked 文件；不能把本问题归因为后端不支持新文件。

### AUD08 终端直接显示 ANSI 控制字符

- 优先级：P2。证据：staging 实际复现。
- 复现：Start terminal，执行普通 `git status --short`。
- 实际行为：显示 `[?2004h`、`[31m`、`[m` 等终端转义序列，颜色、括号粘贴等控制指令成为可见噪声。
- 原因定位：[TerminalTab.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/TerminalTab.tsx#L99) 用普通 pre 直接渲染 PTY 字节解码后的文本。
- 修复建议：使用合适的终端渲染组件及键盘输入模型；若产品仅支持逐行命令，应明确按命令控制台呈现，并以流式解析器安全处理控制序列。
- 回归建议：颜色、回车覆盖行、进度条、宽字符、跨 chunk 的 ANSI 序列。

### AUD09 切换工作区标签会丢失终端连接入口

- 优先级：P2。证据：staging 实际复现及源码。
- 复现：连接 Terminal → 切到 Files → 返回 Terminal。
- 实际行为：只剩 Start terminal，之前的 terminal id、输出和连接状态消失。再次点击会创建另一个终端，原进程并没有相应的恢复或关闭入口。
- 原因定位：[WorkspacePanel.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/WorkspacePanel.tsx#L112) 条件挂载 TerminalTab；[TerminalTab.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/TerminalTab.tsx#L25) 只在组件局部状态中保存 terminal。
- 修复建议：至少在同一 session 的面板切换期间保留终端；提供终端列表与关闭按钮。无需承诺在沙箱重建后恢复原进程，但必须区分“进程仍在”和“连接视图重置”。
- 回归建议：切换标签、面板收起、路由切换、退出命令、沙箱释放后的明确状态。

### AUD11 桌面审阅链接会跳转到不存在的嵌套路由

- 优先级：P2。证据：staging 实际复现。
- 复现：1440px 桌面双栏，当前路由为 `/sessions/<id>/changes` 或 `/overview`，点击对话中完成状态旁的 Review changes。
- 实际行为：跳到 `/changes/changes` 或 `/overview/changes`，显示 Page not found；原会话工作台消失。
- 原因定位：[ConversationPane.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/ConversationPane.tsx#L148) 使用相对链接 `to="changes"`，而 Conversation 在桌面同时存在于带 tab 的路由下。
- 修复建议：生成基于 session id 的绝对路径，或通过统一的会话路由构造函数跳转。404 页增加返回当前会话的恢复入口。
- 回归建议：在 conversation、overview、changes、files、terminal 等每个工作台路由上点击 Review changes，结果都应为同一个合法 changes 路由。
- 证据：404 截图。

### AUD16 终端命令输入没有兑现 HTTP 重试的幂等保证

- 优先级：P1，需在故障注入测试中补验。证据：源码确认；本轮未向 staging 注入网络故障或重复有副作用的命令。
- 触发条件：terminal input 请求已到达服务端，但响应丢失或代理返回 502/503/504，客户端按既有策略自动重试。
- 原因定位：[Http.request](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/api/http.ts#L99) 用同一个 Idempotency-Key 重试 mutation；[terminal_input API](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/api/live.py#L55) 不传递该 key；[LiveWorkspace.terminal_input](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/live.py#L138) 每次生成新的 runtime operation id。终端创建同样每次生成新的 operation id。
- 影响：相同的一行命令可能执行多次。对于构建、迁移、提交等有副作用命令，结果可能与用户的一次输入不一致。
- 修复建议：把客户端请求 key 贯穿授权、业务去重和 runtime operation id；为同 key 不同 payload 返回冲突。对无法提供幂等保证的终端流输入，禁止静默自动重试，并明确显示传输结果未知。
- 回归建议：模拟已执行但响应丢失、网关 503、同 key 不同内容，验证命令执行次数与终端数量。

### AUD19 Diff 的行号和新增文件标记不准确

- 优先级：P3。证据：staging 实际 diff 与源码。
- 实际行为：明确为 new file mode 的新增 Python 文件在列表标成 M；diff 元信息如 `diff --git`、`index`、`---`、`+++` 被赋予 0、1 等伪代码行号，尾部空行也被当成上下文行。
- 原因定位：[fileKind 与 LETTER](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/ChangesTab.tsx#L251) 将 manifest 的 file 类型落入 changed；[DiffView](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/ChangesTab.tsx#L61) 在 hunk 外也递增代码行号。
- 修复建议：从 ChangeSet 的差异状态或解析后的 patch 得出 A/M/D；用统一 diff 解析器区分文件头、hunk、上下文、增删行和 no-newline 标记。
- 回归建议：新文件、删除、重命名、多 hunk、二进制文件、无尾换行，核对每个代码行号。
