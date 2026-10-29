# RUNBOOK

面向无人值守运行的故障处理手册。分支契约见 [ADR-015](ADR/015.branch.runtime-data.md)。

## 0. 快速定位

```bash
gh run list --workflow "RSS Collection & Classification" --limit 10
gh run view <run-id> --log-failed
```

没有 `gh` 时在 GitHub Actions 页面查看。`test` job 失败时 `collect` 不会启动；`collect` 失败时，标了 `if: always()` 的 `--report`、`--check` 和两个持久化步骤仍会执行。

## 1. 排查失败运行

### test job 失败

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
ruff check .
pytest -q
```

修复后重新运行。`test` 只读 `main`，不会碰运行时分支。

### collect job 失败

按步骤判断：

| 失败步骤 | 常见原因 | 处理 |
|----------|----------|------|
| Restore runtime data | `data` 分支不存在 | 正常 bootstrap，查看是否只是 warning |
| Restore output | `gh-pages` 不存在或无 `output/` | 正常 bootstrap，查看是否只是 warning |
| Fetch RSS | `state.json` 损坏、feeds 配置错误、网络问题 | 校验/恢复 `state.json`（见第 3 节），重跑 |
| Classify articles | API key 失效、额度不足、`base_url` 错误 | 轮换 key（第 4 节），检查 `config.toml` |
| Cleanup | `articles.json` 损坏 | 恢复 `articles.json` 或删除让其重建 |
| Generate run report | `--report` 实现/数据问题 | 查看日志；修复后重跑 |
| Verify runtime contract | `--check` 发现数据或输出不一致 | 按日志修复，修复前不会部署 Pages |
| Persist data/site | 权限、分支保护、clone 失败 | 检查 job 的 `contents: write`，确认分支未被保护 |

补充：

- 同一 `rss-collect` 组的运行会排队，不会取消正在运行的采集。
- fetch/classify 可以安全重跑：`articles.json` 和 `output/` 都按链接去重。
- `--check` 失败时 Pages 不部署，但 `always()` 的数据/站点快照仍会尝试写入；修复后用 `workflow_dispatch` 重跑。
- 持久化步骤发现 `state.json` 或 `articles.json` 缺失时会 warning 并跳过 `data` push，这是保护行为，不要绕过。

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

操作步骤（建议在两次采集之间进行，必要时先临时停用 workflow）：

```bash
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

`data` 是快照分支，正常 fast-forward push 即可；如果 CI 同时 force-push 导致推送被拒，等当前运行结束后重试。

如果 feed 已经彻底失效：

- 在 `main` 上通过 PR 修改 `feeds.toml`，设置 `skip = true` 或删除条目；
- 检查 `sources.toml` / `--sync` 是否会把它重新加回来；
- 仅改 `state.json` 不够，下一次 `sync-feeds` 或手工加回后它仍会被抓取。

## 3. 恢复 data 分支

先确认最新状态：

```bash
git fetch origin
git ls-tree -r --name-only origin/data
git log --oneline origin/data -10
```

### 分支还在，但文件损坏

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

git push --force origin data
```

### 分支被删除或需要重建

从迁移备份恢复（推荐）：

```bash
git fetch origin
git branch -a | grep 'backup/data-'
git push --force origin refs/remotes/origin/backup/data-<timestamp>:refs/heads/data
```

没有可用备份时做空状态 bootstrap：

1. 删除远端 `data` 分支（或让它不存在）；
2. 手动运行 `collect`；
3. 工作流会以空状态抓取，首次成功后自动创建只含 `state.json` / `articles.json` / `status.json` 的 orphan `data` 分支。

空状态意味着所有 feed 会被重新抓取、失败计数清零、禁用状态丢失；`output/` 的链接去重会阻止重复文章写入。

### articles.json 丢失但 state.json 正常

可以直接让 `articles.json` 缺失后重跑 fetch：在保留 `state.json` 的前提下重建 `data` 分支，只放 `state.json`，CI 会重新采集窗口内文章。不要伪造空的 `articles.json` 之外的结构。

### 恢复后校验

```bash
git fetch origin
git ls-tree --name-only origin/data
# 期望仅：articles.json  state.json  status.json（status 可能尚未生成）
```

## 4. 轮换 API key

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

## 5. 执行分支迁移（一次性）

前置条件：

- 新 workflow 已合并到 `main`；
- 本地仓库有 push 权限；
- 暂停或等待当前 `collect` 运行结束（迁移会 force-push 两个分支）。

先看计划，不做修改：

```bash
git fetch origin
bash scripts/migrate_branches.sh
```

确认计划后执行：

```bash
bash scripts/migrate_branches.sh --apply
```

脚本会：

1. 把旧分支备份到 `backup/gh-pages-<timestamp>` / `backup/data-<timestamp>`；
2. 创建只含 `state.json` / `articles.json` 的 orphan `data`；
3. 把 `gh-pages` 重写为只含 `output/`；
4. 打印回滚命令。

迁移后校验：

```bash
git fetch origin
git ls-tree --name-only origin/data
git ls-tree -r --name-only origin/gh-pages | head
```

回滚（把 `<timestamp>` 换成脚本输出的值）：

```bash
git push --force origin refs/heads/backup/gh-pages-<timestamp>:refs/heads/gh-pages
git push --force origin refs/heads/backup/data-<timestamp>:refs/heads/data
```

确认稳定后再删除 `backup/*` refs。

## 6. 验证 Pages

1. Actions 中 `collect` 的 **Deploy to GitHub Pages** 为绿色；
2. 打开 job 输出的 environment URL，或：

   ```bash
   curl -sI https://est.github.io/rss_aggr/ | head
   ```

3. 首页日期链接应以 `.html` 结尾，且包含最近一次采集的日期；
4. 进入某天的页面，确认表格可读、链接可点击；
5. 确认部署来源是 Actions artifact：仓库 **Settings → Pages → Build and deployment → Source = GitHub Actions**；
6. 确认分支契约：

   ```bash
   git fetch origin
   git ls-tree --name-only origin/data
   git ls-tree -r --name-only origin/gh-pages | grep -E 'state\.json|articles\.json|status\.json' || echo "ok: no runtime files on gh-pages"
   ```

7. 页面未更新时：先硬刷新/清缓存；再检查 `deploy-pages` 是否被 `--check` 失败跳过；最后确认 `environment: github-pages` 的 deployment branch/tag 规则没有阻止部署。
