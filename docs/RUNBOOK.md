# RUNBOOK

面向无人值守运行的故障处理手册。当前架构见 [ADR-017](ADR/017.archive.permanent-markdown.md)（ADR-016 已被取代）。

核心模型：

- **`data` 分支** 是持久归档：`archive/YYYY/MM/DD.md`、`feeds/<feed_id>.jsonl`、`state.json`、metadata-only `articles.json`、`status.json`。`output/` 只是 cache 内的人工摘要，不属于持久归档。
- **Actions cache** 保存同一套文件的热副本；可能被 GitHub 驱逐，但不再是正文的唯一副本。
- **`archive/` / `feeds/`** 在 `data` 上 append-only；`state.json` / `articles.json` / `status.json` 每轮整体替换。`output/` 不写入 `data`。
- **Pages** 从 `src.render` 生成的 `_site/` artifact 部署，没有 Jekyll，也没有 `gh-pages`。
- 每次运行必须成功恢复（cache 或 `data`）且归档校验通过，才会写 `data` 快照；失败时用 marker 阻止覆盖。

## 0. 快速定位

```bash
gh run list --workflow "RSS Collection & Classification" --limit 10
gh run view <run-id> --log-failed
```

没有 `gh` 时在 GitHub Actions 页面查看。`test` job 失败时 `collect` 不会启动；`collect` 失败时，标了 `if: always()` 的 `--report`、`--check`、缓存保存和 `data` 快照步骤仍会尝试执行。

关键 marker（工作区临时文件，不代表远端状态）：

| Marker | 含义 |
|--------|------|
| `.restore-data-ok` | 恢复、迁移和归档校验全部成功，允许写 `data` |
| `.cache-restore-miss` | 本次 cache miss，从 `data` / legacy 冷启动；已设置 `RSS_FORCE_FETCH=1` |
| `.cache-save-ok` | 本轮运行产物通过校验，允许保存 cache 和写 `data` |

## 1. 排查失败运行

### test job 失败

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
ruff check .
pytest -q
```

修复后重新运行。`test` 只读 `main`，不会碰运行时数据。

### collect job 失败

| 失败步骤 | 常见原因 | 处理 |
|----------|----------|------|
| Restore runtime cache | cache 服务/网络错误 | GitHub 侧故障时稍后重跑；不要手工伪造 `.runtime/` |
| Restore runtime data | cache 未命中后 `data` 取不到、缺 state/articles 或 JSON 损坏 | 按第 3 节恢复 `data`；损坏必须修，不能绕过 |
| Restore runtime data（warning） | `data` 不存在，回退 legacy `gh-pages` 或空状态 | 正常 bootstrap；确认日志里没有 error |
| Migrate legacy articles | 旧 raw `articles.json` 损坏、`src.backfill` 失败 | 检查 `.legacy/`；可手工运行第 5 节回填 |
| Validate runtime archive | 归档 framing/sha256 错误，或 feed 索引 header 损坏 | 第 4 节 repair/validate；索引损坏按第 6 节重建 |
| Fetch RSS | `state.json` 损坏、feeds 配置错误、网络问题 | 校验/恢复 `state.json`（第 3 节），重跑 |
| Classify articles | API key 失效、额度不足、`base_url` 错误 | 轮换 key（第 9 节），检查 `config.toml` |
| Cleanup | `articles.json` 损坏 | 从 cache/`data` 恢复；不要手写空结构覆盖 |
| Generate run report | `--report` 实现/数据问题 | 查看日志；修复后重跑 |
| Render archive site | 归档记录损坏、`src.render` 依赖缺失 | 先校验归档（第 4 节），再重跑 |
| Verify runtime contract | `--check` 发现 backlog/freshness 契约破坏 | 按日志修复；内容可能已发布，job 会保持红色 |
| Prepare / Save runtime cache | 文件缺失、JSON/归档/索引校验失败 | warning 表示跳过保存；下一次按冷启动处理，不影响 job 成败 |
| Snapshot archive to data branch | `data` clone/push 失败、归档校验拒绝、raw content 泄漏 | 按第 7 节处理；校验失败时远端 `data` 未被修改 |

补充：

- 同一 `rss-collect` 组的运行会排队，不会取消正在运行的采集。
- fetch/classify 可以安全重跑：`archive/`、`feeds/`、`articles.json` 和 `output/` 都按链接/guid 去重。
- `--check` 在 Pages 部署之后运行：不回滚已发布内容，但会让 job 失败。
- 恢复失败时 `.restore-data-ok` 不存在，快照步骤会 warning 并跳过，**这是保护行为，不要绕过**。
- `data` 使用普通 push；如果被拒绝，工作流自动 `pull --rebase` 重试一次。由于快照会重写 `state.json` / `articles.json`，并发提交通常会在 rebase 时冲突，此时重试会失败并保持远端不变，需要人工解决或等待下一次快照。不要 force-push。

## 2. 重新启用被自动禁用的 feed

禁用标记保存在 `data` 分支的 `state.json`：

```json
{
  "feeds": {
    "https://example.com/rss.xml": {
      "disabled": true,
      "disabled_at": "...",
      "disabled_reason": "failed 3x consecutively",
      "consecutive_failures": 3
    }
  }
}
```

操作步骤（建议先暂停 workflow，避免热 cache 的下一次快照覆盖手工修改）：

```bash
gh workflow disable "RSS Collection & Classification"
git clone --depth 1 --branch data git@github.com:est/rss_aggr.git /tmp/rss-data
cd /tmp/rss-data

python3 - <<'PY'
import json
from pathlib import Path

path = Path("state.json")
state = json.loads(path.read_text())
needle = "example.com"  # 改成目标 feed 的域名

for url, info in state.get("feeds", {}).items():
    if needle in url:
        info.pop("disabled", None)
        info.pop("disabled_at", None)
        info.pop("disabled_reason", None)
        info["consecutive_failures"] = 0
        print("re-enabled", url)

path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
PY

git add state.json
git commit -m "state: re-enable feed example.com"
git push origin data
```

**必须让下一轮冷启动读取 `data`。** 热 cache 不会被普通的 `data` push 覆盖，反而会在下一轮恢复时盖掉手工修改。修改后删除运行时 cache：

```bash
gh cache list --limit 100
gh cache delete --all   # 或按 key 删除 rss-runtime-* 的最新几条
```

也可以等 cache 自然过期/被驱逐，但期间 CI 仍使用旧状态。删除 cache 后恢复 workflow 并手动触发一次 `collect`：

```bash
gh workflow enable "RSS Collection & Classification"
gh workflow run "RSS Collection & Classification"
```

它会走 `data` 恢复（含归档和 feed 索引）+ `RSS_FORCE_FETCH=1` 刷新 14 天窗口，然后重新建立 cache。若在暂停前已有一次运行正在执行，等它结束后再启用。

如果 feed 已经彻底失效：

- 在 `main` 上通过 PR 修改 `feeds.toml`，设置 `skip = true` 或删除条目；
- 检查 `sources.toml` / `--sync` 是否会把它重新加回来；
- 仅改 `state.json` 不够，下一次 `sync-feeds` 或手工加回后它仍会被抓取。

## 3. 恢复 data 分支

先确认最新状态。手工修改 `data` 前建议暂停 workflow，避免热 cache 的下一次快照覆盖修复：

```bash
gh workflow disable "RSS Collection & Classification"
git fetch origin
git ls-tree -r --name-only origin/data | head -40
git log --oneline origin/data -10
```

期望内容：

```
state.json
articles.json
status.json
archive/YYYY/MM/DD.md
feeds/<feed_id>.jsonl
```

`articles.json` 不应包含 `content` / `content_length`。

### 分支还在，但元数据文件损坏

```bash
git clone git@github.com:est/rss_aggr.git /tmp/rss-restore
cd /tmp/rss-restore
git checkout -B data <good-commit>

python3 - <<'PY'
import json
for name in ("state.json", "articles.json"):
    json.load(open(name))
    print(name, "ok")
PY

git push origin data
```

`data` 是每日快照，历史 commit 通常就是前一两天可用的完整状态。选择 commit 前确认 `articles.json` 没有正文：

```bash
git show <good-commit>:articles.json | python -c 'import json,sys; d=json.load(sys.stdin); print(sum(1 for a in d if a.get("content")))'
# 期望输出 0
```

### 归档缺失或元数据是旧 ADR-016 布局

新架构合并后的第一次运行会自动处理：

- 如果 `data` 只有 state/articles/status/output、没有 `archive/` / `feeds/`，恢复后运行一次 `src.backfill`；
- 回填输入优先使用 cache 里的 raw `articles.json`；否则使用旧 `gh-pages` 的 `articles.json` / `cache.json`；
- 两者都没有时，设置 `RSS_FORCE_FETCH=1`，重新抓取 14 天窗口并写入新归档。

如果自动回填失败，按第 5 节手工执行。**不要**把旧 `archive/YYYY-MM-DD.jsonl` 当成新归档；快照会删除这些根级 `.jsonl`。

### 分支被删除或需要重建

优先让工作流从 cache 恢复后自动重建：

1. 确认 Actions cache 仍有 `rss-runtime-*`（`gh cache list`）；
2. 手动触发 `collect`；
3. 恢复成功后，快照步骤会创建 orphan `data`。

如果 cache 也已被驱逐：

1. 确认远端 `data` 确实不存在；
2. 手动运行 `collect`；
3. 工作流尝试 legacy `gh-pages` 的 state/output/raw articles；都不存在时以空状态启动并重新抓取 14 天窗口；
4. 首次成功后自动创建含归档和索引的 `data`。

空状态意味着失败计数清零、禁用状态丢失、所有 feed 重新抓取；`output/` 的链接去重会阻止重复文章写入，`archive/` 的 append-only 特性保证不会删除已有记录。

### articles.json 丢失但 state.json 正常

`data` 快照必须同时有 `state.json` 和 `articles.json`，否则恢复会失败。不要只推 `state.json`：

- 优先从上一个 `data` commit 恢复两个文件；
- 如果确实没有可用 `articles.json`，删除 `data` 分支并清理 cache，让工作流按空状态 bootstrap 并重新抓取窗口内文章。归档本身仍在 `archive/`，回填/重新抓取会重新建立索引和元数据。

### 恢复后校验

```bash
git fetch origin
git ls-tree --name-only origin/data
# 期望：archive  articles.json  feeds  output  state.json  status.json
```

然后在本地 checkout 归档并校验：

```bash
python -m src.md_archive --repair archive
python -m src.md_archive --validate archive
```

然后**删除 `rss-runtime-*` cache**（第 2 节），否则下一轮仍会从热 cache 恢复，而不是你恢复的 `data`。最后恢复 workflow：

```bash
gh workflow enable "RSS Collection & Classification"
```

如果需要立即验证，手动触发一次 `collect` 并观察 `Restore runtime data` 是否走了冷启动路径。

## 4. 归档修复与校验

归档是事实来源。任何 framing、长度或 sha256 错误都必须先修复。

```bash
# 只截断不完整/不可解析的尾部记录，保留之前所有完整记录
python -m src.md_archive --repair archive

# 校验所有 archive/YYYY/MM/DD.md
python -m src.md_archive --validate archive
```

- `--repair` 只处理尾部（例如运行中断留下的半条记录）；不会重写中间记录；
- `--validate` 返回非零表示仍有错误。CI 在 repair 之后 validate 失败会 **fail closed**，不写 cache、不写 `data`；
- 如果错误在文件中间（不是尾部），不能靠 repair 修：从 `data` 历史恢复对应文件，或从旧 `output/` / cache 回填；
- 修复后建议再跑一次 `--rebuild-indexes`（第 6 节），确保偏移索引与归档一致。

手工检查单条记录：

```bash
python - <<'PY'
from src import md_archive
rec = md_archive.read_record("archive/2026/05/01.md", offset=0, length=None)
print(rec["meta"]["title"], len(rec["body"]), rec["sha256"])
PY
```

## 5. 回填旧 articles.json

旧 ADR-016 的 `articles.json` 可能含 raw HTML；用回填工具清洗并写入归档：

```bash
python -m src.backfill \
  --articles OLD_ARTICLES_JSON \
  --root . \
  --feeds feeds \
  --out articles.json
```

或：

```bash
scripts/backfill_archive.sh OLD_ARTICLES_JSON --out articles.json
```

行为：

1. 读取旧 JSON 数组；
2. 按 canonical link / guid 去重；
3. 用 sanitizer 把 `content` 转成 Markdown，追加到 `archive/YYYY/MM/DD.md`；
4. 创建/扩展 `feeds/<feed_id>.jsonl`；
5. 写出 metadata-only `articles.json`，带 `archive_file` / `archive_offset` / `archive_length` / `archive_body_len` / `archive_sha256`。

注意：

- 回填是幂等的：已在 feed 索引中的链接/guid 会跳过，不会重复追加；
- `--out` 默认覆盖 `--articles`；迁移时建议先输出到临时文件，确认后再替换；
- 回填不会删除 pending 元数据。CI 的自动回填会把回填结果与 `data` 恢复的 metadata 合并，保留状态和分类信息；
- 回填后运行：

```bash
python -m src.md_archive --validate archive
```

## 6. Feed 索引恢复

`feeds/*.jsonl` 是派生数据；第一行必须是 `kind=feed` header。

### 索引完全丢失

如果 `archive/` 有每日文件但 `feeds/` 为空，CI 会自动执行：

```bash
python -m src.md_archive --rebuild-indexes archive --feeds feeds
```

也可以手工执行。重建会从所有 `archive/YYYY/MM/DD.md` 重新生成 article 记录和 header。

### 索引 offset 失效 / 归档文件缺失

先尝试逐条校验并保留仍然有效的记录（CI 冷启动会自动做这一步）：

```bash
python -m src.md_archive --reconcile-indexes archive --feeds feeds
```

它只删除 offset/length/sha 无法验证的 article 及其 annotation；随后需要让对应 `articles.json` 清掉 archive 引用，fetch 会重新归档正文。若只想从归档整体重建，继续看下一节。

### 索引损坏

先备份现有索引（annotation 可能仍有价值）：

```bash
cp -R feeds /tmp/feeds-backup-$(date -u +%Y%m%dT%H%M%SZ)
python -m src.md_archive --rebuild-indexes archive --feeds feeds
```

**重建会丢失 annotation**（category/summary/score/classified_at），因为归档只保存原文。重建后：

- 已分类文章不会自动重新分类；如需恢复，可以从 `/tmp/feeds-backup-*` 或 `data` 历史里提取 annotation 记录并手工追加；
- 或者接受索引只保留原文链接，等待后续重新分类（会重新产生 annotation）。

### 校验索引

```bash
python - <<'PY'
from pathlib import Path
from src import md_archive

for path in sorted(Path("feeds").glob("*.jsonl")):
    header, records = md_archive.read_feed_index(path)
    assert header.get("kind") == "feed", path
    print(path.name, len(records), "records")
PY
```

## 7. data 快照失败

快照触发条件：

- `data` 最后一次 commit 超过约 20 小时；或
- 手动触发 `collect` 时勾选 `snapshot=true`。

行为：

1. clone `data`（不存在则 orphan init）；
2. 保留远端 `archive/`、`feeds/`；本地 append-only 内容只有在“远端内容是本地前缀”时才覆盖，否则 fail closed。`output/` 不参与快照；
3. 替换 `state.json`、`articles.json`、`status.json`；
4. 删除旧 ADR-016 的根级 `archive/*.jsonl`；
5. 校验归档（repair 尾部后 validate）、索引可解析、`articles.json` 无正文、有归档时有索引；
6. 提交 `data: snapshot YYYY-MM-DD`，普通 push；被拒时 `pull --rebase` 重试一次。若选择文件冲突，重试会失败而不是覆盖远端，需要人工解决。

失败处理：

- **归档校验失败**：先按第 4 节 repair/validate；远端 `data` 不会被半成品覆盖；
- **feed 索引校验失败**：按第 6 节恢复；不要手写空索引；
- **`articles.json` 含正文**：这是保护。检查 `src.articles.save_articles` / 迁移逻辑；CI 在缓存准备阶段会剥离 `content` / `content_length`；
- **`archive contains articles but feeds/ has no indexes`**：跑 `--rebuild-indexes`；
- **push 被拒且 rebase 后仍失败**：通常是 `data` 上有不兼容的人工提交。拉取 `data`、解决冲突后再手动触发；不要 force-push；
- **恢复失败导致快照跳过**：先修恢复（第 3 节），不要直接手工推送不完整状态；
- **长时间没有新快照**：确认最近几次运行是否因 cache 恢复失败、job 提前失败或 20 小时窗口未到。必要时 `snapshot=true`。

检查远端：

```bash
git fetch origin
git log -1 --format='%h %ci %s' origin/data
git ls-tree -r --name-only origin/data | head -40
```

## 8. 首次迁移与删除 gh-pages

新架构合并后的第一次成功运行会：

1. 如果 `data` 不存在，尝试从旧 `gh-pages` 读取 `state.json`、`output/` 和 raw `articles.json` / `cache.json`；
2. 如果 `data` 只有 ADR-016 布局（没有 `archive/` / `feeds/`），从 cache 或旧 `gh-pages` 取 raw articles 做一次 `src.backfill`；
3. 如果都没有 raw articles，设置 `RSS_FORCE_FETCH=1` 重新抓取 14 天窗口并建立归档；
4. 运行结束后创建或更新含 `archive/` / `feeds/` 的 `data`。

注意：以上迁移只在 cache 未命中时发生。合并后第一次运行如果 cache 仍然有效：

- 若 cache 是旧 ADR-016，`articles.json` 里的 raw 正文会被自动回填到新归档；
- 若 cache 已包含新归档，则不需要迁移；
- 需要强制从 `gh-pages` 迁移时，先删除 `rss-runtime-*` cache 再触发运行。

迁移验证：

```bash
git fetch origin
git ls-tree --name-only origin/data
# 期望：archive  articles.json  feeds  output  state.json  status.json
git show origin/data:articles.json | python -c 'import json,sys; d=json.load(sys.stdin); print("raw entries:", sum(1 for a in d if a.get("content")))'
# 期望：raw entries: 0
git ls-tree -r --name-only origin/data | grep -E '^archive/[0-9]{4}/[0-9]{2}/[0-9]{2}\.md$' | head
git ls-tree -r --name-only origin/data | grep -E '^feeds/.+\.jsonl$' | head
```

确认稳定后删除旧分支和旧 cache：

```bash
git push origin --delete gh-pages
```

- 删除前确认 Pages 的 **Settings → Pages → Source = GitHub Actions**，部署不依赖 `gh-pages`；
- `scripts/migrate_branches.sh` 是 ADR-015 时代的 **legacy 工具**，会重写 `gh-pages`，新架构下不要执行；迁移交给第一次 `collect` 和 `src.backfill`；
- 迁移用的 `backup/*` refs 按需保留；确认新架构稳定后再删：

```bash
git for-each-ref --format='%(refname)' refs/remotes/origin/backup/
git push origin --delete backup/gh-pages-<timestamp>
```

## 9. 轮换 API key

1. 在对应 provider 创建新 key；
2. GitHub 仓库 **Settings → Secrets and variables → Actions** 更新：
   - `OPENAI_API_KEY`
   - `ANTHROPIC_API_KEY`
   - `OPENROUTER_API_KEY`
3. 如果 secret 配在 `github-pages` environment 下，更新 **Environment secrets**，因为 `collect` job 使用了 `environment: github-pages`；
4. 手动触发一次 `collect`，确认 classify 成功；
5. 在 provider 侧吊销旧 key。

注意：

- 不要把 key 写进 `config.toml` 或提交到仓库；
- 使用 `config.toml` 的 `api_key_env` 时，确认环境变量名与 secret 名一致；
- 日志不应输出 key；如果怀疑泄露，先吊销再排查。

## 10. 验证 Pages

1. Actions 中 `collect` 的 **Render archive site** 和 **Deploy to GitHub Pages** 为绿色；
2. 打开 job 输出的 environment URL，或：

   ```bash
   curl -sI https://est.github.io/rss_aggr/ | head
   ```

3. 首页应列出最近的日期和 feed；日期链接指向 `YYYY/MM/DD.html`；
4. 进入某天的页面，确认正文、标题链接和 feed 链接可读；
5. 打开 `feeds/<feed_id>.html`，确认 JS 能加载列表；滚动后正文通过 Range 请求加载（DevTools 里应为 `206`，URL 指向 `archive/YYYY/MM/DD.md`）；
6. 打开 `feeds/<feed_id>.json`，确认是合法 JSON Feed 1.1，且 item 带 `_archive` 坐标、没有 `content_html` 全文；
7. 确认部署来源是 Actions artifact：仓库 **Settings → Pages → Build and deployment → Source = GitHub Actions**；
8. 确认分支契约：

   ```bash
   git fetch origin
   git branch -r
   # 期望长期分支只有 origin/main 和 origin/data（backup/* 可临时存在）
   git ls-tree --name-only origin/data
   ```

   不应再有 `gh-pages`。页面未更新时：先硬刷新/清缓存；再检查 `deploy-pages` 是否失败、`src.render` 是否因归档/索引错误失败。

## 11. Cache 驱逐、损坏与保存失败

cache 是热数据，随时可能被 GitHub 驱逐，这是设计内的：

- **驱逐**：下一轮自动从 `data` 恢复 `archive/`、`feeds/`、`state.json`、`articles.json`、`status.json`；`output/` 会由本轮 report 重新生成；正文不再依赖重新抓取。设置 `RSS_FORCE_FETCH=1` 刷新 14 天窗口；
- **损坏**：恢复步骤检测到非法 JSON 后 warning 并回退 `data`，不会把损坏内容写回；
- **保存失败**：只影响下一次冷启动成本。日志中 `Prepare runtime cache` 的 `ok=false` 或 `Save runtime cache` 失败都不应手工补 cache；
- **key**：`rss-runtime-${run_id}`，通过 `rss-runtime-` 前缀恢复最近一份。日常运行是前缀命中，`cache-hit` 通常为 `false`，以恢复后的文件校验为准；
- **配额**：每轮一个 cache，GitHub 按约 10GB/LRU 管理。频繁冷启动通常是旧 cache 被驱逐；可用 `gh cache list` 查看，并只删除旧的 `rss-runtime-*`，不要无差别删除其他 workflow 的 cache。

常用命令：

```bash
gh cache list --limit 100
gh cache delete --all
gh cache delete <cache-id>
```

不要尝试把 cache 下载后手工合并进 `data`；需要长期保留的内容必须走 `data` 快照。
