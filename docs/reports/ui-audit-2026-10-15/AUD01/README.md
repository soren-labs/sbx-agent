## AUD01 工具输出中的不兼容字符会卡住事件入库与取消

- 优先级：P1。证据：staging 浏览器、服务日志和只读数据库查询。
- 现场：测试子会话 `sess_01a13f6ac90a7d16be69a9a366d7178b`。执行过程中，`execution.reconcile` 在更新 `message_parts` 时抛出 `psycopg.errors.UntranslatableCharacter: unsupported Unicode escape sequence`。一个 reconcile Job 已 failed，尝试计数为 211；停止后新 Job 仍反复报 `internal_error`。
- 实际行为：点击 Stop 后 Turn 保持 `cancelling`，无结束时间；Release compute 对应 Job 持续排队，lease 仍为 ready。页面仍显示 Working，消息区域显示原始键名 `turn.cancelling`，用户无法通过正常流程结束任务并回收资源。
- 原因定位：[ingest.py](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/ingest.py#L199) 将工具 input/output/error 直接写入 JSONB；[execution.py](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/execution.py#L982) 在同一批事件入库成功前无法推进；[资源释放](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/execution.py#L1149) 等待活跃 execution 消失。
- 修复建议：在统一持久化边界对 PostgreSQL 不可表示的字符进行明确、确定性的编码或替换，尤其是 NUL；对原始事件保留可审计的安全表示。错误事件不能无限阻挡 terminal/cancel 证据，应有明确的失败状态及可恢复路径。UI 展示后台失败原因与取消进度。
- 回归建议：工具输出含 NUL、控制字符和异常 Unicode；确认入库、watermark、重放、cancel 和 release 都能收敛，不重复执行已启动命令。
- 证据：服务端堆栈、Turn 与 Job 状态、释放等待状态。具体导致数据库拒绝的字符尚需进一步定位；当前未把它断言为已经确认的单一字节。
- 清理：经用户明确批准，仅终止本次故障测试沙箱；Modal 返回 termination_confirmed 为 true、observed_status 为 terminated。未手动改写数据库状态。后台随后将 Turn 标记为 cancelled、lease 标记为 lost，executor.release Job succeeded（146 次尝试）；保留了先前 failed 的 reconcile 证据。清理结果、最终资源状态。
- 清理后界面：

## 证据边界

未重新触发故障。仅包含原现场脱敏日志与恢复后截图；recovery-only.jpg 不能证明故障时的 UI 状态。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

无新的 UI 关键帧。

## 附件

- [approved-cleanup-result.txt](approved-cleanup-result.txt)
- [final-resource-state.txt](final-resource-state.txt)
- [recovery-only.jpg](recovery-only.jpg)
- [server-child-state.txt](server-child-state.txt)
- [server-resource-state.txt](server-resource-state.txt)
- [server-unicode-error.txt](server-unicode-error.txt)
