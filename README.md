# RSS Aggregator

GitHub Actions 驱动的 RSS 采集 + AI 分类打分工具。

## 功能

- **RSS 采集** — 从 TOML 配置的源并行抓取
- **Aliveness 监控** — HTTP 健康检测（HEAD + GET fallback）
- **AI 分类/打分** — 支持 OpenAI、Claude、OpenRouter
- **Markdown 输出** — `YYYYMM/DD.md` 表格格式，GitHub Pages 直接看

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
| `config.yml` | AI provider、分类规则 |

### AI Provider

```yaml
# config.yml
ai:
  provider: openrouter  # openai | claude | openrouter
  model: google/gemma-3-1b-it:free
```

GitHub Secrets 添加对应的 API key：
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `OPENROUTER_API_KEY`

## 输出

```
output/
  202605/
    12.md    # # 2026-05-12
    13.md    # | Author | Title | Summary | Score |
```

GitHub Pages 部署后直接访问 `YYYYMM/DD.md`。
