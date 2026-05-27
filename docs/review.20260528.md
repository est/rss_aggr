# RSS Aggregator — 踩坑与经验教训

从 git 历史回看这个项目，从 5 月 12 日首次部署到 5 月 27 日基本稳定，两周时间踩了不少坑。按主题整理。

---

## 一、GitHub Actions + Pages 部署

这是前期折腾最多的部分，8 个 commit 才搞定。

### 1.1 gh-pages 分支的 output 目录结构反复

**问题**: 最初直接把 output 文件 push 到 gh-pages，结果目录结构混乱——出现了 `output/output/YYYY/` 的嵌套，还有 `202605/12.md` vs `2026/0512.md` 的路径不一致。

**根因**: 没有在一开始就确定 gh-pages 分支上的目录规范。deploy 脚本里 `cp -r output` 和 `cp -r output/*` 的区别导致了嵌套。

**教训**:
- 在写第一行 deploy 脚本之前，先画出两个分支（main vs gh-pages）的目录树
- gh-pages 上的内容就是最终网站根目录，main 上的 `output/` 是工作目录——两者是不同的
- `cp -r src dst` 和 `cp -r src/* dst` 差一个 `*`，后果差一层目录

### 1.2 Jekyll vs 纯 Markdown

**问题**: GitHub Pages 默认用 Jekyll 构建，但 Jekyll 会忽略 `_` 开头的目录、处理 `.md` 链接等，导致很多意外行为。

**经过**: 先尝试 `.nojekyll` 文件 → 发现不够 → 改用 pandoc → 又改回 Jekyll → 最后用 `actions/jekyll-build-pages` 官方 action。

**教训**:
- 如果不需要 Jekyll 的模板功能，直接在 gh-pages 根目录放 `.nojekyll` + 原始 markdown 是最简单的
- 如果用 Jekyll，markdown 里的链接 `.md` 要改成 `.html`（项目里用 `sed` 做了替换）
- 一开始就应该选清楚，中途切换成本很高

### 1.3 state.json 的归属问题

**问题**: state.json 一度被 commit 到 main 分支（7300 行！），后来被 deploy 流程删除，又手动 restore。

**根因**: state.json 是运行时状态，不应该在 main 分支上。但 deploy 流程需要在多次运行之间持久化它。

**教训**:
- 运行时状态（state.json）放 gh-pages 分支，不要放 main
- main 分支只放代码和配置
- gh-pages 分支既是网站，也是持久化存储——一个分支两份工

---

## 二、RSS 采集

### 2.1 timeout 参数类型

**问题**: `fetch_feed` 的 timeout 参数声明为 `int`，但默认值写成 `(2, 5)` 元组（连接超时, 读取超时）。当上层传入单个 int 时，requests 会把它当作两个超时都是同一个值。

**教训**: Python 的类型注解不是运行时检查。参数要么统一为 tuple，要么内部做类型转换。

### 2.2 没有条件请求（ETags/Last-Modified）

**问题**: 每次运行都全量下载所有 feed，即使内容没变。

**教训**: HTTP 的条件请求（`If-None-Match` / `If-Modified-Since`）是 RSS 场景的标配。feedparser 本身不处理这个，需要自己在 requests 层面做。304 响应可以省掉大量无用传输。

### 2.3 编码检测

**问题**: `resp.apparent_encoding` 有时会猜错（比如把 UTF-8 中文猜成 ISO-8859-1）。

**教训**: 优先级应该是 Content-Type charset > BOM > apparent_encoding > utf-8。不能盲信 chardet。

---

## 三、AI 分类

### 3.1 Prompt 演变

项目经历了 3 代 prompt：

1. **JSON 输出**: AI 返回 `{"category": "...", "score": ...}` → 解析经常失败，AI 会加 markdown 代码块
2. **纯文本块**: 改为 `category: xxx\nscore: 8` → 稳定多了
3. **skip_prompt 融入**: 把站点过滤规则放到每篇文章的 user message 中

**教训**:
- AI 输出格式越简单越好，纯文本 > JSON
- 要给 AI "不处理" 的选项（`skip` category），否则它会硬分类
- `normalize_category()` 是必要的——AI 返回的 category 可能带括号、引号、解释文字

### 3.2 Batch 失败的级联效应

**问题**: 一个 batch 10 篇文章，API 超时或返回异常 → 整批 10 篇都丢失。

**教训**: batch 失败后应该逐篇重试。单篇请求虽然慢，但比全丢好。

### 3.3 URL 匹配是最大隐患

**问题**: 整个流程用文章 link 做去重和结果回写。但 AI 返回的 URL 可能和输入的略有不同（加/减斜杠、http vs https、query 参数），导致 "unmatched"——AI 分类结果回写不到文章上。

**根因**: 没有 URL 规范化。`https://example.com/a` 和 `http://example.com/a/` 被当作不同的 URL。

**教训**:
- URL 规范化必须在第一步就做，贯穿整个流程
- 去scheme（http/https）、去尾斜杠、去tracking参数（utm_*）
- 这个问题从第一天就存在，直到最后才发现

### 3.4 内容截断

**问题**: `content[:1500]` 直接截断，可能在句子中间断开，影响 AI 理解。

**教训**: 在句号（中英文）、段落边界处截断，保留语义完整性。

---

## 四、去重与存储

### 4.1 Markdown 作为数据存储的局限

**问题**: 用正则 `r"\]\(([^)]+)\)"` 从 markdown 表格中提取链接。如果文章标题包含 `[]()` 或 `|`，解析就会出错。

**教训**: Markdown 适合展示，不适合存储结构化数据。如果重来，应该用 SQLite 或 JSON 做主存储，markdown 只做渲染输出。

### 4.2 pending_content 丢失

**问题**: fetch 阶段保存的 pending_content 在 classify 阶段被覆盖。如果 fetch 后进程崩溃，未分类的文章内容就丢了。

**教训**: 持久化应该是 merge 模式，不是 overwrite。

### 4.3 去重变量名误导

**问题**: `load_seen_guids()` 实际加载的是 link，不是 guid。变量名 `existing_guids` 存的也是 link。

**教训**: 命名要准确。误导的命名比没有命名更危险。

---

## 五、过滤与 Skip 逻辑

### 5.1 两层过滤

项目最终有两层过滤：
1. **标题关键词过滤**（`skip_titles`）: 确定性规则，免费、快速
2. **AI skip_prompt**（`skip_prompt`）: 站点级规则，让 AI 判断是否跳过

**教训**: 能用确定性规则解决的，不要用 AI。AI 过滤是最后手段。

### 5.2 skip_prompt 的位置

**问题**: skip_prompt 最初放在 system prompt 里，但一个 batch 可能混合多个站点的文章，system prompt 的规则会互相干扰。

**最终方案**: skip_prompt 放在每篇文章的 user message 中（`Category: {skip_prompt}`），让 AI 知道这条规则只适用于这篇文章。

---

## 六、测试

### 6.1 后补测试

项目的测试是在 bug 出现后才加的（`cf15b14 fix`、`37dd16f fix: category`、`3e46939 fix shit`），不是提前写的。

**教训**: 存储层和解析层的单元测试应该从第一天就写。这些函数的输入输出明确，非常适合 TDD。

### 6.2 测试覆盖的盲区

- URL 规范化没有测试（直到这次重构才暴露）
- fetcher 的编码检测没有测试
- classifier 的 prompt 输出解析没有测试（因为依赖 AI 输出，难 mock）

**教训**: 至少 `_parse_blocks`、`_normalize_link`、`_detect_encoding` 这些纯函数应该有测试。

---

## 七、代码质量

### 7.1 commit message

从 git 历史看，很多 commit message 是 `fix`、`fix shit`、`1`、`add: update shit`。两周后回头看完全不知道改了什么。

**教训**: commit message 是写给未来的自己的。至少说明改了什么、为什么改。

### 7.2 异常处理

`except (ValueError, Exception) as e:` — `Exception` 已经包含 `ValueError`，写法多余。这类问题说明 code review 缺失。

### 7.3 配置与硬编码

`max_workers=10`、`ARTICLE_LEN=1500`、`BATCH_SIZE=10` 这些值最初都是硬编码的。后来才移到 config.toml。

**教训**: 影响行为的数值都应该可配置，哪怕默认值不变。

### 7.4 "改进" 引入的回归

**问题**: 重构时把 aggregator 条目从 `all_entries` 中过滤掉了（`else: all_entries.extend(...)`），本意是"aggregator 源的文章不进 output"。但之前这些源虽然被标记为 aggregator，文章仍然正常输出。改动后 24 个源（Hacker News、The Verge、OpenAI 等）的文章全部消失，output 从 15 个日期文件缩水到 3 个。

**根因**: 没有理解原代码的行为——标记 aggregator 只是为了统计，不是为了过滤。改代码时只看了"应该怎样"，没看"原来怎样"。

**教训**:
- 改现有行为前，先确认原来的行为是有意的还是无意的
- "改进" 比 "新写" 更危险，因为你以为你理解了，但可能没有
- gh-pages 上的 output 就是回归测试——对比前后输出是最有效的验证

---

## 八、架构反思

### 8.1 fetch 和 classify 的耦合

fetch 保存文章到 markdown，classify 从 markdown 读取文章再分类。中间通过 pending_content.json 传递 content。这个设计导致：
- markdown 解析脆弱
- pending_content 可能丢失
- 文章内容被序列化/反序列化了两次

**更好的方案**: fetch 的结果直接存在内存或临时文件中，classify 直接消费，不需要经过 markdown 中转。

### 8.2 单文件 vs 数据库

用 markdown 文件做存储，导致：
- 去重需要扫描所有文件（O(n)）
- 更新分类需要重写整个文件
- 没有原子写入，并发不安全

**更好的方案**: SQLite 做主存储，markdown 只做展示层的渲染输出。

### 8.3 三个步骤的分离

`--sync`、`--fetch`、`--classify` 三步分离是正确的设计。每步可以独立运行、独立调试。但步骤之间通过文件系统传递状态（state.json、pending_content.json、output/*.md），增加了脆弱性。

---

## 总结

| 类别 | 核心教训 |
|------|---------|
| 部署 | 先设计目录结构，再写 deploy 脚本 |
| 采集 | HTTP 条件请求是标配，编码不能盲信 |
| AI | 输出格式越简单越好，URL 规范化必须从第一天做 |
| 存储 | Markdown 适合展示不适合存储，去重变量名要准确 |
| 过滤 | 确定性规则优先，AI 是最后手段 |
| 测试 | 存储层和解析层的测试应该提前写 |
| 代码 | commit message 要有意义，异常处理不要冗余 |
| 回归 | "改进" 比新写更危险，改之前先确认原行为是有意的 |

最大的经验：**前期花 1 小时设计，后期省 10 小时修 bug**。这个项目很多问题（目录结构、URL 规范化、存储选型）如果在第一天就想清楚，后面不需要那么多 `fix` commit。
