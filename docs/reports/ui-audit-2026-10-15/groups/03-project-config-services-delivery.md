# 保留项目完整配置并对齐检查、服务与交付能力

Console 发布项目版本会重建不完整 spec，可能重置隐藏配置；检查命令的引号被错误拆分；服务与无仓库交付入口又承诺当前后端不支持的行为。统一修复 ProjectVersion 配置和运行能力在前后端之间的契约。

## 环境与证据原则

发现于 staging.sbx-agent.com，构建 `018a0e02370bcae3c3f17ec97aa91b105d232f8b`（2026-10-15）。这是包含多个验收子项的一个 Issue。组内标明 staging 实测、本地隔离复现或源码风险；后两者先补本地集成验证，再决定最终修复方式。原测试计算已释放，不能假设旧 session 仍可写。

## 接手范围与前置数据

console/src/features/projects/ProjectsPage.tsx、sessions/tabs/ServicesTab.tsx 与 ChangesTab.tsx 的 delivery 表单；control/application/projects.py、project_resolution.py、services.py、delivery.py；control/api/live.py 的 preview 能力。

本地创建完整 spec fixture，含非默认 Harness、effort、environment、services 和自定义 ship_policy；另建无 repository 的测试 session 与 ready ChangeSet。服务测试使用仅输出测试日志的本地进程，不引入持久凭证、公网暴露或真实 PR。

## 串联复现

1. 打开包含隐藏字段的测试项目，仅修改一个 Model 字段并发布新版本，对比发布前后的完整 spec。
2. 录入 smoke=python3 -c "print(123)" 以及带空格路径、空参数和转义的检查命令；核对 argv 和真实 stdout/断言。
3. 声明一个测试服务，启动、读日志、停止；检查未声明服务和未启用 preview 时的引导及 eligibility。
4. 对无仓库 ChangeSet 查看交付入口；再用 fake GitHub 对有仓库场景测试能力分支，未获发布授权时不创建外部 PR。

## 修复顺序与边界

先确保项目版本派生保留完整 spec（AUD03），再固定 argv 语义（AUD06）；最后基于有效配置/能力修复服务与交付入口（AUD17/AUD15）。

不顺带实现尚未承诺的公网 preview/upload，不创建或合并真实 PR。ChangesTab 仅修改能力判断和 delivery 表单；文件状态、diff 和 live 查询失效由 G01 负责。services/policy 配置先用隔离 fixture，不能拿现有业务项目验证丢配置。

## 验收清单

- [ ] AUD03：只编辑可见单个字段时，其余有效 spec、隐藏字段和 Harness/effort 保留；交付策略变化必须明确呈现，不能静默放宽。
- [ ] AUD06：引号、空格路径、空参数和转义得到明确 argv；smoke 命令实际输出 123；不能仅以 exit 0 误判，也不能无提示改为 shell 执行。
- [ ] AUD15：无 repository 的 session 不再提供必然失败的 PR 表单；展示准确能力原因，只有确实支持时才提供 export/连接仓库的恢复动作。
- [ ] AUD17：UI 有明确的服务声明/配置路径或准确说明其来源；空状态不暗示自动发现任意进程；Start/Stop/Logs 成功链路通过，preview 不可用时准确提示。

## 验证与完成条件

回归覆盖完整 spec 单字段发布不变性、实际检查 argv/stdout、服务生命周期，以及 projectless/有仓库的交付能力矩阵。执行 make lint、make test、make console-check；可见配置流程纳入 make console-e2e。变更已实施契约时同步规格、OpenAPI、SDK 和用户文档，并执行适用的 make docs-check。

每个子项均记录：复现结论、修复提交、自动化测试及复测结果（含构建 SHA）。尚未实际覆盖的场景不可标为通过。全部验收满足且无遗留计算/临时有效凭证后再关闭本 Issue；无需为组内每个 AUD 再建子 Issue。先阅读仓库 AGENTS.md，测试隔离 HOME/XDG 与云凭证。

## 附件映射

上传附件时只需选取本组对应的 AUD 目录。JPG 是实际截图，MP4 是固定每帧 3 秒的关键帧回放，不是连续录屏；缺少视频不代表该风险已经端到端验证。源码风险或辅助截图的限制见每项证据说明。

- AUD03：step-01.jpg
- AUD06：step-01.jpg
- AUD15：aud15-keyframes.mp4、step-01.jpg、step-02.jpg
- AUD17：aud17-keyframes.mp4、step-01.jpg、step-02.jpg

## 历史范围澄清（#84）

#84 是 2026-09-26 旧版 `/v1/github/*` 零配置验收的一次性交接通道，评论中的 state/安装链接有时效。当前已实施规格使用 `/api` 及显式 Connection；本组不恢复旧 `/v1` 路由，不重新执行过期授权，不读取、转存或注入浏览器 session cookie。关闭旧交接通道仅代表流程过期，不宣称该旧功能已在新架构重新实现。

## 各子项的现场、定位和修复建议

### AUD03 发布项目版本会重置表单未展示的配置

- 优先级：P1。证据：源码确认，条件为项目原先包含 API 或 SDK 配置的扩展字段；未修改现有业务项目验证。
- 触发：已配置 services、environment、ship_policy 或非 OpenCode 默认 Harness 的项目，在 Console 中仅修改 Model 后发布版本。
- 原因定位：[ProjectsPage.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/projects/ProjectsPage.tsx#L61) 从零构造 repository/checks/defaults，并固定 `provider_id: opencode`；[normalize_spec](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/projects.py#L38) 为缺少的字段填入空服务、空环境及默认交付策略。
- 影响：正常 UI 编辑可能清除服务定义和环境配置，并改变检查、独立审阅或合并策略；项目的后续会话使用改变后的配置。
- 修复建议：从当前完整 spec 派生新版本，只覆盖明确编辑的字段；保留隐藏字段及未知字段，避免硬编码 Harness。发布前显示语义差异，特别标明交付策略变化。
- 回归建议：使用包含自定义 ship_policy、services、environment、Harness effort 的项目，只编辑单个可见字段，验证其余配置逐项保留。

### AUD06 项目检查命令未按引号规则解析

- 优先级：P2。证据：staging 项目配置、只读数据库查询及源码函数隔离执行。
- 输入：`smoke=python3 -c "print(123)"`。
- 实际 argv：`["python3", "-c", "\"print(123)\""]`。Python 执行的是字符串字面量，exit 0 但没有任何输出。带空格的代码还会被进一步拆成错误参数。
- 原因定位：[parseChecks](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/projects/ProjectsPage.tsx#L30) 直接按空白 split；[后端](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/projects.py#L30) 逐项保留 argv。
- 修复建议：提供结构化 argv 编辑器，或使用明确指定语法的命令解析器，并显示实际 argv 预览。不要为了兼容引号而无提示地改为任意 shell 执行。
- 回归建议：引号、带空格的路径、空参数、转义、包含等号的参数；验证检查实际执行预期断言而不仅仅是 exit 0。

### AUD15 无仓库会话提供必然失败的交付操作

- 优先级：P2。证据：staging 实际复现。
- 复现：无 repository 的会话捕获 ready ChangeSet 后点击 Request delivery。
- 实际行为：完整 PR title/Draft 表单和可点击按钮都存在，提交后才报 `projectless ChangeSets support export only (not enabled)`。请求标识 `req_3f0f85209ecc4de0`。没有创建 Delivery 或外部 PR。
- 原因定位：[ChangeSetDetail](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/ChangesTab.tsx#L470) 仅按 ready 判断按钮；[delivery.py](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/delivery.py#L168) 明确拒绝无仓库交付，export 也尚未启用。
- 修复建议：服务端发布交付能力及不可用原因，前端在动作之前解释并隐藏不适用的 PR 字段；提供实际可用的导出方式或明确的连接仓库入口。
- 回归建议：projectless、无 GitHub Connection、已撤销连接、尚未 ready 的 ChangeSet、支持交付的仓库会话。

### AUD17 服务面板的文案与可配置能力不匹配

- 优先级：P2，产品能力缺口。证据：界面与源码契约。
- 实际界面：Services 空状态称服务会在 agent 启动后自动出现，但 Console 创建和编辑项目都没有 services 配置入口。
- 后端行为：[services.py](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/services.py#L39) 只列出 effective_spec 声明的服务，不发现任意后台进程；未声明服务的启动请求会被拒绝。[preview-grants](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/api/live.py#L102) 固定返回 unsupported_capability。文件上传入口也明确未启用。
- 修复建议：为声明式服务增加配置入口、示例和启动前检查；空状态明确说明 ProjectVersion 声明方式。后端发布能力开关，避免将尚未启用的 preview/upload 暗示为可用功能。
- 回归建议：声明服务的 Start/Stop/Logs、跨 lease 恢复、无服务声明时的操作引导、部署未启用 preview 时的准确反馈。本轮此成功链路尚未实际执行，不能标记通过。
