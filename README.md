# RSS Aggregator

把一堆 RSS 源变成可以长期保存、按天浏览、按 feed 聚合的 Markdown 归档。

它做四件事：

1. 抓取 RSS；
2. 把文章 HTML 清洗成 Markdown，永久保存；
3. 可选地用 AI 分类/打分；
4. 生成每日静态页面、每 feed 的轻量 HTML（JS 读 JSON，再按 Range 拉正文）和 JSON Feed。

> feed 提供的正文（多数情况下是清洗后的 Markdown，也可能是摘要）会进入 `data` 分支的 git；AI 只是增强，不是必需品，且默认关闭（`config.toml` 的 `[ai] enabled = false`）。关闭时 `--classify` 是 no-op：文章仍然输出标题和链接，只是没有分类、摘要和评分，也不会产生分类器 warning/error。

## 先理解三个概念

先说结论：**`archive/`、`feeds/` 这些是工作区里的相对路径；它们的持久化位置是 `data` 分支上的同名路径，不在 `main`。Actions cache 只是这些路径的热副本。**

| 概念 | 工作区路径（真实文件） | 持久化在哪 | 谁写 | 丢了会怎样 |
|------|------------------------|------------|------|------------|
| 归档 | `archive/YYYY/MM/DD.md` | `data` 分支 | `--fetch` | 不能丢，事实来源 |
| 每 feed 索引 | `feeds/<feed_id>.jsonl` | `data` 分支 | `--fetch` / `--classify` | 可从 archive 补回 / 重建（全量重建会丢 annotation） |
| feed 状态 | `feed_state.json` | `data` 分支 | `--fetch` / `src.feed_state` | 可从 `data` 上一份快照恢复；缺失会重新抓取并重建失败计数 |
| 文章状态 | `article_state.json`（`{"v":1,"articles":[...]}`；`pending` / `classified` / `skipped` / `unclassified`、attempts、归档坐标、`indexed`） | `data` 分支 | `--fetch` / `--classify` / `--cleanup` | 可从 `data` 上一份快照恢复；缺失会强制重抓窗口 |
| 运行报告 | `status.json` | 不持久化（仅工作区当轮） | `--report` / `--check` | 可随时重新生成 |
| 热缓存 | `.runtime/`（CI 内部） | Actions cache | workflow | 能丢，丢了从 `data` 冷启动 |
| 人工摘要 | `output/` | 工作区/cache；旧 `data` 可能残留（下一份快照删除） | `--classify`（仅 AI 开启时） | 能丢，不是归档，也不参与去重 |
| 对外站点 | `_site/` | Pages artifact | `src.render` | 能随时重建 |

- **archive**：按文章发布日期（UTC）写入 `archive/YYYY/MM/DD.md`，append-only，永不修改已有字节。
- **feeds**：`feeds/<feed_id>.jsonl`，按 feed 聚合；用字节 offset 指向 archive。
- **data 分支**：持久化 `archive/`、`feeds/`、`feed_state.json`、`article_state.json`；分支根目录下就是这些路径。
- **Actions cache**：只用于加速，不是唯一真相；cache 可能比 `data` 新。
- **main 分支**：只有代码、配置和文档；本地运行时数据被 `.gitignore` 忽略，本地跑完也不会自动推 `data`。

> 注意：`feeds.toml` 是订阅源清单（在 `main`），`feeds/` 是每 feed 的索引目录（在 `data`），两者不是一回事。

`output/` 是给人看的摘要表格，派生自文章状态和分类结果；**安全删除**，并且不再是 fetch/classify 的去重依据。它只在工作区/cache，旧 `data` 可能残留历史副本，但新快照不会写入并会删除它。给其他系统消费请读渲染后的 JSON Feed（`_site/feeds/<id>.json`）；JSON 里只有元数据、摘要和归档坐标（`_archive`），没有全文。全文在 `archive/YYYY/MM/DD.md` 和每日页面里；per-feed 页面会按坐标对 `_site/archive/YYYY/MM/DD.md` 发 HTTP Range 请求，只取需要的那一段。不要直接依赖 `data` 分支的文件布局。

完整的“概念 → 路径/分支 → 运行流程”对照见 [docs/ARCHITECTURE-MAP.md](docs/ARCHITECTURE-MAP.md)。

## 快速开始（本地）

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 可选：配置 AI（同时在 config.toml 设 [ai] enabled = true）
export OPENAI_API_KEY=sk-...

# 抓取 + 分类 + 清理
python -m src.main

# 生成静态站点（feeds.toml 提供当前显示 title/site）
python -m src.render --site _site --archive archive --feeds feeds --feed-config feeds.toml
```

per-feed 页面通过 `fetch` 加载 JSON 和 Markdown 片段，需要通过 HTTP 打开（GitHub Pages 或本地静态服务器）；直接 `file://` 打开会加载失败。

也可以按步骤运行：

```bash
python -m src.main --sync       # 同步 sources.toml 里的外部源列表
python -m src.main --fetch      # 抓 RSS，写 archive/、feeds/、article_state.json、feed_state.json
python -m src.main --classify   # AI 分类（默认 no-op）；写 output/ 和 feed annotation
python -m src.main --cleanup    # 清理过期 article_state（done 1 天；pending/failed 按 keep_days）
python -m src.main --report     # 生成 status.json 派生报告（不进 data/cache）
python -m src.main --check      # 检查运行时契约
```

本地生成的文件都在 `.gitignore` 里，不会进 `main`：

```text
archive/        永久归档
feeds/          per-feed JSONL 索引
output/         人工摘要（派生，可删）
_site/          渲染结果
feed_state.json 每 feed 运行状态
article_state.json  {"v":1,"articles":[...]} 文章处理状态
status.json     派生报告（可删，不进 data/cache）
```

## 配置

主要看三个文件：

| 文件 | 作用 |
|------|------|
| `config.toml` | AI、抓取、过滤、限制 |
| `feeds.toml` | 具体订阅源，按分类组织 |
| `sources.toml` | 外部 OPML/TOML 源列表，`--sync` 会合并进 `feeds.toml` |

- `feeds.toml` 里的 URL 按 canonical URL 去重（http/https、www、尾斜杠等），**第一次出现的条目胜出**；`--sync` 也会清理 canonical 重复项。
- 要永久停止一个 feed，保留条目并设 `skip = true`，不要直接删除；直接删除的 URL 只要还在 `sources.toml` 里，下一次 `--sync` 会把它加回来。
- `--sync` 会拒绝非 http(s)/内网地址；源站抓取或解析失败会让本次同步失败，单个非法 feed URL 只记 warning 并跳过。

存储布局固定为工作区根目录下的 `archive/`、`feeds/`、`output/`（`src/paths.py`）。`config.toml` 里不再有 `[storage]` 覆盖；写了会被拒绝启动。

`config.toml` 常用项：

```toml
[ai]
enabled = false           # 默认关闭；关闭时 --classify 为 no-op
provider = "openai"       # 仅 enabled = true 时生效
model = "gpt-4o-mini"

[fetch]
keep_days = 14
# 短正文回抓原文：off | short（只处理正文小于阈值的条目）| always
full_text_mode = "off"
full_text_min_chars = 500
full_text_timeout_seconds = 10
full_text_max_per_feed = 3

[filter]
skip_titles = ["周报", "newsletter", "weekly digest"]
skip_prompt = "如果是新闻汇总、链接合集、个人日记，category 设为 skip"
# 如果 feed 只输出短摘要，连续 N 篇短正文后自动停用该源
skip_no_full_text = true
min_full_text_chars = 500
no_full_text_after = 5

[limits]
max_feeds = 800           # 每轮最多抓多少个源（活跃源约 1548，6 小时一轮 → 实际间隔接近 12 小时）
max_articles = 200        # 每轮最多分类多少篇
max_entries_per_feed = 50 # 单个源每轮最多取多少篇
max_backlog = 5000        # pending+failed 超过这个数 --check 失败（AI 关闭时只计 failed）
```

单篇 feed 的过滤规则可以直接写：

```toml
[[category.feed]]
title = "example"
url = "https://example.com/rss.xml"
site = "https://example.com"
skip_prompt = "标题以 TIL 开头就设为 skip"
```

## 归档长什么样

`archive/2026/11/01.md` 由一条条记录追加而成：

```markdown
<!-- rss-aggr:article v1
{"v":1,"guid":"...","feed_id":"...","body_len":1234,"body_sha256":"...", ...}
-->
这里是清洗后的 Markdown 正文。
<!-- /rss-aggr -->
```

- `body_len` 是 UTF-8 字节数，用来精确切分正文。
- 正文里出现 `<!--`、`-->`、代码块都不会破坏解析。
- 记录 append-only；只有运行中断留下的“未完成尾巴”可以修复。
- 文件按 LF 固定，偏移量在 checkout 后仍然有效。

每个 feed 对应一个 `feeds/<feed_id>.jsonl`：

```json
{"v":1,"kind":"feed","feed_id":"...","feed_url":"...","title":"Example"}
{"v":1,"kind":"article","file":"archive/2026/11/01.md","offset":0,"length":1450,"guid":"...","link":"...","title":"..."}
{"v":1,"kind":"annotation","guid":"...","category":"Tech","summary":"...","score":8}
```

- article 记录负责“这篇文章在哪”。
- annotation 记录负责“AI 怎么评价”，最新一条胜出。
- 索引是派生数据，坏了可以从 archive 补回或重建；`--repair-indexes` 只补缺失记录、保留 annotation，`--rebuild-indexes` 会丢 annotation。
- 分类时先追加 annotation，再写 `output/`，最后才标记 `classified`；annotation 写入失败的行保持 `pending`（带 `annotation_error`）并在下一轮重试，不会提交 `classified` 状态，也不会为它写 `output/` 行。
- AI 判定为 `skip` 的文章会追加一条 `category=skip` 的 annotation；渲染每日页、feed JSON 和 Range 用的 Markdown 副本时会隐藏它，archive 本身仍保留。
- feed header 里的 title/site 是历史值；`src.render` 默认用 `feeds.toml` 当前配置覆盖显示，不回写 append-only 的 JSONL。新文章用新配置，历史归档记录保留历史元数据。

## 分支模型

只有两个长期分支：

| 分支 | 内容 | 谁写 |
|------|------|------|
| `main` | 代码、配置、文档、workflow | 人 + `sync-feeds` |
| `data` | `archive/`、`feeds/`、`feed_state.json`、`article_state.json`、`.gitattributes`（旧快照可能残留 `output/`，下一份快照会删除） | 采集 workflow，约每 20 小时最多一次普通 commit |

- `data` 是永久归档；`archive/` 和 `feeds/` 只追加。
- `feed_state.json`（每 feed 的 `last_ok` / `last_attempt` / 失败计数 / `disabled` / `retry_at` / ETag / `is_aggregator` / `short_content_streak`，以及顶层 `last_fetch` / `last_classify`）和 `article_state.json` 每轮整体替换。
- `status.json` 是 `--report` / `--check` 现算的派生报告，不写入 `data`，也不进 cache；部署时复制为站点上的 `_site/health.json`。
- Actions cache 保存 `feed_state.json`、`article_state.json`、`archive/`、`feeds/`、`output/`；不含 `status.json`。cache key 为 `rss-runtime-<run_id>-<run_attempt>`，重跑不会命中失败尝试的精确 key。
- `output/` 是派生摘要；新快照不再写入，并会删除旧快照残留的 `output/`。
- `data` 快照节奏按最近的 `data: snapshot ...` commit 计算（约 20 小时）；人工 commit 不会重置计时。`data` 上带 `.gitattributes` 固定 `archive/**/*.md` 和 `feeds/*.jsonl` 的 LF。
- `state.json` / `articles.json` 只是迁移期旧名：loader 仍能 fallback 读取，`python -m src.migrate_state` 会拆分，下一份 `data` 快照会删除。
- 每轮 `--fetch` 会按 `feeds.toml` 清理 `feed_state.json` 中已移除的 feed；`skip = true` 的条目会保留。被移除后重新加回的 URL 从干净状态开始。
- **不需要 `gh-pages`**：GitHub Pages 直接从 Actions artifact 部署。
- 旧仓库迁移完成后可以删除 `gh-pages`。

## GitHub Actions

| Workflow | 触发 | 做什么 |
|----------|------|--------|
| `collect.yml` | 每 6 小时 + 手动 | 恢复 cache/data → 抓取 → 清洗归档 → AI 分类（默认 no-op）→ 渲染 → 发布 health.json → 部署 Pages → 保存 cache → 约每 20 小时一次 data 快照 |
| `sync-feeds.yml` | 每周一 + 手动 | 同步外部源列表，只提交 `feeds.toml` |

首次运行如果只有旧 `gh-pages`：

1. 从 `gh-pages` 读取 legacy 的 `state.json` 和 raw `articles.json`（`output/` 只是历史摘要，不参与去重或回填）；
2. 运行 `python -m src.migrate_state` 把 legacy 状态拆成 `feed_state.json` / `article_state.json`；
3. 用 `src.backfill` 把旧正文清洗后写入新的 `archive/` 和 `feeds/`；
4. 成功后再创建 `data` 分支；
5. 确认页面正常后删除旧 `gh-pages`：

```bash
git push origin --delete gh-pages
```

> GitHub Pages 的 Source 需要设置为 **GitHub Actions**。这里不使用 Jekyll。

## 手动迁移旧数据

如果不想等 workflow 自动回填：

### 1. 迁移状态文件（legacy）

旧工作区可能只有 `state.json` / `articles.json`。loader 仍会 fallback，但建议显式拆分：

```bash
python -m src.migrate_state
# 迁移后删除旧文件：
python -m src.migrate_state --remove-legacy
```

结果写入 `feed_state.json` 和 `article_state.json`；下一份 `data` 快照会移除旧文件。

### 2. 回填旧的 raw 正文

```bash
python -m src.backfill \
  --articles OLD_ARTICLES_JSON \
  --root . \
  --feeds feeds \
  --out article_state.json
```

或者用包装脚本：

```bash
scripts/backfill_archive.sh OLD_ARTICLES_JSON --out article_state.json
```

`--articles` 读取 legacy 的 bare JSON 数组（如果拿到 `{"v":1,"articles":[...]}`，先取出 `articles` 再传入；CI 会自动做这一步）。

回填会：

- 把 raw HTML 清洗成 Markdown；
- 写入 `archive/YYYY/MM/DD.md`；
- 创建/扩展 `feeds/<feed_id>.jsonl`；
- 输出 metadata-only 的 `article_state.json`（versioned object：`{"v":1,"articles":[...]}`）。

旧行如果已经有分类结果，会顺便写入 feed annotation。重复执行不会重复归档。

## 运维工具

```bash
# 校验归档 framing / checksum
python -m src.md_archive --validate archive

# 只修复未完成的尾部记录（不会动中间损坏）
python -m src.md_archive --repair archive

# 只补 feed 索引里缺失的记录（保留已有记录和 annotation）
python -m src.md_archive --repair-indexes archive --feeds feeds

# 从 archive 全量重建 feed 索引（annotation 会丢）
python -m src.md_archive --rebuild-indexes archive --feeds feeds

# 校验 offset/length/sha，删除失效索引记录
python -m src.md_archive --reconcile-indexes archive --feeds feeds

# 让单个 feed 忘掉 disabled/ETag/retry，下一轮强制重试
python -m src.feed_state --forget "https://example.com/rss.xml"

# 按 feeds.toml 清理 feed_state（fetch 每轮也会自动做；skip=true 会保留）
python -m src.feed_state --prune --feeds-config feeds.toml
```

AI 默认关闭（`[ai] enabled = false`），此时 `--classify` 是 no-op：

- 文章仍然会输出标题和链接；
- `Category / Summary / Score` 为空；
- 不会产生分类器 warning/error；`status.json` 仍会统计 pending/classified/skipped/unclassified/failed、output 状态和其他非分类 warning，并记录 `ai_enabled: false`；
- 需要分类时再设 `enabled = true` 并配置 provider/model/key；job 不会因为“AI 没配好”而不产出内容。
- 分类结果为 `skip` 的文章会写入一条 `category=skip` 的 annotation；渲染每日页、feed JSON 和 Range 用的 Markdown 时会隐藏它，但 archive 本身仍保留。
- 原文回抓默认关闭（`full_text_mode = "off"`）。需要时设为 `"short"`：feed 正文短于 `full_text_min_chars` 的条目会尝试回抓原文（trafilatura），成功则用提取后的 Markdown 覆盖 feed 摘要；失败则保留原内容。单 feed 每轮最多回抓 `full_text_max_per_feed` 条。
- `skip_no_full_text = true`（默认）时，如果某个 feed 的新文章连续 `no_full_text_after` 篇正文都短于 `min_full_text_chars`，会自动停用该 feed，`disabled_reason` 为 `rss does not provide full text`；以后不再抓取，直到用 `python -m src.feed_state --forget URL` 重新启用。只要有一篇长文就会把连续计数清零。

## 体积和限制

- feed 正文/摘要永久保留，所以仓库会增长；按当前数据估算，年增量在**数百 MB/年**量级，抓取量大时可能接近 1GB/年。
- Markdown 比 gzip/SQLite 更适合 git：diff 可读、delta 压缩有效。
- 旧 `gh-pages` / 旧历史里的正文不会自动消失；要立刻缩小仓库需要重写历史或重建仓库。
- 当前 Actions cache 会复制完整 `archive/` + `feeds/`；归档很大后，cache 传输和 10GB 配额会成为下一个瓶颈，后续可以改成只缓存最近窗口或增量渲染。
- Pages artifact 也有大小限制，长期可以只保留最近页面或把旧归档移到对象存储。

## 测试

```bash
pip install -r requirements.txt -r requirements-dev.txt
python3.12 -m pytest -q
ruff check .
```

## 一句话总结

```text
RSS → 清洗后的 Markdown 永久归档 → per-feed 偏移索引 → 静态站点 / JSON Feed
```

`main` 放代码，`data` 放归档，cache 只负责跑得快。
