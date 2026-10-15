## AUD09 切换工作区标签会丢失终端连接入口

- 优先级：P2。证据：staging 实际复现及源码。
- 复现：连接 Terminal → 切到 Files → 返回 Terminal。
- 实际行为：只剩 Start terminal，之前的 terminal id、输出和连接状态消失。再次点击会创建另一个终端，原进程并没有相应的恢复或关闭入口。
- 原因定位：[WorkspacePanel.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/WorkspacePanel.tsx#L112) 条件挂载 TerminalTab；[TerminalTab.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/TerminalTab.tsx#L25) 只在组件局部状态中保存 terminal。
- 修复建议：至少在同一 session 的面板切换期间保留终端；提供终端列表与关闭按钮。无需承诺在沙箱重建后恢复原进程，但必须区分“进程仍在”和“连接视图重置”。
- 回归建议：切换标签、面板收起、路由切换、退出命令、沙箱释放后的明确状态。

## 证据边界

本次 staging 实测截图；具体动作和页面 URL 见 [capture-log.json](../capture-log.json)。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. Connected terminal has output and command input
2. Navigate away from Terminal to Files
3. Returning loses terminal handle and shows Start terminal

## 附件

- [aud09-keyframes.mp4](aud09-keyframes.mp4)
- [captions.srt](captions.srt)
- [step-01.jpg](step-01.jpg)
- [step-02.jpg](step-02.jpg)
- [step-03.jpg](step-03.jpg)
