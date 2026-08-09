# AstrBot Growth Memory 技术设计与可行性验证

详细的模块, DDL, API, 状态机, 测试和部署任务见 [工程实施规格](IMPLEMENTATION_SPEC.md).
自动学习 target, 调度, 窗口和两阶段 LLM 的规范见 [定时学习管线规格](SCHEDULED_LEARNING_PIPELINE.md).

## 1. 结论

实现独立插件 `astrbot_plugin_growth_memory` 可行, 而且比继续扩写人格, 增加世界书常驻条目, 或重新启用 `astrbot_plugin_self_learning` 更符合当前问题.

插件只负责可执行的长期偏好和分作用域画像:

- 主人画像: 做事习惯, 沟通偏好, 重要事实和共同经历的精简里程碑.
- 固定经验: 例如绘图不要复古滤镜, 不要黄调, 工具失败时不要重复调用.
- 群画像: 群用途, 聊天节奏, 禁忌和已确认倾向.
- 人物画像: 指定平台 ID 对应的人, 与主人的关系, 交流方式和注意事项.

插件不替代人格系统, 不重复 LivingMemory 的原始聊天总结和向量记忆, 不自动改人格, 不对每条消息做 LLM 反思, 不允许普通群友或模型推断产生全局规则.

## 2. 已核验基线

### 2.1 `@ivangdavila/self-improving` 1.2.16

- 用户 zip SHA-256: `2da5166db74343312588c27273038963f1137f8c0a6e935c5d22eeb061a89cd2`.
- zip 完整性检查通过, 与 SkillHub CLI 下载包逐文件一致.
- 可复用机制: 显式纠错优先, 不从沉默推断, 三次证据后晋升, HOT/WARM/COLD 分层, 具体 scope 覆盖一般 scope, 已确认规则不静默删除.
- 不能照搬: 它依赖 Agent 读写 Markdown, 不处理 AstrBot 事件并发, 多用户权限, prompt injection 和模型故障隔离, 且明确不从群聊和第三方信息学习.

### 2.2 `astrbot_plugin_self_learning`

- 核验 main HEAD: `3dc5c57604cb27f59beabe32b78dcab6ff9814b8`.
- Python 总量约 103,569 行, 包含人格, 情绪, 好感度, 社交关系, 图谱, ML 和 WebUI.
- 线上残留配置使用 filter/refine/reinforce 三类 LLM provider, 每 6 小时学习, 最少 50 条, 每批最多 200 条, 表达学习和话题检测可每 10 条触发.
- 当前京东云插件目录中没有该插件, 但保留两份历史配置.
- 判断: 它把人格改写, 行为经验和社交分析混在一起, 功能面和成本都超过本项目需求. 只参考其 Pages 和审查界面, 不继承架构.

### 2.3 京东云 Worldbook 2.2.6

- 使用 `@filter.on_llm_request()` 按 scope, keyword, priority, duration 和 times 激活条目, 最终追加到 `req.system_prompt`.
- 线上有 19 个启用条目, 内容总计 5,540 字符, 实际单轮按 scope 和 trigger 取子集.
- 多个条目使用 `.*` 常驻触发, `max_inject_count=0` 表示不限制单轮条目数.
- 判断: scope 和条目编辑思路可复用, 动态 system prompt 注入方式不复用.

### 2.4 京东云 LivingMemory 2.3.6

- 当前数据约 122 MB, 另有 58 MB 手工备份和约 3.2 GB 重置备份.
- 当前 `top_k=3`, `max_k=6`, 每 15 轮反思, 开启全群捕获, 每会话最多 500 条.
- 当前注入模式为 `user_message_before`; 源码已支持更合适的 `extra_user_content_parts + mark_as_temp()`.
- 判断: LivingMemory 继续负责情节和事实召回. Growth Memory 不访问其内部数据库, 避免版本耦合和重复索引.

### 2.5 AstrBot 4.26.8

- 京东云镜像为 `soulter/astrbot:v4.26.8`, 当前 running, `RestartCount=0`, `OOMKilled=false`.
- 官方支持 `on_llm_request`, `extra_user_content_parts`, `mark_as_temp`, plugin_data 和 Plugin Pages.
- 官方明确警告每轮变化的内容追加到 `system_prompt` 会破坏前缀缓存, 可能显著增加成本和首 token 延迟.

### 2.6 AstrBot 4.26.8 运行时审计, 2026-08-08

- Pipeline 固定顺序为 `WakingCheck -> Whitelist -> SessionStatus -> RateLimit -> ContentSafety -> PreProcess -> Process -> ResultDecorate -> Respond`.
- `TargetCaptureFilter + @filter.event_message_type(ALL)` 只有精确命中 enabled target 才在 WakingCheck 激活. 命中的未 @ Bot 背景群消息仍可采集; handler 不设置 result 且 `is_at_or_wake_command=false` 时, ProcessStage 不会发起主 LLM 请求.
- `on_waiting_llm_request` 在 per-UMO session lock 前; `on_llm_request` 在 lock 内. 因此 injection 和 anchor 标记只能读 RAM snapshot/`put_nowait`, 不得 await SQLite 或 selector rebuild.
- `on_agent_done` 是 final `LLMResponse` 的共同落点. Streaming 会跳过 ResultDecorate 和 after-message-sent, 所以 answer 主来源不能依赖后两者.
- 普通发送异常会在 RespondStage 被 catch, 后续仍可能触发 after-message-sent. AstrBot 未向该 hook 提供平台回执, 因而只能记录 `attempted_unknown`.
- Dashboard 写标准 plugin config 会 reload plugin; plugin 内 `save_config()` 仅原子写文件. Target/schedule/runtime switch 必须用 SQLite 单真源.
- Core cron basic job 可用, 但 reload 必须删除旧 job, 30 秒 misfire grace 必须由 plugin startup catch-up 补齐.
- Web API 同 route/method 会替换 handler, 但 plugin unbind 不清理 class-level route list. Terminate 必须按插件前缀原地清理.

## 3. 职责边界

| 能力 | 归属 |
|---|---|
| 核心人格和身份 | AstrBot persona |
| 手工世界观 | Worldbook |
| 对话情节和事实召回 | LivingMemory |
| 主人长期偏好和失败经验 | Growth Memory |
| 群和人物稳定画像 | Growth Memory |
| 变更审计和回滚 | Growth Memory |

边界口诀:

1. Persona 定义她是谁.
2. LivingMemory 记住发生过什么.
3. Growth Memory 记住以后怎么做, 以及当前对象和场景有什么稳定特点.

## 4. 身份, scope 和条目类型

`owner_identities` 使用 canonical key, 例如:

```text
qq:user:1215198344
webchat:user:muyouzhi
telegram:user:123456
```

多个平台身份可映射到一个 owner. 该配置只能由 AstrBot 配置页或管理员 WebUI 修改, 对话和 LLM tool 永远不能修改.

| Scope | Key 示例 | 生效条件 |
|---|---|---|
| `global` | 空 | 所有会话, 只允许可信规则 |
| `owner` | `owner` | 当前发送者匹配 owner identity |
| `task` | `drawing` | 当前消息命中 task trigger |
| `group` | `qq:group:741379052` | 当前群精确匹配 |
| `person` | `qq:user:2936169201` | 当前发送者或结构化 mention 匹配 |

人物条目 v1 不从纯文本昵称猜 ID.

| Kind | 含义 | 可执行来源 |
|---|---|---|
| `behavior_rule` | 应该怎么做 | owner 明确指令, owner 纠错, WebUI 管理员 |
| `profile_fact` | 对象或群的稳定特点 | owner, 重复观察, 人工编辑 |
| `milestone` | 与 owner 的重要共同经历 | owner 明确表达, 人工确认 |

Visibility:

- `public`: 可作为背景事实.
- `owner_only`: 只在当前发送者是 owner 时注入.
- `behavior_only`: 可以影响行为, 但禁止主动披露条目内容.

## 5. 数据模型

`entries` 保存当前物化状态:

```text
entry_id, scope_type, scope_key, kind, title, content, triggers_json,
conflict_key, status, trust_level, confidence, priority, visibility,
evidence_count, first_evidence_at, last_evidence_at, last_used_at,
expires_at, source_kind, created_at, updated_at, version, content_hash
```

辅助表:

- `entry_versions`: 每次变更保存完整快照. rollback 创建新版本, 不覆盖历史.
- `evidence`: 保存 message id, sender key, 时间, 最长 500 字符摘录和 hash.
- `candidates`: 等待提炼或审核的候选.
- `learning_targets`: 显式启用的 QQ 私聊和群聊, 默认空.
- `conversation_messages`: 启用 target 中有界保留的结构化消息账本.
- `trigger_anchors`: 实际触发 Bot 的问答和学习状态.
- `learning_runs`, `learning_batches`, `staged_proposals`: 两阶段持久化任务和中间结果.
- `target_checkpoints`: 每个 target 连续 committed 的 anchor 水位线.
- `injection_audit`: 只记 entry id, token 估算, 耗时和结果, 不记完整 prompt.
- `daily_budget`: 学习请求数和 token 估算.

SQLite 使用 WAL, `busy_timeout=5000`, `foreign_keys=ON`, 小事务和 versioned migration.

## 6. 学习信号与频率

信任顺序:

```text
manual > owner_explicit > owner_correction > repeated_observation > model_inference
```

| 信号 | 默认结果 |
|---|---|
| WebUI 或管理员新增 | `active` |
| Owner 明确说"以后/永远/记住/不要再" | `active` |
| Owner 明确纠错且提炼置信度 >= 0.90 | `active` |
| Owner 纠错但提炼不确定 | `draft` |
| 群或人物重复观察 >= 3 次, 跨 >= 2 天, 置信度 >= 0.85 | `trial` |
| 模型自我反思 | `draft` |
| 普通群友要求改全局或固定规则 | 拒绝 |

`trial` 可低优先级注入, 但不能生成 `behavior_rule`, 不能覆盖 owner 可信规则, 不能改 owner identity.

热路径不调用 LLM:

- 每条消息只做身份规范化, 高信号 regex, 有界 buffer 写入和本地选择.
- Hot path 只做身份规范化, snapshot lookup, 有界 buffer `put_nowait` 和本地选择, 禁止 SQLite/network/await.
- 目标 capture+injection p95 < 2 ms, selector p95 < 20 ms.

自动学习入口:

- 默认不学习任何会话. 用户在 Plugin Page 明确填写 QQ 私聊或 QQ 群号, 或由管理员在当前对话发送 `/进化` 后才开始 capture.
- `/停止进化` 只暂停当前会话的新 capture, 不删除 backlog, 不影响已有条目注入.
- 只有实际触发 Bot LLM/Agent 的问答成为 anchor. 同一会话的其他消息只作为 anchor 前后文.
- 每个 anchor 使用问题前 10 条, 问答本身和回复后 10 条. 重叠窗口先合并, 单 batch 内消息不重复; 跨 batch 只重复 anchor 问答和最小连续上下文.
- 默认每天 `03:00` 执行, timezone 为 `Asia/Shanghai`. WebUI 可增删 `HH:MM` time slot, 所有 slot 共享每日预算.
- 单个 Extractor batch 最多 10 个 anchor, 不足 10 个直接处理, 同时受 4,000 输入 token 硬上限约束.
- Stage 1 Extractor 提取高召回 proposal. Stage 2 Reviewer 对全部 proposal 和相关现有条目做筛选, 去重与整合.
- 质量优先默认档每天最多 64 次学习请求、1,000,000 输入 token 和 1,000,000 输出 token, 单 worker 并发固定为 1.
- 每个 Extractor batch 准入时持久化预留一个 Reviewer request/token 额度, 防止只抽取不审查.
- 超预算任务转为 deferred. Stage 2 失败时复用 Stage 1 结果, 不重复付费抽取.
- 只有 reviewer decision 和本地 mutation/no-change/human-review 事务完成后才推进 target checkpoint.
- 手工 WebUI 条目和 `/growth remember` 继续立即生效, 不等待 schedule.

## 7. 提炼和写入权限

Extractor 和 Reviewer 都只返回受限 JSON, 不直接写数据库. 以下是 Reviewer 可交给本地策略的 mutation proposal:

```json
{
  "operation": "propose_upsert",
  "scope_type": "task",
  "scope_key": "drawing",
  "kind": "behavior_rule",
  "content": "绘图避免复古滤镜和偏黄画面",
  "triggers": ["画图", "自拍", "生图"],
  "conflict_key": "image.color_tone",
  "confidence": 0.96,
  "evidence_ids": ["msg-1"]
}
```

Extractor proposal 必须引用当前 batch 内真实 message/anchor ID. Reviewer 必须对每个 Stage 1 proposal 恰好给出一个 decision, 包括 `no_change`.

写入前执行 JSON schema, evidence ownership, sender identity, scope permission, kind trust, 长度, trigger, 重复, 冲突和乐观版本校验.

模型只有 `propose_upsert`, `propose_archive`, `propose_merge`. delete 和 owner identity 变更必须由管理员完成. LLM 输出中的 owner id, status 和 trust_level 均由服务端覆盖.

## 8. 检索, 冲突和 token 预算

v1 不使用 embedding. LivingMemory 已负责 embedding 和事实召回. Growth Memory 使用 scope 精确匹配, 预编译 trigger, conflict key 和确定性排序.

冲突顺序:

1. owner/manual 可信规则不能被 repeated observation 或 model inference 覆盖.
2. 同一信任层内, person > group > task > owner > global.
3. 同 scope 和 trust 时, priority 高者优先.
4. 仍相同时, 新版本优先.
5. 冲突写入 WebUI 待处理列表.

默认硬限制:

- 总预算 800 token 估算, 绝对上限 1,000.
- 总条目最多 8.
- global 2, owner 2, task 3, group 2, person 2.
- 超大条目整条跳过并排入 compaction, 不在句中截断.

注入位置:

- 默认所有学习内容使用 `req.extra_user_content_parts` 并 `.mark_as_temp()`.
- 只有极少变化, 无 trigger 的可信 global rule 在缓存测试通过后才允许进入 `system_prompt`.
- 观察内容使用 `<learned_context>` 包裹, 明确它是事实参考而非指令.
- 不注入原始聊天, 只注入结构化摘要.

## 9. Prompt injection 防护

1. 普通用户消息不能直接成为 behavior rule.
2. 群和人物观察只能生成 profile fact 或 draft.
3. Owner 按平台结构化 sender ID 校验, 不信昵称和纯文本 ID.
4. Raw message 不直接注入, 只注入 schema 规范化陈述.
5. `behavior_only` 带 `DO_NOT_DISCLOSE` 标记.
6. 观察条目禁止 tool call, shell 命令和"忽略之前规则"等指令形式.
7. person 候选不能自行升级到 global.
8. 模型没有 owner identity, trust 和 status 的决定权.

## 10. AstrBot WebUI

使用官方 Plugin Pages, 不监听独立端口. 页面位于 `pages/dashboard/`, API 用 `context.register_web_api()` 注册.

页面包含:

- Overview: 条目, 状态, 今日预算, 注入耗时和队列.
- Learning targets: QQ 私聊/群聊 target, 状态, backlog, 上次学习和 Run now.
- Schedule: timezone, 每日 time slot, 下次运行和共享预算.
- Runs: anchor, 合并窗口, Extractor/Reviewer batch, proposal 和 retry.
- Entries: 按 scope, kind, status, trust, platform 和 ID 筛选.
- Candidate inbox: 证据, diff, approve, reject, merge 和 scope 调整.
- Entry detail: trigger, 来源, 使用历史, 冲突和版本时间线.
- Injection preview: 输入 platform, sender, group 和 message, 预览命中和 token.
- Jobs: 状态, 失败原因, retry 和 circuit breaker.
- Audit: mutation 和 rollback.
- Backup: 导出, 导入预检和恢复.
- Settings: owner identities, provider, 频率和预算.
- Kill switch: 分别关闭 capture, learning 和 injection.

所有 mutation API 复用 AstrBot Dashboard 认证. 不实现第二套密码系统.

## 11. 稳定性设计

Chat fail-open:

- 本地选择异常时本轮不注入, 正常聊天继续.
- SQLite busy 时读取最近成功 snapshot, capture writer 原序重试, 聊天继续.
- 学习 provider 失败只影响 job.
- schema 错误不写 entry.
- WebUI 异常不影响消息钩子.

Capture writer:

- 一个有界 FIFO 接收 immutable capture envelope, 一个 `aiosqlite` writer actor 是唯一写 owner.
- 每个 hook 预生成 row/anchor ID. Anchor envelope 内嵌 question snapshot, 所以异步写入不产生 question/anchor 外键竞态.
- 满队列先逐出 context-only. 全 critical 满载时设置 degraded 并暂停学习, 不同步落盘, 不无限等待, 正常聊天继续.
- mutation commit 后立即构建完整 runtime snapshot 并单引用切换; injection 永远读取旧或新完整 snapshot, 不读半成品.

Worker:

- 单 worker, 并发 1, provider timeout 45 秒.
- 推荐使用独立 learning provider. 复用主 provider 时, 任何新消息后先等 30 秒 quiet period.
- 每任务最多 1 次立即 retry.
- 连续 3 次 provider 失败后 circuit breaker 30 分钟.
- shutdown/reload 先注销 core cron 和 Page routes, flush writer, 最多等待 7 秒, 覆盖 SQLite `busy_timeout=5s`, 未完成 job 退回 pending.
- reload 后按 DB schedule 重注册 cron, catch-up 12 小时内错过的 slot.

Backup:

- 每日 SQLite online backup, 保留 14 个日备份和 8 个周备份.
- migration 前自动备份.
- 导入先写临时数据库并验证, 成功后原子替换.
- 确认 owner 规则不因低使用率自动删除.
- 自动生成的 draft 90 天未确认后 archive; 显式 `expires_at` 到期后最多 5 分钟退出 runtime snapshot 并 archive.

指标:

- `selector_latency_ms` p50/p95/p99.
- `injected_entries`, `injected_tokens_estimated`.
- `candidate_created/promoted/rejected`.
- `learning_calls/input_tokens/failures`.
- `queue_depth`, `oldest_job_age`.
- `capture_context_dropped`, `capture_critical_overflow`, `capture_degraded`.
- `old_core_cron_jobs`, `old_route_handlers`, `tracked_task_count`.
- `db_size`, `backup_last_success_at`, `circuit_breaker_state`.

日志禁止打印完整 persona, 完整 prompt, owner 私密条目和原始群聊批次.

## 12. 与现有插件共存

Worldbook:

- v1 不自动修改其配置.
- 可做 import preview, 用户确认后再禁用重复条目.
- 未迁移前只提示重复, 不越权控制另一个插件.

LivingMemory:

- 不读写其 SQLite, FAISS 或图谱文件.
- 后续可增加用户主动"固定为成长条目"的单向导入.
- 不做自动双向同步.

Persona:

- 不自动覆盖.
- WebUI 可展示"建议写回人格", 但写回必须手工确认并先备份.

## 13. 分阶段落地

### Phase 1: Core and shadow

- 完成 schema, selector, budget, versioning, rollback 和 WebUI.
- 完成 target matcher, conversation ledger, anchor/window 和定时 run.
- 只允许一个 owner 私聊和一个测试群 capture.
- 自动候选只预览, 不注入.
- 运行 7 天, 统计误命中和 token.

### Phase 2: Trusted owner learning

- 开启两阶段定时管线中的 owner explicit 和 owner correction 自动 mutation.
- 开启 task behavior rule 动态注入.
- 暂不开启群和人物 trial.
- 运行 7 到 14 天检查错误规则和冲突.

### Phase 3: Group and person profiles

- 在明确启用的 target 中开放群和人物观察.
- 开启群和人物重复观察到 trial.
- 每周生成 owner 待审摘要.
- 继续禁止普通用户创建 behavior rule.

### Phase 4: Optional import

- 增加 Worldbook import preview.
- 评估 LivingMemory 单向 pin.
- 只有实际数据证明 trigger 不足时才考虑 embedding.

## 14. 验证

离线原型位于 `prototype/growth_memory_core.py`, 覆盖:

- target matcher 的默认关闭, 全局 kill switch, QQ 群/私聊类型隔离和 account ID 精确匹配.
- owner, task, group, person scope 筛选.
- owner_only 防泄露.
- owner 规则不被群画像覆盖.
- 同信任层更具体 scope 覆盖全局.
- 硬 token 预算和整条跳过.
- 稳定 global 与动态内容分离.
- owner explicit 自动 active.
- repeated group observation 最高只到 trial.
- model reflection 只能 draft.
- 非可信来源不能创建 behavior rule.
- SQLite WAL, 版本追加, rollback 和重启读取.

2026-08-08 实测结果:

- `ruff check` 通过.
- `ruff format --check` 通过.
- `compileall` 通过.
- 50 项单元测试通过, 包括 target gate, runtime-boundary tests, delayed anchor completion, stale anchor cleanup, schedule timezone/toggle, runtime validation、旧库迁移、人物跨日证据晋级、proposal 证据隔离、snapshot 即时刷新和 Web API rollback.
- 完整 DDL 已通过内存 SQLite 验证: 18 张业务表与 `sqlite_sequence`, 共 56 个 table/index 对象; `PRAGMA integrity_check` 为 `ok`, `PRAGMA foreign_key_check` 为空.
- 120 个候选条目下执行 10,000 次选择:
  - p50 `0.1824 ms`.
  - p95 `0.2685 ms`.
  - p99 `0.6675 ms`.
  - 单次最大 `18.9703 ms`.
- 完整渲染后的 token 估算已纳入 wrapper 和信任说明, 测试确认不超过 hard budget.

这些结果证明 core selector, trust gate, budget, versioned store, AstrBot lifecycle 和管理台实现可行. provider 故障注入已覆盖 timeout/retry/deferred 路径; 京东云真实 QQ 闭环和 24 小时 soak 仍是部署后的验收项, 在完成前不得宣称生产稳定.

运行:

```bash
cd /Users/lifeilong/astrbot_plugin_growth_memory
PYTHONPATH=.. python3 -m unittest discover -s tests -v
```

插件实现后必须通过:

1. `compileall` 和单元测试.
2. AstrBot 4.26.8 与当前开发版本双版本加载.
3. Pages route, bridge, theme 和认证.
4. 10,000 条离线压测, p95 < 20 ms.
5. provider timeout, JSON 错误, SQLite busy, 磁盘满和 reload 故障注入.
6. 24 小时 soak, 检查内存, queue, task 泄漏和数据库增长.
7. Prompt snapshot 确认每轮注入 <= hard budget.
8. 非 owner 攻击测试.
9. backup -> 修改 -> rollback -> restart 闭环.

京东云验收:

1. 备份 config, plugin_data 和当前 persona.
2. 只部署 Growth Memory, 初始 `shadow_mode=true`.
3. 重载插件或只重启 AstrBot, 不重启 NapCat.
4. 验证 WebUI, SQLite, worker 和日志.
5. Owner QQ 用 `/进化` 开启当前私聊, 说一次明确偏好, 执行 Run now 后 WebUI 出现 proposal, 下次匹配任务命中, 非匹配任务不注入.
6. 非 owner QQ 尝试改全局规则, 必须拒绝.
7. 模拟 learning provider 超时, QQ 正常回复不受影响.
8. rollback 后重启 AstrBot, 确认版本恢复.
9. 观察 7 天后再开 group/person trial.

## 15. 完成标准

- 聊天热路径无网络请求.
- 选择耗时 p95 < 20 ms.
- 默认 800 token, 绝对 1,000 token 硬限制.
- 非 owner 无法修改 global/task behavior rule.
- provider 故障不影响正常聊天.
- 自动变更可见, 有证据, 有版本, 可 rollback.
- restart/reload 后 entry, queue 和预算状态一致.
- 真实 QQ owner 路径和非 owner 攻击路径通过.
- 7 天 shadow 证明命中准确后才开自动 trial.

## 16. 最终技术选择

- 新建 `astrbot_plugin_growth_memory`.
- SQLite WAL + 追加版本日志.
- 结构化 scope + trigger 确定性检索.
- owner 信任边界优先于 scope 覆盖.
- 动态内容用 `extra_user_content_parts.mark_as_temp()`.
- 官方 Plugin Pages, 不开独立端口.
- 显式 target + 定时 anchor 增量学习 + 两阶段 LLM + 每日硬预算.
- v1 不用 embedding, 不做 ML 聚类, 不自动改 persona, 不耦合 LivingMemory 内部数据.

## 17. 研究来源

- SkillHub install guide: https://skillhub.cn/install/skillhub.md
- 用户提供的 `@ivangdavila/self-improving` 1.2.16 原包.
- https://github.com/NickCharlie/astrbot_plugin_self_learning
- https://github.com/Zhalslar/astrbot_plugin_worldbook
- https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory
- https://docs.astrbot.app/dev/star/guides/listen-message-event.html
- https://docs.astrbot.app/dev/star/guides/plugin-pages.html
- https://docs.astrbot.app/dev/star/guides/storage.html
- https://docs.astrbot.app/dev/star/guides/ai.html
