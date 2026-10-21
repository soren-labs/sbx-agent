# SBX 开源版：Mac Codex 接手说明

日期：2026-10-21。状态：Mac 工作区及两机订阅调用已验证；Ubuntu 系统开发包仍待一次 sudo，完整 Linux 后端验收尚未完成。

## 本轮范围与接手原则

本轮执行范围是配置 Mac／Ubuntu 开发环境、接入开发用订阅并交接任务。环境配置代理不因本文件存在而启动大规模产品开发。后续产品任务交给 Mac 上的 Codex 接手组织，功能开发优先由 Ubuntu 上的 Claude Code 执行，Codex 独立验收。

用户本次所说的“Cloud”按 **Claude Code** 理解，指功能开发执行者；不表示恢复 SaaS 或优先购买云资源。

用户最新决定优先于旧计划：

- 开源优先，继续使用 `soren-labs/sbx-agent`，SaaS 暂缓。
- 先完成服务端、外部 MCP 和 API；客户端等待用户使用 Claude Design 等重新设计。
- 保留 Mac 上已有 SwiftUI 原型，不擅自重写、接入或发布客户端。
- 沿用一套开源核心；不因 SaaS 暂缓删除持久化、恢复、API 鉴权或凭证隔离。
- 不把原型中的个人版架构草案直接视为另造一套服务端的授权。
- Mac Codex 负责分配、整合、验收，可使用 Computer Use 协助官方登录；需要用户本人操作时及时说明，并继续不受影响的工作。

## 接手前先读

1. 当前工作区的 `AGENTS.md`，并确认所在分支及已有未提交修改。
2. 本文件与 `OSS_BACKEND_TASKS.md`。
3. 配置负责人补齐的环境状态和验证记录。
4. `docs/specs/unified/` 中与当前工作包有关的已实现约定；架构背景参考 `docs/architecture/unified/`。
5. 当前任务涉及的代码和最近 PR。`docs/archive/` 仅为历史；任务范围以用户指示及本次工作包为准。

遇到旧文档与用户最新目标冲突时，按最新目标限定范围并在任务交接中记录；不要悄悄扩大工作。

## 环境交接表：已完成与待收尾分开记录

以下状态来自配置负责人本轮实测进展。“进行中”“待安装”“待用户”不得直接改为“通过”。只记录版本、路径和认证结果，不记录凭证；接手时复核仍在运行的检查，避免重复启动。

| 项目 | 实测结果 |
| --- | --- |
| Mac 正式工作区、分支、基线 | `/Users/soren/Developer/sbx-agent`；`setup/oss-environment-20261021`；基线 `c0aed37d731121a39c7c41eb4ba47229d8fbe081` |
| Ubuntu 工作区 | `/home/dev/Developer/sbx-agent` **尚未 clone**。续办脚本将创建同名准备分支，并导入 Mac 的 Git bundle，带上这些交接文件 |
| Git 基线与交付 | 本轮以 `c0aed37` 为基线；环境工作保存在本地准备分支，不自动更新生产。接手时用 `git log -1` 读取实际交接提交 |
| Mac → Ubuntu SSH | 别名 `ubuntu-dev`，已反复新建 SSH 会话并完成远程命令与订阅调用；使用现有 SSH key 和主机记录 |
| Mac 工具 | Python **3.12.15**、uv **0.13.1**、Node **24.22.0**、npm **11.19.1**、PostgreSQL **17.11** 已安装 |
| Ubuntu 用户级工具 | Python **3.12.15**、uv **0.13.4**、Node **22.23.3**、npm **10.9.9** 已就绪 |
| Ubuntu 系统依赖 | Git／PostgreSQL／Docker 等等待用户执行 sudo 安装脚本；不记为开发环境全部就绪 |
| Mac Codex | **0.162.1**；ChatGPT 订阅已登录；真实最小调用 **通过** |
| Mac Claude Code | **2.1.292**；Claude Pro 已登录；真实最小调用 **通过** |
| Ubuntu Codex | **0.163.0**；ChatGPT 订阅登录、真实最小调用 **通过**；使用官方支持的远端 auth cache 迁移，凭证只保存在设备认证目录 |
| Ubuntu Claude Code | **2.1.306**；用户已完成 Claude Pro 官方授权，真实最小调用 **通过** |
| Mac 仓库依赖 | `uv sync --frozen --python 3.12`、Console 与 Docs 的锁定 `npm ci` **通过** |
| Mac 开发检查 | `make lint`、Console typecheck、Console build **通过**。完整后端测试受 Linux runtime 限制；Console 测试有不稳定失败，见下方基线说明 |
| Ubuntu 仓库依赖与检查 | 尚未开始；需系统依赖、clone 与分支准备完成后执行 |
| 长任务持续运行、重连 | Mac 已验证 Claude 原生 `--bg` 返回会话 ID，另一次命令可读取完成输出，并已停止冒烟会话。Ubuntu 的持续任务验证状态见 `ENVIRONMENT_NOTES.md` |
| 剩余人工步骤 | **一项：Ubuntu sudo 安装系统开发包**。两机两家开发 CLI 的订阅调用均已通过，无需重复登录 |

**环境机登录成功不等于 SBX 产品已经支持该订阅。**开发工具直接登录的验证，与用户从 SBX 发起订阅任务的端到端验证，是两个不同交付项。

开发机的 CLI 版本也不等于 SBX 运行镜像的锁定版本；环境准备不自动修改产品中的 harness 版本声明。

## 当前需要用户完成的一项操作

Ubuntu 的系统开发包需要本机 sudo 密码。在 Mac 终端执行下面第一条命令，按系统提示输入；密码不用发到聊天。第二条命令不需要 sudo，可由接手的 Mac Codex 执行。

```bash
ssh -t ubuntu-dev 'bash /home/dev/.cache/sbx-setup/install-system.sh'
ssh ubuntu-dev 'bash /home/dev/.cache/sbx-setup/ubuntu-bootstrap-after-system.sh'
```

第一条安装 Git、编译工具、PostgreSQL、Docker／Compose 等，不修改 sudoers 或用户组。第二条创建工作区、导入本轮交接、安装锁定依赖并执行 Linux lint／test；日志保存在 Ubuntu 的 `/home/dev/.cache/sbx-setup/logs/`。若已有工作区带改动，脚本保留现场并停止，不能 reset。

Claude 官方授权已完成；只有实际复查显示失效时才使用 `claude auth login --claudeai` 重新授权。Codex、Claude 的认证状态与真实调用记录均已验证，不把任何令牌或授权码放进 Git。

## 环境入口与基线说明

- `bash scripts/dev/doctor.sh`：只读检查工具与依赖，不代表订阅或产品已验收。
- `bash scripts/dev/run.sh make lint`：在当前 checkout 中设置开发 PATH 后运行命令；Mac 会加入 Homebrew PostgreSQL 17 的路径。
- 完整 `make test` 在 Ubuntu 运行。Mac 的 `runtime/daemon/process_anchor.py` 调用 Linux `prctl`，`supervisor.py` 读取 `/proc`；已通过无凭证最小复现确认 macOS 的 `managed_popen` 启动失败。Mac 全套运行中止时为 184 passed／62 failed，**不是已通过的后端基线**。
- Console 现有测试首次 192 passed／1 timeout；减少 worker 后 189 passed／4 failed。构建与类型检查通过，但测试不能记成全绿。客户端设计暂停，本轮记录问题，没有改产品测试或用放宽断言来通过。
- 控制面／数据库定向测试及脚本最终检查见 `ENVIRONMENT_NOTES.md`。
- Mac GitHub CLI 已有用户登录。Ubuntu 可从公开 origin 拉取并作本地提交；Mac 从 `ubuntu` Git remote 取回分支后统一审查和推送，不向 Ubuntu 复制 GitHub 令牌。
- 本次会话的本地浏览器控制入口未连接。Mac Codex 的 Computer Use 权限需在其实际会话中使用并验证；不能把 CLI 登录成功视为 Computer Use 已验收。

## 已知基线，需与接手时现状对照

此前核查的 GitHub 主干为 `c0aed37d731121a39c7c41eb4ba47229d8fbe081`（2026-10-16，PR #235）。这不是对接手时最新 HEAD 的保证。

- 已有统一 API、Python SDK／CLI、耐久 Session／Turn、任务队列、日志和 checkpoint 基础。
- 已有 Local／Modal 执行器；Local 不能直接算已交付的任意工作站／VPS 纳管。
- Codex、Grok Build、Antigravity 订阅已有 Modal 路线；Claude Code／OpenCode 当前 harness 的 API key 路线不能算订阅接入。
- 内部任务工具网关可以复用，但未交付外部 Codex／Claude 可直接配置的成品 MCP 服务。
- `LICENSE` 仍是待选择的占位文本。许可证由用户决定，列入公开发布门槛，配置和功能开发无需因此停下。
- Mac 旧主工作区曾位于 `/Users/soren/Documents/SBX-Network/tmp/sbx-agent`，在 `018a0e0` 且有未跟踪审计资料；不要覆盖这些资料。
- Mac 原型原目录 `/Users/soren/Documents/SBX-Network/tmp/sbx-agent-macos-prototype` 保留。源文件已另存到 `/Users/soren/Developer/sbx-agent-prototype-archive`，本地分支 `archive/macos-prototype-20261021`，快照提交 `802fb5779122e79265ad055d02dba1e83362612b`；不把该分支合入当前后端工作。
- 原型是模拟运行与连接，不能用来证明服务端、订阅或远端资源已经接通。

## Mac Codex 如何组织 Claude 开发

1. 用 `ssh ubuntu-dev` 核对机器和工作目录，读取该目录的真实 Git 状态；不要把任务误发给 WSL 或生产机器。
2. 根据 `OSS_BACKEND_TASKS.md` 选择依赖已满足的工作包。先验证现有行为，再让 Claude 修改。
3. 为执行者准备独立工作区和分支。已有未提交改动先保留并确认归属，不 reset、不覆盖。
4. 将工作包作为文件交给 Ubuntu 上的 Claude Code，使用本机已验证的官方 CLI 入口和订阅认证。具体命令以安装版本的帮助为准，不猜测标志。
5. 记录任务 ID、负责人、工作区、分支、基线、执行会话和状态。派发长任务前，先补齐并验证持续运行与重连方式；未验证时不能承诺 SSH 断开后会继续。
6. Claude 完成后交付实现、验证结果和剩余问题；Mac Codex 在独立审查会话验收实际分支及累计改动。
7. 验收通过后由唯一整合负责人按依赖顺序合并到整合分支。生产发布不属于普通开发工作包。

### 每次派发必须说清的内容

| 字段 | 内容 |
| --- | --- |
| 目标 | 用户能完成什么操作，为什么需要它 |
| 基线 | 仓库、工作区、分支、基线提交、当前相关行为 |
| 范围 | 本工作包负责的行为，以及已决定暂缓的内容 |
| 约定 | 依赖任务、共享状态、对 API／SDK／MCP 的影响 |
| 验收 | 可复现的正常流程、关键异常和需要保留的现有能力 |
| 交付 | 提交／PR、检查结果、实际运行证据、兼容说明、阻塞 |

## 协作边界

- Mac Codex 是唯一任务统筹和整合负责人；Claude 执行功能开发，Codex 负责验收。
- 工作包之间允许合理并行。共享约定尚未确定时，先完成依赖，不用增加代理数量掩盖不确定性。
- Claude 可以在已分配工作包内拆分并行子任务；跨工作包的拆分、共享约定和并发安排由 Mac Codex 统筹，避免多个执行者覆盖同一工作区。
- 同一 Mac 桌面的 Computer Use 轮流使用，避免多个会话同时操作焦点、浏览器与授权页面。
- 现有生产环境、真实业务数据、正式发布和账户计费配置不作为开发试验场。
- 订阅使用官方认证或官方支持的远端凭证迁移流程。不能把真实令牌、登录配置、密码、授权码写进任务文件、提交、日志或报告。
- 不自动把订阅任务切换到计费 API key 路线来“通过验收”；需如实记录实际认证模式。
- 工具能力、套餐限制或官方登录流程需要用户操作时，记录具体等待项；其他独立工作继续。

## 验收与停止条件

依照当前 `AGENTS.md` 执行相关检查：后端至少遵守其 lint／test 门槛，文档与外部约定变化同步维护对应检查。只为具体风险补充验证，不无限重复测试。

Codex 验收必须区分：

1. 代码／文档声明存在。
2. 自动化测试通过。
3. 真实指定工具、认证和资源的流程通过。

只有第 3 项能支撑兼容表中的“已真实验证”。环境不可用造成未验证，要明确写出，不能用假 provider 结果替代。

P0／P1 按仓库规则处理，需给可复现条件和影响；非阻塞建议集中记录，避免无限审查循环。共享 API、CLI、MCP 必须对同一任务呈现一致的结果和可用操作。

## 每个工作包的交接记录

- 工作包 ID、状态、负责人、分支／提交／PR。
- 完成的用户行为，以及本包仍未覆盖的边界。
- 验证命令、环境、实际结果；真实订阅验证使用了哪种工具和哪类资源。
- 是否新增用户可见要求或改变现有行为。
- 未完成项、需要人工介入的具体步骤、下一位执行者应从哪里继续。
- 若任务仍在运行，记录可重连的会话标识和查看方法，不记录凭证。

任务状态建议统一记录为：待开始、进行中、等待用户、等待依赖、待验收、已验收、阻塞。任务状态文件本身不得被当成产品数据库或另造的调度服务。

## 可直接交给 Mac Codex 的接手提示词

正式交接目录：`docs/handoff/oss-2026-10-21/`。

```text
请接手 /Users/soren/Developer/sbx-agent，先读取根目录 AGENTS.md，以及
docs/handoff/oss-2026-10-21/CODEX_HANDOFF.md 和 OSS_BACKEND_TASKS.md。
先核对 setup/oss-environment-20261021 的真实状态，保留所有已有修改。
当前阶段先完成 ENV 环境收尾：接续已有检查，不重复启动仍在运行的安装／测试；
确认 Ubuntu sudo 系统依赖的实际结果；两机两家订阅已验证，不要重复登录，
验证 SSH、仓库依赖和两家 CLI 的真实订阅调用，再更新交接状态。
若必须由用户完成密码或官方授权，及时说明具体步骤，并继续不受影响的工作。

环境可用后按既定优先级安排 B1 自托管服务端基线，再推进 B2 常驻资源
和 B4 外部 MCP 的第一条真实流程；B5 API 一致性随相关改动同步验收。
功能实现优先交给 Ubuntu 上的 Claude Code；你负责拆分、整合与独立验收。
每次只派发依赖已满足且边界明确的工作包，不自动并行启动整张产品路线图，
工作包内可合理并行，跨包拆分由你统筹；先给出本批目标和验收条件，再执行。
客户端等待我用 Claude Design 等重新设计，现有 SwiftUI 原型仅保留；SaaS 暂缓。
沿用现有核心，不另造平行服务端，不自动发布生产，也不为通过验证切换到计费 API key。
报告事实：完成了什么、哪些真实验证通过、哪些仍待处理，以及下一步如何接续。
```
