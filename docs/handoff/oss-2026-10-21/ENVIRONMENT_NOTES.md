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
| 工作区 | /Users/soren/Developer/sbx-agent | /home/dev/Developer/sbx-agent；待系统包后创建 |
| Python | uv 管理的 3.12.15，项目 .venv 已安装 | uv 管理的 3.12.15 已安装 |
| Node / npm | 24.22.0 / 11.19.1 | 22.23.3 / 10.9.9 |
| uv | 0.13.1 | 0.13.4 |
| PostgreSQL | 17.11；Homebrew server binaries 已安装 | 待 sudo 系统包 |
| Git | 2.56.0；GitHub CLI 既有用户登录 | 待 sudo；提交身份已沿用 Mac 用户身份准备 |
| Docker / Compose | 本阶段运行资源放 Ubuntu | 安装列入系统脚本；daemon 与普通用户访问尚未验收 |
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

新工作树先完成正常目录信任。需要命令授权时由 Mac Codex 查看实际提示并处理；需要用户本人时明确说明，不使用绕过所有权限的启动参数。

这是本项目开发期间的 SSH／CLI 协作方式。让第三方用户从 SBX API／MCP 派发任务属于 B2／B4，尚未因这些脚本而实现。终端断开后的短冒烟也不等于已验证跨天、休眠或重启恢复；产品级长期验收在 B6。

## Ubuntu 系统包后的接续顺序

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

本轮只配置环境、验证基线、保存原型与准备任务，没有启动 B1 等功能实现。脚本及任务文档已由独立审查确认无本轮范围内的 P0/P1；这不代替 Ubuntu 实机执行结果。

Mac Codex 接手后读取 CODEX_HANDOFF.md 的提示词，复核现场，再继续唯一未完成的系统安装链和 Linux 基线。

Mac 上已安装的 Codex CLI 已独立只读检查根目录两份入口与三份交接文档，正确复述分工、唯一 sudo 待办、首批工作包和未通过测试，返回 SBX_HANDOFF_READABLE。该检查没有开始安装、认证或功能开发；输出保存在用户的本地配置工作记录中。
