# runtime/

Sandbox 内组件（镜像定义、entrypoint、Codex runner）。行为契约见 `docs/contracts/runner-cli.md` 与 Linear `SOR-30`。WP0 仅占位。

Codex 的原生执行默认通过 `codex app-server --listen stdio://` 接收真实文本 delta，转为既有 `item.started/updated/completed` 累计快照，保持 item ID。续轮恢复同一个 thread；失败不会自动重跑任务。自定义 `CODEX_BIN` 默认沿用 exec 协议；`SBX_CODEX_TRANSPORT=app-server` 可显式选择新通道，`SBX_CODEX_TRANSPORT=exec` 可选择旧通道。

发布 runner 改动时必须重新发布 runtime 镜像，再更新控制部署所选的 `SBX_IMAGE_CODEX`。仅部署控制应用不会更新已发布镜像或已启动 sandbox。并行验收使用独立镜像名、应用名和全部 Dict（含 `SBX_RUN_ACTIVITY_DICT`），不覆盖生产资源。
