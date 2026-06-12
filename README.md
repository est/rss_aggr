# RSS Aggregator

GitHub Actions 驱动的 RSS 采集 + AI 分类打分工具。

## 设计原则

- **Output 不可变** — `output/YYYY/MMDD.md` 一旦写入不再修改，已发布的 HTML 不受影响
- **Cache 14 天窗口** — `cache.json` 保存最近 14 天的采集记录，老条目自动清理
- **Fetch/Classify 独立** — 采集和分类可以分开运行，AI 额度不够时等下一轮定时任务
- **Per-feed 筛选** — `skip_prompt` 按 feed 粒度配置，用于对特定源的特定类型文章进行 AI 筛选

## 功能

- **RSS 采集** — 从 TOML 配置的源并行抓取，14 天窗口内的文章
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
--sync      从 sources.toml 拉取外部 RSS 源列表 → 合并到 feeds.toml
--fetch     抓取 RSS，追加新文章到 cache.json（去重：output + cache）
--classify  从 cache.json 读取，AI 分类后写入 output/YYYY/MMDD.md（只新建，不覆盖）
--cleanup   清理 cache.json 中超过 14 天的老条目
```

- **抓取**：每次运行抓 14 天内的文章，追加到 `cache.json`
- **AI 分类**：从 cache 中排除已输出的文章，分类后写入 output；skip 的文章不写入
- **清理**：cache 中超过 14 天的条目被移除，output 不受影响
- 各步骤完全独立，可任意顺序重跑

## 配置

| 文件 | 说明 |
|------|------|
| `feeds.toml` | RSS 源（TOML 格式，带 priority、skip_prompt） |
| `sources.toml` | 外部源列表 URL（OPML/TOML） |
| `config.toml` | AI provider、fetch 间隔、禁用阈值 |

### Per-feed skip_prompt

在 `feeds.toml` 中为特定 feed 配置 `skip_prompt`，用于指示 AI 筛选该源中的特定类型文章：

```toml
[[category.feed]]
title = "暗无天日"
url = "https://www.lujun9972.win/rss.xml"
site = "https://www.lujun9972.win"
skip_prompt = "if title starts with 读 or TIL then 'skip'"
```

- `skip_prompt` 是 per-feed 的，不是全局的
- 目的：某个 RSS 源夹杂没意思的内容，用 AI 针对性筛选，避免错过有趣的文章
- 如果整个源都不想要，直接 `skip = true` 或删掉，不需要用 skip_prompt

### 标题过滤

`config.toml` 中的 `[filter].skip_titles` 是确定性规则（免费、快速），匹配标题的文章直接跳过，不进入 AI 分类。

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
