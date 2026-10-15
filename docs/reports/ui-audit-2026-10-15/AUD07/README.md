## AUD07 排队追问使同一 Turn 重复显示结果卡片

- 优先级：P2。证据：staging 实际复现。
- 复现：启动一个任务 → 在其执行期间发送排队追问 → 停止前一个任务 → 等待队列任务成功。
- 实际行为：同一个取消结果和 Retry 出现两次；队列任务也有一张空 Completed 和一张包含实际回答的 Completed。操作历史因此难以对应到用户请求。
- 原因定位：[blocksOf](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/worklog.ts#L292) 只合并相邻且 turn_id 一致的消息。排队用户消息与先前 Turn 的 assistant 消息交错，导致同一 Turn 分成多个块，每个块又渲染一遍 TurnStatus。
- 修复建议：稳定地按 Turn 身份组织执行结果，每个 Turn 只渲染一份结束状态；保留排队消息的接收时间及真正执行顺序，避免重排造成语义混乱。
- 回归建议：执行期间连续排队、备注穿插、取消后自动运行队列、重试与刷新后状态一致。

## 证据边界

两张截图分别展示历史消息的不同可见区域，属于状态证据，不是本次重新启动队列任务的连续录制。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. Repeated cancelled and completed status cards around queued message

## 附件

- [prior-duplicate-completed.jpg](prior-duplicate-completed.jpg)
- [step-01.jpg](step-01.jpg)
