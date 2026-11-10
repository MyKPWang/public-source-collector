# 公共资讯日报工具使用说明

这是一套运行在本机的公共资讯日报工具，由两个独立项目组成：

```text
公开资讯网站 / RSS
        |
        v
public-source-collector
抓取公开列表元数据、按来源限频、SQLite 缓存、审计与健康检查
        |
        v  http://127.0.0.1:4100（仅本机）
ai-news-pipeline
刷新缓存、按时间窗取数、跨源去重、筛选、MiniMax 改写、生成 HTML / 可选上传草稿
```

当前生产链路**不使用 Docker，不抓取微信公众号文章，也不需要微信扫码**。Collector 默认只采集列表页可获得的标题、链接、摘要、发布时间和封面等元数据；Pipeline 默认不抓文章详情页。

本文同时面向人工使用者和自动化 Agent。所有命令均以当前这台 Mac 的目录为例；如迁移目录，只需修改 Pipeline 的 `deploy/macos/local-machine.env`，不要修改脚本源码。

## 1. 目录与职责

| 组件 | 本机目录 | 负责什么 | 不负责什么 |
| --- | --- | --- | --- |
| Collector | `/Users/wangkp/Documents/wechat-paper-collect/public-source-collector` | 访问公开信息源、来源级限频与降级、SQLite 缓存、数据去重、采集审计、健康摘要、只读 API | 资讯相关性判断、日报改写、公众号发布 |
| Pipeline | `/Users/wangkp/Documents/tools/ai-news-pipeline` | 触发刷新、按日报时间窗读缓存、跨源去重、AI 筛选与改写、HTML 生成、可选草稿上传 | 直接访问网站、绕过来源限频、维护 Collector 数据库 |

重要配置与数据：

| 内容 | 位置 | 说明 |
| --- | --- | --- |
| 两个项目的本机协同配置 | `ai-news-pipeline/deploy/macos/local-machine.env` | 本机私密文件，保存项目路径和 Collector API Token，权限应为 `600` |
| Collector 运行配置 | `public-source-collector/collector.toml` | 数据库、日志、浏览器与健康检查配置 |
| 来源目录 | `public-source-collector/public-sources.toml` | 来源开关、列表入口、每个入口的最小采集间隔 |
| Pipeline 非敏感配置 | `ai-news-pipeline/config.yaml` | 取数范围、来源范围、模型与输出策略 |
| Pipeline 密钥 | `ai-news-pipeline/secrets.yaml` | MiniMax Key；只有正式发布时才需要公众号凭证 |

## 2. 首次配置与启动

### 2.1 配置本机 Token

Collector API 仅监听 `127.0.0.1:4100`，但读取和刷新接口仍需要 Bearer Token。编辑 `ai-news-pipeline/deploy/macos/local-machine.env`，填写：

```text
PUBLIC_SOURCE_COLLECTOR_PIPELINE_TOKEN=<随机长字符串>
```

可用下面的命令生成随机值，再手工填入文件：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
chmod 600 deploy/macos/local-machine.env
```

不要把 Token 写入 Git、`config.yaml`、plist 或命令行参数。`run_collector_api.sh` 与 `run_pipeline.sh` 会从该文件读取同一个值并自动传给两个进程。

同时确认：

1. `ai-news-pipeline/secrets.yaml` 已填写 `minimax_api.api_key`。
2. `public-source-collector/public-sources.toml` 中只启用了已验证的来源。
3. 已安装 Google Chrome。部分浏览器型来源需要它；Collector 使用自己的浏览器 Profile，不读取日常 Chrome Cookie。

### 2.2 初始化数据库与来源目录

在 Collector 目录执行一次：

```bash
cd /Users/wangkp/Documents/wechat-paper-collect/public-source-collector
.venv/bin/public-source-collector --config collector.toml db upgrade
.venv/bin/public-source-collector --config collector.toml sources sync
```

`sources sync` 会把 `public-sources.toml` 写入数据库；从目录中删除的来源或入口会在数据库中自动停用。修改来源目录后必须重新执行它。

若这是全新机器，先分别在两个项目中创建各自的 Python 虚拟环境并安装依赖。正常安装命令为：

```bash
# Collector
cd /Users/wangkp/Documents/wechat-paper-collect/public-source-collector
python3 -m venv .venv
.venv/bin/python -m pip install -e .

# Pipeline
cd /Users/wangkp/Documents/tools/ai-news-pipeline
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### 2.3 启动 API

新开一个终端，保持它持续运行：

```bash
cd /Users/wangkp/Documents/tools/ai-news-pipeline
./scripts/macos/run_collector_api.sh
```

另开一个终端验证：

```bash
curl http://127.0.0.1:4100/health/ready
python3 scripts/macos/doctor.py
```

`/health/ready` 返回 `{"status":"ready"}` 即表示 API 和 Collector 数据库可用。Doctor 同时检查 Token、配置、Chrome、CLI、端口与 `launchd` 安装状态，且不会输出密钥。手工运行时未安装 `launchd` 会显示一条预期告警；安装完两个 Job 后，再使用 `python3 scripts/macos/doctor.py --strict` 做无人值守验收。

## 3. 人工使用

### 3.1 日常生成一份日报

这是最常用的操作。API 已启动时，在 Pipeline 目录运行：

```bash
./scripts/macos/run_pipeline.sh
```

该命令完整执行以下过程：

1. 调用 Collector 的刷新接口，请求所有启用来源；每个来源仍受自身最小间隔控制，不会因重复点击而重复访问网站。
2. 刷新结束后，按上海时区查询“昨天 00:00（含）至明天 00:00（不含）”的 SQLite 缓存。
3. Pipeline 对结果做跨源去重、时间校验、AI 相关性筛选和 MiniMax 改写。
4. 默认只生成审阅文件与 HTML，不上传微信公众号草稿箱。

默认输出位置：

| 内容 | 位置 |
| --- | --- |
| 原始、处理后、待审阅数据 | `ai-news-pipeline/output/raw_*.json`、`processed_*.json`、`review_*.json` |
| 最终 HTML | `ai-news-pipeline/wechat-publish-tool/output/news_*.html` |
| Pipeline 日志 | `ai-news-pipeline/logs/app_YYYYMMDD.log`、`logs/llm_YYYYMMDD.jsonl` |
| Pipeline 运行审计 | `ai-news-pipeline/data/news.db` |

本机环境文件中的 `PIPELINE_NO_PUBLISH_DEFAULT=true` 会强制默认不发布。只有在人工明确确认、`secrets.yaml` 已配置公众号凭证且 `config.yaml` 的发布开关也符合预期时，才应临时将该值改为 `false` 后运行；这不是日常操作。

### 3.2 只采集，不生成日报

用于验收新来源、排查某来源或希望先把缓存补齐时：

```bash
cd /Users/wangkp/Documents/wechat-paper-collect/public-source-collector

# 所有启用来源
.venv/bin/public-source-collector --config collector.toml sources collect

# 只采集一个或几个来源；--source 可重复
.venv/bin/public-source-collector --config collector.toml sources collect \
  --source jiqizhixin --source qwen
```

不要为了“确保成功”反复执行全量采集。若某来源在最小间隔内已采集，系统会跳过网络请求并使用已有缓存；这是预期的保护行为，不是卡住。

当前已启用来源及定点采集 `slug`：

| `slug` | 来源 |
| --- | --- |
| `ifanr` | 爱范儿 |
| `google-ai` | Google AI |
| `openai` | OpenAI News |
| `infoq` | InfoQ AI Briefs |
| `qbitai` | 量子位 |
| `huxiu` | 虎嗅 AI 资讯 |
| `aibase` | AIBase 新闻 |
| `android-developers` | Android Developers |
| `aiera` | 新智元 |
| `jiqizhixin` | 机器之心 |
| `zhidx` | 智东西 |
| `apple-developer` | Apple Developer |
| `qwen` | 千问 |

来源开关以 `public-sources.toml` 为准；上表在修改目录后应同步更新。当前未启用的来源不会被全量采集，也不会进入日报。

### 3.3 查看采集结果、失败原因与健康状态

每次采集均有一个 `run_id`。采集后依次执行：

```bash
cd /Users/wangkp/Documents/wechat-paper-collect/public-source-collector
.venv/bin/public-source-collector --config collector.toml sources diagnose
.venv/bin/public-source-collector --config collector.toml sources health
```

需要查看某一批时增加 `--run-id <run_id>`。`diagnose` 用于定位每个来源/入口的 accepted、rejected、new、updated、耗时和原因码；`health` 用于看整体是否存在失败、连续空列表、来源过期或数量异常。

相关文件：

| 文件 | 用途 |
| --- | --- |
| `public-source-collector/data/collector.log` | 结构化采集日志 |
| `public-source-collector/data/health/latest.json` | 最近一次采集的健康摘要 |
| `public-source-collector/data/health/<business-date>-<run-id>.json` | 每次采集的历史健康摘要 |
| `ai-news-pipeline/logs/collector-*.launchd.*.log` | `launchd` 启动的 API / 定时采集进程日志 |

### 3.4 新增、删除或临时停用来源

1. 编辑 `public-source-collector/public-sources.toml`。
2. 新来源必须先确认列表页能稳定给出标题、规范链接、发布时间和摘要；正常路径不抓详情页。
3. 将新来源的 `enabled` 设为 `true`，并为入口设置合理的 `minimum_interval_seconds`。
4. 执行 `sources sync`，再用该来源的 `--source <slug>` 进行一次定点采集与诊断。

临时停用时，保留来源定义、将 `enabled = false` 即可；删除来源定义后再执行 `sources sync`，数据库中的对应来源和入口会被停用，但历史文章与审计记录仍保留。

## 4. 自动运行

自动采集由 macOS `launchd` 完成，而不是由 Pipeline 自己轮询：

| Job | 作用 |
| --- | --- |
| `com.wangkp.ai-news.collector-api` | 登录后启动 Collector API，异常退出时自动重启 |
| `com.wangkp.ai-news.collector-refresh` | 每天 23:50 触发一次采集，并增加 0 到 120 秒随机抖动 |

首次安装（确认 Token 已配置后）在 Pipeline 目录执行：

```bash
mkdir -p "$HOME/Library/LaunchAgents"
cp deploy/macos/com.wangkp.ai-news.collector-api.plist "$HOME/Library/LaunchAgents/"
cp deploy/macos/com.wangkp.ai-news.collector-refresh.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.wangkp.ai-news.collector-api.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.wangkp.ai-news.collector-refresh.plist"
launchctl kickstart -k "gui/$(id -u)/com.wangkp.ai-news.collector-api"
```

定时采集只负责更新缓存。日报可由人工随时执行，也可由已有的 Pipeline 调度任务触发；它会再次请求刷新，但 Collector 会依据来源级间隔和幂等键避免无意义的重复请求。

## 5. Agent 使用约定

Agent 的正常入口是 Pipeline 包装脚本，而不是直接操作数据库或模拟浏览器：

```bash
cd /Users/wangkp/Documents/tools/ai-news-pipeline
./scripts/macos/run_pipeline.sh
```

### Agent 可以自主执行

- 运行 `python3 scripts/macos/doctor.py`，读取失败项；已安装 `launchd` 的无人值守环境可增加 `--strict`。
- 启动或检查本机 API：`./scripts/macos/run_collector_api.sh`、`curl http://127.0.0.1:4100/health/ready`。
- 发起一次全量或指定来源的采集；排错时优先用 `--source <slug>`，不要形成重试循环。
- 运行 `sources diagnose`、`sources health`，读取 Collector 与 Pipeline 日志、健康 JSON 和输出 JSON。
- 修改 `public-sources.toml` 后运行 `sources sync`，并用定点采集验证新增来源。
- 运行 Pipeline 的默认无发布日报，审阅 `output/` 和生成 HTML。
- 在配置允许的范围内调整来源开关、最小间隔或浏览器适配器，并通过定点采集和测试验证。

### Agent 不应自主执行

- 不得读取、打印、提交或复制 `local-machine.env`、`secrets.yaml` 中的密钥。
- 不得直接写 `collector.db` 或 `news.db`；所有业务写入必须经 Collector CLI/API 或 Pipeline。
- 不得为绕过限频而反复重试、缩短最小间隔、切换伪造身份或抓取文章详情页。
- 不得开启草稿上传、发布公众号内容、改动发布凭证，除非有人工明确指令。
- 不得把 API 监听地址改为局域网或公网地址；当前设计仅支持 `127.0.0.1`。
- 不得卸载、停用或重置 `launchd` Job，除非人工明确要求。

### Agent 调用 API（仅在需要系统集成时）

Pipeline 已封装 API；其他自动化系统若确实需要集成，可遵循下列顺序。Token 只从受保护环境变量传入，不要写进命令历史。

```bash
# 仅示例：由安全的运行环境注入 TOKEN。
curl -X POST http://127.0.0.1:4100/api/v1/pipeline/refresh \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"source_slugs":["google-ai","openai"],"idempotency_key":"external-20260809-001"}'

curl -G http://127.0.0.1:4100/api/v1/pipeline/articles \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'published_from=2026-08-08T00:00:00+08:00' \
  --data-urlencode 'published_before=2026-08-10T00:00:00+08:00' \
  --data-urlencode 'limit=500' \
  --data-urlencode 'batch_id=<refresh 返回的 id>'
```

`POST /api/v1/pipeline/refresh` 的 `source_slugs` 为空时代表所有启用来源；`idempotency_key` 建议由调用方生成并在同一业务任务内复用。`GET /api/v1/pipeline/articles` 返回的是**完整缓存中符合时间范围的数据**，不是该刷新批次新增的文章；`batch_id` 仅用于确认对应刷新已结束。分页时沿用返回的 `page.next_cursor`，直到 `has_more` 为 `false`。

## 6. 常见现象与排查顺序

| 现象 | 先检查什么 | 正确处理 |
| --- | --- | --- |
| `sources health` 显示 `NO_COLLECTION_RUN` | 是否从未成功采集 | 执行一次 `sources collect`，再查看健康摘要 |
| Pipeline 提示 API 不可用或 Token 缺失 | `doctor`、`/health/ready`、`local-machine.env` | 启动 API，核对 Token 已填写且文件权限为 `600` |
| 某来源没有新数据或显示 skipped | `sources diagnose` 的原因码、入口最小间隔 | 等待间隔到期；不要重复点击全量采集 |
| 采集为 `partial` | `sources diagnose --run-id <id>` | 只对失败来源做定点验证；其他来源数据仍已入库，Pipeline 会记录降级 |
| 采集为 `failed` | Collector 日志、浏览器可用性、来源入口 | 修复 API/适配器后定点采集；Pipeline 不会用未知状态缓存生成日报 |
| 日报条目少 | `raw_*.json`、`processed_*.json`、`review_*.json` | 区分“来源没采到”“时间窗不符”“被去重/筛选”“改写进入 review”四类原因 |
| 数据量增长担忧 | 无需全表扫描 | 报表查询已使用 `published_at + id` 与 `source_id + published_at + id` 索引；按时间窗分页读取 |

## 7. 一次标准验收

完成首次配置后，按下面顺序验收即可：

```bash
# 终端 A：启动 API
cd /Users/wangkp/Documents/tools/ai-news-pipeline
./scripts/macos/run_collector_api.sh

# 终端 B：环境检查、采集、诊断、生成无发布日报
cd /Users/wangkp/Documents/tools/ai-news-pipeline
python3 scripts/macos/doctor.py

cd /Users/wangkp/Documents/wechat-paper-collect/public-source-collector
.venv/bin/public-source-collector --config collector.toml sources collect
.venv/bin/public-source-collector --config collector.toml sources diagnose
.venv/bin/public-source-collector --config collector.toml sources health

cd /Users/wangkp/Documents/tools/ai-news-pipeline
./scripts/macos/run_pipeline.sh
```

验收成功的标志是：Collector 健康摘要有本次运行记录，Pipeline 生成了 `raw`、`processed`、`review` 数据和 HTML，且全过程没有公众号发布动作。
