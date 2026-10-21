# 环境验证与接续记录

日期：2026-10-21。这里记录本轮实际操作；产品能力与未来工作包见 OSS_BACKEND_TASKS.md。

## 已验证的开发入口

| 设备 | Codex | Claude Code |
| --- | --- | --- |
| Mac mini | 0.162.1；ChatGPT 登录；真实返回 SBX_CODEX_AUTH_OK | 2.1.292；Claude Pro；真实返回 SBX_CLAUDE_AUTH_OK |
| Ubuntu 工作站 | 0.163.0；ChatGPT 登录；真实返回 SBX_CODEX_AUTH_OK | 2.1.306；Claude Pro；真实返回 SBX_CLAUDE_AUTH_OK |

这些调用禁用了工具或明确要求只返回标记，没有运行产品任务。开发 CLI 登录不证明 SBX 已支持对应订阅。

Codex 使用[官方认证文档](https://developers.openai.com/codex/auth/)支持的远端 auth cache 迁移；只通过现有加密 SSH 连接传输到用户认证目录，目录 0700、文件 0600，不进入 Git。Claude 使用[官方订阅登录](https://code.claude.com/docs/en/authentication)，用户完成浏览器授权；没有复制 Mac 钥匙串。

Mac 正式工作区已通过 Claude 的正常目录信任提示，并验证交互调用。Ubuntu 已完成 CLI 首次启动提示，验证了配置目录中的交互调用；正式工作树建立后，按正常提示确认相应目录，不编辑信任状态绕过流程。

## 工具与目录

| 项目 | Mac | Ubuntu |
| --- | --- | --- |
| 系统 | macOS 27.0 arm64 | Ubuntu 26.04.1 LTS x86_64 |
| 工作区 | /Users/soren/Developer/sbx-agent | /home/dev/Developer/sbx-agent；已创建，分支 `setup/oss-environment-20261021`，基线 `aa532f2a` |
| Python | uv 管理的 3.12.15，项目 .venv 已安装 | uv 管理的 3.12.15 已安装 |
| Node / npm | 24.22.0 / 11.19.1 | 22.23.3 / 10.9.9 |
| uv | 0.13.1 | 0.13.4 |
| PostgreSQL | 17.11；Homebrew server binaries 已安装 | 18.6（apt，server binaries 已安装）；与 Mac 版本不同，完整 Linux 测试以 Ubuntu 为准 |
| Git | 2.56.0；GitHub CLI 既有用户登录 | 2.53.0（apt）；gh 2.46.0 已安装，Ubuntu 上未验证 GitHub 登录，推送仍由 Mac 完成 |
| Docker / Compose | 本阶段运行资源放 Ubuntu | CLI 29.1.3、Compose 2.40.3 已安装；**daemon 拒绝 dev 用户访问**（socket 为 `root:docker`，dev 不在 docker 组），未验收；按用户边界不改用户组，Docker 资源不在本批次 |
| 浏览器依赖 | Console 与 Docs 的锁定 npm 依赖已安装 | Playwright 1.64.0 对应 Chromium 等已下载 |

Ubuntu 用户工具位于 /home/dev/.local/bin。非交互 SSH 的 PATH 已验证，原 .bashrc 已备份到 /home/dev/.cache/sbx-setup/bashrc-before-user-tools。

## Mac 基线的准确结果

| 检查 | 结果 | 对接手工作的意义 |
| --- | --- | --- |
| uv sync --frozen --python 3.12 | 通过 | 项目使用锁定 Python 依赖 |
| Console / Docs npm ci | 通过 | 两处锁定 Node 依赖安装完成 |
| make lint | 通过，339 个 Python 文件格式一致 | 基线静态检查可用 |
| Console typecheck / build | 通过 | 旧 Console 可构建；不是新 Mac 客户端成品 |
| Console 全部测试，默认并发 | 192 passed / 1 timeout | 没有全绿 |
| Console 全部测试，2 workers | 189 passed / 4 failed | 仍有时序与断言失败；保留问题，不继续盲目重试 |
| Mac 完整后端测试 | 主动中止时 184 passed / 62 failed | runtime 依赖 Linux，不能在 Mac 宣称完整通过 |
| 控制面／数据库定向测试 | 73 passed / 1 failed | 数据库与多数控制面用例可运行；续约用例需 Ubuntu 复核 |
| Docs build / link check | 通过；构建完成，check-links: ok | 文档依赖、生成与链接检查可用 |
| 环境 doctor / 脚本检查 | Mac doctor 通过；脚本在 Mac 与 Ubuntu 均通过 bash -n；git diff --check 通过 | doctor 只检查组件，不代表产品验收 |

Mac 完整测试的共同平台根因：

- runtime/daemon/process_anchor.py 调用 Linux prctl 作为 subreaper。
- runtime/daemon/supervisor.py 使用 /proc。
- 在 Mac 上，prctl 符号与 /proc 都不存在；现有 managed_popen 启动一个无凭证 Python 子进程即出现 Errno 5。
- 当前 CI 的后端任务使用 Ubuntu。完整后端／runtime 验收放 Ubuntu，不通过修改测试来掩盖平台限制。

控制面定向测试命令：

```bash
bash scripts/dev/run.sh uv run pytest tests/unit/domain tests/unit/test_layer_boundaries.py tests/unit/test_openapi_drift.py tests/integration/postgres tests/integration/api/test_identity_api.py tests/integration/api/test_api_key_issuance.py tests/integration/api/test_api_key_scopes.py -q --tb=short
```

其中 test_jobs_claims.py 的 test_keepalive_holds_the_claim_through_a_slow_call_and_stops_after_it 失败：原执行者续约期间 rival 获得了后续 claim。这关系到长任务可靠性，不能把它未经确认归为 macOS 平台问题。先在 Ubuntu 复核，若可复现则由 Claude 在 B1 中修复、Codex 验收。

## 后台会话与远程派发

Mac 已验证 Claude 原生后台流程：--bg 返回会话 ID，在另一次命令中 logs 能读取 SBX_BACKGROUND_OK，然后 stop 正常结束该冒烟会话。

Ubuntu 后台会话验证通过：从 Mac 的 SSH 命令运行 Claude --bg，启动 SSH 连接正常退出；另开 SSH 连接使用 logs 读取到实际回复 SBX_UBUNTU_BACKGROUND_OK 和完成时间，随后 stop 成功结束冒烟会话。验证使用专门的配置目录，不包含任何功能开发任务。

实际开发优先使用安装版本自带的入口：

```text
claude --bg <已明确范围的任务>
claude agents
claude logs <会话 ID>
claude attach <会话 ID>
claude stop <会话 ID>
```

新工作树先完成正常目录信任。需要命令授权时由 Mac Codex 查看实际提示并处理；需要用户本人时明确说明。用户已于 2026-10-21 授权 bypassPermissions、Opus 5.5 高强度与 550K 自动压缩，见下方验证记录。

这是本项目开发期间的 SSH／CLI 协作方式。让第三方用户从 SBX API／MCP 派发任务属于 B2／B4，尚未因这些脚本而实现。终端断开后的短冒烟也不等于已验证跨天、休眠或重启恢复；产品级长期验收在 B6。

## Ubuntu 系统包后的接续顺序（历史记录，已执行）

唯一仍需用户的操作是 sudo 系统安装。两家订阅不需要重复登录。

```bash
# 在 Mac 终端，由用户输入 Ubuntu sudo 密码
ssh -t ubuntu-dev 'bash /home/dev/.cache/sbx-setup/install-system.sh'

# 下面由 Mac Codex 接着执行，不需要 sudo
ssh ubuntu-dev 'bash /home/dev/.cache/sbx-setup/ubuntu-bootstrap-after-system.sh'
```

续办脚本：

1. 核查系统工具、Node 22+、PostgreSQL server binaries。
2. 新建工作区；若有本轮 oss-environment.bundle，则导入准备分支及交接资料。
3. 使用既定主干基线；已有工作区必须来源匹配且干净，保留分支和已有文件，不 reset。
4. 安装冻结 Python、Console、Docs 依赖；运行 make lint 与 make test。
5. 将结果写入 Ubuntu 的 /home/dev/.cache/sbx-setup/logs/，由 Codex更新本文件；出现基线失败先诊断。

Mac 的 Git remote ubuntu 指向 ubuntu-dev:/home/dev/Developer/sbx-agent。Ubuntu 的 Claude 在独立分支／worktree 中开发，Mac 使用 git fetch ubuntu 取回结果后审查、整合和统一推送。Ubuntu 不需要保存 Mac 的 GitHub 令牌。

每项任务记录工作包、基线、分支、工作树、Claude 会话 ID、日志与验收结果。第一批先 ENV/B1，再推进 B2 和 B4；B5 随接口变化验收。客户端和 SaaS 的暂缓决定保持有效。

## 原工作成果

原 Mac 主工作区与审计文件保留。未提交 SwiftUI 原型的 26 个源文件已复制为独立本地快照：

- 工作树：/Users/soren/Developer/sbx-agent-prototype-archive
- 分支：archive/macos-prototype-20261021
- 提交：802fb5779122e79265ad055d02dba1e83362612b

原型原目录保持原状。这个快照不决定新客户端设计，也不合入当前后端开发分支。

## 本轮开发状态

本轮只配置环境、验证基线、保存原型与准备任务，没有启动 B1 等功能实现。（后续：B1 已由另一会话在独立工作树进行，不包含在本 ENV 分支中。）脚本及任务文档已由独立审查确认无本轮范围内的 P0/P1；这不代替 Ubuntu 实机执行结果。

Mac Codex 接手后读取 CODEX_HANDOFF.md 的提示词，复核现场，再继续唯一未完成的系统安装链和 Linux 基线。（历史：系统安装链与 Linux 基线已于 2026-10-21 完成，见下方验证记录。）

Mac 上已安装的 Codex CLI 已独立只读检查根目录两份入口与三份交接文档，正确复述分工、唯一 sudo 待办、首批工作包和未通过测试，返回 SBX_HANDOFF_READABLE。该检查没有开始安装、认证或功能开发；输出保存在用户的本地配置工作记录中。

## 2026-10-21 Ubuntu 环境验证记录（ENV 收尾）

范围：只记录环境与基线，不含 B1 实现。ENV 分支为 `chore/oss-env-validation-20261021`，基线 `aa532f2a`。本分支的最终提交 SHA 与结果保存在 `/home/dev/.cache/sbx-oss-20261021/results/env-complete.txt`（不在 Git 中）。

### 工具与仓库

- Ubuntu 26.04.1 LTS；git 2.53.0；Node 22.23.3；npm 10.9.9；uv 0.13.4；Python 3.12.15；PostgreSQL 18.6（apt）；Docker CLI 29.1.3；Compose 2.40.3；gh 2.46.0；tmux 3.6；ripgrep 15.1.0。
- 正式 checkout `/home/dev/Developer/sbx-agent`：分支 `setup/oss-environment-20261021`，HEAD `aa532f2a`，工作区干净，由续办脚本创建。
- ENV 工作树 `/home/dev/Developer/sbx-env-20261021`：分支 `chore/oss-env-validation-20261021`。
- `uv lock --check` 通过（57 个包）。
- `scripts/dev/` 中的 doctor、续办与系统安装脚本，与运行时使用的副本逐字一致。
- `scripts/dev/doctor.sh` 指向正式 checkout 时结果为 READY。它不验证订阅或 Docker daemon，也不运行测试。Playwright 浏览器缓存存在，但未执行浏览器测试。

### Docker

- `docker version` 的 server 部分报错 `permission denied while trying to connect to the docker API at unix:///var/run/docker.sock`。socket 属 `root:docker`，`dev` 不在 `docker` 组。未修改用户组或 sudoers。Docker 资源不在本批次。

### 续办脚本与基线测试

- 续办脚本 `ubuntu-bootstrap-after-system.sh`（PID 19584）只运行一次，日志为 `/home/dev/.cache/sbx-setup/logs/after-system-20261021T102218Z-19584.log`。
- `uv sync --frozen`、Console 与 docs-site 的 `npm ci` 通过。
- `make lint` 通过。
- `make test` 首次结果：`1 failed, 567 passed, 1 skipped`。唯一失败项为 `tests/integration/api/test_machine_slots_api.py::test_official_login_flows_to_ready_without_a_second_step`。
- 失败原因：断言用字符串前缀 `2099-01-01` 比较过期时间，但本机会话时区为 America/New_York，PostgreSQL 返回同一时刻 `2098-12-31T19:00:00-05:00`。
- ENV 修复：该断言改为比较带时区的 datetime 实例（`datetime(2099, 1, 1, tzinfo=UTC)`）。没有修改产品代码，没有强制 TZ=UTC，也没有放宽为日期比较。
- 修复后，该用例在 America/New_York 下通过；`make lint` 通过；完整 `make test` 只运行一次，结果为 `568 passed, 1 skipped`，退出码 0。
- `test_keepalive_holds_the_claim_through_a_slow_call_and_stops_after_it`：首次全量运行未出现在失败列表中；修复后单独运行 PASSED。Linux 上未复现此前 Mac 记录的续约失败。这只是一次复现，不能作为稳定性结论，相关边界仍由 B1 负责。
- `make docs-check` 通过：124 个页面，check-links ok。注意：该目标只构建 docs-site 并检查其链接，不检查 `docs/handoff/` 中的文档。

### 订阅与 CLI（不含凭证）

- Codex 0.163.0：`codex login status` 显示使用 ChatGPT 登录。先前最小调用返回 SBX_CODEX_AUTH_OK，本次未再次调用。
- Claude Code 2.1.306：`claude auth status` 显示 claude.ai 登录、firstParty、订阅类型 pro。当前环境未设置 `ANTHROPIC_API_KEY` 与 `OPENAI_API_KEY`。
- 模型探测（先前完成，证据在 `~/.cache/sbx-setup/`，不入 Git）：Opus 5.5 high 通过。Haiku 5.5 带 Opus 5.5 advisor 的探测中，transcript 出现 `advisor_message`，模型为 `claude-opus-5-5`。探测输出中的费用字段是按标价计算，不作为计费依据，因此不在此记录。
- 用户级 Claude settings（只记录键名与取值）：`model` 为 `claude-opus-5-5`；`modelSettings.claude-opus-5-5.effortLevel` 为 `high`；`autoCompactWindow` 为 550000；`permissions.defaultMode` 为 `bypassPermissions`；`skipDangerousModePermissionPrompt` 为 true。改动前备份位于 `~/.cache/sbx-setup/claude-settings-before-oss-20261021T102251Z.json`。
- 运行时读回：没有非交互的官方读回命令（`claude config` 不是子命令），也未执行会话内 `/autocompact`。可见的运行时证据是 B1 会话进程参数包含 `--model claude-opus-5-5 --effort high --autocompact 550k`。
- 后台会话：帮助中存在 `--bg`、`agents`、`logs`、`attach`、`stop`、`respawn`、`rm`。`claude agents --json` 当前列出运行中的 `sbx-b1-opus55`（ID 64dbd49c）以及本 ENV 会话。
- SSH 断线重连：本次在 Ubuntu 端执行，无法从 Mac 发起重连测试。沿用此前 Mac 的记录（`ubuntu-dev` SSH 与后台会话已验证），本次未重新验证。

### 时区观察（产品侧，未改动）

- 协调者确认：PostgreSQL 返回的 `code_expires_at` 带主机时区偏移，如 `2098-12-31T19:00:00-05:00`。时间点不变，但字符串形式随主机时区变化。
- 本次只修了测试断言。API 是否统一输出 UTC，属于产品契约问题，交 B1／B5 决定。

### 未完成与阻塞

- Docker daemon 访问未通过。这是用户决定事项，本批次不处理。
- 完整后端测试仍需在 Linux 上运行。Mac 上的既有限制与 Console 测试的既有不稳定性未改动，记录见上文。
- 本 ENV 分支未推送、未建 PR，由 Mac 完成。
- `CODEX_HANDOFF.md` 基线段落中"当前只剩 Ubuntu sudo 系统包安装需要人工"一条仍是历史内容，本次未改，留给 Mac 合并时统一整理。

### 下一步

- Mac 从 `ubuntu` remote 取回 `chore/oss-env-validation-20261021`，审查、推送并开 PR。
- B1 由 Ubuntu Claude 会话 `sbx-b1-opus55` 在 `/home/dev/Developer/sbx-b1-20261021` 负责，本 ENV 分支不包含其实现。
- B4 等待协调者依据 B1 的共享契约派发。
