## AUD08 终端直接显示 ANSI 控制字符

- 优先级：P2。证据：staging 实际复现。
- 复现：Start terminal，执行普通 `git status --short`。
- 实际行为：显示 `[?2004h`、`[31m`、`[m` 等终端转义序列，颜色、括号粘贴等控制指令成为可见噪声。
- 原因定位：[TerminalTab.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/TerminalTab.tsx#L99) 用普通 pre 直接渲染 PTY 字节解码后的文本。
- 修复建议：使用合适的终端渲染组件及键盘输入模型；若产品仅支持逐行命令，应明确按命令控制台呈现，并以流式解析器安全处理控制序列。
- 回归建议：颜色、回车覆盖行、进度条、宽字符、跨 chunk 的 ANSI 序列。

## 证据边界

本次 staging 实测截图；具体动作和页面 URL 见 [capture-log.json](../capture-log.json)。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. Start terminal before executing a harmless status command
2. Run git status and create only an audit marker file
3. Terminal output exposes raw ANSI control sequences

## 附件

- [aud08-keyframes.mp4](aud08-keyframes.mp4)
- [captions.srt](captions.srt)
- [step-01.jpg](step-01.jpg)
- [step-02.jpg](step-02.jpg)
- [step-03.jpg](step-03.jpg)
