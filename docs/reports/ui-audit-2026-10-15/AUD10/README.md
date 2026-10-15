## AUD10 子会话无法选择 No ChangeSet

- 优先级：P2。证据：staging 实际复现及源码。
- 复现：父会话存在 ready ChangeSet → Children → ChangeSet 选择 No ChangeSet。
- 实际行为：控件立即重新选中第一个 ready ChangeSet。
- 原因定位：[ChildrenTab.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/ChildrenTab.tsx#L89) 用 `changesetId || ready[0]?.id || ""`，把明确选择的空值当成未初始化。
- 修复建议：用 null 表示尚未选择，用空字符串表示明确无对象；仅在初始化时应用默认 ChangeSet。
- 回归建议：research 无对象、手动清空、异步加载对象、切换 Role 后保留明确选择。

## 证据边界

No ChangeSet 通过选择控件执行后仍显示原值；关键帧中不会显示完整下拉操作过程，具体动作见 [capture-log.json](../capture-log.json)。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. ChangeSet defaults to first ready item
2. Selecting No ChangeSet immediately restores first item

## 附件

- [aud10-keyframes.mp4](aud10-keyframes.mp4)
- [captions.srt](captions.srt)
- [step-01.jpg](step-01.jpg)
- [step-02.jpg](step-02.jpg)
