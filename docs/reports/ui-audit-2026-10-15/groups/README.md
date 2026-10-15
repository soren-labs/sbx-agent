# UI 审计：4 个合并 Issue

将 22 个 AUD 观察项收敛为 4 个提交单元。AUD 只是组内验收编号，不再按 22 个独立 Issue 提交。每份草稿自带前置数据、串联复现、修复顺序、验收清单、源码与证据边界，不依赖聊天记录。

| 提交单元 | 优先级 | 子项 | 草稿 |
| --- | --- | --- | --- |
| G01 · 修复工作区文件编辑、终端重试与变更审阅链路 | P1 | AUD02、AUD04、AUD05、AUD08、AUD09、AUD11、AUD16、AUD19 | [01-workspace-files-terminal-changes.md](01-workspace-files-terminal-changes.md) |
| G02 · 修复会话执行取消、子会话与历史生命周期的一致性 | P1 | AUD01、AUD07、AUD10、AUD12、AUD13、AUD14、AUD18、AUD22 | [02-session-lifecycle-and-history.md](02-session-lifecycle-and-history.md) |
| G03 · 保留项目完整配置并对齐检查、服务与交付能力 | P1 | AUD03、AUD06、AUD15、AUD17 | [03-project-config-services-delivery.md](03-project-config-services-delivery.md) |
| G04 · 让 API key 签发可安全重试，并提供明确的权限选择 | P2 | AUD20、AUD21 | [04-api-key-issuance-and-scopes.md](04-api-key-issuance-and-scopes.md) |

建议 G01、G02 优先，G03 同时解决项目配置完整性；G04 独立处理凭证链路。四组可由不同 agent 接手，共用的 ConversationPane、ChangesTab 和 API 类型按各自标注的边界协调，避免整文件重写。

提交时仅创建这 4 个 Issue，原 AUD 目录保留为附件和核销依据，不额外创建 22 个子 Issue。按这四个提交单元维护，AUD 编号作为组内验收项。
