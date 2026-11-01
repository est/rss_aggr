# Single Source of Truth 审计

结论：**没有完全做到。** 现在有几块事实来源是清楚的，但“feed 配置”“文章是否存在”“分类结果”“运行状态”“存储路径”都存在多个副本或隐式权威，修改一个文件不一定能决定以后的行为。

> **2026-11-01 后续：** 第 1-6 节保留审计当时的发现，作为历史记录；第 7 节记录之后的修复结果和仍然存在的限制。阅读当前状态时以第 7 节为准。

## 1. 先看结论表

| 领域 | 名义上的权威 | 实际行为 | 是否 SSOT |
|------|--------------|----------|-----------|
| 文章正文 | `archive/YYYY/MM/DD.md` | 分类、渲染、索引重建都以 archive 为准 | **是** |
| 静态站点 | `archive/` + `feeds/` | `src.render` 不读 `output/`、不读 `articles.json` | **是**（但会继承索引/header 的陈旧） |
| 订阅源列表 | `feeds.toml` | 只决定“这一轮抓谁”；`state.json`、`sources.toml`、旧索引都会影响结果 | **否** |
| 文章是否已存在 | `archive/` | 实际由 `output/`、`articles.json`、`feeds/*.jsonl` 共同决定 | **否** |
| 分类结果 | `feeds/*.jsonl` 的 annotation | `articles.json` 和 `output/` 也各存一份，失败时不自动修复 | **否** |
| feed 显示名/site | `feeds.toml` | 新文章用新值；已有 `feeds/*.jsonl` header 只写一次，页面继续显示旧值 | **否** |
| 存储路径 | `config.toml [storage]` | 只有 Python 步骤读；workflow 和 render 写死 `archive/feeds/output` | **否** |
| 持久运行状态 | `data` 分支 | 有效 cache 优先于 `data`，`data` 最多约 20 小时才更新一次 | **否（cache 优先）** |

## 2. `feeds.toml` 到底能影响什么

### 加一个 feed

一般下一轮 `--fetch` 就会抓。前提：

- 没有被 `max_feeds` 截掉（`config.toml` 默认 500）；
- `state.json` 里没有同一 URL 的 `disabled` / `retry_at`；
- 文件里没有重复 URL。

### 删一个 feed

- 下一轮不再抓：`parse_feeds` 只返回文件里存在的项。
- **旧页面、旧索引、旧 `output/` 不会消失**：render 只看 `feeds/*.jsonl`，不看 `feeds.toml`。
- **`--sync` 可能把它加回来**：只要 `sources.toml` 的外部 OPML 还包含这个 URL，`src/sync.py` 就会追加。
- 想永久停止，应该在 `feeds.toml` 里保留条目并设 `skip = true`，而不是直接删除。

### 设置 `skip = true`

- 下一轮不抓。
- 但已缓存的 pending 文章仍会被分类；旧页面仍在。
- 该 feed 的 `skip_prompt` 会同时失效，因为 `parse_feeds` 在读取 `skip_prompt` 之前就跳过了整条 feed。

### 改 title / site

| 字段 | 立刻生效 | 仍然是旧值 |
|------|----------|------------|
| title | 新抓到的文章、缺失索引的新 feed | 已有 `feeds/*.jsonl` 的 header；因此 feed 页面标题、JSON Feed 标题、日页面里的 feed 名继续显示旧标题 |
| site | 新文章、site-based `skip_prompt` 匹配 | 已有 header 的 `site_url`；feed 页面的 Website 链接、JSON Feed 的 `home_page_url` 可能仍旧 |
| category | 新文章的 `feed_category` | 旧文章；AI 分类 category 来自 `config.toml`，不是这里 |
| priority | 下一轮排队顺序 | 不持久化；sync 重新加回时用源里的 priority |
| skip_prompt | 下一轮对 pending 文章生效 | 已分类文章不会重跑 |
| URL | 新 state key、新 feed_id、新索引 | 旧 state、旧索引、旧页面、旧文章都留下；sync 可能把旧 URL 再加回来 |

关键代码：

- `feeds.toml` 解析：`src/opml_parser.py:19-32`
- JSONL header 只创建一次：`src/md_archive.py:605-612`
- render 用 header 显示 feed 标题/site：`src/render.py:754-778`
- sync 只追加、不移除、不更新已有 URL：`src/sync.py:181-192`

## 3. 最严重的几个问题

### P0：`output/` 仍然参与去重，能永久阻止文章归档

`step_fetch` 会先读 `output/` 的链接：

```python
output_links = load_seen_links(data_dir)   # src/main.py:362
if canonical and canonical in output_links:
    continue                                # src/main.py:397-398
```

`step_classify` 也会用它跳过 pending 行（`src/main.py:514,531-532`）。这和文档里“`output/` 只是临时摘要、可丢”矛盾。

当前迁移后的 `data` 分支里保留了旧 `output/`：

```text
output 里 canonical links:       2996
feeds 索引里 canonical links:    2277
只在 output、不在 archive/feeds: 2574
```

这 2574 个链接以后即使重新出现在 feed 里，也会被 `output_links` 跳过，永远不会补进 archive。**这是目前最直接、最该先修的 SSOT 破坏。**

### P0：feed 的显示身份写死在 JSONL header，改 `feeds.toml` 不会更新

`_append_feed_lines()` 只在文件为空时写 header：

```python
if not existing:
    lines.append(_feed_header(feed_meta, identifier))
```

之后无论 `feeds.toml` 的 title/site 怎么改，header 都不变。render 又优先用 header：

```python
feed.title -> header["title"]
home_page_url/feed_url -> header["site_url"]
```

所以改 `feeds.toml` 的标题/site 后，新文章是新值，feed 页面和 JSON Feed 仍是旧值。`--rebuild-indexes` 还可能用最旧文章的 `feed_title` 把 header 再写回旧值。

### P1：`state.json` 是独立的永久权威，且从不和 `feeds.toml` 对账

- `disabled` / `retry_at` / `etag` / `last_attempt` 只在 `state.json`。
- 删掉 feed 不会清 state；重新加回同一 URL 会继承旧的 disabled/retry/ETag。
- 改 `feeds.toml` 不能覆盖 disabled；`RSS_FORCE_FETCH=1` 也只清 ETag/间隔，不清 disabled。
- state 里已删除 feed 的条目永不清理，`status.json` 的 feed 总数会大于 `feeds.toml`。

### P1：分类结果有三份，失败时不会自动修复

`step_classify` 的写入顺序：

1. 保存 `articles.json` 状态为 `classified`
2. 写 `output/YYYY/MMDD.md`
3. 追加 `feeds/*.jsonl` annotation

如果第 2 或第 3 步失败：

- `articles.json` 已经是 `classified`，下一轮不会再分类；
- render 只读 annotation，所以页面没有 summary/category/score；
- `output/` 可能已经写了摘要，但 render 不用它；
- 没有自动从 `articles.json` 或 `output/` 回填 annotation 的机制。

### P1：`feeds/*.jsonl` 部分丢失不会自动补

- fetch 写 archive → `articles.json` → feed index。
- 如果 feed index 追加失败：archive 和 `articles.json` 已有记录，下一轮 fetch 会认为“已归档”而跳过；classify 只追加 annotation，不会补 article 记录。
- cold start 的 `--reconcile-indexes` 只删无效记录，不补缺失记录。
- 只有 `feeds/` **整个目录为空**时，workflow 才会自动 `--rebuild-indexes`。
- `--rebuild-indexes` 会丢 annotation。

### P1：`[storage]` 配置只影响 Python，不影响 CI

`config.toml` 写着：

```toml
[storage]
data_dir = "output"
archive_dir = "archive"
feeds_dir = "feeds"
```

`src/main.py` 读这些值；但 `collect.yml`、`src/render.py` 的 CLI、RUNBOOK 命令都写死 `archive` / `feeds` / `output`。

改 `archive_dir` 的后果：

- Python 往新目录写；
- workflow 仍然校验/渲染/快照 `./archive`；
- 新正文既不会进 cache 也不会进 data 分支，runner 结束后丢失。

改 `data_dir` 还可能让 `output/index.md` 检查失败，导致 cache save 和 data snapshot 都被跳过。

### P1：`data` 不是最新状态，有效 cache 才是恢复时的权威

- cache 命中时，workflow 直接使用 cache，完全不读 `data`（`collect.yml:145-147`）。
- `data` 只在 cache 未命中时恢复（`collect.yml:148-257`）。
- `data` 快照最多约 20 小时一次（`collect.yml:811-827`）。
- 所以两次快照之间，最新状态只在 cache；cache 被驱逐或保存失败，`data` 就是旧的。
- 手工改 `data` 的 state/metadata，会被下一轮热 cache 覆盖；RUNBOOK 要求先删 `rss-runtime-*` cache。

### P2：`sources.toml` + `--sync` 不是声明式镜像

- 只追加新 URL；
- 不移除已删除 URL；
- 不更新已有 URL 的 title/site/category/priority；
- 导入的 feed 会丢掉 `skip` / `skip_prompt`。

所以“删 feed”不是持久操作，除非保留 `skip = true`。

### P2：遗留工具仍能制造第二套状态

- `src/archive.py` 仍可运行，会生成旧 ADR-016 的 snapshot（含 `output/`、无 `feeds/`）。
- `scripts/migrate_branches.sh` 仍可 force-push `gh-pages`。
- `gh-pages` 回退路径仍然活着。
- `output/` 在 data 分支上还在参与 fetch/classify 去重。

## 4. 真正做对的部分

- `archive/` 是正文的唯一事实来源；分类和渲染都按 offset 从 archive 读正文。
- `_site/` 只从 `archive/` + `feeds/` 生成，不读 `output/` 或 `articles.json`。
- `feeds/*.jsonl` 的 article 记录是 archive 的派生索引，offset/length/sha 可校验。
- `data` 分支的 `archive/` / `feeds/` 有 append-only 保护，快照不会静默缩短远端文件。
- `main` 只放代码和配置，不混运行数据。

## 5. 建议的修复顺序

1. **停止用 `output/` 做去重**（`src/main.py` fetch/classify），让 archive + feed index + `articles.json` 成为唯一判定来源。  
   同时决定是否从 data 分支移除旧 `output/`；不修这个，2574 个旧链接永远进不了 archive。
2. **让 feed header 可更新**：要么允许追加 `kind=feed_update` 并让 `read_feed_index` 以最新为准，要么 render 用 `feeds.toml` 覆盖 title/site（保留 header 作为 fallback）。
3. **给 `state.json` 加对账/清理**：`feeds.toml` 删除的 URL 可从 state 移除；提供显式 `--forget URL` 清 disabled/ETag；`RSS_FORCE_FETCH` 或单独 `--force` 能覆盖 disabled。
4. **修分类写入顺序**：先追加 annotation，再标记 `classified`；或者增加 annotation 回填/重试步骤。
5. **增加 archive → feed index 的自动补全**：不要只在 `feeds/` 为空时 rebuild；可以按 feed 扫描 archive，补缺失 article 记录。
6. **统一路径来源**：要么 workflow/render 从 `config.toml` 读路径，要么删掉 `[storage]` 并明确路径固定。
7. **`--sync` 改成声明式**或明确它是“只追加”；删除 feed 用 `skip = true`，不要直接删。
8. **清理 legacy**：`src/archive.py`、`scripts/migrate_branches.sh`、`gh-pages` 回退在迁移完成后应移除或标记不可用。

## 6. 一句话

> `archive/` 是正文的 single source of truth，`_site/` 是它的可靠派生；但 **feed 配置、文章去重、分类结果、运行状态、存储路径** 都还不是单一来源。改 `feeds.toml` 能影响“以后抓谁”，但不能单独决定“以后显示什么、是否重新归档、是否重新分类”。

## 7. 2026-11-01 修复结果

本节只记录修复，不改写上面的历史发现。核心结论：**写入路径和运行时状态已经收敛；剩余问题主要来自只读快照节奏、`--sync` 的追加语义和遗留工具。**

### 7.1 已修复

| 原问题 | 修复 | 当前行为 |
|--------|------|----------|
| P0：`output/` 参与去重，能永久阻止文章归档 | 删除 fetch/classify 里基于 `output/` 的跳过逻辑；去重改用归档引用、`article_state.json` 的 canonical/guid 和 `feeds/*.jsonl` | 冷启动没有 `output/` 也能正常分类；`output/` 只作为人类摘要，不再决定文章是否存在 |
| P0：feed 显示身份写死在 JSONL header | render 用 `feeds.toml` 叠加当前 `title` / `site` / `feed_url`（`--feed-config`，默认 `feeds.toml`） | 改配置后页面和 JSON Feed 立刻显示新值；JSONL header 保持 append-only 的历史值，只作 fallback |
| P1：`state.json` 是独立永久权威且不跟 `feeds.toml` 对账 | 拆成 `feed_state.json`（feed 运行状态）和 `article_state.json`（文章处理状态）；fetch 前按 `feeds.toml` 对账 | `python -m src.migrate_state` 负责拆分；删除的 feed 会从 state 清理，`skip = true` 的 feed 保留；`--forget URL` 可显式清 disabled/ETag/retry |
| P1：分类结果三份副本，失败时不自愈 | 写入顺序改为 annotation → `output/` → 标记 `classified`；annotation 失败时行退回 pending 并增加 attempts | 不会提交没有 annotation 的 `classified`；下一轮重试；`output/` 失败不会留下“已分类但页面无摘要”的状态 |
| P1：`feeds/*.jsonl` 部分丢失不会补 | fetch 根据 `article_state.json` 的 `indexed=false` 从归档补写；新增 `--repair-indexes` 非破坏性补全 | `indexed` 在索引写成功后才置真；`--repair-indexes archive --feeds feeds` 只追加缺失记录，保留 annotation；`--rebuild-indexes` 仍会丢 annotation |
| P1：`[storage]` 只影响 Python，不影响 CI | 布局固定在 `src/paths.py`；`config.toml` 的 `[storage]` 覆盖会被拒绝 | Python、workflow、render 统一使用 `archive/`、`feeds/`、`output/`；改路径会直接报错而不是静默分叉 |
| 持久运行状态含派生报告 | `status.json` 改为纯派生报告 | 不进 cache、不进 data 快照；远端出现会被拒绝；`--report` / `--check` 从 `feed_state.json` + `article_state.json` 现算 |

补充说明：

- `article_state.json` 是版本化对象 `{"v":1,"articles":[...]}`；`state.json` / `articles.json` 只作为迁移输入和 loader fallback，下一份 data 快照会删除。
- 状态迁移由 `python -m src.migrate_state` 完成；CI 在恢复数据后、归档操作前自动执行。
- `feeds.toml` 里的 URL 现在按 canonical URL 去重（http/https、www、尾斜杠、tracking 参数）；`parse_feeds` 和 `--sync` 都会处理，active 条目优先于 `skip = true` 条目，否则第一次出现胜出。
- 旧 `output/` 可能仍留在 data 分支上，但新快照不再更新它，也没有任何去重逻辑读取它。

### 7.2 仍然存在 / 已知限制

| 问题 | 现状 | 影响 / 处理建议 |
|------|------|-----------------|
| `data` 分支与 cache 存在时间差 | cache 可能比 `data` 新；`data` 最多约 20 小时快照一次 | 手工改 `data` 上的状态前先删 `rss-runtime-*` cache，否则会被下一轮热副本覆盖；不要把 `data` 当实时接口 |
| `--sync` 仍是只追加 | 从 `feeds.toml` 删除的 URL 只要还在 `sources.toml` 里就会被加回；不更新已有 URL 的 title/site/category/priority；现在会按 canonical URL 去重（active 优先于 skip） | 要永久停止，保留条目并设 `skip = true`；不要把删除当作声明式操作 |
| 遗留工具仍可制造第二套状态 | `src/archive.py` 的旧快照 CLI 已加环境变量 guard；`scripts/migrate_branches.sh --apply` 也已加 guard；但代码和 `gh-pages` 回退路径仍在仓库里 | 属于历史兼容；迁移完成后应删除远端 `gh-pages` 并考虑移除 legacy 模块 |
| `output/` 日文件丢失后不会自动重算 | `output/` 是派生摘要，不参与归档/分类/渲染 | 丢失只影响人工阅读，不影响数据正确性；需要恢复时只能从旧 cache/data 副本取回，当前没有从 annotation 反向重建 output 的命令 |
| feed 配置变更的历史残留 | 改 URL 会产生新的 `feed_id`、新 state key、新索引；旧索引和旧页面仍在 | 这是 append-only 的预期行为；需要彻底清理时按索引/归档维护流程人工处理 |
| 索引重建会丢 annotation | `--rebuild-indexes` 仍是全量重建 | 优先用 `--repair-indexes`；必须 rebuild 时先备份 `feeds/`，或接受重新分类 |

### 7.3 一句话更新

> 正文、去重、分类写入、feed 运行状态和存储路径已经收敛到明确权威；`status.json` 和 `output/` 已降级为纯派生。剩余风险主要是 **cache 领先于 data 的只读快照语义**、**`--sync` 的追加语义**，以及**尚未清理的 legacy 工具**。
