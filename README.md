# org-commit-digest-action

GitHub Composite Action：扫描 GitHub **org 全仓**或**单个 repo**，汇总时间窗内指定分支上的 **commits** 与 **tags**，写出 `digest.md`（Markdown 纯文本正文）与配套的 `digest.html`；可选上传 Artifact 或通过 [action-notify-email](https://github.com/workers-world/action-notify-email) 同时发送 **text + HTML** 邮件。

## 快速使用

### Org 扫描 + 发信

```yaml
- uses: workers-world/org-commit-digest-action@v1
  env:
    # Org Secret TOKEN_READ_ORG_COMMIT：PAT 需 repo + read:org，或 fine-grained 对目标 org 全部仓 Contents+Metadata Read
    GH_TOKEN: ${{ secrets.TOKEN_READ_ORG_COMMIT }}
    NOTIFY_WORKER_URL: ${{ vars.NOTIFY_WORKER_URL }}
    NOTIFY_AUTH_TOKEN: ${{ secrets.NOTIFY_GHA_TOKEN }}
  with:
    org: my-org
    exclude-file: exclude.txt
    timezone: Asia/Shanghai
    notify: true
```

### 单仓、仅落盘

```yaml
- uses: workers-world/org-commit-digest-action@v1
  env:
    GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
  with:
    repo: my-org/my-worker
    notify: false
    verbose: true
```

## 范围

| 调用方式 | 行为 |
|----------|------|
| `org: my-org` | 扫描 org 下全部 repo，减去 exclude |
| `repo: owner/name` | 只扫该仓 |
| `org` + 短名 `repo` | 只扫 `org/repo` |
| 两者皆空 | 失败 |

`exclude-file` / `exclude` **仅在 org 模式**生效。

## Inputs

| 名称 | 默认 | 说明 |
|------|------|------|
| `org` / `repo` | | 扫描范围 |
| `branch` | `master` | 列出 commits 的分支；该分支被改名（如 `master`→`dev_00_01_00`）时自动用新名，不存在则回退到仓库默认分支 |
| `include-merges` | `false` | 是否在 Commits 里列出 merge commit（≥2 个 parent）；默认不列，减少 `Merge pull request` 噪音 |
| `noise-filter` | `true` | 过滤可忽略的自动化噪声（action pin、SDK/lockfile bot、空 release 推广等）；`false` 时输出完整列表 |
| `timezone` | `UTC` | IANA 时区，用于 `since`/`until` 墙钟 |
| `since` | 7 天前 | `YYYY-MM-DD` 或无偏移 ISO；空则用 timezone 下 7 天前 |
| `until` | 现在 | 同上 |
| `exclude-file` | | 每行一个 repo 短名，`#` 注释 |
| `exclude` | | 逗号分隔短名 |
| `out` | `digest.md` | 有活动时写出 Markdown 正文；同目录生成 `.html`（如 `digest.html`）与 `.csv`（如 `digest.csv`） |
| `verbose` | `false` | 写入 step summary（公开自跑请保持 false） |
| `notify` | `true` | 有活动时内嵌发信 |
| `notify-to` | | 覆盖 notify-worker `DEFAULT_TO` |
| `fail-on-notify-error` | `true` | |
| `upload-artifact` | `false` | 有活动时上传 digest |

## Outputs

| 名称 | 说明 |
|------|------|
| `digest-file` | markdown 路径；无活动为空 |
| `subject` | 建议邮件标题 |
| `repo-count` | 扫描 repo 数（exclude 后） |
| `active-count` | 窗内有 commit 或 tag 的 repo 数 |
| `scope` | `org:NAME` 或 `repo:owner/name` |

## 邮件模板

有活动时生成的 digest 结构：

1. **Summary（邮件/HTML 顶部，分层）**：一句 **TL;DR**；时间窗与 org KPI（有活动 repo 数、保留 commit/tag 数；`noise-filter: true` 时含 org **Noise%**）。**Cross-repo themes**：跨仓相同 subject 的 fan-out 折叠为一行（见下）。**Top repositories**：默认 Top 8 仓的 `Repo | Commits | Tags | Noise` 表，其余以「+N more repos」提示。**Highlights**：tag/release 与未折叠的 notable commits。**Full detail**（Markdown 内）：完整 Summary 表（含 Noise%）+ 下方按 repo 明细；HTML 邮件正文仅 Summary 层，不含逐仓 commit 列表。
2. **Fan-out fold**：≥2 个 repo 出现「同一主题」commit 时，Summary 里合并为一行（主题 + repo 数 + commit 数，单作者时附作者）。匹配前先规范化 subject：去掉 conventional-commit 前缀、`WW-N` issue 键、全角括号内的仓内限定语（如 `（sch1 试点）`）、末尾 `(#PR)`，折叠空白并小写；**保留** ASCII 括号内的语义（如 `(zizmor secrets-inherit)`），避免无关 CI 主题被并在一起。逐仓完整列表仍在 **Full detail** / CSV 中。
3. **按 repo 明细**（Markdown **Full detail** 之后）：`Commits` 在前、`Tags` 在后；每行短 SHA、单行 subject（过长截断）、作者、按 `timezone` 格式化的日期。
4. **发信**：`notify: true` 时 `body-file` 使用 Markdown 文件作纯文本 fallback，`html-file` 指向同次扫描生成的 HTML 文件（路径见 meta 的 `digest-html-file`，默认与 `out` 同主名、`.html` 后缀）；**另附** `commit-digest.csv`（路径见 `digest-csv-file`，默认与 `out` 同主名、`.csv` 后缀），列含窗口、repo、 type（commit|tag）、sha、name/subject、author、date，**行数与过滤后明细一致**（不受 fan-out 折叠影响）。大 HTML/CSV 不经 `GITHUB_OUTPUT` 内联，避免 `Argument list too long` 与日志泄露正文。`verbose: false` 时日志仍只打印 `digest written: ...`。

### 噪声过滤（`noise-filter`，默认开启）

对 **digest.md / digest.html / digest.csv** 使用同一套规则（first match wins），Summary 计数与明细一致。关闭：`noise-filter: false` 可恢复未过滤的完整 dump。

| 规则 | 典型模式 |
|------|----------|
| R1 | `chore(ci): bump worker-actions … actions/v*`、模板 pin 对齐 |
| R2 | Cloudflare npm / `compatibility_date` 自动 bump |
| R3 | `chore(deps): bump framework_sdk_*` |
| R4 | 仅改 lockfile（含 bot 刷新 package-lock） |
| R5 | dependabot/renovate、窄依赖 bump |
| R6 | `ci: retrigger` |
| R7 | qodana / `.gitignore` 等工具配置 |
| R8 | 仅改 `wrangler.toml/json(c)` 的 trivial app config |
| R9 | `align RELEASE_BRANCH with current dev line` |
| R10 | `release: dev_* → master` 空提交、仅 package/lock/.github、或与同仓窗口内其它提交文件重复 |
| T1 | 浮动主版本 tag（`v1`、`prefix/v2`）；保留 `v1.2.3`、`actions/v0.2.12` |

**保留（不确定时 KEEP）**：WW-45 Issues 扇出、zizmor secrets-inherit、Smart Placement、带真实 src 的 release、人工 CVE 修复、sch1 证据 bot、文档类 deployment 更新等。

R4/R8/R10 会调用 GitHub **commit files** API（与 digest 相同 `GH_TOKEN`），单次 run 内缓存；文件列表不可用时 **fail-open（保留）**，避免误删业务提交。

`verbose: true` 时 stderr 打印各规则忽略条数（不含 subject）；`verbose: false` 时仅一行 dropped 总数，避免公开日志泄露提交标题。

## 空窗行为

时间窗内 **无 commit 且无 tag**：job **success**，log 一行 `no activity in window ...`；**不写** digest、**不发信**、**不上传** artifact；`active-count=0`。

## 排障

- 某仓 commits 列表失败（非空仓 409）：日志只打印 `warn: commit listing failed for N/M repos`（不含仓名，公开日志安全）；`verbose: true` 时才打印仓名与错误。
- **全部**仓 commits 列表失败：job 失败，不会发只有 Tags 的邮件。

## 时区与 since/until

- `YYYY-MM-DD` → 该时区当天 `00:00:00`
- 无偏移 ISO → 按 `timezone` 解释
- 带 `Z` 或 `±offset` → **以字符串偏移为准**，忽略 `timezone`

实现使用 Python `zoneinfo`；请求 GitHub API 前转为 UTC。

## 环境变量

| 名称 | 说明 |
|------|------|
| `GH_TOKEN` / `GITHUB_TOKEN` | 传给 action 的 env；`gh` CLI 使用。Org 扫描请在 workflow 里设 `GH_TOKEN: ${{ secrets.TOKEN_READ_ORG_COMMIT }}`（PAT：`repo` + `read:org`，或 fine-grained 全仓 Contents+Metadata Read）。单仓可用 `GITHUB_TOKEN` |
| `TOKEN_READ_ORG_COMMIT` | **Org Secret 名**（非 action 直接读取）；weekly digest / org 扫描用其值注入 `GH_TOKEN` |
| `NOTIFY_WORKER_URL` | `notify: true` 时必填（Org Variable） |
| `NOTIFY_AUTH_TOKEN` | 与 notify-worker `NOTIFY_GHA_TOKEN` 同值 |

## 本仓自跑

[`.github/workflows/weekly-commit-digest.yml`](.github/workflows/weekly-commit-digest.yml)：

- cron `0 0 * * 1`（周一 08:00 上海）
- `uses: ./`，`org: workers-world`，`exclude-file: exclude.txt`
- Secrets：`TOKEN_READ_ORG_COMMIT`（org 读 PAT，注入 `GH_TOKEN`）、`NOTIFY_GHA_TOKEN`

## 本地调试

```bash
export GH_TOKEN=...
export INPUT_ORG=workers-world
export INPUT_REPO=orchestrator-worker  # 或留空扫 org
export INPUT_TIMEZONE=Asia/Shanghai
export INPUT_VERBOSE=true
./digest.sh
cat digest.md .digest-meta.json
python3 -m unittest discover -s tests -v
```

## 发布

与 [action-notify-email](../action-notify-email/) 相同：`dev_*` push → **Validate action** → **Promote to master**（`worker-promote` 开 Release PR 合入 `master`）。

合入 `master` 后打消费 tag（触发 [gh-release-on-tag.yml](.github/workflows/gh-release-on-tag.yml) 建 GitHub Release 页）：

```bash
git tag v1.0.0 && git push origin v1.0.0
git tag -f v1 && git push origin v1 -f
```

消费方引用 `@v1`。须 Org Secret `GHA_TOKEN`（或 `WORKERS_WORLD_GHA_TOKEN`）供 promote 建/合 PR。
