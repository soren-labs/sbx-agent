## AUD18 关闭会话没有不可恢复提示

- 优先级：P2，交互设计。证据：本轮测试会话实际关闭。
- 实际行为：Session actions 中点击 Close session 立即执行，菜单没有解释其与 Archive 的区别，也没有给出撤回入口；关闭后会话永久不再接受消息。
- 修复建议：保留 Archive 为可恢复默认操作；Close 明确说明停止任务、释放计算及不能继续的后果，并提供有对象名称的确认，或提供产品定义的恢复机制。
- 回归建议：键盘误触保护、运行中关闭的取消状态、关闭后的只读状态、与 Archive 的区别。

## 证据边界

本次 staging 实测截图；具体动作和页面 URL 见 [capture-log.json](../capture-log.json)。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. Close session menu item does not describe irreversible read-only effect
2. Select Close session; no confirmation dialog is offered
3. Session is immediately Closed and read only after menu click

## 附件

- [aud18-keyframes.mp4](aud18-keyframes.mp4)
- [captions.srt](captions.srt)
- [step-01.jpg](step-01.jpg)
- [step-02.jpg](step-02.jpg)
- [step-03.jpg](step-03.jpg)
