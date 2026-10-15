## AUD17 服务面板的文案与可配置能力不匹配

- 优先级：P2，产品能力缺口。证据：界面与源码契约。
- 实际界面：Services 空状态称服务会在 agent 启动后自动出现，但 Console 创建和编辑项目都没有 services 配置入口。
- 后端行为：[services.py](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/services.py#L39) 只列出 effective_spec 声明的服务，不发现任意后台进程；未声明服务的启动请求会被拒绝。[preview-grants](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/api/live.py#L102) 固定返回 unsupported_capability。文件上传入口也明确未启用。
- 修复建议：为声明式服务增加配置入口、示例和启动前检查；空状态明确说明 ProjectVersion 声明方式。后端发布能力开关，避免将尚未启用的 preview/upload 暗示为可用功能。
- 回归建议：声明服务的 Start/Stop/Logs、跨 lease 恢复、无服务声明时的操作引导、部署未启用 preview 时的准确反馈。本轮此成功链路尚未实际执行，不能标记通过。

## 证据边界

回放展示 Services 空状态与新项目表单的配置缺口；不代表声明服务的 Start/Stop/Logs 已验证。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. Services empty state implies automatic appearance without configuration UI
2. Supplement: project form has no services configuration

## 附件

- [aud17-keyframes.mp4](aud17-keyframes.mp4)
- [captions.srt](captions.srt)
- [step-01.jpg](step-01.jpg)
- [step-02.jpg](step-02.jpg)
