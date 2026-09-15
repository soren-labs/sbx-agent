# artifacts.md — SOR-83 工件与工作区交接契约

跨 Agent 工作交接的唯一受信通道：源代码只经**持久化工件包**或**已推送的
commit sha** 移动，永不复制进 prompt 文本。工件包由控制面在 sandbox 存活
时采集、存入独立于 sandbox 生命周期的 durable store，因此 teardown 之后
仍然可读、可下载、可被下游 Agent 消费。

## 1. 工作区声明（WorkspaceDecl）

`POST /v1/agents` 可携带 `workspace: {repo, base_ref, base_sha}`：

- `repo` — 控制面 clone 的仓库地址（URL 或路径）。
- `base_ref` — 期望 `base_sha` 所在的 ref。
- `base_sha` — 40-hex commit sha，run-1 必须建立在该提交之上。

prepare 顺序：clone → resolve `base_ref` → 比对 `base_sha` → 不一致即
`base_sha_mismatch`（run-1 显式 `ERROR`，agent 关闭）→ checkout 该提交。
成功后落库 `WorkspaceRecord{checkout_sha, head_sha, reviewed_head_sha}`，
`GET /v1/agents/{id}/workspace` 可查。

## 2. 工件包格式（format = `patch`，schema_version = 1）

成员（member）布局：

| 成员 | 内容 |
| --- | --- |
| `manifest.json` | canonical JSON 清单（排序键），读时校验 |
| `patch.diff` | 工作区相对 `checkout_sha` 的完整 diff（`--binary`，含未跟踪文件；被排除路径经 pathspec 滤除） |
| `repo.bundle` | `git bundle HEAD ^base`——worktree 干净且 HEAD 移动时存在；钉住精确 commit |
| `files/<relpath>` | 采集到的每个工作区文件的原始字节（仅限 git 可传输集合：tracked ∪ 未被 ignore 的 untracked） |

清单字段：`artifact_id`、`format`、`base_sha`（生产者的
`checkout_sha`，即交接链的验证锚点）、`head_sha`、`repo`、
`created_at`、`producer{agent_id,run_id}`、`files[{path,sha256,size}]`、
`tests[{command,exit_code}]`、`payloads{member→sha256}`、`warnings`。

存储写在每个成员上重校验 sha256；读取（含 `/download`）再次校验——
损坏即 `artifact_invalid`，永不静默出数据。

## 3. 边界（采集拒绝表，fail-closed）

采集与打包前，以下路径**先被拒绝再读内容**；symlink 一律不跟随：

- `.git/`、`repo/.git` 等一切 git 内部；
- 凭证与密钥材料：`.env*`、`*.pem`/`*.key`/`*.p12` 类、`auth.json`、
  `credentials*`、`netrc`、SSH/GPG 私钥；
- provider home/config 目录（`.codex/`、`.claude/`、`.config/` 等）；
- runner 簿记：`events*.jsonl`、`inbox/`、`turns/`、`session.json`、
  `runner.pid`、`.sbx-handoff/` 暂存区。

采集范围进一步限制为 **git 可传输文件**（`git ls-files -co
--exclude-standard`）：被 `.gitignore` 覆盖的测试/构建产物、内嵌 repo
内容等下游 apply 无法复现的路径不进入 manifest——否则消费侧 sha256
复核必然失败。被拒绝路径同样以 pathspec 排除出 `patch.diff`；
`git add -N` 也不会触碰内嵌 repo 边界。

另外 `forbidden_values`（账号 credential blob 内容 + 环境里的
`SBX_ACCOUNT_CREDENTIAL` / `CODEX_AUTH_JSON`）在任何允许文件或 payload
中出现即整个快照失败：**`artifact_secret`，不落库、不脱敏后照发**。

## 4. 交接（HandoffRef）

`POST /v1/agents`（需同带 `workspace`）或 `POST /v1/agents/{id}/handoff`
（agent 须 `idle` 且 sandbox 存活）接受且仅接受其一：

- `artifact_id` — 消费持久化工件。校验顺序固定：清单解码 → `repo`
  一致 → payload sha256 → 工作区 HEAD 恰等于工件 `base_sha` → 应用
  （`repo.bundle` 走 `git fetch`+checkout 精确 `head_sha`；否则
  `git apply`+commit）→ 逐文件 sha256 复核。任一环节失败显式报错
  （`artifact_not_found` / `artifact_invalid` / `checksum_mismatch` /
  `base_sha_mismatch` / `head_sha_mismatch`），工作区不被污染。
- `head_sha` — checkout 精确 commit；该 commit 必须在共享仓库可达且
  是声明 `base_sha` 的后代，否则 `base_sha_mismatch`。

消费成功后 `checkout_sha`/`head_sha` 记为工件头；链式交接由此成立——
B 的工件 `base_sha` 恰为 A 的工件 `head_sha`。

## 5. 评审钉住（reviewed_head_sha）

`POST /v1/agents/{id}/workspace/review {head_sha?}`：省略时钉住当前记录
的 `head_sha`；显式值与记录不符即 `head_sha_mismatch`——评审版本永不
被静默错标。

## 6. API 面（详见 api-v1.yaml）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/agents/{id}/artifacts` | 采集快照（`run_id`/`test_command` 可选），run 记录追加 `artifact://<id>` |
| GET | `/v1/artifacts` | 清单列表，`?agent_id=` 过滤 |
| GET | `/v1/artifacts/{id}` | 清单详情 |
| GET | `/v1/artifacts/{id}/download?member=` | 成员字节流，读时校验 sha256 |
| GET | `/v1/agents/{id}/workspace` | 工作区记录 |
| POST | `/v1/agents/{id}/workspace/review` | 钉住 `reviewed_head_sha` |
| POST | `/v1/agents/{id}/handoff` | 对存活 agent 应用交接 |

错误一律 `{error:{code,message}}`；run-1 期的 workspace/handoff 失败以
结构化 run error（`source=control`，`retryable=false`，message 含机器码）
持久化在 durable run ledger 上。
