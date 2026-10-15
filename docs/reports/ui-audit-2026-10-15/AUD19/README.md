## AUD19 Diff 的行号和新增文件标记不准确

- 优先级：P3。证据：staging 实际 diff 与源码。
- 实际行为：明确为 new file mode 的新增 Python 文件在列表标成 M；diff 元信息如 `diff --git`、`index`、`---`、`+++` 被赋予 0、1 等伪代码行号，尾部空行也被当成上下文行。
- 原因定位：[fileKind 与 LETTER](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/ChangesTab.tsx#L251) 将 manifest 的 file 类型落入 changed；[DiffView](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/ChangesTab.tsx#L61) 在 hunk 外也递增代码行号。
- 修复建议：从 ChangeSet 的差异状态或解析后的 patch 得出 A/M/D；用统一 diff 解析器区分文件头、hunk、上下文、增删行和 no-newline 标记。
- 回归建议：新文件、删除、重命名、多 hunk、二进制文件、无尾换行，核对每个代码行号。

## 证据边界

本次 staging 实测截图；具体动作和页面 URL 见 [capture-log.json](../capture-log.json)。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. Captured audit files are marked M
2. Confirmed loaded diff: new file mode shown beside M; metadata lines numbered 0 through 4

## 附件

- [aud19-keyframes.mp4](aud19-keyframes.mp4)
- [captions.srt](captions.srt)
- [step-01.jpg](step-01.jpg)
- [step-02.jpg](step-02.jpg)
