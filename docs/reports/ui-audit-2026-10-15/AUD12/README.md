## AUD12 Activity 刷新后无法查看已经发生的事件

- 优先级：P2。证据：staging 实际复现及源码。
- 复现：已成功运行的会话，整页刷新后打开 Activity。
- 实际行为：Turn 仍在，Events 却显示 No events yet。页面实际上只包含当前浏览器订阅之后的新事件，容易使人误判历史证据丢失。
- 原因定位：[snapshot](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/state/session-live.ts#L142) 只装载 messages/turns/executor，并把 watermark 设为当前值；[ActivityTab](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/ActivityTab.tsx#L35) 仅使用内存 state.events。
- 修复建议：Activity 首次打开时请求有界的历史事件页，并按 seq 与后续流合并；提供加载更早记录和过滤，避免重放全量流进入所有面板。
- 回归建议：已有历史但没有新事件的会话、刷新、重新登录、事件分页、流与历史页的去重。

## 证据边界

本次 staging 实测截图；具体动作和页面 URL 见 [capture-log.json](../capture-log.json)。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. Completed turns are visible but Events says No events yet
2. Reload preserves turns but does not load historical events

## 附件

- [aud12-keyframes.mp4](aud12-keyframes.mp4)
- [captions.srt](captions.srt)
- [step-01.jpg](step-01.jpg)
- [step-02.jpg](step-02.jpg)
