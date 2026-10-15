## AUD21 API key UI 缺少权限选择和展示

- 优先级：P2。证据：staging UI 创建与独立 API 验证。
- 实际：创建弹窗只有名称，提交后 API 返回 scopes 为 `["*"]`；列表仅展示名称、前缀和时间，无法看出该 key 可执行的权限。后端已有 read/write/* 作用域，但 UI 无法签发只读或普通写入权限。
- 影响：仅需查看 session 或运行任务的脚本也会默认取得管理凭证的完整权限；用户缺少判断和收窄权限的入口。这是权限管理体验缺口，未发现鉴权绕过。
- 定位：[SettingsPage.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/settings/SettingsPage.tsx#L184)、[client.ts](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/api/client.ts#L99)、[作用域默认值](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/identity.py#L379)。
- 建议：创建时提供 read/write/full 选择，解释权限范围；列表显示权限徽标。普通脚本默认 write，只读工具默认 read；full 需明确展示其管理权限。保持后端校验为最终权限边界。
- 回归：只读 key 可查询但不能创建；write key 可管理 session 但不能签发 key；撤销后均立即 401。此次未额外签发不同作用域的测试 key，权限矩阵需后续补测。

## 证据边界

页面可见的 key 前缀是产品公开展示的部分前缀，不包含完整可用凭证。本轮没有再次签发密钥。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. API keys list has no scope labels
2. Create form offers only a name; no scope selector

## 附件

- [aud21-keyframes.mp4](aud21-keyframes.mp4)
- [captions.srt](captions.srt)
- [step-01.jpg](step-01.jpg)
- [step-02.jpg](step-02.jpg)
