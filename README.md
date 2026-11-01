# RSS Aggregator

GitHub Actions 驱动的 RSS 采集 + AI 分类打分工具，同时把清洗后的文章正文永久归档为 Markdown。

> **核心问题：能不能用最少的人工干预，自动从海量 RSS 文章中筛选出值得阅读的内容，并长期保留原文？**

## 设计原则

| 原则 | 说明 | ADR |
|------|------|-----|
| Output append-only | `output/YYYY/MMDD.md` 只追加不删除；过去日期只做幂等追加/修复，同一天可扩展 | [ADR-003](docs/ADR/003.output.immutable-output.md) |
| 原文永久归档 | 清洗后的 Markdown 写入 `archive/YYYY/MM/DD.md`，append-only；每 feed 用偏移索引 | [ADR-017](docs/ADR/017.archive.permanent-markdown.md) |
| 分支职责分离 | `main` 代码/配置/文档，`data` 保存归档、索引、元数据和输出 | [ADR-017](docs/ADR/017.archive.permanent-markdown.md) |
| Article 14 天窗口 | `articles.json` 保存最近 14 天的待分类/已采集元数据 | [ADR-004](docs/ADR/004.cache.fifteen-day-window.md) |
| Fetch/Classify 独立 | 采集和分类可以分开运行 | [ADR-005](docs/ADR/005.architecture.fetch-classify-decoupled.md) |
| Per-feed 筛选 | `skip_prompt` 按 feed 粒度配置 | [ADR-006](docs/ADR/006.filter.per-feed-skip-prompt.md) |

ADR-016 的“正文只进 cache、data 只存元数据”已被 ADR-017 取代。

## 功能

- **RSS 采集** — 并行抓取，14 天窗口内的文章
- **Markdown 归档** — 正文清洗后 append 到 `archive/YYYY/MM/DD.md`，永久保留
- **每 feed 索引** — `feeds/<feed_id>.jsonl` 按 feed 聚合，保存字节偏移和分类 annotation
- **Aliveness 监控** — HTTP 健康检测，连续失败自动禁用 [ADR-010](docs/ADR/010.health.aliveness-monitoring.md)
- **AI 分类/打分** — 支持 OpenAI、Claude、OpenRouter [ADR-008](docs/ADR/008.ai.multi-provider.md)
- **无 AI 降级** — 未配置 key、provider 故障或模型未返回结果时，仍然输出标题；Category/Summary/Score 留空（`—`）
- **聚合检测** — 自动识别 HN/Reddit 等聚合源并记录到 state（当前只做统计，不跳过分类） *(draft: [ADR-009](docs/ADR/009.filter.aggregation-detection.md))*
- **静态站点 + JSON Feed** — 从归档渲染每日页面、feed 页面和 JSON Feed 1.1，发布到 GitHub Pages
- **人工摘要** — 继续生成 `output/YYYY/MMDD.md` 表格，便于快速浏览

## 快速开始

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
python -m src.main
```

也可以按步骤运行：

```bash
python -m src.main --fetch      # 抓取 RSS，清洗正文，追加 archive/ 和 feeds/
python -m src.main --classify   # 按偏移读取归档正文，AI 分类，写 output/
python -m src.main --cleanup    # 清理 articles.json 中超过 14 天的条目
python -m src.main --report     # 生成 status.json（最近一次运行状态）
python -m src.main --check      # 校验运行时数据/输出契约
python -m src.render --site _site --archive archive --feeds feeds  # 渲染站点
```

## 流程

```
--sync      从 sources.toml 拉取外部 RSS 源列表 → 合并到 feeds.toml（独立 workflow）
--fetch     抓取 RSS，清洗正文 → 追加 archive/YYYY/MM/DD.md 和 feeds/<feed_id>.jsonl
            articles.json 只写元数据 + 归档偏移
--classify  读取 articles.json，按 archive_file/offset/length hydrate 正文；
            AI 分类后写 output/YYYY/MMDD.md 和 feeds/ 的 annotation 记录
--cleanup   清理 articles.json 中超过 14 天的老条目（不删除归档）
--report    写入 status.json，记录本次运行概况
--check     校验数据/输出契约；在部署后运行，失败时把 job 标记为失败并报警
```

- 各步骤完全独立，可任意顺序重跑
- `output/` 和 `archive/` 都是 append-only：已写入的记录/行不删除，重复运行只追加去重后的新内容
- `articles.json` 不保存正文；正文只在归档中

## 分支模型

| 分支 | 内容 | 写入方式 |
|------|------|----------|
| `main` | `src/`、`tests/`、`docs/`、`config.toml`、`feeds.toml`、`sources.toml`、workflow | 普通提交；`sync-feeds` 每周提交 `feeds.toml` |
| `data` | `state.json`、`articles.json`（仅元数据）、`status.json`、`archive/`、`feeds/` | 每天最多一次快照，普通 push；冲突时自动 rebase 重试一次，仍冲突则需人工处理 |

- 只有 `main` 和 `data` 两个长期分支。GitHub Pages 直接从 Actions artifact 部署，**没有 `gh-pages` 分支**。
- `data` 是持久归档：`archive/`、`feeds/` 只追加；`state.json` / `articles.json` / `status.json` 每轮整体替换。`output/` 是 cache 内的人工摘要，不进入 `data`，丢失后可从归档重新渲染。
- 正文以 sanitized Markdown 形式进入 `data` 的 git 历史。仓库是公开的，因此只保存清洗后的 Markdown，不保存原始 HTML/脚本。
- `data` 由 CI 写入。需要人工恢复时先暂停 workflow，修改后普通 push（不要 force-push），并清理 `rss-runtime-*` cache，否则下一轮会优先使用热 cache；步骤见 RUNBOOK。
- 恢复、回滚、cache 驱逐与删除旧 `gh-pages` 见 [RUNBOOK](docs/RUNBOOK.md)。

### data 分支布局

```
data/
  state.json               # feed 健康、ETag、失败计数
  articles.json            # 14 天窗口元数据；无 content/content_length
  status.json              # 最近一次运行报告
  archive/
    2026/
      05/
        01.md              # 永久归档，append-only
  feeds/
    example.com-1a2b3c4d5e6f.jsonl
```

`output/`（人工摘要表格）只存在于 cache 和本地运行目录，不进入 `data`；页面展示以 `archive/` + `feeds/` 渲染结果为准。

## 归档格式

每日文件 `archive/YYYY/MM/DD.md` 由一条条记录拼接而成：

```markdown
<!-- rss-aggr:article v1
{"v":1,"guid":"tag:example.com,2026:1","feed_id":"example.com-1a2b3c4d5e6f","feed_url":"https://example.com/feed.xml","site_url":"https://example.com/","link":"https://example.com/post/1","title":"An example post","author":"Alice","published":"2026-05-01T08:00:00Z","fetched_at":"2026-05-01T09:12:00Z","body_len":42,"body_sha256":"..."}
-->
Body **Markdown** goes here.

<!-- /rss-aggr -->
```

- 头部 JSON 包含 `body_len`（UTF-8 字节数）和 `body_sha256`；正文按字节长度定界，正文中出现 footer 文本也不会破坏解析。
- 偏移量全部是文件内的 UTF-8 字节偏移，支持中文和 emoji；`.gitattributes` 将 `archive/**/*.md` / `feeds/*.jsonl` 固定为 LF，避免 checkout 改变偏移。
- 每 feed 索引 `feeds/<feed_id>.jsonl` 第一行是 `kind=feed` header，之后是 `kind=article` 和 `kind=annotation` 记录。article 记录包含 `file/offset/length/body_offset/body_len/sha256` 以及 guid/link/title/published；annotation 记录保存 category/summary/score/classified_at，最新一条胜出。
- 索引是派生数据，可以从归档重建：

```bash
python -m src.md_archive --validate archive
python -m src.md_archive --repair archive
python -m src.md_archive --rebuild-indexes archive --feeds feeds
```

`--repair` 只截断不完整的尾部记录，不会改写之前完整的归档。重建索引会丢失 annotation（分类历史），应优先保留现有索引。

## 缓存与恢复

每次采集按以下顺序恢复：

1. `actions/cache/restore` 恢复 `.runtime/`（含 `archive/`、`feeds/`、state/articles/status/output），并校验 JSON；
2. cache 缺失或损坏时从 `data` 恢复完整归档和索引；ADR-016 旧布局没有 `archive/`/`feeds/` 时，仍恢复 state/articles/status/output；
3. `data` 不存在时，允许一次性从旧 `gh-pages` 迁移 `state.json` / `output/`，并把旧 `articles.json` / `cache.json` 作为回填输入；
4. 归档缺失时，用 `src.backfill` 把旧 raw `articles.json` 清洗后写入 `archive/` 和 `feeds/`；
5. 都没有时以空状态启动，首次成功运行后自动创建 `data`。

cache 未命中时仍然设置 `RSS_FORCE_FETCH=1`，让 fetch 忽略 ETag/抓取间隔、刷新 14 天窗口；但正文不再依赖重新抓取，`data` 的归档会直接恢复 pending 记录的正文。只有确实没有归档、也没有可回填的旧数据时，才会退化为重新抓取。

归档恢复后先 `--repair` 再 `--validate`；修复后仍不合法则 job fail closed，不会把损坏内容写回 `data`。feed 索引缺失且归档存在时，工作流会用 `--rebuild-indexes` 从归档重建。

## Workflow

| Workflow | 触发 | 行为 |
|----------|------|------|
| `collect.yml` | 每 6 小时 + 手动（可强制 `snapshot=true`） | 先跑 `test`（ruff + pytest）；从 Actions cache 或 `data` 恢复运行时/归档，执行 fetch/classify/cleanup/report，渲染 `_site` 并部署 Pages，运行 `--check` 报警；`always()` 中保存 cache，并按 20 小时窗口写一次 `data` 快照 |
| `sync-feeds.yml` | 每周一 04:00 UTC + 手动 | 运行 `--sync`，仅在 `feeds.toml` 变化时提交到 `main` |

`collect` 使用 concurrency group `rss-collect` 且 `cancel-in-progress: false`：上一轮未结束时新任务排队，不会中途取消。缺少 `data` 分支时工作流会警告并继续；首次成功运行会自动创建分支。需要立即生成快照时，手动运行并勾选 `snapshot`。

## 渲染与发布

```bash
python -m src.render --site _site --archive archive --feeds feeds
```

输出：

```
_site/
  index.html                 # 日期 + feed 索引
  2026/05/01.html            # 每日文章页
  feeds/
    example.com-1a2b3c4d5e6f.html
    example.com-1a2b3c4d5e6f.json   # JSON Feed 1.1
```

- Pages 使用 `upload-pages-artifact` + `deploy-pages`，来源是 Actions artifact；
- 不使用 Jekyll（没有 `_build` / Jekyll 构建步骤）；
- 不写 `gh-pages`。迁移完成后可删除旧分支。

## 迁移与回填

旧 ADR-016 的 `articles.json` 可能含原始 HTML。一次性回填：

```bash
python -m src.backfill \
  --articles OLD_ARTICLES_JSON \
  --root . \
  --feeds feeds \
  --out articles.json
```

或使用包装脚本：

```bash
scripts/backfill_archive.sh OLD_ARTICLES_JSON --out articles.json
```

回填会清洗正文、追加 `archive/`、创建/扩展 `feeds/`，并写出 metadata-only 的 `articles.json`。它是幂等的：已在索引中的 canonical link / guid 不会重复写入。`collect.yml` 在 cache miss 且归档缺失时会自动执行一次回填。

迁移验证：

```bash
git fetch origin
git ls-tree --name-only origin/data
# 期望：archive  articles.json  feeds  output  state.json  status.json
git show origin/data:articles.json | python -c 'import json,sys; d=json.load(sys.stdin); print("raw entries:", sum(1 for a in d if a.get("content")))'
# 期望：raw entries: 0
```

确认 `data` 稳定、Pages 从 artifact 正常部署后，可以删除旧 `gh-pages`：

```bash
git push origin --delete gh-pages
```

`scripts/migrate_branches.sh` 是 ADR-015 时代的 legacy 工具，会重写 `gh-pages`，新架构下不要执行；迁移交给第一次 `collect` 和 `src.backfill`。

## 仓库体积

归档正文进入 git，按当前抓取量估算，年增量在**数百 MB/年**量级。这是保留原文的预期成本：

- Markdown 是文本，git delta/zlib 压缩有效，diff 可读；
- 不使用 gzip/SQLite（二进制会破坏 delta 和可读 diff）；
- `data` 每天最多一次快照，`archive/`/`feeds/` 只追加；
- Actions cache 只是热副本，不随 git 历史增长；但当前 cache 会复制完整 `archive/` + `feeds/`，archive 达到数百 MB 后 restore/save 传输和 10GB 配额会成为下一阶段瓶颈（后续可改为最近窗口缓存或增量渲染）；
- 迁移前的 `gh-pages` / 旧 `data` 历史仍可能包含旧正文；如需清理体积或版权，需要重写历史而不是只改当前 tip。

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                    GitHub Actions (每 6 小时)             │
├─────────────────────────────────────────────────────────┤
│  TOML 解析 → RSS 抓取 (并行) → Aliveness 检测            │
│       │              │                                   │
│       ▼              ▼                                   │
│  去重 (URL)    清洗 HTML → Markdown                       │
│                      │                                   │
│                      ▼                                   │
│          archive/YYYY/MM/DD.md (append-only)             │
│          feeds/<feed_id>.jsonl (偏移 + annotation)       │
│                      │                                   │
│                      ▼                                   │
│          AI 分类 (按偏移 hydrate 正文)                    │
│                      │                                   │
│          ┌───────────┴───────────┐                       │
│          ▼                       ▼                       │
│   output/YYYY/MMDD.md      src.render → _site/           │
│   (人工摘要)                (HTML + JSON Feed)            │
└──────────────────────────────┬──────────────────────────┘
                               │
                               ▼
                    GitHub Pages (Actions artifact)
```

## 配置

| 文件 | 说明 |
|------|------|
| `feeds.toml` | RSS 源（TOML 格式，带 priority、skip_prompt） |
| `sources.toml` | 外部源列表 URL（OPML/TOML） |
| `config.toml` | AI provider、抓取参数、分类上限 [ADR-002](docs/ADR/002.config.use-toml.md) |

关键配置：

| Key | 默认 | 说明 |
|-----|------|------|
| `fetch.keep_days` | `14` | 只保留最近 N 天的文章 |
| `fetch.max_workers` | `10` | `limits.max_workers` 的兼容 fallback |
| `limits.max_workers` | `10` | 并行抓取线程数 |
| `limits.max_feeds` | `500` | 每次最多抓取的 feed 数（0 = 不限制） |
| `limits.max_articles` | `200` | 每次最多分类的文章数（0 = 不限制） |
| `limits.max_entries_per_feed` | `50` | 单个 feed 每轮最多取多少篇（0 = 不限制） |
| `limits.max_backlog` | `5000` | 待分类+失败文章超过此值时 `--check` 失败 |
| `limits.freshness_hours` | `24` | 最近 fetch/classify 超过此小时数时 `--check` 失败 |
| `limits.fetch_interval_hours` | `12` | 同一 feed 的最小抓取间隔 |
| `limits.disable_after_failures` | `3` | 连续 permanent/parse 失败多少次后隔离；临时错误只退避 |
| `storage.archive_dir` | `archive` | Markdown 归档目录 |
| `storage.feeds_dir` | `feeds` | 每 feed JSONL 索引目录 |

`filter.skip_prompt` 是全局 AI 过滤规则；如果某个 feed 也配置了 `skip_prompt`，两者会合并后一起传给模型。

### Per-feed skip_prompt

```toml
[[category.feed]]
title = "example"
url = "https://example.com/rss.xml"
skip_prompt = "if title starts with 读 or TIL then 'skip'"
```

- 如果整个源都不想要，直接 `skip = true` 或删掉，不需要用 skip_prompt

### 标题过滤

`config.toml` 中的 `[filter].skip_titles` 是确定性规则（免费、快速），匹配标题的文章直接跳过。

### GitHub Secrets

- `OPENAI_API_KEY` — OpenAI API 密钥
- `ANTHROPIC_API_KEY` — Anthropic API 密钥
- `OPENROUTER_API_KEY` — OpenRouter API 密钥

## 本地输出

```
archive/                  # 永久 Markdown 归档（CI 写入 data）
  YYYY/MM/DD.md
feeds/                    # 每 feed JSONL 索引
  <feed_id>.jsonl
output/                   # 人工摘要表格
  index.md
  YYYY/MMDD.md
_site/                    # 本地渲染结果
state.json                # feed 状态
articles.json             # 14 天窗口元数据，不含正文
status.json               # 最近一次运行报告
```

本地生成的 `state.json` / `articles.json` / `status.json` / `output/` / `archive/` / `feeds/` / `_site/` 都被 `.gitignore` 忽略，不会进入 `main`。

## 成本

| 项目 | 月成本 |
|------|--------|
| GitHub Actions | $0 |
| GitHub Pages | $0 |
| AI API (100篇/天) | ~$3 |

## 来源备忘

https://www.zhblogs.net/ · https://list.travellings.cn/ · https://github.com/timqian/chinese-independent-blogs/blob/master/feed.opml · https://www.foreverblog.cn/ · https://storeweb.cn/site · https://bf.zzxworld.com/ · https://www.boyouquan.com/blogs · https://bokequan.cn/boke · https://blogtalk.org/blogs

## todo

https://www.zhihu.com/pin/2043907730642137680
