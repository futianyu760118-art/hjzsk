#!/usr/bin/env python3
"""手动触发摄入 —— 不依赖 Webhook，用于首次全量摄入或排障。

用法:
    python3 scripts/manual_ingest.py                     # 全量摄入配置的所有仓库
    python3 scripts/manual_ingest.py --repo owner/name   # 指定仓库
    python3 scripts/manual_ingest.py --force             # 忽略 sha 状态，强制重新摄入
    python3 scripts/manual_ingest.py --list              # 列出已配置的仓库
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webhook import app  # noqa: E402


def parse_repos():
    """从环境变量 REPOSITORIES 解析仓库列表。

    格式：full_name|url;full_name|url
    也支持从 .env 的 REPOSITORIES 读取。
    """
    raw = os.environ.get("REPOSITORIES", "")
    repos = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "|" in chunk:
            name, url = chunk.split("|", 1)
        else:
            name = chunk
            url = "https://gitee.com/%s.git" % name
        repos.append((name.strip(), url.strip()))
    return repos


def main():
    ap = argparse.ArgumentParser(description="手动触发 EdgeQuake 摄入")
    ap.add_argument("--repo", help="指定仓库 full_name（可重复）")
    ap.add_argument("--force", action="store_true", help="忽略 sha 状态强制重跑")
    ap.add_argument("--list", action="store_true", help="列出配置的仓库")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划不执行")
    args = ap.parse_args()

    repos = parse_repos()
    if args.repo:
        repos = [(args.repo, "https://gitee.com/%s.git" % args.repo)]
    if not repos:
        print("未配置仓库。请在 .env 中设置：")
        print("  REPOSITORIES=owner/repo|https://gitee.com/owner/repo.git")
        return 2

    if args.list:
        print("已配置仓库：")
        for name, url in repos:
            st = app.load_state(name)
            print("  - %s" % name)
            print("      url      : %s" % url)
            print("      last sha : %s" % (st.get("sha") or "(无)"))
            print("      docs 映射: %d 条" % len(st.get("docs") or {}))
        return 0

    if not app.EDGEQUAKE_URL:
        print("错误：未配置 EDGEQUAKE_URL", file=sys.stderr)
        return 2

    for name, url in repos:
        print("\n" + "=" * 60)
        print("仓库: %s" % name)
        print("=" * 60)

        # 准备仓库（clone 或 fetch）
        repo_path = app.ensure_repo(name, url)
        if not repo_path:
            print("  仓库准备失败，跳过")
            continue

        # 取远端最新提交（本地 HEAD 可能是旧提交）
        sha = app.resolve_head(repo_path, name)
        if not sha:
            print("  无法解析远端 HEAD，跳过")
            continue
        print("  远端 HEAD: %s" % sha[:8])

        if args.dry_run:
            st = app.load_state(name)
            print("  已记录 sha: %s" % (st.get("sha") or "(无)"))
            print("  [dry-run] 跳过实际摄入")
            continue

        result = app.ingest_repo(name, url, sha, force_full=args.force)
        print("  结果: %s" % result)

    return 0


if __name__ == "__main__":
    sys.exit(main())
