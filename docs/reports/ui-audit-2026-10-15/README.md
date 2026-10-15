# SBX staging Issue 附件包

环境：https://staging.sbx-agent.com · 构建 `018a0e02370bcae3c3f17ec97aa91b105d232f8b` · 测试日期 2026-10-15（America/Los_Angeles）。

按 4 个合并 Issue 提交；AUD 目录仅作为组内证据子项。README 包含复现、影响、建议与附件说明。JPG 为真实浏览器截图；MP4 为截图关键帧回放，固定每帧 3 秒，没有连续屏幕录制、鼠标轨迹或真实时间承诺。页面内容未经过重绘或生成式修改。captions.srt 为步骤说明。

已排除未完成渲染的画面与不正确的刷新对照标签。截图中的会话为本次隔离测试；没有完整密码、token 或 API key。原始证据目录不整体打包，只包含本目录的精选附件。

使用：复制 [4 个合并 Issue 草稿](groups/README.md) 中对应组的正文，在 Issue 编辑器拖入该组的 JPG / MP4；文件链接不会自动变成 GitHub 附件。四组均已发布，证据已链接到固定 GitHub commit。

## 合并提交计划

- [G01：修复工作区文件编辑、终端重试与变更审阅链路](groups/01-workspace-files-terminal-changes.md)（AUD02、AUD04、AUD05、AUD08、AUD09、AUD11、AUD16、AUD19）
- [G02：修复会话执行取消、子会话与历史生命周期的一致性](groups/02-session-lifecycle-and-history.md)（AUD01、AUD07、AUD10、AUD12、AUD13、AUD14、AUD18、AUD22）
- [G03：保留项目完整配置并对齐检查、服务与交付能力](groups/03-project-config-services-delivery.md)（AUD03、AUD06、AUD15、AUD17）
- [G04：让 API key 签发可安全重试，并提供明确的权限选择](groups/04-api-key-issuance-and-scopes.md)（AUD20、AUD21）

## 组内证据明细（不是独立 Issue）

| 编号 | 截图 | MP4 | 证据说明 |
| --- | ---: | ---: | --- |
| [AUD01](AUD01/README.md) | 1 | 0 | 未重新触发故障。仅包含原现场脱敏日志与恢复后截图；recovery-only.jpg 不能证明故障时的 UI 状态。 |
| [AUD02](AUD02/README.md) | 0 | 0 | 本地隔离文件读写复现，无 staging UI 录像；不以无关截图替代数据完整性证据。 |
| [AUD03](AUD03/README.md) | 1 | 0 | 截图仅证明表单字段范围；隐藏配置被重置的风险由源码确认，尚未对业务项目重演。 |
| [AUD04](AUD04/README.md) | 2 | 1 | 本次 staging 实测截图；具体动作和页面 URL 见 capture-log.json。 |
| [AUD05](AUD05/README.md) | 5 | 1 | 01–03 是本次新文件创建与旧列表的关键帧。刷新对照因 executor 已释放而中断，未纳入回放。prior-before/after 是上一轮独立实测对照，时间与本次不同，不能拼接为同一次操作。 |
| [AUD06](AUD06/README.md) | 1 | 0 | 截图只展示检查命令输入格式；argv 错误由源码函数执行和存储配置确认，截图不证明检查运行结果。 |
| [AUD07](AUD07/README.md) | 2 | 0 | 两张截图分别展示历史消息的不同可见区域，属于状态证据，不是本次重新启动队列任务的连续录制。 |
| [AUD08](AUD08/README.md) | 3 | 1 | 本次 staging 实测截图；具体动作和页面 URL 见 capture-log.json。 |
| [AUD09](AUD09/README.md) | 3 | 1 | 本次 staging 实测截图；具体动作和页面 URL 见 capture-log.json。 |
| [AUD10](AUD10/README.md) | 2 | 1 | No ChangeSet 通过选择控件执行后仍显示原值；关键帧中不会显示完整下拉操作过程，具体动作见 capture-log.json。 |
| [AUD11](AUD11/README.md) | 2 | 1 | 本次 staging 实测截图；具体动作和页面 URL 见 capture-log.json。 |
| [AUD12](AUD12/README.md) | 2 | 1 | 本次 staging 实测截图；具体动作和页面 URL 见 capture-log.json。 |
| [AUD13](AUD13/README.md) | 1 | 0 | 当前截图只证明归档页头 Idle 与正文 archived 的矛盾。筛选菜单截图在渲染未完成时截取，已排除；默认列表过滤缺陷另有源码与上一轮实测记录。 |
| [AUD14](AUD14/README.md) | 0 | 0 | 源码确认的超过 50 条场景，未批量制造会话，因此没有声称能证明第 51 条行为的截图/录像。 |
| [AUD15](AUD15/README.md) | 2 | 1 | 本次 staging 实测截图；具体动作和页面 URL 见 capture-log.json。 |
| [AUD16](AUD16/README.md) | 0 | 0 | 源码确认的故障重试风险，未在 staging 注入响应丢失，未制作误导性 UI 录像。 |
| [AUD17](AUD17/README.md) | 2 | 1 | 回放展示 Services 空状态与新项目表单的配置缺口；不代表声明服务的 Start/Stop/Logs 已验证。 |
| [AUD18](AUD18/README.md) | 3 | 1 | 本次 staging 实测截图；具体动作和页面 URL 见 capture-log.json。 |
| [AUD19](AUD19/README.md) | 2 | 1 | 本次 staging 实测截图；具体动作和页面 URL 见 capture-log.json。 |
| [AUD20](AUD20/README.md) | 0 | 0 | 源码确认的签发重试风险，未额外签发凭证来复现，无对应 UI 视频。 |
| [AUD21](AUD21/README.md) | 2 | 1 | 页面可见的 key 前缀是产品公开展示的部分前缀，不包含完整可用凭证。本轮没有再次签发密钥。 |
| [AUD22](AUD22/README.md) | 2 | 1 | 本次 staging 实测截图；具体动作和页面 URL 见 capture-log.json。 |


## 已发布的 GitHub Issue

- [G01 / #227：修复工作区文件编辑、终端重试与变更审阅链路](https://github.com/soren-labs/sbx-agent/issues/227)
- [G02 / #228：修复会话执行取消、子会话与历史生命周期的一致性](https://github.com/soren-labs/sbx-agent/issues/228)
- [G03 / #229：保留项目完整配置并对齐检查、服务与交付能力](https://github.com/soren-labs/sbx-agent/issues/229)
- [G04 / #230：让 API key 签发可安全重试，并提供明确的权限选择](https://github.com/soren-labs/sbx-agent/issues/230)
