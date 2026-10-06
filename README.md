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
| `timezone` | `UTC` | IANA 时区，用于 `since`/`until` 墙钟 |
| `since` | 7 天前 | `YYYY-MM-DD` 或无偏移 ISO；空则用 timezone 下 7 天前 |
| `until` | 现在 | 同上 |
| `exclude-file` | | 每行一个 repo 短名，`#` 注释 |
| `exclude` | | 逗号分隔短名 |
| `out` | `digest.md` | 有活动时写出 Markdown 正文；同目录生成 `.html`（如 `digest.html`）供 HTML 邮件 |
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

1. **Summary（邮件顶部）**：时间窗（`since .. until` + `timezone`）、有活动的 repo 数、总 commit/tag 数，以及每个 repo 的 commit/tag **数量表**（无活动的 repo 不出现）。
2. **按 repo 明细**：`Commits` 在前、`Tags` 在后；每行短 SHA、单行 subject（过长截断）、作者、按 `timezone` 格式化的日期。
3. **发信**：`notify: true` 时 `body-file` 使用 Markdown 文件作纯文本 fallback，`html-file` 指向同次扫描生成的 HTML 文件（路径见 meta 的 `digest-html-file`，默认与 `out` 同主名、`.html` 后缀）；大 HTML 不经 `GITHUB_OUTPUT` 内联，避免 `Argument list too long` 与日志泄露正文。`verbose: false` 时日志仍只打印 `digest written: ...`。

后续 phase 可能增加可配置的 CI/依赖 bump 过滤；当前版本不做 subject 过滤。

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
