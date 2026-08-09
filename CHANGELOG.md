# 变更记录

## v0.4.0

- Extractor 明确接收主人身份、发送者显示名和 `is_owner` 标记; Reviewer 现在按独立 token 分区同时接收候选、原始证据和相关现有条目, 不再盲审模型摘要.
- 修正学习目标优先级: 精确 Bot target 的关闭状态覆盖 wildcard; 修正 scope 召回语义, 仅 `task` 把 trigger 作为硬门槛, 其他层级按身份或群匹配并只用 trigger 排序.
- 主人画像支持至少 3 条、跨 2 天重复证据进入 `trial`; 自动条目强制生成稳定 conflict key, 非 task trigger 自动清空, 敏感人物事实自动限制为 `owner_only`.
- 新增每日输出 token、单次学习输入和单次学习输出上限, 质量优先默认档为每日 64 次请求和 1,000,000 输入/输出 token, 单次输入 32,000、输出 32,768; Provider 请求透传 `max_tokens`, 并新增真实注入审计、90 天清理和 Dashboard 输入/输出预算、注入次数展示.
- 修复测试对 AstrBot 真实 Starlette `JSONResponse` 的兼容, 65 项回归测试覆盖证据审查、匹配、去重、权限、预算和注入审计.

## v0.3.1

- 修复 `growth_memory_note` 在 AstrBot 4.27.1 / Gemini 兼容 Provider 中生成无 `items` 的数组 schema, 导致上游返回 400 后整轮消息无回复.
- 将 LLM 工具的触发词参数改为兼容所有支持版本的逗号/换行分隔字符串, 服务端仍规范化为内部字符串数组并保留 Python 直接调用的列表兼容性.

## v0.3.0

- 新增 `growth_memory_note` AstrBot LLM tool. LLM 只能提交带当前入站消息证据的候选, 由 Reviewer 审核后才进入正式记忆.
- 新增主动记忆候选开关、SQLite 候选队列、审核 lease/重试/去重和自动条目版本化更新, 人工条目不会被覆盖.
- 总结 Provider 改为从 AstrBot 已配置 Chat Provider 动态读取, WebUI 用下拉框分别选择 Extractor 和 Reviewer, Provider 不可用时保留配置并延迟处理.
- 兼容缺失 `on_waiting_llm_request`, `on_agent_done`, `astrbot.api.web` 和 `TextPart` 的 AstrBot 构建, 使用受限 legacy fallback.

## v0.2.3

- 修复 Plugin Page 缺少 toast 节点导致所有操作反馈抛出异常的问题, 增加请求期间防重复提交、加载状态、可访问性播报和移动端操作区布局.
- Reviewer batch 使用数据库 compare-and-set 和 5 分钟 lease 原子抢占, reload 短暂重叠时不重复调用模型; 过期 lease 可自动恢复.
- 插件初始化改为幂等, 防止同一实例重复初始化遗留 writer、ticker 或 Web API route.
- 启动时重新校验 SQLite runtime flags, 脏值自动修复为安全默认值并记录日志.

## v0.2.2

- 将自动 proposal 的证据改为逐条 `message_row_id` 白名单校验, trust、证据条数和证据天数只按该 proposal 实际引用的入站消息计算.
- 新增跨 batch 的消息级证据去重, 防止重叠上下文重复累计并提前把人物或群画像提升为 `trial`.
- 自动学习成功后立即发布新的内存 snapshot, 不再等待插件 reload 才参与后续注入.
- 拒绝自动生成的 `global profile_fact/milestone`, 避免把普通技术问答当成跨会话长期记忆.
- 过期条目最多 5 分钟退出运行 snapshot, 自动草稿 90 天未确认后归档; 成功恢复后清理陈旧错误告警.
- 新增 proposal 证据隔离、global 普通知识拒绝、snapshot 即时刷新、证据去重和条目生命周期回归测试.

## v0.2.1

- 修正 Extractor/Reviewer 每次尝试的请求、输入 token 和输出 token 统计, 延迟 Reviewer 成功后同步 run 状态.
- 为自动人物事实增加跨批次幂等证据累计, 至少 3 条证据且跨 2 天才进入 `trial`; 兼容旧数据库迁移.
- WebUI 增加主人身份编辑、时间点编辑/启停/删除保护、真实今日预算、队列深度、归档条目、证据条数/天数和历史版本回滚选择.
- 保持学习冷路径与聊天热路径隔离, SQLite WAL、版本追加、审计和失败重试策略不变.
- 新增旧版 SQLite 升级幂等、时间点删除保护和人物画像晋级测试.
