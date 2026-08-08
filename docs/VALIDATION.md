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
- [x] 完成 `initialize -> terminate`, 创建 13 个 Web API route, 停止 ticker, 清理 route.
- [x] 通过 57 项 `unittest`.
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
- [x] 验证旧版 `entries` 表迁移新增证据字段和 `entry_evidence_batches` 表可重复执行且保留历史条目.
- [x] 验证最后一个学习时间点不能删除, 需要停用时不破坏调度状态.
- [x] 验证人物 `user` scope 输出安全归一化为 `person`, 跨 2 天、至少 3 条证据才进入 `trial`.
- [x] 验证 proposal 只能引用当前 batch 的真实入站消息 ID, 同一消息跨 batch 不重复累计证据.
- [x] 验证同批主人指令不会提升无关 proposal, 自动 `global profile_fact/milestone` 被拒绝.
- [x] 验证学习写入后立即刷新 runtime snapshot, 过期条目退出注入, 90 天陈旧自动草稿归档.
- [x] 验证 Plugin Page 桌面/390px 移动端无横向溢出, 弹窗可滚动, 暗色主题变量和禁用态可读.
- [x] 验证 Dashboard toast 反馈、异步请求期间控件禁用、动态状态 `aria-live` 和 reduced-motion.
- [x] 验证同一插件实例重复 `initialize()` 不重复创建 writer、ticker 或 Web API route.
- [x] 验证 Reviewer batch 数据库 compare-and-set 抢占, 并发执行同一 batch 只调用一次 Provider.
- [x] 验证启动时非法 SQLite runtime flag 自动修复为安全默认值.
- [x] 验证 `growth_memory_note` 已注册到 AstrBot LLM tool registry, 只写候选队列, 同一消息确定性去重, 普通成员权限拒绝和敏感内容拦截.
- [x] 验证 `/providers` API 从 `context.get_all_providers()` 读取 Chat Provider, 去重排序, WebUI Extractor/Reviewer 下拉可恢复已保存值并标记已下线 Provider.
- [x] 完成 2,000 条上下文和 40 个关键事件的有界 writer 压力探针, 队列清空且 SQLite 无异常.

## 京东云基础部署证据

2026-08-08 已将 GitHub `main` 的 `34f067ac71b44b4c390816e81059b11bd188f4f6` 部署到 `/opt/1panel/apps/astrbot/astrbot/data/plugins/astrbot_plugin_growth_memory`, 当前为 `v0.2.3`; `v0.3.0` 的主动记忆和 Provider 下拉改动待本轮发布后追加证据.

- 变更前备份: `/opt/1panel/apps/astrbot/astrbot/data/backups/astrbot_plugin_growth_memory/20260808-213108-v0.2.3-predeploy/`, 含旧插件、配置和 SQLite 在线备份.
- 插件归档 SHA-256: `16753700be94e84638b135808c3021fec18e5b4df95e351f62efd86c56312394`.
- AstrBot 4.27.1 日志确认加载 `v0.2.3`、作者 `木有知`, 完成 `GrowthMemory.initialize`, 并在 reload 时移除旧的 9 个 handler 后重新注册.
- `astrbot` 重启计数为 `0`, 状态为 `running`; NapCat 未重启且仍为 `running`.
- 插件 SQLite 位于 `/opt/1panel/apps/astrbot/astrbot/data/plugin_data/astrbot_plugin_growth_memory/growth_memory.db`, reload 后 `PRAGMA integrity_check=ok`, `journal_mode=wal`.
- 受保护 Plugin Page API 未认证访问仍返回 `401`; 认证后 `state/settings/runs/entries` 均返回 `200`.
- `v0.2.3` 真实 `run-now` 返回 `processed=5`, 最近 run 为 `succeeded`, `request_count=2`, `1876 estimated input tokens / 5170 output tokens`, queue `0`, `degraded=false`, `last_error=""`.
- 本轮新增 `entry_evidence_messages=8`, 每条为 proposal 实际引用的入站 `message_row_id`; 当前 2 条人物草稿, 历史 3 条全局知识均保持 `archived`.
- 学习完成后再次插件级 reload, target 5 个、`03:00/20:00`、主人身份、Provider、预算 `8/8`、runtime snapshot 和 evidence links 均恢复.

上述是版本、运行、reload 和数据恢复证据. Dashboard 页面需要使用现有 AstrBot 管理账号登录后打开 `小本本记下来`; 未在服务器上绕过认证重置密码.

## 部署后必须验证

- [x] 在京东云 AstrBot WebUI 受保护 Plugin Page API 确认插件页面、管理命令和全部 hook 已被 AstrBot 4.27.1 发现; 浏览器页面需要管理账号登录后查看.
- [ ] 在一个测试 QQ 私聊发送 `/进化`, 验证 target 落库且普通回复不受影响.
- [ ] 在一个测试群观察未 @ Bot 的 context 消息只捕获、不触发 LLM 回复.
- [ ] 完成一次真实流式和一次非流式问答, 验证 anchor question/answer.
- [x] 执行一次真实 Extractor + Reviewer run, 核对 provider 请求数和 token 预算.
- [ ] 在下一轮聊天检查条目实际注入并确认没有泄露 `behavior_only` 内容.
- [x] 部署 `v0.2.3` 后 reload 插件, 验证 target, `03:00/20:00` schedule, runtime flags、条目、evidence links 和预算均恢复.
- [ ] 连续运行 24 小时, 检查 event loop latency, queue depth, WAL 大小, memory, provider 错误和 QQ 收发.

当前状态为“已部署并完成真实学习/reload 验证”; 真实 QQ `/进化` 路径和 24 小时 soak 尚未冒充完成, 仍需按上表持续观察.
