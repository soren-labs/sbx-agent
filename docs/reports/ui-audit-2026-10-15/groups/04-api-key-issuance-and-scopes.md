# 让 API key 签发可安全重试，并提供明确的权限选择

前端创建 key 的请求会自动重试，但后端没有消费幂等标识，可能留下用户未取得明文的额外有效 key；UI 又只允许签发 full-scope key 且不展示权限。将签发、权限展示和撤销验证作为一个凭证管理闭环修复。

## 环境与证据原则

发现于 staging.sbx-agent.com，构建 `018a0e02370bcae3c3f17ec97aa91b105d232f8b`（2026-10-15）。这是包含多个验收子项的一个 Issue。组内标明 staging 实测、本地隔离复现或源码风险；后两者先补本地集成验证，再决定最终修复方式。原测试计算已释放，不能假设旧 session 仍可写。

## 接手范围与前置数据

console/src/features/settings/SettingsPage.tsx、api/client.ts 的 apiKeys、相关类型和 i18n；control/api/auth.py、control/application/identity.py；SDK/CLI 的 key 签发调用如契约变化则同步。

本地隔离账号和数据库，使用确定性故障注入模拟签发事务已提交但响应丢失；准备 read、write、full 测试权限。staging 上一轮专用 key 已 revoked，不可恢复其明文；如需签发新凭证，遵循当前环境的授权规则并在结束后撤销。

## 串联复现

1. 签发提交成功后模拟响应丢失，以相同 Idempotency-Key 重试；记录有效 key 条数、用户看到的操作状态及明文恢复语义。
2. 从 UI 分别选择 read/write/full，核对列表展示与服务端 scopes；用独立客户端验证查询、session 写入、key 管理的权限矩阵。
3. 验证一次性明文展示、Copy、关闭/刷新后不可再次获取、列表不泄露明文；撤销后读写均 401。

## 修复顺序与边界

先定义签发命令的幂等与一次性明文恢复策略（AUD20），再把 scope 参数、默认值、权限解释和列表展示贯穿前后端（AUD21）。

不读取或替换已有业务 key，不把明文凭证写进测试日志、Issue、截图或 fixtures。AUD20 当前是源码风险而非 staging 丢包实测；先在本地集成环境确认。不能仅关闭全局 HTTP 重试来宣称签发已具备幂等保证。

## 验收清单

- [ ] AUD20：响应丢失/502–504 后用同 key 重试不新增多条有效凭证；同 key 不同请求冲突；用户有明确的结果不确定恢复指引，不能为重放而长期存储明文。
- [ ] AUD21：创建可选 read/write/full，默认遵循最小权限并解释含义；列表可见 scopes；read 不可写，write 不可管理 key，full 允许预期管理；撤销后即时 401。

## 验证与完成条件

使用事务提交后丢响应的集成测试与受控 transport 验证记录数量/恢复行为，避免仅断言实现细节。补充权限矩阵、明文不泄漏和 UI 创建展示回归。执行 make lint、make test、make console-check；UI 流程纳入 make console-e2e；契约变更同步规格及 SDK/CLI。

每个子项均记录：复现结论、修复提交、自动化测试及复测结果（含构建 SHA）。尚未实际覆盖的场景不可标为通过。全部验收满足且无遗留计算/临时有效凭证后再关闭本 Issue；无需为组内每个 AUD 再建子 Issue。先阅读仓库 AGENTS.md，测试隔离 HOME/XDG 与云凭证。

## 附件映射

上传附件时只需选取本组对应的 AUD 目录。JPG 是实际截图，MP4 是固定每帧 3 秒的关键帧回放，不是连续录屏；缺少视频不代表该风险已经端到端验证。源码风险或辅助截图的限制见每项证据说明。

- AUD20：无 UI 故障附件，见下方源码/隔离复现记录。
- AUD21：aud21-keyframes.mp4、step-01.jpg、step-02.jpg

## 各子项的现场、定位和修复建议

### AUD20 API key 创建未消费幂等标识

- 优先级：P2。证据：源码确认的重试风险，未在 staging 人为丢包以创建额外凭证。
- 条件与影响：前端创建请求带 Idempotency-Key，网络错误或 502–504 时会自动重试（默认最多额外两次）。但创建 key 的 API 没有读取此标识，应用层每次生成新 token 和新记录。如果服务端提交成功而响应丢失，会创建多个有效 key；用户通常只得到最后一次的明文，其余记录仍需单独撤销。
- 定位：[auth.py](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/api/auth.py#L111)、[identity.py](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/identity.py#L393)、[http.ts](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/api/http.ts#L99)。
- 建议：为密钥签发定义明确的幂等语义。同一创建命令不得新增多条有效凭证；一次性明文的安全恢复需明确设计，可采用短期加密响应重放，或在结果不确定时展示可查询的创建操作、引导撤销后重新签发。不要为便于重放而长期保存明文 key。
- 回归：模拟提交已成功但客户端未收到响应，以同一 key 重试，确认有效 key 数量、可见状态和恢复指引一致。

### AUD21 API key UI 缺少权限选择和展示

- 优先级：P2。证据：staging UI 创建与独立 API 验证。
- 实际：创建弹窗只有名称，提交后 API 返回 scopes 为 `["*"]`；列表仅展示名称、前缀和时间，无法看出该 key 可执行的权限。后端已有 read/write/* 作用域，但 UI 无法签发只读或普通写入权限。
- 影响：仅需查看 session 或运行任务的脚本也会默认取得管理凭证的完整权限；用户缺少判断和收窄权限的入口。这是权限管理体验缺口，未发现鉴权绕过。
- 定位：[SettingsPage.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/settings/SettingsPage.tsx#L184)、[client.ts](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/api/client.ts#L99)、[作用域默认值](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/identity.py#L379)。
- 建议：创建时提供 read/write/full 选择，解释权限范围；列表显示权限徽标。普通脚本默认 write，只读工具默认 read；full 需明确展示其管理权限。保持后端校验为最终权限边界。
- 回归：只读 key 可查询但不能创建；write key 可管理 session 但不能签发 key；撤销后均立即 401。此次未额外签发不同作用域的测试 key，权限矩阵需后续补测。
