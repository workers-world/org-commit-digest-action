#!/usr/bin/env node
/**
 * 发版闸：PR 触及产品文件时，package.json version 须大于最新已发 tag（bump 了才发）。
 * 非产品文件（*.md、docs/**、LICENSE、.gitignore、qodana 配置等）不计。
 * 对齐版本权威语义：版本 = package.json version，见 docs/ci-and-release.md / README「发版」。
 * 用法：node check-release-bump.mjs --base=master --tag-prefix=v
 *   --changed-file=<path>（可多次；供测试注入，缺省用 git diff）
 */
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const args = process.argv.slice(2);
const argOf = (name, fallback) => {
  const hit = args.find((a) => a === name || a.startsWith(`--${name}=`));
  if (!hit) return fallback;
  if (!hit.includes("=")) return true;
  return hit.split("=").slice(1).join("=");
};

const base = argOf("base");
const tagPrefix = argOf("tag-prefix", "v");
const injected = args
  .filter((a) => a.startsWith("--changed-file="))
  .map((a) => a.split("=").slice(1).join("="));

if (!base) {
  console.error("::error::缺少 --base=<PR base 分支>");
  process.exit(1);
}

const NON_PRODUCT = /(^|\/)(docs\/|[^/]*\.md$|LICENSE$|\.gitignore$|\.gitattributes$|qodana\.yaml$)/;

function git(args_, allowFail = false) {
  try {
    return execFileSync("git", args_, { encoding: "utf8" }).trim();
  } catch (err) {
    if (allowFail) return "";
    console.error(`::error::git ${args_.join(" ")} 失败: ${err.message}`);
    process.exit(1);
  }
}

git(["fetch", "origin", `refs/heads/${base}`, "--tags"]);

const changed = injected.length
  ? injected
  : git(["diff", "--name-only", `origin/${base}...HEAD`]).split("\n").filter(Boolean);
const product = changed.filter((f) => !NON_PRODUCT.test(f));

if (product.length === 0) {
  console.log(`ok: 无产品文件变更（${changed.length} 个文件均为 docs/配置），跳过 bump 检查`);
  process.exit(0);
}

const pkg = JSON.parse(readFileSync(new URL("../../package.json", import.meta.url), "utf8"));
const parse = (v) => {
  const m = /^(\d+)\.(\d+)\.(\d+)$/.exec(String(v).trim());
  if (!m) throw new Error(`version 须为 X.Y.Z，当前: ${v}`);
  return m.slice(1).map(Number);
};
const cmp = (a, b) => {
  const [a1, a2, a3] = parse(a);
  const [b1, b2, b3] = parse(b);
  return a1 !== b1 ? Math.sign(a1 - b1) : a2 !== b2 ? Math.sign(a2 - b2) : Math.sign(a3 - b3);
};

const version = pkg.version;
parse(version);
const latest = git(["tag", "-l", `${tagPrefix}[0-9]*`, "--sort=-v:refname"], true).split("\n")[0] || "";

if (!latest) {
  console.log(`ok: 无既有 ${tagPrefix}* tag（首次发版），version=${version} 可用`);
  process.exit(0);
}

const latestVer = latest.slice(tagPrefix.length);
if (cmp(version, latestVer) > 0) {
  console.log(`ok: version ${version} > ${latest}（${product.length} 个产品文件待发）`);
  process.exit(0);
}

console.error(
  `::error::本次 PR 触及 ${product.length} 个产品文件（如 ${product.slice(0, 3).join(", ")}），但 package.json version ${version} 未高于最新 tag ${latest}。请在 Release PR 里 bump version（如 ${nextSuggest(latestVer)}）再合入。`,
);
process.exit(1);

function nextSuggest(v) {
  const [maj, min, pat] = parse(v);
  return `${maj}.${min}.${pat + 1}`;
}
