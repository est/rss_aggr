# RSS Aggregator

把一堆 RSS 源变成可以长期保存、按天浏览、按 feed 聚合的 Markdown 归档。

它做四件事：

1. 抓取 RSS；
2. 把文章 HTML 清洗成 Markdown，永久保存；
3. 可选地用 AI 分类/打分；
4. 生成静态站点和每 feed 的 JSON Feed。

> 原文会进入 git；AI 只是增强，不是必需品。没有 API key 时仍然会输出标题和链接，只是没有分类、摘要和评分。

## 先理解三个概念

```text
archive/   = 事实来源，永久保存的 Markdown 原文
feeds/     = 索引，记录每篇文章在 archive 里的位置
cache      = 热数据，加速运行；丢了可以从 data 恢复
```

- **archive**：`archive/YYYY/MM/DD.md`，按天追加，永不修改已有字节。
- **feeds**：`feeds/<feed_id>.jsonl`，按 feed 聚合；用字节 offset 指向 archive。
- **data 分支**：持久化 archive、feeds、state 和元数据。
- **Actions cache**：只用于加速，不是唯一真相。

`output/` 是给人看的摘要表格，只是 cache 里的临时产物，不是归档。

## 快速开始（本地）

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 可选：配置 AI
export OPENAI_API_KEY=sk-...

# 抓取 + 分类 + 清理
python -m src.main

# 生成静态站点
python -m src.render --site _site --archive archive --feeds feeds
```

也可以按步骤运行：

```bash
python -m src.main --sync       # 同步 sources.toml 里的外部源列表
python -m src.main --fetch      # 抓 RSS，清洗并写入 archive/ 和 feeds/
python -m src.main --classify   # AI 分类，写 output/ 和 feed annotation
python -m src.main --cleanup    # 清理 14 天前的 articles.json 元数据
python -m src.main --report     # 写 status.json
python -m src.main --check      # 检查运行时契约
```

本地生成的文件都在 `.gitignore` 里，不会进 `main`：

```text
archive/   永久归档
feeds/     per-feed JSONL 索引
output/    人工摘要
_site/     渲染结果
state.json
articles.json
status.json
```

## 配置

主要看三个文件：

| 文件 | 作用 |
|------|------|
| `config.toml` | AI、抓取、过滤、限制、存储路径 |
| `feeds.toml` | 具体订阅源，按分类组织 |
| `sources.toml` | 外部 OPML/TOML 源列表，`--sync` 会合并进 `feeds.toml` |

`config.toml` 常用项：

```toml
[ai]
provider = "openai"       # openai | claude | openrouter | custom
model = "gpt-4o-mini"

[fetch]
keep_days = 14

[filter]
skip_titles = ["周报", "newsletter", "weekly digest"]
skip_prompt = "如果是新闻汇总、链接合集、个人日记，category 设为 skip"

[limits]
max_feeds = 500           # 每轮最多抓多少个源
max_articles = 200        # 每轮最多分类多少篇
max_entries_per_feed = 50 # 单个源每轮最多取多少篇
max_backlog = 5000        # pending+failed 超过这个数 --check 会失败
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
- 索引是派生数据，坏了可以从 archive 重建。

## 分支模型

只有两个长期分支：

| 分支 | 内容 | 谁写 |
|------|------|------|
| `main` | 代码、配置、文档、workflow | 人 + `sync-feeds` |
| `data` | `archive/`、`feeds/`、`state.json`、`articles.json`、`status.json` | 采集 workflow，每天最多一次普通 commit |

- `data` 是永久归档；`archive/` 和 `feeds/` 只追加。
- `state.json` / `articles.json` / `status.json` 每轮整体替换。
- **不需要 `gh-pages`**：GitHub Pages 直接从 Actions artifact 部署。
- 旧仓库迁移完成后可以删除 `gh-pages`。

## GitHub Actions

| Workflow | 触发 | 做什么 |
|----------|------|--------|
| `collect.yml` | 每 6 小时 + 手动 | 恢复 cache/data → 抓取 → 清洗归档 → AI 分类 → 渲染 → 部署 Pages → 保存 cache → 每天一次 data 快照 |
| `sync-feeds.yml` | 每周一 + 手动 | 同步外部源列表，只提交 `feeds.toml` |

首次运行如果只有旧 `gh-pages`：

1. 从 `gh-pages` 读取旧的 `state.json`、`output/`、raw `articles.json`；
2. 用 `src.backfill` 把旧正文清洗后写入新的 `archive/` 和 `feeds/`；
3. 成功后再创建 `data` 分支；
4. 确认页面正常后删除旧 `gh-pages`：

```bash
git push origin --delete gh-pages
```

> GitHub Pages 的 Source 需要设置为 **GitHub Actions**。这里不使用 Jekyll。

## 手动迁移旧数据

如果不想等 workflow 自动回填：

```bash
python -m src.backfill \
  --articles OLD_ARTICLES_JSON \
  --root . \
  --feeds feeds \
  --out articles.json
```

或者用包装脚本：

```bash
scripts/backfill_archive.sh OLD_ARTICLES_JSON
```

回填会：

- 把 raw HTML 清洗成 Markdown；
- 写入 `archive/YYYY/MM/DD.md`；
- 创建/扩展 `feeds/<feed_id>.jsonl`；
- 输出 metadata-only 的 `articles.json`。

旧行如果已经有分类结果，会顺便写入 feed annotation。重复执行不会重复归档。

## 运维工具

```bash
# 校验归档 framing / checksum
python -m src.md_archive --validate archive

# 只修复未完成的尾部记录（不会动中间损坏）
python -m src.md_archive --repair archive

# 从 archive 重建 feed 索引（annotation 会丢）
python -m src.md_archive --rebuild-indexes archive --feeds feeds

# 校验 offset/length/sha，删除失效索引记录
python -m src.md_archive --reconcile-indexes archive --feeds feeds
```

如果 AI 不可用：

- 文章仍然会输出标题和链接；
- `Category / Summary / Score` 为空；
- `status.json` 和日志里会有 warning；
- job 不会因为“AI 没配好”而不产出内容。

## 体积和限制

- 原文永久保留，所以仓库会增长；按当前数据估算，年增量在**数百 MB/年**量级，抓取量大时可能接近 1GB/年。
- Markdown 比 gzip/SQLite 更适合 git：diff 可读、delta 压缩有效。
- 旧 `gh-pages` / 旧历史里的正文不会自动消失；要立刻缩小仓库需要重写历史或重建仓库。
- 当前 Actions cache 会复制完整 `archive/` + `feeds/`；归档很大后，cache 传输和 10GB 配额会成为下一个瓶颈，后续可以改成只缓存最近窗口或增量渲染。
- Pages artifact 也有大小限制，长期可以只保留最近页面或把旧归档移到对象存储。

## 测试

```bash
python3.12 -m pytest -q
ruff check .
```

## 一句话总结

```text
RSS → 清洗后的 Markdown 永久归档 → per-feed 偏移索引 → 静态站点 / JSON Feed
```

`main` 放代码，`data` 放归档，cache 只负责跑得快。
