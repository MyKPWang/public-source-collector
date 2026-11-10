# 数据与 API 契约

> **已被公共来源采集路径替代：** 本文定义的是遗留微信公众号采集模型。
> 当前 Pipeline 的权威契约见 [PUBLIC_LIST_INGESTION.md](PUBLIC_LIST_INGESTION.md)：
> `public_*` 表、`schema_version: "2.1"` 和 metadata-only 列表采集优先。

> 本文是数据库和 HTTP 接口的权威定义。实现代码、测试、Pipeline adapter 和管理页面必须使用本文中的状态名、字段语义和错误结构。

## 1. 通用约定

### 1.1 标识与时间

- 内部主键使用 UUIDv7 字符串；外部不得依赖数据库自增 ID。
- 数据库存储时间统一为 UTC、带时区的 ISO 8601 或等价 UTC timestamp。
- API 输出 ISO 8601，默认带 `Z`；页面按 `Asia/Shanghai` 展示。
- `business_date` 是 `Asia/Shanghai` 下的自然日，格式 `YYYY-MM-DD`。
- API 区间统一使用 `[from, before)`，即包含起点、不包含终点。
- 表名和 JSON 字段使用 `snake_case`。

### 1.2 数据库基线

- SQLite 开启 `PRAGMA journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout=5000`。
- migration 使用 Alembic，版本号写入标准 `alembic_version`。
- 写事务应短小；网络调用期间不能持有数据库写事务。
- 删除公众号使用禁用或软删除，不能级联删除历史文章。
- 所有枚举在 Python 中集中定义，数据库用 CHECK 约束防止非法值。

## 2. 状态枚举

### 2.1 `CollectionCommandStatus`

```text
queued -> claimed -> completed
                  -> rejected
                  -> failed
queued -> cancelled
```

### 2.2 `CollectionRunStatus`

```text
queued
running
completed
partial
failed
rate_limited
session_expired
protocol_changed
rejected
cancelled
missed
```

### 2.3 `RunAccountStatus`

```text
queued
running
completed
empty
page_limit_reached
failed
rate_limited
session_expired
protocol_changed
not_started
```

`empty` 表示请求成功且微信明确返回空列表；`not_started` 表示从未请求，两者不得混用。

### 2.4 `AccountCollectionJobStatus`

```text
queued
running
completed
empty
page_limit_reached
failed
rate_limited
session_expired
protocol_changed
cancelled
```

### 2.5 `WechatRequestStatus`

```text
planned
waiting
sent
succeeded
rate_limited
session_expired
network_failed
protocol_changed
unknown
```

进程恢复时，遗留的 `sent` 转为 `unknown`，不能直接视为失败后立即重试。

### 2.6 `LoginSessionStatus`

```text
creating
waiting_scan
scanned
confirming
validating
succeeded
expired
cancelled
failed
```

终态为 `succeeded/expired/cancelled/failed`。

## 3. 表结构

下列字段是最小必需集合。实现可增加技术字段，但不得改变既有字段语义。

### 3.1 `official_accounts`

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | TEXT PK | 内部 UUIDv7 |
| `source_account_id` | TEXT NOT NULL UNIQUE | 微信侧稳定标识 |
| `fakeid_ciphertext` | BLOB NOT NULL | 加密后的请求标识 |
| `name` | TEXT NOT NULL | 展示名称 |
| `avatar_url` | TEXT NOT NULL DEFAULT '' | 头像 |
| `enabled` | INTEGER NOT NULL CHECK 0/1 | 是否进入计划 |
| `archived_at` | TIMESTAMP NULL | 删除订阅时间；非空时不进入计划和默认列表 |
| `display_order` | INTEGER NOT NULL | 基础顺序 |
| `watermark_article_id` | TEXT NULL | 最近确认水位线，引用 source article id |
| `last_success_at` | TIMESTAMP NULL | 最近成功或空列表完成时间 |
| `consecutive_failure_count` | INTEGER NOT NULL DEFAULT 0 | 连续失败次数 |
| `created_at`、`updated_at` | TIMESTAMP NOT NULL | 审计时间 |

索引：`(archived_at, enabled, display_order)`、`(last_success_at)`。

状态语义：

- `enabled=1, archived_at=NULL`：正常订阅。
- `enabled=0, archived_at=NULL`：暂停采集，仍显示在订阅列表。
- `archived_at IS NOT NULL`：已删除订阅，默认隐藏但历史数据保留。

Collection plan 只选择 `enabled=1 AND archived_at IS NULL`。重新添加相同 `source_account_id` 时返回已有记录；若已归档，前端提示恢复，不允许新建重复公众号。生产界面不提供物理删除；仅当公众号从未产生文章、批次或请求引用时，维护 CLI 才可在显式确认后硬删除。

Pipeline 文章查询默认只返回 `archived_at IS NULL` 的公众号文章；归档前已经生成的报告和数据库历史不改变。Admin 历史查询可以通过显式筛选查看已归档公众号。暂停状态 `enabled=0, archived_at=NULL` 不自动排除历史文章，因为暂停只控制采集，不等同于删除信息源。

### 3.2 `articles`

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | TEXT PK | 内部 UUIDv7 |
| `account_id` | TEXT FK NOT NULL | `official_accounts.id` |
| `source_article_id` | TEXT NOT NULL | 微信侧文章标识 |
| `title` | TEXT NOT NULL | 标题，可为空字符串但不能 NULL |
| `digest` | TEXT NOT NULL DEFAULT '' | 列表摘要 |
| `url` | TEXT NOT NULL | 原文 URL |
| `cover_url` | TEXT NOT NULL DEFAULT '' | 封面 URL |
| `published_at` | TIMESTAMP NOT NULL | 微信发布时间 |
| `first_seen_at` | TIMESTAMP NOT NULL | 首次入库时间 |
| `last_seen_at` | TIMESTAMP NOT NULL | 最近观察时间 |
| `content_hash` | TEXT NOT NULL | 规范化元数据 SHA-256 |
| `source_deleted` | INTEGER NOT NULL DEFAULT 0 | 未来软标记 |
| `created_at`、`updated_at` | TIMESTAMP NOT NULL | 审计时间 |

约束：`UNIQUE(account_id, source_article_id)`。

索引：`(published_at DESC, id DESC)`、`(account_id, published_at DESC, id DESC)`、`(first_seen_at DESC)`。

`content_hash` 输入固定为规范化后的 `title/digest/url/cover_url/published_at`，字段顺序和空值规则必须有单元测试。

### 3.3 `article_revisions`

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | TEXT PK | revision UUID |
| `article_id` | TEXT FK NOT NULL | 规范文章 |
| `revision_number` | INTEGER NOT NULL | 从 1 递增 |
| `content_hash` | TEXT NOT NULL | 对应版本 hash |
| `title/digest/url/cover_url/published_at` | 与文章表一致 | 版本快照 |
| `source_payload_json` | TEXT NOT NULL | 字段白名单后的源数据 |
| `observed_at` | TIMESTAMP NOT NULL | 发现变化时间 |

约束：`UNIQUE(article_id, revision_number)`、`UNIQUE(article_id, content_hash)`。

初次创建文章时不生成 revision，`articles` 本身就是当前版本。后续 hash 变化时，先把更新前的文章值写入 revision，再更新 `articles`。例如 A -> B -> C 后，revision 保存 A、B，`articles` 保存 C。这样未发生变化的绝大多数文章不会天然复制一份。

### 3.4 `collection_commands`

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | TEXT PK | command UUID |
| `command_type` | TEXT NOT NULL | `collect_all/collect_account/collect_selected/search_account/backfill_account` |
| `trigger` | TEXT NOT NULL | `scheduled/manual_web/manual_cli/feishu` |
| `payload_json` | TEXT NOT NULL | 经过 schema 校验的目标参数 |
| `business_date` | DATE NULL | 定时/回填业务日期 |
| `idempotency_key` | TEXT NOT NULL UNIQUE | 调用幂等键 |
| `status` | TEXT NOT NULL | Command 状态枚举 |
| `run_id` | TEXT NULL UNIQUE | collect 命令关联批次 |
| `submitted_by` | TEXT NOT NULL | 脱敏操作者或 system |
| `lease_owner` | TEXT NULL | Worker 实例 ID |
| `lease_expires_at` | TIMESTAMP NULL | 认领期限 |
| `created_at/claimed_at/finished_at` | TIMESTAMP | 生命周期 |
| `reason_code` | TEXT NOT NULL DEFAULT '' | 稳定原因码 |

定时命令的幂等键固定为 `scheduled:{schedule_name}:{business_date}`。

#### `collection_command_items`

记录一次定点采集命令中每个公众号的提交结果：`command_id/account_id/outcome/job_id/active_run_id/reason_code`。`outcome` 只能为 `enqueued/promoted_existing_job/ignored_already_running/rejected`。约束为 `UNIQUE(command_id, account_id)`。

### 3.5 `account_collection_jobs`

真正的公众号采集执行单元：

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | TEXT PK | job UUID |
| `account_id` | TEXT FK NOT NULL | 目标公众号 |
| `status` | TEXT NOT NULL | AccountCollectionJobStatus |
| `priority` | INTEGER NOT NULL DEFAULT 10 | manual=100，scheduled=10，backfill=1 |
| `queued_at/started_at/finished_at` | TIMESTAMP NULL | 生命周期 |
| `lease_owner/lease_expires_at` | TEXT/TIMESTAMP NULL | Worker 认领 |
| `page_count/request_count` | INTEGER NOT NULL DEFAULT 0 | 执行统计 |
| `fetched_count/new_count/updated_count` | INTEGER NOT NULL DEFAULT 0 | 结果统计 |
| `stop_watermark_source_article_id` | TEXT NOT NULL DEFAULT '' | job 开始时冻结的历史水位线 |
| `first_page_head_source_article_id` | TEXT NOT NULL DEFAULT '' | 本次第一页最前文章，成功后候选新水位线 |
| `reason_code/message` | TEXT NOT NULL DEFAULT '' | 脱敏终态 |

使用 partial unique index 保证同一 `account_id` 最多一个 `status IN ('queued','running')` 的 job。Worker 按 `priority DESC, queued_at ASC` 认领，当前 running job 不抢占。

人工命令遇到 running job 时按用户操作语义忽略；定时命令遇到 queued 或 running job 时可以关联现有 job，以其最终结果满足定时批次覆盖。任何情况都不创建第二个活动 job。

### 3.6 `collection_runs`

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | TEXT PK | run UUID |
| `command_id` | TEXT FK NOT NULL UNIQUE | 来源命令 |
| `trigger` | TEXT NOT NULL | 与命令一致 |
| `business_date` | DATE NULL | 业务日期 |
| `status` | TEXT NOT NULL | Run 状态枚举 |
| `planned_count` | INTEGER NOT NULL DEFAULT 0 | 计划公众号数 |
| `success_count` | INTEGER NOT NULL DEFAULT 0 | completed/empty 数 |
| `failed_count` | INTEGER NOT NULL DEFAULT 0 | 失败数 |
| `not_started_count` | INTEGER NOT NULL DEFAULT 0 | 未执行数 |
| `request_count` | INTEGER NOT NULL DEFAULT 0 | 内容请求数 |
| `new_article_count` | INTEGER NOT NULL DEFAULT 0 | 首次插入数 |
| `updated_article_count` | INTEGER NOT NULL DEFAULT 0 | 新 revision 数 |
| `coverage_ratio` | REAL NOT NULL DEFAULT 0 | success/planned，0..1 |
| `started_at/finished_at` | TIMESTAMP NULL | 生命周期 |
| `stop_reason_code` | TEXT NOT NULL DEFAULT '' | 稳定原因码 |
| `stop_message` | TEXT NOT NULL DEFAULT '' | 可展示脱敏文本 |

索引：`(business_date DESC)`、`(started_at DESC)`、`(status, started_at DESC)`。

### 3.7 `collection_run_accounts`

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | TEXT PK | 关联行 UUID |
| `run_id`、`account_id` | TEXT FK NOT NULL | 批次和公众号 |
| `job_id` | TEXT FK NULL | 关联的唯一执行 job；running 被忽略时可空 |
| `plan_position` | INTEGER NOT NULL | 本次实际执行顺序 |
| `status` | TEXT NOT NULL | RunAccount 状态 |
| `page_count/request_count` | INTEGER NOT NULL DEFAULT 0 | 分页及请求数 |
| `fetched_count` | INTEGER NOT NULL DEFAULT 0 | 返回文章数 |
| `new_count/updated_count` | INTEGER NOT NULL DEFAULT 0 | 入库结果 |
| `started_at/finished_at` | TIMESTAMP NULL | 生命周期 |
| `last_source_article_id` | TEXT NOT NULL DEFAULT '' | 本次最前文章 |
| `reason_code/message` | TEXT NOT NULL DEFAULT '' | 脱敏原因 |

约束：`UNIQUE(run_id, account_id)`、`UNIQUE(run_id, plan_position)`。

多个 run_account 可以关联同一个 queued job。job 结束后，在事务内把终态和统计回填给所有关联 run_account，并重新计算各自父 run。人工点击遇到 running job 时不创建关联，run_account 为 `not_started` 并记录 `ALREADY_RUNNING`。

### 3.8 `collection_run_articles`

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `run_id/article_id/account_id` | TEXT FK NOT NULL | 观察关系 |
| `page_number` | INTEGER NOT NULL CHECK >= 1 | 页码 |
| `position` | INTEGER NOT NULL CHECK >= 0 | 页内位置 |
| `observed_at` | TIMESTAMP NOT NULL | 观察时间 |
| `is_first_seen` | INTEGER NOT NULL CHECK 0/1 | 是否首次发现 |

约束：`UNIQUE(run_id, article_id)`。

job 被多个批次共享时，观察结果写入所有关联 run 的 `collection_run_articles`；这只是批次关联，不复制 `articles` 或 revision。

### 3.9 `wechat_requests`

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | TEXT PK | request UUID，同时作为 request_id |
| `command_id` | TEXT FK NOT NULL | 来源命令 |
| `job_id` | TEXT FK NULL | 公众号采集 job；搜索时为空 |
| `run_id/account_id` | TEXT FK NULL | 搜索时可空 |
| `operation` | TEXT NOT NULL | `account_search/article_page` |
| `page_number/begin/count` | INTEGER NULL | 非敏感分页信息 |
| `publish_record_count/article_count` | INTEGER NULL | 本页发布记录数和展开后文章数 |
| `status` | TEXT NOT NULL | Request 状态 |
| `gate_wait_ms` | INTEGER NOT NULL DEFAULT 0 | 实际等待 |
| `planned_at/sent_at/finished_at` | TIMESTAMP NULL | 生命周期 |
| `duration_ms` | INTEGER NULL | 网络耗时 |
| `wechat_ret` | INTEGER NULL | 微信返回码 |
| `response_schema` | TEXT NOT NULL DEFAULT '' | 已识别结构版本 |
| `reason_code` | TEXT NOT NULL DEFAULT '' | 稳定错误码 |

禁止字段：Cookie、token、fakeid、搜索词、完整查询参数、原始响应。

索引：`(run_id, sent_at)`、`(account_id, sent_at DESC)`、`(status, sent_at DESC)`。

### 3.10 `request_control_state`

单例行 `id=1`：

| 字段 | 说明 |
|---|---|
| `last_request_started_at` | 上一次内容请求开始时间 |
| `cooldown_until` | 全局冷却截止 |
| `rolling_window_limit` | 滚动 24 小时硬上限，默认 60 |
| `consecutive_rate_limit_count` | 连续频控次数 |
| `last_rate_limit_at` | 最近 `200013` 时间 |
| `version` | 乐观并发版本 |

Gate 获得许可时，在 `BEGIN IMMEDIATE` 短事务内完成以下操作：

1. 检查冷却、当前批次请求上限和 `wechat_requests` 中滚动 24 小时已预留/已发出的内容请求数。
2. 计算间隔允许时间和最早滚动预算槽位，再得到 `scheduled_at`，最后增加 jitter。
3. 把 `last_request_started_at` 预留为 `scheduled_at`；预算以请求账本为准，不使用零点清零计数器。
4. 插入状态为 `waiting` 的 `wechat_requests`，记录计划时间和等待时长。
5. 提交事务后 sleep 到 `scheduled_at`，再用短事务把请求标记为 `sent` 并发出网络请求。

若进程在等待期间崩溃，预留间隔和预算仍然有效，这是有意的保守行为。具体实现需确保两个进程不能获得重叠的 `scheduled_at`。

常规定时批次另有 `45` 次批次上限，等于 15 个公众号 × 每号 3 页。滚动 24 小时上限为 `60`，搜索和人工采集也占用该额度。达到滚动上限时，Gate 计算最早可用槽位并等待；若等待会超过命令允许的最晚完成时间，则以 `REQUEST_BUDGET_EXHAUSTED` 明确停止，不能绕过上限。

### 3.11 登录与运维表

`wechat_login_sessions`：`id/channel/status/created_by/created_at/expires_at/completed_at/reason_code/message`。不得存二维码、微信 UUID、Cookie 或 token。

`account_searches`：搜索任务状态、command_id、创建/完成时间和原因码。

`account_search_candidates`：search_id、候选 source_account_id、加密 fakeid、名称、头像、排序。默认 24 小时清理。

`collection_events`：id、timestamp、level、event_type、request_id、run_id、account_id、reason_code、details_json。`details_json` 必须字段白名单化并设置保留期。

## 4. 事务契约

### 4.1 提交采集命令

一个事务内：

1. 按 `idempotency_key` 查询已有命令。
2. 已存在则返回原 `command_id/run_id`，不新增记录。
3. 插入 `collection_commands(status=queued)`。
4. 插入 `collection_runs(status=queued)`。
5. 回写 command.run_id。

### 4.2 写入文章页

网络响应返回后，一个事务内：

1. 将 request 更新为 `succeeded` 或具体终态。
2. 对每篇文章按 `(account_id, source_article_id)` upsert。
3. 新文章不生成 revision；hash 变化时先保存更新前版本，再更新当前文章。
4. 写 `collection_run_articles`，重复观察不重复插入。
5. 更新 run_account 页面数、返回数、新增数和更新数。
6. 更新文章 `last_seen_at`；满足规则时更新公众号水位线。

页间去重和停止判断使用 job 内状态，不使用“当前数据库是否已经存在”作为唯一依据。第一页刚插入、第二页因 offset 漂移再次返回的文章属于 `seen_in_current_job`，不能触发历史边界停止。只有 `stop_watermark_source_article_id` 或 job 开始前已冻结的历史集合可作为停止证据。

事务失败时整页回滚，但 request 必须通过补偿短事务标记为 `unknown/failed`，不能保持 `sent` 无解释。

### 4.3 完成批次

根据所有 `collection_run_accounts` 计算计数与覆盖率，禁止调用方传入汇总值。全局错误之后，把剩余 `queued` 项更新为 `not_started`，再写批次终态。

终态归并规则固定为：

1. 没有启用公众号：`rejected`，原因 `NO_ENABLED_ACCOUNTS`。
2. 出现全局错误：按实际原因归为 `rate_limited/session_expired/protocol_changed/failed`。
3. 所有公众号均为 `completed/empty`：`completed`。
4. 没有全局错误，但存在 `page_limit_reached` 或可隔离的数据告警：`partial`。
5. `success_count` 只统计 `completed/empty`；`coverage_ratio = success_count / planned_count`。
6. `page_limit_reached/failed/not_started` 均进入 Pipeline 的 `missing_accounts`，并附带各自原因。

## 5. 容量规划与查询性能

### 5.1 数据量估算

假设：15 个公众号，每个每天平均 10 篇，不删除文章事实。

| 周期 | 新文章数 | 文章观察数估算 | 内容请求数估算 |
|---:|---:|---:|---:|
| 1 天 | 150 | 150-300 | 15-45 |
| 1 年 | 54,750 | 54,750-109,500 | 5,475-16,425 |
| 3 年 | 164,250 | 164,250-328,500 | 16,425-49,275 |
| 5 年 | 273,750 | 273,750-547,500 | 27,375-82,125 |
| 10 年 | 547,500 | 547,500-1,095,000 | 54,750-164,250 |

这里的接口 `count=5` 是 5 个 `publish_list` 发布记录，不是 5 篇文章；每个发布记录中的 `appmsgex` 可能包含多篇文章。因此请求数不能由“每天 10 篇”直接算成固定 3 页。上表按每号 1-3 页给出区间，观察数则为容量估算范围。

若第三页展开后仍全部为新文章且看不到旧水位线，该公众号必须进入 `page_limit_reached`，不更新水位线，由人工决定是否使用独立预算回填；不得在常规批次中无上限自动翻页。

其他 10 年量级：

- `collection_runs`：约 3,650 行。
- `collection_run_accounts`：约 54,750 行。
- `article_revisions`：只记录变化；若 5% 文章发生一次变化，约 27,375 行。
- `collection_events`：采用保留策略，不按 10 年无限增长。

按标题、摘要、URL、索引和 SQLite 页开销保守估计，10 年数据库约 3-8 GB；源字段明显变大时可能接近 10 GB。这个规模不是 SQLite 的容量瓶颈。

### 5.2 保留策略

| 数据 | 保留期 |
|---|---|
| 文章和 revision | 长期保留 |
| 批次、公众号结果、观察关系、请求账本 | 长期保留 |
| `collection_events` | 在线保留 180 天，更早事件按月归档或删除 |
| 轮转日志 | 30 天或受总容量上限控制 |
| 搜索候选 | 24 小时 |
| 登录会话元数据 | 1 年；秘密和二维码不入表 |

备份建议保留 7 个日备、4 个周备、12 个月备。备份任务使用 SQLite online backup API 或 `VACUUM INTO`，不能在文件仍写入时直接复制主文件。

### 5.3 列表查询规则

- 所有文章列表必须走索引和游标分页，禁止 OFFSET 深分页。
- 管理站点默认 `limit=50`，Pipeline 最大 `500`，公开站点默认 `20`。
- 列表查询只选择列表需要的字段，不 JOIN revisions/observations，不计算每篇文章的完整历史。
- 获取 `limit + 1` 行判断 `has_more`，不在每次列表请求执行全表 `COUNT(*)`。
- 文章详情再按 article_id 查询 revision 和观察历史，避免列表 N+1。
- 总览统计使用按日/公众号汇总表或批次已有计数，不实时扫描 articles。

标题/摘要关键词搜索不能长期使用 `%LIKE%` 全表扫描。文章达到 100,000 行前启用 SQLite FTS5 外部内容索引：

```text
article_search_fts(title, digest, content='articles', content_rowid='rowid')
```

FTS 同步通过 migration 定义的 trigger 或明确 Repository 写入维护，并有重建命令。未来迁移 PostgreSQL 时由 SearchRepository adapter 替换为 PostgreSQL full-text search，HTTP 接口不变。

### 5.4 性能目标

在 1,000,000 篇文章、1,500,000 条观察关系的合成测试库上：

| 操作 | 目标 |
|---|---:|
| 默认文章列表 50 条 | p95 < 150 ms |
| 单公众号文章列表 50 条 | p95 < 150 ms |
| FTS 关键词查询 50 条 | p95 < 300 ms |
| 文章详情 + 最近 100 条观察 | p95 < 200 ms |
| Pipeline 两自然日窗口，最多 500 条 | p95 < 300 ms |
| 页面级入库事务，50 篇以内 | p95 < 100 ms，不含网络 |

CI 至少验证 `EXPLAIN QUERY PLAN` 使用预期索引；发布前用百万行合成库执行性能基线。性能目标是应用验收标准，不假设所有硬件天然达到。

### 5.5 迁移 PostgreSQL 的触发条件

数据行数本身不是迁移理由。出现任一条件再评估 PostgreSQL：

- 需要多个写入实例或数据库跨机器部署。
- 公开站点持续并发导致 WAL 读写协调成为瓶颈。
- 百万行基线下索引查询持续达不到上述 p95，且已排除错误查询和硬件问题。
- 数据库文件超过约 20 GB，备份/恢复窗口难以接受。
- 需要复杂全文检索、分析查询或更严格的在线备份能力。

## 6. HTTP 基线

### 6.1 认证 scope

| scope | 使用方 | 权限 |
|---|---|---|
| `pipeline:read` | 资讯 Pipeline | Pipeline Read API |
| `site:read` | 未来站点 | Site Read API 字段白名单 |
| Admin session | 本地管理站点 | Admin API，写操作需 CSRF |

### 6.2 错误结构

```json
{
  "error": {
    "code": "COLLECTION_COOLDOWN_ACTIVE",
    "message": "采集处于冷却期",
    "request_id": "019...",
    "details": {
      "retry_after": "2026-07-31T06:20:00+08:00"
    }
  }
}
```

`message` 可展示；`details` 只含字段白名单。禁止返回异常堆栈和微信原始响应。

### 6.3 分页

文章接口使用不透明游标，排序固定为 `published_at DESC, id DESC`：

```json
{"page": {"next_cursor": "opaque-or-null", "has_more": false}}
```

最大 `limit=500`，默认 `100`。非法/过期游标返回 `400 INVALID_CURSOR`。

## 7. Pipeline Read API

### 7.1 文章查询

```http
GET /api/v1/pipeline/articles
  ?published_from=2026-07-29T00:00:00%2B08:00
  &published_before=2026-07-31T00:00:00%2B08:00
  &limit=500
Authorization: Bearer <pipeline:read token>
```

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-07-30T08:23:00+08:00",
  "batch": {
    "id": "019...",
    "status": "completed",
    "trigger": "scheduled",
    "finished_at": "2026-07-30T00:54:12+08:00",
    "planned_accounts": 15,
    "successful_accounts": 15,
    "coverage_ratio": 1.0,
    "is_fresh": true,
    "missing_accounts": []
  },
  "articles": [
    {
      "id": "019...",
      "source_article_id": "source-id",
      "account": {"id": "019...", "name": "公众号名称"},
      "title": "文章标题",
      "digest": "摘要",
      "url": "https://mp.weixin.qq.com/s/example",
      "cover_url": "https://example/cover.jpg",
      "published_at": "2026-07-29T09:20:00Z",
      "first_seen_at": "2026-07-29T15:58:00Z"
    }
  ],
  "page": {"next_cursor": null, "has_more": false}
}
```

文章来自规范事实表，不限定为最近批次新发现的文章。`batch` 单独表达采集质量。

### 7.2 其他 Pipeline 接口

| 方法与路径 | 响应 |
|---|---|
| `GET /api/v1/pipeline/batches/latest` | 最近一次 scheduled 批次质量 |
| `GET /api/v1/pipeline/accounts` | 启用公众号、最近成功和连续失败 |
| `GET /health/live` | 进程存活，不访问微信 |
| `GET /health/ready` | DB、migration 和 read model 就绪 |

## 8. Admin API

| 方法与路径 | 成功响应 | 说明 |
|---|---|---|
| `POST /api/v1/admin/session` | `204` | 管理员登录 |
| `DELETE /api/v1/admin/session` | `204` | 登出 |
| `GET /api/v1/admin/overview` | `200` | 总览聚合，只读本地状态 |
| `POST /api/v1/admin/login-sessions` | `202 {id,status}` | `channel=local_web/feishu` |
| `GET /api/v1/admin/login-sessions/{id}` | `200` | 登录状态 |
| `GET /api/v1/admin/login-sessions/{id}/qr` | `200 image/png` | 仅 local_web、活动会话 |
| `GET /api/v1/admin/login-sessions/{id}/events` | SSE | 状态变化 |
| `POST /api/v1/admin/login-sessions/{id}/cancel` | `202` | 幂等取消 |
| `GET /api/v1/admin/accounts` | `200` | 公众号列表 |
| `POST /api/v1/admin/accounts` | `201` | 从候选结果添加，默认启用但不立即采集 |
| `PATCH /api/v1/admin/accounts/{id}` | `200` | 启停、名称、顺序 |
| `POST /api/v1/admin/accounts/{id}/archive` | `200` | 删除订阅但保留历史数据 |
| `POST /api/v1/admin/accounts/{id}/restore` | `200` | 恢复已归档订阅，默认保持暂停 |
| `POST /api/v1/admin/account-searches` | `202 {search_id}` | 入队，不能同步搜微信 |
| `GET /api/v1/admin/account-searches/{id}` | `200` | 状态与候选结果 |
| `POST /api/v1/admin/collection-runs` | `202 {run_id}` | command + queued run |
| `POST /api/v1/admin/collection-commands` | `202` | 单个/多个公众号定点采集，返回逐项排队结果 |
| `GET /api/v1/admin/collection-runs` | `200` | 游标分页、筛选 |
| `GET /api/v1/admin/collection-runs/{id}` | `200` | 批次摘要 |
| `GET /api/v1/admin/collection-runs/{id}/accounts` | `200` | 公众号计划结果 |
| `GET /api/v1/admin/collection-runs/{id}/events` | SSE | 运行中状态变化 |
| `GET /api/v1/admin/articles` | `200` | 管理查询 |
| `GET /api/v1/admin/articles/{id}` | `200` | 当前值、revision、观察 |
| `GET /api/v1/admin/events` | `200` | 脱敏结构化事件 |
| `GET /api/v1/admin/effective-config` | `200` | 脱敏配置，只读 |

所有 POST/PATCH 必须验证 CSRF。`POST /collection-runs` 必须接受 `Idempotency-Key` header。

定点采集请求：

```http
POST /api/v1/admin/collection-commands
Idempotency-Key: manual-selection-uuid
Content-Type: application/json

{"account_ids": ["account-a", "account-b", "account-c"]}
```

```json
{
  "command_id": "019...",
  "run_id": "019...",
  "items": [
    {"account_id": "account-a", "outcome": "enqueued", "job_id": "job-1"},
    {"account_id": "account-b", "outcome": "promoted_existing_job", "job_id": "job-2"},
    {"account_id": "account-c", "outcome": "ignored_already_running", "active_run_id": "run-x"}
  ]
}
```

如果全部目标都被忽略，不创建空 run，`run_id` 返回 `null`；command 和 command_items 仍保留审计记录。

## 9. Site Read API（预留）

- `GET /api/v1/site/articles`
- `GET /api/v1/site/articles/{id}`
- `GET /api/v1/site/accounts`

仅输出已批准字段，不输出 fakeid、内部错误、请求账本、授权状态或源 payload。第一阶段只固定路径和字段边界，不要求实现。

## 10. Pipeline adapter 契约

现有 Pipeline 的微信 adapter 应替换为 `CollectorReadClient`：

1. 先取最近批次质量，再取自然日窗口文章。
2. 映射为 `NewsItem`，扩展字段包含 `collector_article_id/source_article_id/account_id/first_seen_at/latest_batch_id`。
3. 移除微信 source 内部 `lookback_hours` 过滤。
4. Read API 不可用、批次过旧或覆盖率不足时返回明确 degradation。
5. 不能捕获异常后静默返回空列表。
6. 导入公众号配置时按 `source_account_id` 去重，同 ID 多名称必须人工确认。

## 11. 契约测试清单

- 每个状态枚举都有数据库 CHECK 和序列化测试。
- `[from,before)` 在 UTC/上海时区边界测试通过。
- 重复 Idempotency-Key 返回同一 run_id。
- 同文章重复观察不新增文章/revision，hash 变化只新增一个 revision。
- `empty`、`not_started` 和请求失败在 API 中可区分。
- 游标排序在相同发布时间下稳定。
- 所有只读接口断言未调用 WeChat Gateway。
- 响应快照中不存在 `cookie/token/fakeid/uuid/raw_response`。
