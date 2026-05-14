# RSS Aggregator

GitHub Actions 驱动的 RSS 采集 + AI 分类打分工具。

## 功能

- **RSS 采集** — 从 TOML 配置的源并行抓取，7 天内的文章
- **Aliveness 监控** — HTTP 健康检测，连续失败自动禁用
- **AI 分类/打分** — 支持 OpenAI、Claude、OpenRouter，失败跳过不阻断
- **聚合检测** — 自动识别 HN/Reddit 等聚合源，跳过 AI 分类
- **Markdown 输出** — `YYYY/MMDD.md` 表格格式，GitHub Pages 直接看

## 快速开始

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
python -m src.main
```

## 流程

```
--sync     从 sources.toml 拉取外部 RSS 源列表 → 合并到 feeds.toml
--fetch    抓取 RSS，7 天内的文章写入 output/YYYY/MMDD.md（score = —）
--classify 读 output 里 score = — 的文章，AI 分类后原地更新
```

- **抓取**：每次运行抓 7 天内的文章，按发布时间归档到对应日期文件
- **AI 分类**：从新到老处理，失败的文章保持 `—`，下次继续
- 各步骤完全独立，可任意顺序重跑

## 配置

| 文件 | 说明 |
|------|------|
| `feeds.toml` | RSS 源（TOML 格式，带 priority） |
| `sources.toml` | 外部源列表 URL（OPML/TOML） |
| `config.toml` | AI provider、fetch 间隔、禁用阈值 |

## 输出

```
output/
  index.md        # 按日期倒序索引
  2026/
    0512.md       # | Author | Title | Summary | Score |
    0513.md
```

## 来源备忘

https://www.zhblogs.net/
https://list.travellings.cn/
https://github.com/timqian/chinese-independent-blogs/blob/master/feed.opml
https://www.foreverblog.cn/
https://storeweb.cn/site
https://bf.zzxworld.com/
https://www.boyouquan.com/blogs
https://bokequan.cn/boke
https://blogtalk.org/blogs
