# 小本本记下来验证记录

## 当前基线

- 使用 AstrBot `4.26.8`, commit `e80e01c7` 做 SDK 兼容检查.
- 使用 Python 3.12 执行离线测试.
- 保持 AstrBot Core 未修改.
- 线上只修改本插件目录和插件专属 `plugin_data`, 未修改 AstrBot Core, Compose 或 NapCat.

## 已通过

- [x] 读取 `metadata.yaml` 和 `_conf_schema.json`.
- [x] 在 AstrBot `uv` 环境导入 `GrowthMemory`.
- [x] 注册 9 个 handler.
- [x] 确认 capture filters 顺序为 `TargetCaptureFilter -> EventMessageTypeFilter`.
- [x] 完成 `initialize -> terminate`, 创建 10 个 Web API route, 停止 ticker, 清理 route.
- [x] 通过 43 项 `unittest`.
- [x] 通过 `ruff check .`.
- [x] 通过 `ruff format --check .`.
- [x] 通过 `python3 -m compileall -q .`.
- [x] 验证 SQLite WAL schema, target/schedule/entry/version/rollback/runtime flag.
- [x] 验证默认空 target、群聊/私聊隔离、Bot account 精确匹配.
- [x] 验证有界 FIFO critical overflow 进入 degraded.
- [x] 验证流式答案依赖 `on_agent_done`, 错误回答不学习.
- [x] 验证 Extractor/Reviewer 成功路径和 owner behavior rule 本地权限校验.
- [x] 验证 Reviewer 连续失败不重跑 Extractor.
- [x] 验证每日 request budget 和 input token budget.
- [x] 验证 schedule 12 小时 catch-up 和 slot 幂等.
- [x] 验证 14 天 TTL 不删除仍在处理中的 anchor 证据, 且 stale missing anchor 会被取消.
- [x] 验证注入不依赖 learning target 开关.
- [x] 验证 Web API 只使用 AstrBot Plugin Page bridge 支持的 `GET/POST`.
- [x] 验证聊天 hook 不等待 SQLite, writer 迟到时仍能绑定最终答案.
- [x] 验证 anchor hook 内存状态 TTL 回收, 不随问答次数无限增长.
- [x] 验证 Reviewer 不能依据模型自报的 trust 修改人工条目.
- [x] 完成 2,000 条上下文和 40 个关键事件的有界 writer 压力探针, 队列清空且 SQLite 无异常.

## 京东云基础部署证据

2026-08-08 已将 GitHub `main` 的 `08079280c2bcd389293f60143b5506c63d15b182` 部署到 `/opt/1panel/apps/astrbot/astrbot/data/plugins/astrbot_plugin_growth_memory`.

- 变更前备份: `/opt/1panel/apps/astrbot/astrbot/data/backups/astrbot_plugin_growth_memory/20260808-184301`.
- AstrBot 日志确认加载 `v0.2.0`、作者 `木有知`, 并完成 `GrowthMemory.initialize`.
- `astrbot` 重启计数为 `0`, 状态为 `running`; NapCat 未重启且仍为 `running`.
- 插件 SQLite 位于 `/opt/1panel/apps/astrbot/astrbot/data/plugin_data/astrbot_plugin_growth_memory/growth_memory.db`, `PRAGMA integrity_check=ok`, `journal_mode=wal`.
- 初始运行状态为 0 个学习目标、0 条消息、0 个 anchor、0 个条目, 自动创建 1 个 `03:00 Asia/Shanghai` 时间点.
- 未认证访问管理 API 返回 `401`, 说明管理接口没有裸奔.

上述是安装和启动证据, 不是生产稳定结论. Dashboard 页面需要使用现有 AstrBot 管理账号登录后打开 `小本本记下来`; 未在服务器上绕过认证重置密码.

## 部署后必须验证

- [ ] 在京东云 AstrBot WebUI 安装并 reload, 确认插件卡片和 `小本本记下来` Page 可打开.
- [ ] 在一个测试 QQ 私聊发送 `/进化`, 验证 target 落库且普通回复不受影响.
- [ ] 在一个测试群观察未 @ Bot 的 context 消息只捕获、不触发 LLM 回复.
- [ ] 完成一次真实流式和一次非流式问答, 验证 anchor question/answer.
- [ ] 执行一次真实 Extractor + Reviewer run, 核对 provider 请求数和 token 预算.
- [ ] 在下一轮聊天检查条目实际注入并确认没有泄露 `behavior_only` 内容.
- [ ] reload 插件, 验证 target, schedule, runtime flags, deferred review 和条目均恢复.
- [ ] 连续运行 24 小时, 检查 event loop latency, queue depth, WAL 大小, memory, provider 错误和 QQ 收发.

只有上述部署后检查完成, 才能把状态从“本地可加载实现”提升为“生产验证完成”.
