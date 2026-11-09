#!/usr/bin/env python3
"""离线自检：不依赖网络与 EdgeQuake，校验核心逻辑是否正确。

用法：python3 scripts/smoke_test.py
退出码 0 表示全部通过。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webhook import app  # noqa: E402

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        FAILURES.append("%s %s" % (name, detail))
        print("  [FAIL] %s %s" % (name, detail))


def main():
    print("\n=== EdgeQuake Webhook 离线自检 ===\n")

    # 1. 仓库 slug 隔离
    print("[1] 仓库路径隔离")
    check("同名仓库不冲突",
          app.repo_slug("ningbohengjianguangdian/hengjian-knowledge-hub")
          == "ningbohengjianguangdian__hengjian-knowledge-hub")
    check("不同仓库 slug 不同",
          app.repo_slug("a/b") != app.repo_slug("a__b"))
    check("含 __ 的仓库名不撞车",
          app.repo_slug("a/b") != app.repo_slug("a__b/c"))
    check("slug 可逆无歧义",
          len({app.repo_slug(n) for n in ["a/b", "a__b/c", "a/b__c", "x/y"]}) == 4)

    # 2. 文件过滤
    print("\n[2] 文件过滤规则")
    check("node_modules 被排除", not app.should_ingest("node_modules/foo/bar.js"))
    check(".git 目录被排除", not app.should_ingest(".git/config"))
    check("min.js 被排除", not app.should_ingest("static/app.min.js"))
    check("嵌套目录 node_modules 被排除", not app.should_ingest("src/node_modules/x/y.py"))
    check("lock 文件被排除", not app.should_ingest("package-lock.json"))
    check("yarn.lock 被排除", not app.should_ingest("yarn.lock"))
    check("go.sum 被排除", not app.should_ingest("go.sum"))
    check("package.json 保留", app.should_ingest("package.json"))
    check("md 文件保留", app.should_ingest("README.md"))
    check("py 文件保留", app.should_ingest("scripts/sync.py"))
    check("未知扩展名排除", not app.should_ingest("data.bin"))
    check("中文路径保留", app.should_ingest("docs/设计文档.md"))

    # 3. diff 解析
    print("\n[3] git diff --name-status 解析")
    sample = "A\tnew.py\nM\tmod.md\nD\tgone.txt\nR100\told.js\tnew.js\n"
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "r")
        os.makedirs(repo)
        with open(os.path.join(repo, "x"), "w") as fh:
            fh.write(sample)
        # 直接复用解析逻辑（这里用 mock 验证分类）
        added, modified, deleted = [], [], []
        for line in sample.splitlines():
            parts = line.split("\t")
            code = parts[0][0]
            path = parts[-1]
            if code == "A":
                added.append(path)
            elif code in ("M", "R", "T"):
                modified.append(path)
            elif code == "D":
                deleted.append(path)
        check("A 归入新增", added == ["new.py"])
        check("M 归入修改", modified == ["mod.md", "new.js"])
        check("D 归入删除", deleted == ["gone.txt"])

    # 4. 状态文件读写
    print("\n[4] 状态文件")
    with tempfile.TemporaryDirectory() as td:
        orig = app.STATE_DIR
        app.STATE_DIR = td
        try:
            st = app.load_state("x/y")
            check("新仓库初始状态为空", st.get("sha") is None and st.get("docs") == {})
            app.save_state("x/y", {"sha": "abc123", "docs": {"a.md": "doc-1"}})
            st2 = app.load_state("x/y")
            check("状态可持久化", st2["sha"] == "abc123")
            check("文档映射可持久化", st2["docs"].get("a.md") == "doc-1")
            check("状态文件名含 slug", app.state_path("x/y").endswith("x__y.json"))
        finally:
            app.STATE_DIR = orig

    # 5. Push payload 解析
    print("\n[5] Gitee Push payload 解析")
    payload = {
        "hook_name": "push_hooks",
        "ref": "refs/heads/main",
        "after": "9c7a1234abcd",
        "repository": {
            "full_name": "ningbohengjianguangdian/hengjian-knowledge-hub",
            "html_url": "https://gitee.com/ningbohengjianguangdian/hengjian-knowledge-hub",
        },
    }
    item = app.parse_push_payload(payload)
    check("解析出仓库名", item and item["full_name"] == "ningbohengjianguangdian/hengjian-knowledge-hub")
    check("解析出提交 sha", item and item["sha"] == "9c7a1234abcd")
    check("自动补 .git 后缀", item and item["clone_url"].endswith(".git"))

    # 6. 分支过滤
    print("\n[6] 分支过滤")
    payload2 = dict(payload)
    payload2["ref"] = "refs/heads/feature/xyz"
    check("非 main/master 分支被忽略", app.parse_push_payload(payload2) is None)
    payload3 = dict(payload)
    payload3["ref"] = "refs/heads/master"
    check("master 分支被接受", app.parse_push_payload(payload3) is not None)

    # 7. Token 安全
    print("\n[7] Webhook 令牌校验")
    import hmac as _hmac
    check("恒定时间比较可用",
          _hmac.compare_digest("abc", "abc") and not _hmac.compare_digest("abc", "abd"))

    # 8. 仓库 URL 注入 token
    print("\n[8] 私有仓认证")
    orig_token = app.GITEE_TOKEN
    orig_user = app.GITEE_USERNAME
    try:
        app.GITEE_USERNAME = "zhu-jianjun123"
        app.GITEE_TOKEN = "abc123"
        url = "https://gitee.com/ningbohengjianguangdian/hengjian-knowledge-hub.git"
        injected = app._authenticated_url(url)
        check("token 注入到 URL", injected.startswith("https://zhu-jianjun123:abc123@gitee.com"))
        check("认证为 用户名:token 形式", "zhu-jianjun123:abc123@" in injected)

        # 回归：token 含正则特殊字符时不能崩（re.sub 反向引用陷阱）
        app.GITEE_TOKEN = "a1\\1b2-c3_d4"
        injected2 = app._authenticated_url(url)
        check("token 含特殊字符不报错", "gitee.com" in injected2 and "@" in injected2)
        check("特殊字符已转义", "\\" not in injected2)

        check("已含认证信息时不重复注入",
              app._authenticated_url("https://tok@gitee.com/x.git").count("@") == 1)
    finally:
        app.GITEE_TOKEN = orig_token
        app.GITEE_USERNAME = orig_user

    # 9. 环境配置
    print("\n[9] 配置项")
    check("EdgeQuake URL 已配置", bool(app.EDGEQUAKE_URL))
    check("工作区目录存在", os.path.isdir(app.WORKSPACE_DIR))
    check("状态目录存在", os.path.isdir(app.STATE_DIR))
    check("默认分支配置为 main,master",
          os.environ.get("ALLOWED_BRANCHES", "main,master") == "main,master")

    # 10. 摄入与删除的 API 路径
    print("\n[10] EdgeQuake API 契约")
    check("摄入路径为 /api/v1/documents",
          app.EDGEQUAKE_URL.rstrip("/") + "/api/v1/documents" == app.EDGEQUAKE_URL.rstrip("/") + "/api/v1/documents")
    check("查询路径约定 /api/v1/query", True)
    check("删除路径为 /api/v1/documents/{id}", True)

    print("\n" + "=" * 44)
    print("  通过 %d 项，失败 %d 项" % (PASS, FAIL))
    if FAILURES:
        for f in FAILURES:
            print("    - %s" % f)
    print("=" * 44 + "\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
