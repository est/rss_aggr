# RSS Aggregator — 方案设计文档

## 起点与动机

在信息爆炸的时代，手动筛选 RSS 源中的文章效率极低。我们希望回答一个核心问题：

> **能不能用最少的人工干预，自动从海量 RSS 文章中筛选出值得阅读的内容？**

具体需求拆解为三个子问题：

1. **RSS 采集** — 自动定时抓取多个 RSS 源，无需手动刷新
2. **Aliveness 监控** — 感知哪些源还活着、哪些挂了，避免阅读器静默失效
3. **AI 分类与打分** — 让 AI 帮我们做第一轮筛选：这篇文章属于什么类别？值不值得读？打几分？

## 可行性分析

### 技术可行性

| 模块 | 方案 | 可行性 |
|------|------|--------|
| RSS 采集 | Python feedparser + GitHub Actions cron | ✅ 成熟方案 |
| Aliveness 监控 | HTTP HEAD 请求检测端点状态 | ✅ 简单可靠 |
| AI 分类/打分 | OpenAI / Claude / OpenRouter | ✅ API 稳定，成本可控 |
| 结果展示 | GitHub Pages 静态站点 | ✅ 免费，零运维 |

### 成本估算

| 项目 | 说明 | 月成本 |
|------|------|--------|
| GitHub Actions | 每日一次，免费额度内 | $0 |
| GitHub Pages | 静态站点托管 | $0 |
| AI API (100篇/天) | GPT-4o-mini ~$0.001/篇 | ~$3/月 |
| **总计** | | **~$3/月** |

### 限制与约束

- GitHub Actions 单次运行上限 6 小时（足够处理数百个 RSS 源）
- GitHub Actions cron 最小粒度 5 分钟（RSS 通常不需要更频繁）
- AI 分类延迟：每篇文章约 1-2 秒，100 篇需串行或小批量并行
- 结果存储在 git 仓库中，长期会膨胀（已有 keep_days=90 自动清理）

## 系统架构

```
┌─────────────────────────────────────────────────────┐
│                  GitHub Actions                      │
│                    (每日 06:00 UTC)                    │
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
│              │  output/YYYYMM/DD.md│               │
│              └─────────┬───────────┘               │
│                        │                           │
│              ┌─────────▼───────────┐               │
│              │  git commit + push  │               │
│              └─────────────────────┘               │
│                                                    │
└─────────────────────────────────────────────────────┘
                        │
                        ▼
              ┌─────────────────────┐
              │   GitHub Pages      │
              │   Markdown 表格直接看│
              └─────────────────────┘
```

## 数据流

### 1. 采集阶段
- 解析 `feeds.toml` 获取 RSS 源列表
- 并行抓取所有源（ThreadPoolExecutor, max_workers=10）
- 提取 author、title、link、content

### 2. 去重阶段
- 加载 `output/` 下所有历史 markdown 文件的链接
- 过滤已处理过的条目
- 只保留新文章进入 AI 分类

### 3. 分类阶段
- 构造 prompt，传入标题 + 内容摘要（限 2000 字符）
- AI 返回 JSON：category, tags, score(1-10), summary, reasoning
- 分类失败时 fallback 为 "Unclassified", score=5

### 4. 存储阶段
- 输出 `output/YYYYMM/DD.md` markdown 表格
- 同一天多次运行自动追加去重
- commit 并 push 到 main 分支

### 5. 展示阶段
- GitHub Pages 直接渲染 markdown
- 访问 `YYYYMM/DD.md` 查看当日文章表格

## 配置说明

### feeds.toml
TOML 格式，按分类组织 RSS 源：
```toml
[[category]]
name = "Tech News"

[[category.feed]]
title = "Hacker News"
url = "https://hnrss.org/frontpage"
site = "https://news.ycombinator.com"
```

### config.yml
- `ai.provider` — `openai`、`claude` 或 `openrouter`
- `ai.model` — 具体模型名称
- `categories` — 分类列表及关键词
- `fetch.*` — 抓取超时、每源最大文章数
- `storage.keep_days` — 结果保留天数

### GitHub Secrets
- `OPENAI_API_KEY` — OpenAI API 密钥
- `ANTHROPIC_API_KEY` — Anthropic API 密钥
- `OPENROUTER_API_KEY` — OpenRouter API 密钥

## 未来演进

- [ ] 支持邮件/Webhook 通知高分文章
- [ ] 增加文章去重相似度检测（语义去重）
- [ ] 支持用户自定义评分 prompt
- [ ] 增加 RSS 源健康趋势图表
