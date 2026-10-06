# P0 Spike 结论（SOR-28）— 2026-09-13 实测

执行：WSL 本机 `spike/spike.py`，Modal SDK 1.5.5，Codex CLI 0.153.0，模型 `gpt-5.6-luna`（本机 config 默认），凭证 `~/.codex/auth.json`（`auth_mode=chatgpt`）。两次完整运行（run1 冒烟 + run2 全量），所有 Sandbox 均已 terminate，`Sandbox.list` 无残留。

## 结果表

| # | 验证项 | 结果 | 实测数据 |
| -- | -- | -- | -- |
| 1 | 精简镜像冷启动 | **PASS**（目标 ≤ 20 s） | 镜像首次构建 52 s（debian_slim + Node 22 + codex 0.153.0 + git/rg/jq，一次性）；之后 `Sandbox.create` 0.8 s，首个 `exec("codex --version")` 返回 2.7–3.0 s，**合计 3.5–3.8 s**。容器：Debian 12、Node v22.23.2、root、1 vCPU、4 GB |
| 2a | 订阅 auth.json 注入 | **PASS** | `Secret.from_dict` 传入 → 写 `$CODEX_HOME/auth.json`（chmod 600）；`codex exec` 正常完成 shell + 写文件任务。单轮 7–14 s，首事件 1.2–1.6 s。auth.json 在 3 轮 + 2 并发后 `last_refresh` 未变化（token 仍在有效期，未触发刷新，回写机制在 MVP 中按"有则回写"实现） |
| 2b | 同一份 auth.json 被 2 个 Sandbox 并发使用 | **PASS** | 两个 Sandbox 同时各跑一轮，均 rc=0（13.2 s / 9.2 s），文件写入正确，无限流/冲突 |
| 2c | 备选：DisTokens（仅 chat/completions） | **可通但不推荐** | DisTokens `/v1/responses` 404；LiteLLM `openai/` + `use_chat_completions_api: true` 可桥接。但 Codex 默认请求带 `reasoning_effort={summary:auto}`、`store`、`prompt_cache_key`、`parallel_tool_calls`，DisTokens 源站返回 **502**，Codex 随之挂死到超时；加 `additional_drop_params` 丢掉这 4 个参数后任务可完成（命令执行 + 文件创建成功），但最终回复出现无关的"jailbreak 拒绝"文本，input_tokens 仅 5.4k（正常 26k），怀疑上游非原模型或截断。**结论：MVP 不接入，作为 P3 可选 provider 记录** |
| 3 | 多轮 `codex exec resume` | **PASS** | 3 轮独立 `sb.exec`，`thread_id` 全程一致；第 2 轮不点名文件即能修改第 1 轮创建的文件，第 3 轮正确复述 4 行内容。session 落盘于 `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`。usage 在 `turn.completed.usage`：`input_tokens / cached_input_tokens / cache_write_input_tokens / output_tokens / reasoning_output_tokens` |
| 4 | 空闲保活与回收 | **PASS** | `Sandbox.create(idle_timeout=90)` → 空闲 96 s 后 Sandbox 自行结束（returncode 0），无需 runner 自管；`timeout` 作硬兜底。成本（Sandbox 价：$0.00003942/core/s + $0.00000667/GiB/s）：预留 1 core + 1 GiB ≈ **$0.166/h**，30 min 空闲窗口 ≈ $0.08；一小时空闲 ≈ $0.17 |
| 5 | `tail -F events.jsonl` 转发 | **PASS** | 单个 `sb.exec("tail -n 0 -F …")` 连续 10 min 静默后仍活着，第 602.7 s 写入的行即时到达 |
| 6 | （新增）`codex exec` 在 stdin 为管道时的行为 | **发现风险** | stdin 是打开的管道时，Codex 会等待 stdin EOF（"stdin is appended as a `<stdin>` block"），**在 Sandbox 里直接挂死**（25 s 超时复现）。runner 必须 `</dev/null` 或 `p.stdin.write_eof()` |
| 7 | （新增）Modal stdout 流式分行 | **发现风险** | `sb.exec` 默认 `bufsize=-1` 按 chunk 产出，一个 chunk 可含多行 JSON，run1 中 2 个事件被当成 1 条坏行丢掉。**必须 `bufsize=1`**（按行）或自行 split |

## 对 P1 各包的直接影响

- **WP1-B runner（SOR-30）**：① 调用 Codex 必须关闭 stdin；② 事件名与字段以 `spike/fixtures/real_events.jsonl` 为准（`item.started/completed` 对 `command_execution`、`file_change` 各一对，`agent_message` 只有 `completed`；错误以 `item.completed{item.type="error",message}` 出现）；③ `codex exec resume <thread_id> --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox PROMPT` 语法已验证；④ 认证模式默认 `auth_json`，`provider` 模式保留但 MVP 不实现网关。
- **WP1-C 控制面（SOR-31）**：① `ModalBackend.exec` 必须传 `bufsize=1`；② 空闲回收用 `Sandbox.create(idle_timeout=1800)` 原生能力，reaper 只做 Dict 对账；③ `Sandbox.create(..., env=, secrets=[Secret.from_dict(...)], workdir="/work", tags=)` 签名已验证。
- **WP1-A 镜像（SOR-29）**：`debian_slim(python_version="3.12") + apt(curl git ca-certificates ripgrep jq procps) + nodesource Node 22 + npm i -g @openai/codex@0.153.0`，构建 52 s，冷启动 < 4 s，满足目标。
- **已决定**：默认模型 `gpt-5.6-luna`、推理强度用 Codex 默认（medium）；MVP 并发上限 2 维持。
