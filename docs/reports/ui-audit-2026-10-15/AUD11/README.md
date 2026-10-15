## AUD11 桌面审阅链接会跳转到不存在的嵌套路由

- 优先级：P2。证据：staging 实际复现。
- 复现：1440px 桌面双栏，当前路由为 `/sessions/<id>/changes` 或 `/overview`，点击对话中完成状态旁的 Review changes。
- 实际行为：跳到 `/changes/changes` 或 `/overview/changes`，显示 Page not found；原会话工作台消失。
- 原因定位：[ConversationPane.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/ConversationPane.tsx#L148) 使用相对链接 `to="changes"`，而 Conversation 在桌面同时存在于带 tab 的路由下。
- 修复建议：生成基于 session id 的绝对路径，或通过统一的会话路由构造函数跳转。404 页增加返回当前会话的恢复入口。
- 回归建议：在 conversation、overview、changes、files、terminal 等每个工作台路由上点击 Review changes，结果都应为同一个合法 changes 路由。
- 证据：404 截图。

## 证据边界

本次 staging 实测截图；具体动作和页面 URL 见 [capture-log.json](../capture-log.json)。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. From overview, Review changes points to nested overview/changes
2. Click leads to Page not found

## 附件

- [aud11-keyframes.mp4](aud11-keyframes.mp4)
- [captions.srt](captions.srt)
- [step-01.jpg](step-01.jpg)
- [step-02.jpg](step-02.jpg)
