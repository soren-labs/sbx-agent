## AUD22 关闭后的会话仍显示 Retry，但执行必然失败

- 优先级：P2。证据：staging 实际复现。
- 步骤：通过 API 取消 Turn，关闭 session，打开 Console；历史取消卡片仍有 Retry。点击后返回 `invalid_transition: session is closed; new execution requests are rejected`，request ID 为 `req_108e92cdc4f64031`。
- 定位：[projections.py](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/projections.py#L47) 只依据 Turn 状态生成 actions，没有考虑 session.lifecycle；[ConversationPane.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/ConversationPane.tsx#L84) 据此直接渲染 Retry。
- 建议：由服务端结合 session 生命周期计算允许的 actions；关闭后清除 Retry，UI 同时用只读状态兜底。若产品支持 continuation，可提供明确的新建关联会话入口。
- 回归：取消/失败 Turn 在 open、archived、closed 状态下的按钮和 API eligibility 保持一致。
- 证据：关闭后 Retry 的反馈截图。

## 证据边界

本次 staging 实测截图；具体动作和页面 URL 见 [capture-log.json](../capture-log.json)。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. Closed session still offers Retry on cancelled turn
2. Retry rejected: invalid_transition because session is closed

## 附件

- [aud22-keyframes.mp4](aud22-keyframes.mp4)
- [captions.srt](captions.srt)
- [step-01.jpg](step-01.jpg)
- [step-02.jpg](step-02.jpg)
