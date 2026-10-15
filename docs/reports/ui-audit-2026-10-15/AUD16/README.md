## AUD16 终端命令输入没有兑现 HTTP 重试的幂等保证

- 优先级：P1，需在故障注入测试中补验。证据：源码确认；本轮未向 staging 注入网络故障或重复有副作用的命令。
- 触发条件：terminal input 请求已到达服务端，但响应丢失或代理返回 502/503/504，客户端按既有策略自动重试。
- 原因定位：[Http.request](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/api/http.ts#L99) 用同一个 Idempotency-Key 重试 mutation；[terminal_input API](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/api/live.py#L55) 不传递该 key；[LiveWorkspace.terminal_input](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/live.py#L138) 每次生成新的 runtime operation id。终端创建同样每次生成新的 operation id。
- 影响：相同的一行命令可能执行多次。对于构建、迁移、提交等有副作用命令，结果可能与用户的一次输入不一致。
- 修复建议：把客户端请求 key 贯穿授权、业务去重和 runtime operation id；为同 key 不同 payload 返回冲突。对无法提供幂等保证的终端流输入，禁止静默自动重试，并明确显示传输结果未知。
- 回归建议：模拟已执行但响应丢失、网关 503、同 key 不同内容，验证命令执行次数与终端数量。

## 证据边界

源码确认的故障重试风险，未在 staging 注入响应丢失，未制作误导性 UI 录像。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

无新的 UI 关键帧。

## 附件


