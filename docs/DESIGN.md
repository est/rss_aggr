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
| AI 分类/打分 | OpenAI GPT-4o-mini / Claude Haiku | ✅ API 稳定，成本可控 |
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
│  │ OPML 解析 │──→│ RSS 抓取  │──→│   Aliveness 检测  ││
│  │          │   │ (并行)    │   │   (HTTP HEAD)    ││
│  └──────────┘   └──────────┘   └──────────────────┘│
│       │                              │              │
│       ▼                              ▼              │
│  ┌──────────┐   ┌──────────────────────────────┐  │
│  │  去重     │──→│   AI 分类/打分 (OpenAI/Claude) │  │
│  │ (GUID)   │   │   类别 + 标签 + 1-10分        │  │
│  └──────────┘   └──────────────────────────────┘  │
│                         │                          │
│                         ▼                          │
│              ┌─────────────────────┐               │
│              │  JSON 结果存储       │               │
│              │  output/data/       │               │
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
              │   (暗色主题仪表盘)   │
              │   筛选 / 搜索 / 排序 │
              └─────────────────────┘
```

## 数据流

### 1. 采集阶段
- 解析 `feeds.opml` 获取 RSS 源列表
- 并行抓取所有源（ThreadPoolExecutor, max_workers=10）
- 每个条目生成 GUID（SHA256 哈希前 16 位）

### 2. 去重阶段
- 加载 `output/data/` 下所有历史 JSON 文件的 GUID
- 过滤已处理过的条目
- 只保留新文章进入 AI 分类

### 3. 分类阶段
- 构造 prompt，传入标题 + 内容摘要（限 2000 字符）
- AI 返回 JSON：category, tags, score(1-10), summary, reasoning
- 分类失败时 fallback 为 "Unclassified", score=5

### 4. 存储阶段
- 合并到当日 JSON 文件（支持多次运行增量写入）
- commit 并 push 到 main 分支

### 5. 展示阶段
- GitHub Pages 读取 `output/data/*.json`
- 支持按类别筛选、按分数排序、关键词搜索
- 健康状态网格展示所有 RSS 源的存活情况

## 配置说明

### feeds.opml
标准 OPML 2.0 格式，支持嵌套分类。每个 feed 节点需要：
- `xmlUrl` — RSS/Atom 订阅地址
- `htmlUrl` — 网站主页
- `text` / `title` — 显示名称

### config.yml
- `ai.provider` — `openai` 或 `claude`
- `ai.model` — 具体模型名称
- `categories` — 分类列表及关键词
- `scoring.dimensions` — 评分维度和权重
- `fetch.*` — 抓取超时、每源最大文章数
- `storage.keep_days` — 结果保留天数

### GitHub Secrets
- `OPENAI_API_KEY` — OpenAI API 密钥（使用 OpenAI 时）
- `ANTHROPIC_API_KEY` — Anthropic API 密钥（使用 Claude 时）

## 未来演进

- [ ] 增加更多 AI provider（本地 Ollama、Google Gemini）
- [ ] 支持邮件/Webhook 通知高分文章
- [ ] 增加文章去重相似度检测（语义去重）
- [ ] 支持用户自定义评分 prompt
- [ ] 增加 RSS 源健康趋势图表
