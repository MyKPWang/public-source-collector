# 实施指南

> 目标：让新开发者或 AI Agent 在不重新做架构推演的情况下开始编码。本文定义技术栈、目标目录、依赖方向、开发阶段、测试门槛和完成定义。

> 实施状态（2026-07-30）：Phase 1、2、3、5 的核心代码已落地；管理站点、加密 Vault 和本机 Playwright 扫码已落地。飞书二维码投递仍未实施，不能按本文的接口假定其可用。

## 1. 当前起点

当前实现位于 `src/wechat_list_collector/`：

| 现有模块 | 可保留价值 | 不能直接视为目标实现的部分 |
|---|---|---|
| `wechat_client.py` | 列表请求参数和响应解析样本 | 内部隐藏分页，分页绕过逐请求 Gate |
| `throttle.py` | 持久化间隔/冷却概念 | 无抖动、预算、请求账本和原子多进程许可 |
| `service.py` | 串行停止和日志事件 | 模块职责过多，无批次公众号/分页事务 |
| `store.py` | SQLite 可行性验证 | 启动建表、明文凭据、表模型不完整 |
| `session_import.py` | 迁移期导入工具 | 不能作为最终授权方案 |
| `cli.py` | 命令入口基础 | 长任务直接执行，没有 Inbox/Worker |
| `logging_config.py` | JSON 日志和秘密排除原则 | 需补 request/account/reason 上下文 |

开始每个阶段前先运行：

```bash
python -m unittest discover -s tests -v
```

测试不得访问真实微信。真实登录和采集只能通过明确的手工 smoke 命令执行。

## 2. 锁定技术栈

| 类别 | 选择 | 用途 |
|---|---|---|
| 运行时 | Python 3.11+ | 当前基线 |
| HTTP client | `requests` | 微信协议，保留可控 Session |
| Web | FastAPI + Uvicorn | Admin API、Read API、SSE |
| Schema | Pydantic v2 | 命令、配置和 API 契约 |
| ORM/SQL | SQLAlchemy 2.x | 明确事务和未来 PostgreSQL 迁移 |
| Migration | Alembic | 版本化 schema |
| 模板 | Jinja2 | Admin 服务端页面 |
| 秘密 | `keyring` + `cryptography` fallback | Credential Vault adapters |
| 测试 | unittest 或 pytest（二选一后统一） | 单元/集成/契约 |
| 页面验收 | Playwright | 状态与响应式截图 |

不要引入 Redis、Celery、APScheduler、Playwright 抓取、React 或消息队列服务器。持久化 Command Inbox 使用 SQLite 即可。

若继续使用现有 unittest，整个重构期间保持 unittest，避免仅为风格迁移测试框架。新增第三方依赖时固定兼容版本范围并更新 README 安装说明。

## 3. 目标目录

```text
wechat-list-collector/
├─ alembic.ini
├─ migrations/
│  ├─ env.py
│  └─ versions/
├─ src/wechat_list_collector/
│  ├─ domain/
│  │  ├─ enums.py
│  │  ├─ errors.py
│  │  ├─ models.py
│  │  └─ policies.py
│  ├─ application/
│  │  ├─ command_inbox.py
│  │  ├─ collection_runner.py
│  │  ├─ login_coordinator.py
│  │  ├─ account_management.py
│  │  └─ read_models.py
│  ├─ infrastructure/
│  │  ├─ db/
│  │  │  ├─ engine.py
│  │  │  ├─ schema.py
│  │  │  └─ repositories.py
│  │  ├─ wechat/
│  │  │  ├─ gateway.py
│  │  │  ├─ login_gateway.py
│  │  │  └─ parsers.py
│  │  ├─ request_gate.py
│  │  ├─ credential_vault.py
│  │  ├─ feishu.py
│  │  └─ logging.py
│  ├─ entrypoints/
│  │  ├─ cli.py
│  │  ├─ worker.py
│  │  ├─ admin_app.py
│  │  └─ read_api.py
│  ├─ web/
│  │  ├─ templates/
│  │  └─ static/
│  ├─ config.py
│  └─ bootstrap.py
├─ tests/
│  ├─ fixtures/wechat/
│  ├─ unit/
│  ├─ integration/
│  ├─ contract/
│  └─ web/
└─ docs/
```

### 3.1 依赖方向

```text
entrypoints ──> application ──> domain
      │               │
      └────> infrastructure <──┘（通过 Protocol 注入）
```

- `domain` 不导入 FastAPI、requests、SQLAlchemy 或文件系统。
- `application` 定义所需 Protocol，不构造具体 adapter。
- `infrastructure` 实现数据库、微信、Vault、飞书和日志 adapter。
- `entrypoints` 只解析输入、调用 application、映射输出。
- `bootstrap.py` 是唯一组装具体 adapter 的 composition root。

不要为每个小函数创建文件。以上目录是职责划分，不要求机械地把所有类拆开；同一领域内高度关联的模型和策略应保持 locality。

## 4. 核心接口

以下是实现方向，具体字段以数据/API 契约为准。

```python
class CommandInbox(Protocol):
    def submit(self, command: Command) -> SubmittedCommand: ...
    def claim_next(self, worker_id: str, lease_seconds: int) -> ClaimedCommand | None: ...
    def renew(self, command_id: str, worker_id: str) -> None: ...
    def finish(self, command_id: str, status: CommandStatus, reason_code: str = "") -> None: ...


class RequestGate(Protocol):
    def acquire(self, intent: RequestIntent) -> RequestPermit: ...
    def complete(self, permit: RequestPermit, outcome: RequestOutcome) -> None: ...


class WeChatGateway(Protocol):
    def search_accounts(self, keyword: str, limit: int) -> list[AccountCandidate]: ...
    def fetch_article_page(self, fakeid: str, begin: int, count: int) -> ArticlePage: ...


class CollectionRepository(Protocol):
    def create_plan(self, run_id: str, account_ids: list[str]) -> None: ...
    def store_page(self, page_result: PageStoreCommand) -> PageStoreResult: ...
    def finish_run(self, run_id: str, status: RunStatus, reason_code: str = "") -> RunSummary: ...


class AccountJobQueue(Protocol):
    def attach_or_enqueue(self, run_id: str, account_id: str, priority: int) -> JobSubmission: ...
    def claim_next(self, worker_id: str, lease_seconds: int) -> AccountCollectionJob | None: ...
    def finish(self, job_id: str, result: AccountJobResult) -> None: ...


class CredentialVault(Protocol):
    def load_wechat(self) -> WeChatCredentials: ...
    def replace_wechat(self, credentials: WeChatCredentials, profile: SessionProfile) -> int: ...


class LoginCoordinator(Protocol):
    def start(self, channel: LoginChannel, requested_by: str) -> LoginSession: ...
    def get(self, session_id: str) -> LoginSession: ...
    def cancel(self, session_id: str) -> LoginSession: ...
```

接口错误使用领域错误，不把 `requests`、SQLite 或 FastAPI 异常泄漏给 application 调用方。

## 5. 编码不变量

Agent 每次提交改动前逐项检查：

1. 微信内容请求是否全部经过 `RequestGate.acquire`。
2. Gateway 方法是否只发一个 HTTP 请求且无自动重试。
3. 网络调用期间是否没有数据库写事务。
4. 每个请求是否先有 `wechat_requests` 意图记录。
5. 日志和异常是否排除了 Cookie、token、fakeid、微信 UUID 和原始响应。
6. Pipeline/Read API 是否完全不导入或调用 WeChat Gateway。
7. 文章时间过滤是否仍然只属于 Pipeline。
8. 新状态是否同步更新枚举、CHECK、API schema、页面和测试。
9. 幂等键重复时是否返回原对象而非重复执行。
10. 错误路径是否产生明确终态而非空结果。

## 6. 分阶段开发

阶段必须按顺序推进。一个阶段的“退出条件”未满足，不应同时铺开下一阶段的大量代码。

### Phase 1：数据底座

**目标**：建立后续所有状态的单一事实来源。

工作项：

- 加入 SQLAlchemy/Alembic，创建初始 migration。
- 实现数据契约中的核心表、约束和索引。
- 开启 WAL、foreign keys、busy timeout。
- 实现 Repository：命令幂等提交、计划创建、页面事务入库、批次汇总。
- 从旧表迁移 feeds/articles/runs；明文 credentials 迁出或标记待迁。
- 正确区分 new、updated、observed。

测试：

- migration 可在空库升级，并可从旧 MVP fixture 升级。
- 重复文章、hash 变化、重复 observation、页面回滚；新文章不生成冗余 revision。
- 重复 idempotency key 返回同一 command/run。
- 覆盖率只能从 run_accounts 计算。
- 百万文章/150 万观察合成库的索引计划和查询基线。

退出条件：不调用微信也能构造完整 completed、partial、rate_limited 批次并通过查询验证。

### Phase 2：Request Gate 与 Collection Worker

**目标**：所有内容请求只有一条可审计路径。

工作项：

- 实现 SQLite Command Inbox 和单实例 Worker lease/heartbeat。
- 将 collect 命令展开为独立 `account_collection_jobs`，使用 partial unique index 防止同一公众号重复 queued/running。
- 允许多个批次关联同一个 queued job，完成后向所有关联批次回填结果。
- 实现定点采集的 enqueued/promoted/ignored 逐项结果和人工优先队列。
- 实现 Request Gate 的间隔、抖动、批次 45 次上限、滚动 24 小时 60 次上限、冷却和原子许可。
- 将 WeChat Gateway 改为一方法一请求，移除内部分页。
- 将 `count=5` 命名为发布记录页大小，并分别记录 publish_record_count 与展开后的 article_count。
- 实现 Collection Runner、确定性计划轮转和自适应分页。
- 归一化 `200013/200003/network/protocol` 错误。
- CLI 改为提交命令/查询状态；保留显式 worker 命令。

测试：

- FakeClock/FakeRandom 下精确验证每页间隔。
- 两个 Gate 实例竞争时只有一个获得许可。
- 多选公众号严格串行；running 目标被忽略，queued 目标只提升优先级。
- 跨午夜不会重置滚动 24 小时预算；搜索和人工采集占用同一预算。
- `200013` 停止剩余计划并持久化冷却。
- Worker 崩溃和 lease 恢复不立即重发 unknown 请求。
- max page、watermark、empty 和公平轮转。
- 使用固定响应证明一个 publish_list 记录可展开多篇文章；不得把 count 当成文章数。
- 模拟第二页与第一页重叠，证明当前 job 重复项不会被误判为历史水位线或导致提前停止。
- 模拟分页期间列表头部新增发布记录，验证跨页去重、冻结水位线和 page_limit 终态。

退出条件：使用 Fake Gateway 完成 15 公众号批次，任一页都能从 run 追到 request 和文章。

### Phase 3：Read API 与 Pipeline

**目标**：Pipeline 完全脱离 `we-mp-rss` 和微信实时请求。

工作项：

- 实现 FastAPI Read API、Bearer scope、游标分页和错误结构。
- 实现 latest scheduled batch 的 freshness/coverage 计算。
- 在实际 Pipeline 中加入 `CollectorReadClient`。
- 移除旧 we-mp-rss 登录/搜索/详情调用和 source-local lookback。
- 在 Pipeline 统一过滤模块实现前一日 00:00 到次日 00:00 窗口。
- 增加 source degradation 记录。

测试：

- Read API 断言零 WeChat Gateway 调用。
- 时间区间、同时间稳定排序、游标续页。
- completed/partial/stale/missing batch 的 Pipeline 行为。
- Read API 失败不会变成“成功 0 篇”。

退出条件：Pipeline 仅凭本系统数据库生成包含微信资讯的测试报告，并正确呈现降级。

### Phase 4：原生扫码与 Credential Vault

**目标**：不依赖 `wx.lic` 或 we-mp-rss 完成授权。

工作项：

- 参考已研究的扫码协议实现 Login Gateway，不复制上游全局变量/静态文件设计。
- 实现 Login Coordinator 状态机、单活动会话和进程重启过期。
- 实现 Keyring Vault 和 encrypted-file fallback。
- 登录与内容 Worker 使用互斥操作锁。
- 验证成功后原子换证；失败保留旧凭据。
- 保留 `import-werss-license` 作为一次性迁移工具并标记 deprecated。

测试：

- Fake Login Gateway 覆盖全部状态转换。
- 重复 start 返回同一会话。
- 过期、取消、进程重启和验证失败。
- 秘密扫描测试覆盖日志、DB 和 API fixture。

退出条件：人工本机扫码 smoke 成功，随后单公众号列表采集成功；普通数据库中无明文凭据。

### Phase 5：Admin API 与管理站点

**目标**：日常运维不需要 CLI、SQL 和原始日志。

工作项：

- 实现管理员登录、session、CSRF 和审计。
- 实现 Admin API 与 Jinja2 页面。
- 登录状态和运行状态使用 SSE；实现断线轮询降级。
- 完成总览、授权、公众号、批次、文章、诊断、设置页面。
- 完成公众号暂停、归档、恢复；归档不删除历史事实。
- 完成单个采集、多选采集、逐项提交结果和实时队列状态。
- 按 [ADMIN_UI.md](ADMIN_UI.md) 实现全部状态和响应式布局。

测试：

- Admin API 权限、CSRF、幂等和字段脱敏。
- Playwright fixture 覆盖页面状态和三个 viewport。
- 页面刷新不触发微信。
- 百万文章 API fixture 下只做服务端 cursor 分页，列表 DOM 不超过约 100 行。

退出条件：运维人员只用部署机浏览器即可添加公众号、扫码、提交采集和定位失败。

### Phase 6：飞书、调度与生产运维

**目标**：完成远程重授权和单机长期运行闭环。

工作项：

- 实现 Feishu QR Delivery adapter 和用户 ID 白名单。
- 使用飞书长连接或已有 OpenClaw 通道接收授权命令，不开放公网 webhook。
- 提供 systemd/launchd 示例和 Worker/Web/API service 定义。
- 实现 backup、restore、integrity-check 命令。
- 增加 health/readiness、Worker heartbeat 和告警摘要。

测试：

- Fake Feishu adapter 验证二维码/状态消息无秘密。
- 定时重复唤醒只有一个 scheduled run。
- 空环境恢复备份后 API/批次/文章一致。

退出条件：无需内网穿透完成飞书扫码；连续运行和重启演练不丢状态、不重复请求。

## 7. 测试结构

### 7.1 Test doubles

- `FakeClock`：控制当前时间和 sleep。
- `FakeRandom`：固定抖动。
- `FakeWeChatGateway`：按调用序列返回页面或领域错误。
- `FakeLoginGateway`：驱动扫码状态。
- `InMemoryCredentialVault`：测试换证，不写系统钥匙串。
- `FakeQrDelivery`：记录图片和消息，不访问飞书。

Application 测试应只通过模块接口注入这些 adapter，不 patch 私有实现细节。

### 7.2 测试层级

| 层级 | 范围 | 是否可访问网络 |
|---|---|---:|
| Unit | 策略、状态机、解析、hash、时间窗 | 否 |
| Integration | SQLite migration、Repository、Gate 竞争 | 否 |
| Contract | API schema、微信响应 fixture、错误映射 | 否 |
| Web | Jinja 页面、SSE、Playwright fixture | 否 |
| Manual smoke | 扫码、单公众号一页 | 仅人工显式 |

## 8. 日志规则

统一上下文字段：

```text
timestamp level event request_id command_id run_id account_id duration_ms reason_code
```

日志事件使用稳定英文 snake_case，例如 `command_claimed`、`request_waiting`、`article_page_stored`、`rate_limit_opened`。用户可见中文由 UI 根据 reason code 映射，不直接依赖日志消息。

禁止日志：Cookie、token、fakeid、微信 UUID、二维码内容、搜索词、完整请求参数、原始响应、未脱敏异常字符串。

## 9. 配置与入口

目标 CLI：

```text
wechat-list-collector db upgrade
wechat-list-collector worker
wechat-list-collector admin
wechat-list-collector api
wechat-list-collector enqueue collect-all --trigger scheduled --business-date YYYY-MM-DD
wechat-list-collector enqueue collect-account --account-id UUID
wechat-list-collector auth login --channel local|feishu
wechat-list-collector status
wechat-list-collector backup
wechat-list-collector restore --from PATH
wechat-list-collector integrity-check
```

配置分组：`storage/collection/rate_limit/schedule/admin/read_api/feishu/logging`。秘密配置只填写环境变量名，不在 TOML 中放值。

## 10. Definition of Done

每个 PR/变更集必须满足：

- 对应实施阶段和文档契约明确。
- 新行为有成功、错误、幂等和恢复测试。
- migration 有升级测试；破坏性迁移有恢复说明。
- 新 HTTP 字段有 Pydantic schema 和契约测试。
- 新页面处理 loading/empty/partial/failed。
- 无真实网络测试，无秘密进入 fixture/日志/快照。
- `python -m unittest discover -s tests -v` 通过。
- 更新 README 当前能力，不把目标功能写成已实现。

## 11. Agent 接手模板

新 Agent 开始任务时，应先输出并遵循：

```text
当前阶段：Phase N
本次目标：一句话
涉及模块：列出目标文件/模块
保持不变：列出相关锁定决策
需要新增的测试：列出测试行为
完成判断：引用该阶段退出条件
```

如果任务跨越两个阶段，先完成前一阶段的最小纵向闭环，再开始后一阶段；不要一次性创建所有空目录和占位类。

## 12. 下一步

当前应从 **Phase 1：数据底座** 开始。第一项可独立交付的工作是：

1. 引入 SQLAlchemy/Alembic。
2. 建立初始 migration，先覆盖 `official_accounts/articles/article_revisions/collection_commands/collection_runs/collection_run_accounts/collection_run_articles`。
3. 用 Repository 集成测试证明命令幂等和文章 revision 语义。

在这三项完成前，不开始管理页面和真实扫码实现。
