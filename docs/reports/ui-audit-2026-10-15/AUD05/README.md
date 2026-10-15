## AUD05 Live changes 使用旧缓存导致改动漏显示

- 优先级：P2。证据：staging 实际复现，已对照整页刷新。
- 复现：打开 Changes 形成初始缓存 → Terminal 执行 `touch ux-audit-terminal.txt` → 回到 Live changes。
- 实际行为：列表只显示之前的 `ux-audit-existing.txt`；整页刷新后才显示两个文件。此前 projectless 的冒烟会话中也出现新文件已存在、Live changes 却显示 No uncommitted changes 的现象。
- 原因定位：[WorkspacePanel.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/WorkspacePanel.tsx#L50) 预先请求 live changes；[ChangesTab.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/ChangesTab.tsx#L287) 无轮询或手动刷新；[事件失效规则](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/state/session-events.ts#L79) 未在 Turn 结束时刷新 changes。终端内直接产生的 Git 变化也没有对应失效事件。
- 修复建议：激活该面板时刷新；提供明确的 Refresh；Turn 结束及恢复连接后更新相关查询。活跃面板可低频轮询，避免“Live”显示静态缓存。
- 回归建议：agent 新增文件、Terminal 新增/删除文件、无 repository 的会话、在运行期间提前访问过 Changes 的情况。
- 澄清：源码的 Git observation 支持 untracked 文件；不能把本问题归因为后端不支持新文件。

## 证据边界

01–03 是本次新文件创建与旧列表的关键帧。刷新对照因 executor 已释放而中断，未纳入回放。prior-before/after 是上一轮独立实测对照，时间与本次不同，不能拼接为同一次操作。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. Live changes initially contains the two prior audit files
2. Terminal creates ux-audit-video-marker.txt and confirms execution
3. After terminal file creation, Live changes still contains only the old two files

## 附件

- [aud05-keyframes.mp4](aud05-keyframes.mp4)
- [captions.srt](captions.srt)
- [prior-after-reload.jpg](prior-after-reload.jpg)
- [prior-before-reload.jpg](prior-before-reload.jpg)
- [step-01.jpg](step-01.jpg)
- [step-02.jpg](step-02.jpg)
- [step-03.jpg](step-03.jpg)
