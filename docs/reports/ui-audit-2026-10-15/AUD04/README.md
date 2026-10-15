## AUD04 新建文件入口无法完成新建

- 优先级：P1。证据：staging 实际复现。
- 复现：活跃会话 → Files → Open or create file 输入不存在的 `ux-audit-new.txt` → Open。
- 实际行为：页面同时显示“File does not exist yet; saving creates it”和 Not found，但没有文本输入区、Edit 或 Save；用户无法完成该功能。请求标识 `req_6a0e10bd0e2e4917`。
- 原因定位：[FilesTab.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/FilesTab.tsx#L168) 的 editing 初始为 false，而 Edit 按钮仅在 file 已存在时渲染。另一个问题是 `Boolean(file.error)` 把所有错误都解释成 missing。
- 修复建议：仅把明确的 not_found 视为可新建，直接进入空白编辑器；权限、连接或网络错误应显示各自恢复动作。按预期使用 `expected_digest: absent` 防止覆盖并发创建的文件。
- 回归建议：新文件创建成功；嵌套目录；已存在文件；权限错误；并发创建冲突。

## 证据边界

本次 staging 实测截图；具体动作和页面 URL 见 [capture-log.json](../capture-log.json)。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. Enter a nonexistent test path into Open or create file
2. Not found and saving-creates-it hint, but no editor or Save action

## 附件

- [aud04-keyframes.mp4](aud04-keyframes.mp4)
- [captions.srt](captions.srt)
- [step-01.jpg](step-01.jpg)
- [step-02.jpg](step-02.jpg)
