# 概念 → 实物对照

本文回答两个问题：

1. README 里的 `archive/`、`feeds/`、`cache` 到底是**路径**还是**分支**？
2. 这个项目从 RSS 到“给别的系统读取”，实际是怎么跑起来的？

一句话版本：

```text
main  = 代码和配置
data  = 数据分支；分支根目录下就是 archive/、feeds/、state.json 等路径
cache = data 的热副本（.runtime/），只加速，不是事实来源
Pages = 从 archive + feeds 渲染出来的站点和 JSON Feed，不是数据源
```

## 1. 同一个名字，三种“位置”

`archive/` 这类名字首先是**工作区里的相对路径**。它是否长期存在，取决于它被放在哪：

| 位置 | 长什么样 | 里面有什么 | 是否长期 |
|------|----------|------------|----------|
| 本地/CI 工作区 | `./archive/`、`./feeds/`、`./state.json` | 本次运行真正读写的文件 | 否，工作区是临时的 |
| Actions cache | `.runtime/archive/`、`.runtime/feeds/` … | 上一轮运行的热副本 | 是，但会被驱逐 |
| `data` 分支 | `origin/data` 根目录下的同名路径 | 持久归档和元数据 | 是，事实来源 |
| `main` 分支 | **没有这些路径** | 只有代码、配置、文档、workflow | 是，但不含运行数据 |

所以：

- **说路径**时，指的是工作区里的 `archive/`、`feeds/`、`state.json`。
- **说持久化**时，指的是 `data` 分支根目录下的同名文件。
- **说 cache** 时，指的是 `.runtime/` 下的一份拷贝。
- `main` 上永远不提交这些运行产物（`.gitignore` 已忽略）。

`data` 分支的树大致是：

```text
data
├── archive/2026/11/01.md
├── feeds/example.com-1a2b3c4d5e6f.jsonl
├── state.json
├── articles.json
└── status.json
```

> 容易混的一点：`feeds.toml` 和 `feeds/` 不是一回事。
> `feeds.toml` 是订阅源清单，放 `main`；`feeds/` 是每 feed 的 JSONL 索引，放 `data`。

## 2. 概念对照表

| README 概念 | 工作区路径 | 谁生成 | 写入方式 | 持久化在哪 | 谁消费 |
|-------------|------------|--------|----------|------------|--------|
| 订阅源配置 | `feeds.toml` | 人 / `--sync` | 覆盖 | `main` | `--fetch` |
| 外部源列表 | `sources.toml` | 人 | 覆盖 | `main` | `--sync` |
| 运行配置 | `config.toml` | 人 | 覆盖 | `main` | 所有步骤 |
| **永久归档** | `archive/YYYY/MM/DD.md` | `--fetch` → `src.md_archive.append_articles` | append-only | `data` | `--classify`、`src.render`、索引重建 |
| **每 feed 索引** | `feeds/<feed_id>.jsonl` | `--fetch` 写 header/article；`--classify` 追加 annotation | append-only | `data` | `src.render`、抓取去重、feed 聚合 |
| feed 抓取状态 | `state.json` | `--fetch` | 整体替换 | `data` | 到期判断、自动禁用、`--report` |
| 待分类元数据 | `articles.json` | `--fetch` / `--classify` / `--cleanup` | 整体替换 | `data` | 分类队列、状态统计 |
| 运行状态报告 | `status.json` | `--report` / `--check` | 整体替换 | `data` | 运维、健康检查 |
| 人工摘要 | `output/` | `--classify` → `src.storage.save_daily_results` | 按天重写 | 工作区/cache；新 `data` 快照不写入（旧数据可能残留） | 人看，不是对外数据接口 |
| 热数据 | `.runtime/` | workflow 的 Prepare runtime cache | 整目录 | Actions cache | 下一轮恢复 |
| 静态站点 | `_site/` | `python -m src.render` | 每次重建 | Pages artifact | 浏览器、JSON Feed 消费者 |
| 旧站点 | `gh-pages` | 旧架构遗留 | — | 旧分支 | 迁移用，最终删除 |

## 3. 一条数据是怎么流动的

```text
feeds.toml / sources.toml（main）
        │
        │  --sync
        ▼
   feeds.toml（main）
        │
        │  --fetch：HTTP 抓取 → sanitize → Markdown
        ├──────────────► archive/YYYY/MM/DD.md      （正文，append-only）
        ├──────────────► feeds/<feed_id>.jsonl       （article 记录）
        ├──────────────► articles.json                （metadata-only，待分类）
        └──────────────► state.json                   （每 feed 的成功/失败）
        │
        │  --classify：按 offset 从 archive 读回正文 → AI
        ├──────────────► output/YYYY/MMDD.md         （给人看的表格，仅 cache）
        └──────────────► feeds/<feed_id>.jsonl       （追加 annotation）
        │
        │  src.render：archive + feeds
        ├──────────────► _site/YYYY/MM/DD.html        （每日页，含渲染后的正文）
        ├──────────────► _site/archive/YYYY/MM/DD.md  （原文副本，供 Range 请求）
        ├──────────────► _site/feeds/<id>.html        （轻量 JS 壳）
        └──────────────► _site/feeds/<id>.json        （JSON Feed 1.1，元数据+摘要+_archive）
        │
        ├──► Actions artifact → GitHub Pages
        ├──► Actions cache（.runtime/，每轮）
        └──► data 分支（archive/feeds/state/articles/status，约每天一次）
```

关键点：

- `archive/` 是事实来源；`feeds/`、`output/`、`_site/` 都可以从它重新生成（annotation 除外）。
- 分类时不重新抓网页，而是用 `articles.json` 里的 `archive_offset` / `archive_length` 从归档里读正文。
- `output/` 是给人看的 Markdown 表格，不在新的 `data` 快照里；不要把它当归档。
- 对外消费用的是 `_site/feeds/<id>.json`，不是 `data` 分支本身。
- JSON 里的 `_archive` 给出 `file` / `body_offset` / `body_len` / `sha256`；per-feed 页面用这些坐标对 `_site/archive/...md` 发 Range 请求，只取目标正文。

## 4. 本地怎么跑

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 全流程：sync + fetch + classify + cleanup
python -m src.main

# 分步
python -m src.main --sync       # sources.toml → feeds.toml
python -m src.main --fetch      # RSS → archive/ + feeds/ + articles.json + state.json
python -m src.main --classify   # archive → AI → output/ + feed annotation
python -m src.main --cleanup    # 清理 14 天前的 articles.json 条目
python -m src.main --report     # → status.json
python -m src.main --check      # 校验 backlog / freshness 契约

# 渲染静态站点和 JSON Feed
python -m src.render --site _site --archive archive --feeds feeds
```

per-feed 页面依赖 `fetch`，请通过 HTTP 访问（Pages 或本地静态服务器）；直接双击 `file://` 打开不会加载内容。GitHub Pages 支持 Range（返回 `206`），普通静态服务器如果不支持，JS 会退化为整文件读取后切片。

注意：

- `python -m src.main` **不会**自动 render、report、check；这些是独立步骤。
- 本地跑出来的 `archive/`、`feeds/`、`state.json` 等都在当前工作区，**不会自动推到 `data` 分支**。
- 本地第一次跑时没有 `state.json`，会把所有 feed 当成到期源；`articles.json` 缺失会强制全量刷新窗口。
- AI 没配好不会中断产出：文章仍会以标题 + 链接写入，分类字段为空，状态记为 `unclassified`。

## 5. CI 怎么跑

`.github/workflows/collect.yml`，每 6 小时一次，也可以手动触发。

```text
test（ruff + pytest，只读 main）
  │
  ▼
collect
  1. checkout main
  2. 恢复 cache：.runtime/ → 工作区
  3. 如果 cache 无效/不存在：从 data 分支恢复
  4. 校验归档和索引（损坏则修复尾部 / 重建索引 / fail closed）
  5. --fetch
  6. --classify
  7. --cleanup
  8. --report
  9. src.render → _site
 10. upload-pages-artifact + deploy-pages
 11. --check
 12. Prepare runtime cache → Save runtime cache
 13. Snapshot 到 data 分支（约每 20 小时一次，或手动勾选 snapshot）
```

恢复优先级和写回位置：

```text
读：有效 cache  >  data 分支  >  空状态
写：cache（每轮，校验通过后）
    data（校验通过 + 距上次快照 ≥ 20 小时，或手动强制）
```

因此要记住：**cache 可能比 `data` 新**。`data` 是每日快照，不是实时状态；cache 被驱逐时，最近不到 20 小时的增量需要从 `data` 冷启动并重新抓取窗口内内容。

## 6. 给别的系统读取时，读什么？

推荐按 feed 读渲染后的 **JSON Feed**：

```text
https://<owner>.github.io/<repo>/feeds/<output_id>.json
```

例如：

```text
https://est.github.io/rss_aggr/feeds/example.com-1a2b3c4d5e6f.json
```

每个 JSON 是 JSON Feed 1.1，包含：

- `version`、`title`、`home_page_url`、`feed_url`
- `items[]`：`id`、`title`、`url`、`date_published`
- 有分类结果时还有 `summary`、`tags`（category）和 `_score`
- `content_text`：用 `summary` 填充；没有 summary 时用 `title`（JSON Feed 要求至少一个 content 字段）
- `_archive`：`file`、`offset`、`length`、`body_offset`、`body_len`、`sha256`，用于对 `_site/archive/...md` 发 Range 请求
- `_daily_url`：对应的每日 HTML 页面锚点

JSON Feed **不包含** `content_html` 全文，也不包含归档正文。全文在 `archive/YYYY/MM/DD.md`、`_site/archive/YYYY/MM/DD.md`，以及渲染出的每日页面；JSON 只是索引、短摘要和归档坐标。

`_site/feeds/<id>.html` 是一个轻量壳：JS 先加载同名 JSON，再按 `_archive.body_offset` / `body_len` 对 Markdown 发 HTTP Range 请求，只取目标正文。GitHub Pages 返回 `206` 时直接使用；如果服务器忽略 Range 返回 `200`，JS 会取整文件后按字节切片兜底。

`<output_id>` 通常等于 `feeds/<feed_id>.jsonl` 的文件名；渲染时会对文件名做 slug 化和重名后缀处理。需要发现有哪些 feed，可以看 `_site/index.html` 或 `feeds/` 目录。

不建议让外部系统直接依赖：

- **`data` 分支**：它是归档和内部状态，文件布局是内部实现；快照节奏也不是实时接口。
- **`feeds/*.jsonl`**：内部索引格式，可能随实现调整；JSON Feed 才是对外契约。
- **`output/`**：新流程只写工作区/cache；旧 `data` 可能保留历史副本，但它仍是给人看的表格，不是对外数据接口。
- **`gh-pages`**：旧架构遗留，迁移完成后应删除。

当前渲染器只生成**每 feed** 的 JSON Feed，没有全站聚合 JSON。如果消费方需要“所有文章一个流”，需要另外加一个聚合 feed，或按 feed 逐个拉取。

## 7. 常见误解

| 误解 | 实际 |
|------|------|
| `archive/` 在 `main` 上 | 不在；`main` 只有代码和配置，归档在 `data` |
| `feeds/` 就是 `feeds.toml` | 不是；前者是每 feed 索引，后者是订阅源清单 |
| `output/` 是归档 | 不是；它只是给人看的摘要，归档是 `archive/` |
| JSON Feed 里有全文 | 没有；全文在 `archive/` 和每日页面，JSON 只有元数据、摘要和 `_archive` 坐标 |
| cache 是唯一数据源 | 不是；cache 可丢，`data` 才是持久归档 |
| `data` 每轮都更新 | 约每 20 小时一次，或手动强制；中间状态只在 cache |
| `python -m src.main` 会发布站点 | 不会；要单独跑 `src.render` 和部署步骤 |
| Pages 从 `gh-pages` 部署 | 不是；Pages 从 Actions artifact 部署，`gh-pages` 是旧遗留 |
| 归档按“抓取日”分文件 | 按文章发布日期（UTC）分；缺失时回退 `fetched_at` |

## 8. 需要记住的三句话

```text
路径是工作区里的相对路径；
持久化看 data 分支；
cache 只负责跑得快。
```

```text
archive/ 是事实来源；
feeds/ 是偏移索引；
output/ 和 _site/ 是派生产物。
```

```text
对外系统读 Pages 上的 JSON Feed；
不要读 data 分支的内部布局。
```
