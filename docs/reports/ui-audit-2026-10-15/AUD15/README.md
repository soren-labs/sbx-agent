## AUD15 无仓库会话提供必然失败的交付操作

- 优先级：P2。证据：staging 实际复现。
- 复现：无 repository 的会话捕获 ready ChangeSet 后点击 Request delivery。
- 实际行为：完整 PR title/Draft 表单和可点击按钮都存在，提交后才报 `projectless ChangeSets support export only (not enabled)`。请求标识 `req_3f0f85209ecc4de0`。没有创建 Delivery 或外部 PR。
- 原因定位：[ChangeSetDetail](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/ChangesTab.tsx#L470) 仅按 ready 判断按钮；[delivery.py](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/delivery.py#L168) 明确拒绝无仓库交付，export 也尚未启用。
- 修复建议：服务端发布交付能力及不可用原因，前端在动作之前解释并隐藏不适用的 PR 字段；提供实际可用的导出方式或明确的连接仓库入口。
- 回归建议：projectless、无 GitHub Connection、已撤销连接、尚未 ready 的 ChangeSet、支持交付的仓库会话。

## 证据边界

本次 staging 实测截图；具体动作和页面 URL 见 [capture-log.json](../capture-log.json)。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. Projectless session offers an enabled PR delivery form
2. Request rejected: projectless export is not enabled; no PR is created

## 附件

- [aud15-keyframes.mp4](aud15-keyframes.mp4)
- [captions.srt](captions.srt)
- [step-01.jpg](step-01.jpg)
- [step-02.jpg](step-02.jpg)
