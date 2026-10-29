# RSS Aggregator

GitHub Actions 驱动的 RSS 采集 + AI 分类打分工具。

> **核心问题：能不能用最少的人工干预，自动从海量 RSS 文章中筛选出值得阅读的内容？**

## 设计原则

| 原则 | 说明 | ADR |
|------|------|-----|
| Output append-only | `output/YYYY/MMDD.md` 只追加不删除；过去日期只做幂等追加/修复，同一天可扩展 | [ADR-003](docs/ADR/003.output.immutable-output.md) |
| 分支职责分离 | `main` 代码/配置/文档，`data` 运行时状态，`gh-pages` 渲染输出 | [ADR-015](docs/ADR/015.branch.runtime-data.md) |
| Article 14 天窗口 | `articles.json` 保存最近 14 天的待分类/已采集记录 | [ADR-004](docs/ADR/004.cache.fifteen-day-window.md) |
| Fetch/Classify 独立 | 采集和分类可以分开运行 | [ADR-005](docs/ADR/005.architecture.fetch-classify-decoupled.md) |
| Per-feed 筛选 | `skip_prompt` 按 feed 粒度配置 | [ADR-006](docs/ADR/006.filter.per-feed-skip-prompt.md) |

## 功能

- **RSS 采集** — 并行抓取，14 天窗口内的文章
- **Aliveness 监控** — HTTP 健康检测，连续失败自动禁用 [ADR-010](docs/ADR/010.health.aliveness-monitoring.md)
- **AI 分类/打分** — 支持 OpenAI、Claude、OpenRouter [ADR-008](docs/ADR/008.ai.multi-provider.md)
- **聚合检测** — 自动识别 HN/Reddit 等聚合源，跳过 AI 分类 *(draft: [ADR-009](docs/ADR/009.filter.aggregation-detection.md))*
- **Markdown 输出** — `YYYY/MMDD.md` 表格格式，GitHub Pages 直接看 [ADR-007](docs/ADR/007.platform.github-actions-pages.md)

## 快速开始

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
python -m src.main
```

也可以按步骤运行：

```bash
python -m src.main --fetch      # 抓取 RSS，写入 articles.json
python -m src.main --classify   # AI 分类，写入 output/
python -m src.main --cleanup    # 清理 articles.json 中超过 14 天的条目
python -m src.main --report     # 生成 status.json（最近一次运行状态）
python -m src.main --check      # 校验运行时数据/输出契约
```

## 流程

```
--sync      从 sources.toml 拉取外部 RSS 源列表 → 合并到 feeds.toml（独立 workflow）
--fetch     抓取 RSS，追加新文章到 articles.json（去重：output + articles）
--classify  从 articles.json 读取，AI 分类后写入 output/YYYY/MMDD.md
--cleanup   清理 articles.json 中超过 14 天的老条目
--report    写入 status.json，记录本次运行概况
--check     校验数据文件与输出目录，失败时阻止 Pages 部署
```

- 各步骤完全独立，可任意顺序重跑
- `output/` 采用 append-only：已写入的行不删除，重复运行只追加去重后的新行

## 分支模型

| 分支 | 内容 | 写入方式 |
|------|------|----------|
| `main` | `src/`、`tests/`、`docs/`、`config.toml`、`feeds.toml`、`sources.toml`、workflow | 普通提交；`sync-feeds` 每周提交 `feeds.toml` |
| `data` | `state.json`、`articles.json`、`status.json` | 每次采集一个 snapshot commit，force-push |
| `gh-pages` | `output/` | 每次采集一个 snapshot commit，force-push |

- `data` 和 `gh-pages` 都是**派生快照**，只由 CI 写入，不要手工 merge 或把 `main` 的内容推上去。
- `gh-pages` 不保存 `state.json` / `articles.json`；Pages 部署来自 Actions artifact，不依赖该分支。
- 恢复、回滚与重新启用 feed 见 [RUNBOOK](docs/RUNBOOK.md)。

## Workflow

| Workflow | 触发 | 行为 |
|----------|------|------|
| `collect.yml` | 每 6 小时 + 手动 | 先跑 `test`（ruff + pytest）；再从 `data` 恢复运行时状态、从 `gh-pages` 恢复 `output/`，执行 fetch/classify/cleanup/report/check，构建并部署 Pages，最后在 `always()` 中持久化两个快照 |
| `sync-feeds.yml` | 每周一 04:00 UTC + 手动 | 运行 `--sync`，仅在 `feeds.toml` 变化时提交到 `main` |

`collect` 使用 concurrency group `rss-collect` 且 `cancel-in-progress: false`：上一轮未结束时新任务排队，不会中途取消。缺少 `data` / `gh-pages` 分支时工作流会警告并继续，首次成功运行会自动创建分支。

## 架构

```
┌─────────────────────────────────────────────────────┐
│                  GitHub Actions                      │
│                    (每 6 小时)                         │
├─────────────────────────────────────────────────────┤
│                                                      │
│  ┌──────────┐   ┌──────────┐   ┌──────────────────┐│
│  │TOML 解析  │──→│ RSS 抓取  │──→│   Aliveness 检测  ││
│  │          │   │ (并行)    │   │   (HEAD+GET)     ││
│  └──────────┘   └──────────┘   └──────────────────┘│
│       │                              │              │
│       ▼                              ▼              │
│  ┌──────────┐   ┌──────────────────────────────┐  │
│  │  去重     │──→│   AI 分类/打分                 │  │
│  │ (URL)    │   │  OpenAI / Claude / OpenRouter │  │
│  └──────────┘   └──────────────────────────────┘  │
│                         │                          │
│                         ▼                          │
│              ┌─────────────────────┐               │
│              │  Markdown 输出       │               │
│              │  output/YYYY/MMDD.md│               │
│              └─────────────────────┘               │
└─────────────────────────────────────────────────────┘
                        │
                        ▼
              ┌─────────────────────┐
              │   GitHub Pages      │
              │  (Actions artifact) │
              └─────────────────────┘
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
| `fetch.max_workers` | `10` | 并行抓取线程数 |
| `fetch.keep_days` | `14` | 只保留最近 N 天的文章 |
| `limits.max_feeds` | `500` | 每次最多抓取的 feed 数（0 = 不限制） |
| `limits.max_articles` | `200` | 每次最多分类的文章数（0 = 不限制） |
| `limits.fetch_interval_hours` | `12` | 同一 feed 的最小抓取间隔 |
| `limits.disable_after_failures` | `3` | 连续失败多少次后自动禁用 |

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

## 输出

```
output/
  index.md        # 按日期倒序索引
  2026/
    0512.md       # | Author | Title | Category | Summary | Score |
    0513.md
```

运行时文件保存在 `data` 分支：

```
data/
  state.json      # feed 状态（last_ok、失败次数、disabled、etag）
  articles.json   # 14 天窗口的待分类文章
  status.json     # 最近一次运行报告
```

本地运行生成的 `state.json` / `articles.json` / `status.json` / `output/` 都被 `.gitignore` 忽略，不会进入 `main`。旧版 `cache.json` 会在恢复时迁移为 `articles.json`。

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
