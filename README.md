# RSS Aggregator

GitHub Actions 驱动的 RSS 采集 + AI 分类打分工具。

> **核心问题：能不能用最少的人工干预，自动从海量 RSS 文章中筛选出值得阅读的内容？**

## 设计原则

| 原则 | 说明 | ADR |
|------|------|-----|
| Output 不可变 | `output/YYYY/MMDD.md` 一旦写入不再修改 | [ADR-003](docs/ADR/003.output.immutable-output.md) |
| Cache 14 天窗口 | `cache.json` 保存最近 14 天的采集记录 | [ADR-004](docs/ADR/004.cache.fifteen-day-window.md) |
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

## 流程

```
--sync      从 sources.toml 拉取外部 RSS 源列表 → 合并到 feeds.toml
--fetch     抓取 RSS，追加新文章到 cache.json（去重：output + cache）
--classify  从 cache.json 读取，AI 分类后写入 output/YYYY/MMDD.md（只新建，不覆盖，不删除）
--cleanup   清理 cache.json 中超过 14 天的老条目
```

- 各步骤完全独立，可任意顺序重跑

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
              └─────────────────────┘
```

## 配置

| 文件 | 说明 |
|------|------|
| `feeds.toml` | RSS 源（TOML 格式，带 priority、skip_prompt） |
| `sources.toml` | 外部源列表 URL（OPML/TOML） |
| `config.toml` | AI provider、fetch 间隔、禁用阈值 [ADR-002](docs/ADR/002.config.use-toml.md) |

### Per-feed skip_prompt

```toml
[[category.feed]]
title = "暗无天日"
url = "https://www.lujun9972.win/rss.xml"
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

cache.json        # 14 天窗口的采集缓存（运行时文件，不提交）
state.json        # feed 状态（运行时文件，不提交）
```

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
