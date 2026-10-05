# 01 · Amp 公开设计调研与借鉴边界

## 调研来源（全部为公开材料）

| 来源 | 位置 | 看到了什么 |
|------|------|-----------|
| `github.com/ampcode/official-plugins` | 插件、skills（building-plugins / building-agents / building-schedules / creating-webhooks）、`official-modes/index.ts` | Plugin API 的使用方式；单入口注册多个 agent mode；webhook/schedule 语义 |
| `github.com/ampcode/substrate` | README | 大量持久 actor 复用少量 worker、suspend/resume、状态持久化、executor 无关 |
| `github.com/ampcode/amp-sdk-demo`、`cra-github`、`amp-contrib*`、`trestle`、`wmux` | 示例代码 | SDK `execute()` 的真实调用方式（cra-github 用 thread + toolbox 做代码审查）；wmux 单 tmux 会话投影到浏览器 |
| npm `@ampcode/sdk`（`dist/*.d.ts`） | 类型声明 | `execute(): AsyncIterable<StreamMessage>`、`continue`、`executor: local \| orb \| runner`、`runnerId/runnerDir/project/mode/effort/labels` |
| npm `@ampcode/plugin`（`index.d.ts`） | 类型声明 | `PluginAPI.on/registerTool/registerSkill/registerAgentMode/createAgent`、`PluginThread`、`ThreadState = idle\|running\|awaiting-approval\|error`、`createWebhook` |
| ampcode.com/docs（markdown 版） | threads、orbs、projects、runners、model-routing、the-dial、automations、event-driven、agent-to-agent、shipping、handling-secrets、customizing、streaming-json、puck | 产品模型与生命周期 |

**未看到**：Amp 的 harness / 推理循环 / 服务端实现。本文所有关于“Amp 如何实现”的描述都限定为**公开文档可观察的行为**；“sbx 应如何实现”属于本提案的设计推断。

## Amp 的核心抽象（观察到的事实）

1. **Thread 是工作单元**：一次对话 = prompt、回复、tool call、改动的文件。URL 稳定（`T-…`），任意客户端（web / CLI / iOS / macOS）打开同一个 Thread。**执行位置与观看位置分离**：thread 可以在 orb、runner 或本地 CLI 中运行。
2. **Project**：一个代码库的全部设置——仓库、附加仓库、base branch、ship behavior、orb size、pre-clone/pre-setup 脚本、secrets/env、snapshot。Thread 至多属于一个 Project。
3. **Orb**：每个 orb thread 一台隔离远程机器；空闲几分钟后自动暂停（不计费），新消息到来时带着对话、文件、服务一起唤醒。项目级 **snapshot**（`.agents/setup` 结果，最多复用 72h），每次激活/唤醒运行 `.agents/resume`。机器提供 Changes / Files / Terminal（共享 tmux）/ Portals（服务端口暴露）/ Desktop。
4. **Runner**：用户在任意机器上运行 `amp --no-tui --runner-id X`，它**出站注册**并服务若干目录；web 端可以在 runner 上创建 thread。
5. **Mode（The Dial）**：`low / medium / high / ultra`；每个 mode 有自己的 system prompt 与工具定义；**mode 在第一条消息时固定，之后不可切换**（理由：模型接续别的模型的对话效果差；工具定义不同）。
6. **Model Routing**：Connection = 一个凭证 + 设置；类型有 API key、**订阅（ChatGPT、SuperGrok…）**、云平台、网关；按 个人 → workspace → Amp 的优先级、按 model mapping 模式匹配路由；同类订阅每用户只能有一个 active。
7. **消息投递语义**：agent 运行时发送的消息默认 **steer**（当前 step 后插入）、可 **queue**（等完全结束）、双击 Esc **interrupt**。
8. **Automations**：schedule + prompt 保存在 thread 上，每 thread 一个；触发时以短消息唤醒 agent，agent 用 `get_schedule` 工具读取完整 prompt；保留上下文；可设完成条件自动清除。
9. **Webhooks**：插件 `createWebhook({key, headers, handler})` 返回稳定 URL；registration 属于 (user, project, plugin, key)；请求先持久化再返回 200；**至少一次投递**，handler 需按 `event.id` 去重；失败指数退避；thread 归档则暂停投递。
10. **Agent to Agent**：agent 可创建其他 thread（可跨 project、跨 runner）、发送指令、交换文件、等待结果；各 thread 独立工作区，文件需显式传输。
11. **Ship**：Changes 面板的 Ship 按钮本质是**发一条预定义 prompt**（默认 trunk-based；或 Push to Branch；或 Custom Ship prompt）。提交带 `Amp-Thread-ID` trailer。
12. **Secrets**：workspace / project / personal 三级 env 与 secret，个人覆盖项目覆盖 workspace；输出中自动遮蔽 secret；orb 可签发 OIDC token（按 workspace/project/user/thread scope）；setup 阶段不提供个人身份，凭证不进入 snapshot。
13. **Plugin API**：事件钩子 `session.start / tool.call / tool.result / agent.start / agent.end`；`registerTool / registerSkill / registerAgentMode / registerCommand / createAgent`；插件作用域：项目 `.amp/plugins/`、全局、workspace 共享。
14. **SDK / Streaming JSON**：`execute()` 产出 `system(init) → user/assistant(content blocks: text/tool_use/tool_result/thinking) → result(usage, duration, is_error)`，与 Claude Code stream-json 兼容。
15. **Puck**：一个“管理 agent 的 agent”，用用户权限调用平台 API（开 thread、找 thread、改项目设置）。
16. **Substrate**：海量持久 actor 复用少量 worker，支持挂起/恢复与状态持久化。

## sbx 借鉴什么

| Amp 概念 | sbx2 对应 | 说明 |
|----------|-----------|------|
| Thread / Turn | `Thread` / `Turn` | 唯一工作单元；Task/Agent/Session/Run 全部并入 |
| Project | `Project` | 仓库 + 环境 + ship behavior + snapshot + secrets |
| Orb | `Machine`（executor=`modal`） | 每 thread 一台、惰性创建、空闲休眠、按需唤醒 |
| Runner | `Runner`（executor=`runner`） | 同一个 `sbxd` 二进制，出站注册；取代现在的“本地 backend”特例 |
| Local CLI thread | `sbx` CLI 本地执行（executor=`local`） | 本地跑 CLI，事件同步到控制面（cross-client） |
| Mode / The Dial | `Mode` | **Mode = Driver + model + effort + prompt/tool/skill policy**；首个 Turn 后冻结 |
| Model Routing / Connection | `Connection` + `Router` | **sbx 的护城河**：订阅登录是主路径；多账号池 + 并发槽 |
| steer / queue / interrupt | `Turn.delivery` | 取决于 Driver capability，不支持 steer 时降级为 queue |
| Automations | `Automation`（schedule） | 绑定 thread，触发 = 追加一条系统消息 |
| Webhooks | `WebhookEndpoint` + 持久 inbox | 至少一次、幂等、退避、归档暂停 |
| Agent to Agent | 内置 MCP 工具 `sbx.threads.*` | **可跨 CLI**：codex thread 可以派生 grok/opencode thread |
| Ship / Restack | Project `ship_behavior` = prompt 模板 | 控制面观察 git 结果生成 ChangeSet/Delivery |
| `Amp-Thread-ID` trailer | `Sbx-Thread-ID` trailer | commit → thread 可追溯 |
| Plugin / Skill / Tool | `Plugin` manifest + Skill + Tool(MCP) | 适配“我们不拥有 harness”的现实，见 07 |
| Streaming JSON / SDK `execute()` | `sbx.execute()` async stream + `/v1/threads/{id}/events` | 事件信封 + Amp/Claude 兼容的 message content blocks |
| Substrate actor/worker | Thread actor（Postgres lease）+ worker pool | 不引入 Substrate 本身，借鉴模型 |
| Portals / Terminal / Files | Machine `ports` / `terminal` / `files` 子资源 | 由 `sbxd` 提供，控制面代理 |
| Puck | 可选的 `sbx` 系统 Mode（“operator”）+ 平台 MCP 工具 | 后期功能 |

## sbx 不借鉴什么（以及原因）

| 不借鉴 | 原因 |
|--------|------|
| 自研 harness / 直接调模型 API | sbx 的定位就是“跑官方 CLI，吃订阅”；harness 由 CLI 厂商负责 |
| `tool.call` 前置拦截作为通用能力 | 官方 CLI 不一定暴露 pre-tool hook。sbx 中它是 **Driver capability**（`hooks.pre_tool`），通用层只保证 post-hoc 观察 |
| 插件在 agent 进程内执行（TS runtime） | 我们不拥有 CLI 进程；插件分为“控制面声明式部分”与“机器内 MCP/hook 进程”，见 07 |
| Mode 名称 `low/medium/high/ultra` 作为唯一维度 | sbx 的 mode 必须同时表达 **哪个 CLI**；采用 `driver/profile` 命名（如 `codex/high`），再提供 `auto/*` 别名 |
| 以 Amp credits 为默认计费路径 | sbx 默认没有“平台兜底”模型；无可用 Connection 时 Turn 进入 `waiting_for_capacity` 而不是偷偷换计费方式 |
| Space（音视频）、Desktop 流媒体 | 与核心无关，非目标（Desktop 可作为未来 Machine capability） |

## 关键差异带来的额外设计

1. **原生会话身份**：CLI 有自己的 session id（codex thread id、opencode session…）。sbx Thread ↔ `driver_session_ref` 1:1，Machine 重建后必须能 resume，因此 CLI 的会话存储目录属于 Worktree 持久层（见 05）。
2. **凭证是文件**：订阅登录通常是 CLI 的 HOME 下的 auth 文件，会被 CLI 自行刷新。需要 lease + CAS 回写（见 06）。
3. **事件是翻译出来的**：每个 CLI 输出格式不同，Driver 负责翻译到统一信封并保留 raw（见 04）。
4. **能力不对齐**：steer、approval、pre-tool hook、MCP、skills 目录、structured output、usage 精度都随 CLI 而异 → `DriverCapabilities` 是一等概念，UI 与 API 按能力降级。
