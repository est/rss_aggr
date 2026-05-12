# RSS Aggregator

GitHub Actions 驱动的 RSS 采集 + AI 分类打分工具。

## 功能

- **RSS 采集** — 从 TOML 配置的源并行抓取
- **Aliveness 监控** — HTTP 健康检测（HEAD + GET fallback）
- **AI 分类/打分** — 支持 OpenAI、Claude、OpenRouter
- **Markdown 输出** — `YYYY/MMDD.md` 表格格式，GitHub Pages 直接看

## 快速开始

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
python -m src.main
```

## 配置

| 文件 | 说明 |
|------|------|
| `feeds.toml` | RSS 源（TOML 格式） |
| `config.toml` | AI provider、分类规则 |



## 输出

```
docs/
  2026/
    0512.md    # # 2026-05-12
    0513.md    # | Author | Title | Summary | Score |
```

GitHub Pages 部署后直接访问 `YYYYMM/DD.md`。
