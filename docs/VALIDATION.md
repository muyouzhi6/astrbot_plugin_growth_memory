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
- [x] 通过 65 项 `unittest`.
- [x] 通过 `ruff check .`.
- [x] 通过 `ruff format --check .`.
- [x] 通过 `python3 -m compileall -q .`.
- [x] 验证 SQLite WAL schema, target/schedule/entry/version/rollback/runtime flag.
- [x] 验证默认空 target、群聊/私聊隔离、Bot account 精确匹配.
- [x] 验证有界 FIFO critical overflow 进入 degraded.
- [x] 验证流式答案依赖 `on_agent_done`, 错误回答不学习.
- [x] 验证 Extractor/Reviewer 成功路径和 owner behavior rule 本地权限校验.
- [x] 验证 Reviewer 连续失败不重跑 Extractor.
- [x] 验证每日 request/input/output token budget, 单次 `max_tokens` 和输出预算耗尽后的 fail-closed.
- [x] 验证 schedule 12 小时 catch-up 和 slot 幂等.
- [x] 验证 14 天 TTL 不删除仍在处理中的 anchor 证据, 且 stale missing anchor 会被取消.
- [x] 验证注入不依赖 learning target 开关.
- [x] 验证成功注入实际写入 `injection_audit`, 审计只包含 session、entry ID 和 token 估算.
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
- [x] 验证 Reviewer prompt 同时包含候选引用的原始入站证据、`is_owner` 标记和现有条目, 三部分分别限额后总量不超过可配置的单次学习输入上限.
- [x] 验证精确 disabled target 覆盖 enabled wildcard, `owner/group/person/global` trigger 只参与排序且 `task` 仍必须命中 trigger.
- [x] 验证主人重复画像跨 2 天、至少 3 条证据进入 `trial`, 自动 conflict key 去重, 非 task trigger 清空, 敏感人物事实为 `owner_only`.
- [x] 完成 2,000 条上下文和 40 个关键事件的有界 writer 压力探针, 队列清空且 SQLite 无异常.

## 京东云基础部署证据

2026-08-09 已将 `v0.3.1` 兼容修复部署到 `/opt/1panel/apps/astrbot/astrbot/data/plugins/astrbot_plugin_growth_memory`.

- 根因证据: 2026-08-09 09:07:43 AstrBot 4.27.1 的 NewAPI 请求因 `properties[triggers].items: missing field` 返回 `400`, 错误来自 `growth_memory_note` 的数组参数 schema.
- 变更前备份: `/opt/1panel/apps/astrbot/astrbot/data/backups/astrbot_plugin_growth_memory/20260809-091641-v0.3.1-predeploy/`, 含旧插件、配置和 SQLite `db/wal/shm`.
- 发布包 SHA-256: `63b8439ac4448eed37b8aba00dfc4043e741adc634e86db467cdf2065a7e6c95`.
- `astrbot` 仅重启一次, NapCat 未重启; 启动日志确认加载 `v0.3.1`, 作者 `木有知`, 注册 `growth_memory_note`, 初始化恢复 `learning_targets=5`, `entries=8`, `learning_runs=8`, `learning_batches=21`.
- SQLite 重启后 `PRAGMA integrity_check=ok`, `journal_mode=wal`, `candidates=0`; `owner_identities=["aiocqhttp:1215198344"]`, `llm_note_enabled=true` 保留.
- 认证 native OneBot `send_private_msg` 返回 `retcode=0`, `message_id=1707079192`; NapCat 容器仍为 `running`, `RestartCount=0`, `OOMKilled=false`.
- 修复后的工具参数声明为跨版本兼容的 `string`, 多个触发词由逗号/换行分隔, 服务端继续规范化为内部字符串数组.
- 真实 QQ 闭环: 09:23:09 私聊入站 `测试回复`, 09:23:10 SiliconFlow embedding `200`, 09:23:18 NewAPI chat completion `200`, NapCat 随后记录 `发送 -> 私聊 (1215198344) 测什么呢` 和 `我正喝冰美式呢`.

2026-08-08 已将 GitHub `main` 的 `72550388c707851b55bbf764dc18be1d6b2268d7` 部署到 `/opt/1panel/apps/astrbot/astrbot/data/plugins/astrbot_plugin_growth_memory`, 当前为 `v0.3.0`.

- 变更前备份: `/opt/1panel/apps/astrbot/astrbot/data/backups/astrbot_plugin_growth_memory/20260808-233856-v0.3.0-predeploy/`, 含旧插件、配置和 SQLite 在线备份.
- 旧插件归档 SHA-256: `132eff0d200785b6b6dbf13aad07d218525b2e9f3b7567c1eb77a814f49a371e`; SQLite 备份 SHA-256: `c09434f71db08721b0d20f25a111a965162bef34f3b6fd01361e3781e9946c36`.
- 最终发布包 SHA-256: `febc1e86b051df01cd34f869159b9e65918fe6f7252921bffedf4f605c302bdd`.
- AstrBot 4.27.1 日志确认加载 `v0.3.0`、作者 `木有知`, 注册 `growth_memory_note`, 完成 `GrowthMemory.initialize`; 第二次重启清理了 macOS `._*` 元数据文件, 不再有 i18n 解码警告.
- `astrbot` 重启计数为 `0`, 状态为 `running`; NapCat 未重启且仍为 `running`.
- 插件 SQLite 位于 `/opt/1panel/apps/astrbot/astrbot/data/plugin_data/astrbot_plugin_growth_memory/growth_memory.db`, 重启后 `PRAGMA integrity_check=ok`, `journal_mode=wal`; `learning_targets=5`, `entries=5`, `learning_runs=7`, `candidates=0`.
- 重启后 runtime flags 保留 `llm_note_enabled=true`, Extractor/Reviewer Provider 均为 `newapigemini/gemini-3.5-flash-low`; 配置文件中的显式预算值保持不变.
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
- [x] 部署 `v0.3.0` 后重启 AstrBot, 验证 target、runtime flags、Provider、条目、SQLite 完整性和 `growth_memory_note` registry 均恢复.
- [x] 部署 `v0.3.1` 后确认 `growth_memory_note` 注册成功, 不再生成无 `items` 的数组参数声明, 并完成 native OneBot 出站发送探针.
- [x] 在 `v0.3.1` 部署后完成真实 QQ 入站 -> LLM 成功请求 -> AstrBot/OneBot 出站回复闭环.
- [ ] 连续运行 24 小时, 检查 event loop latency, queue depth, WAL 大小, memory, provider 错误和 QQ 收发.

当前状态为“已部署并完成真实学习/reload 验证”; 真实 QQ `/进化` 路径和 24 小时 soak 尚未冒充完成, 仍需按上表持续观察.
