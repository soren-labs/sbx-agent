## AUD20 API key 创建未消费幂等标识

- 优先级：P2。证据：源码确认的重试风险，未在 staging 人为丢包以创建额外凭证。
- 条件与影响：前端创建请求带 Idempotency-Key，网络错误或 502–504 时会自动重试（默认最多额外两次）。但创建 key 的 API 没有读取此标识，应用层每次生成新 token 和新记录。如果服务端提交成功而响应丢失，会创建多个有效 key；用户通常只得到最后一次的明文，其余记录仍需单独撤销。
- 定位：[auth.py](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/api/auth.py#L111)、[identity.py](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/identity.py#L393)、[http.ts](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/api/http.ts#L99)。
- 建议：为密钥签发定义明确的幂等语义。同一创建命令不得新增多条有效凭证；一次性明文的安全恢复需明确设计，可采用短期加密响应重放，或在结果不确定时展示可查询的创建操作、引导撤销后重新签发。不要为便于重放而长期保存明文 key。
- 回归：模拟提交已成功但客户端未收到响应，以同一 key 重试，确认有效 key 数量、可见状态和恢复指引一致。

## 证据边界

源码确认的签发重试风险，未额外签发凭证来复现，无对应 UI 视频。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

无新的 UI 关键帧。

## 附件


