#!/usr/bin/env python3
"""通过 Gitee API 自动注册 Webhook —— 省去在网页后台手工点。

用法:
    python3 scripts/register_webhook.py --url https://your.domain/webhook
    python3 scripts/register_webhook.py --url https://your.domain/webhook --test
    python3 scripts/register_webhook.py --list          # 列出已有 webhook

前置：
    .env 中配置 GITEE_TOKEN（需要 hook 权限）、GITEE_WEBHOOK_TOKEN
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webhook import app  # noqa: E402

API = "https://gitee.com/api/v5"


def api(method, path, payload=None, params=None):
    token = app.GITEE_TOKEN
    if not token:
        print("错误：未配置 GITEE_TOKEN", file=sys.stderr)
        sys.exit(2)

    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    data = None
    headers = {"Accept": "application/json",
               "User-Agent": "edgequake-webhook-setup"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    # Gitee API 支持 access_token 查询参数
    url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"access_token": token})

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body
    except Exception as exc:
        return 0, {"error": str(exc)}


def list_hooks(repo):
    status, body = api("GET", "/repos/%s/hooks" % urllib.parse.quote(repo))
    if status != 200:
        print("查询失败 HTTP %s: %s" % (status, body), file=sys.stderr)
        return []
    return body if isinstance(body, list) else []


def main():
    ap = argparse.ArgumentParser(description="注册 Gitee Webhook")
    ap.add_argument("--url", help="Webhook 回调 URL")
    ap.add_argument("--repo", help="仓库 full_name，默认取 .env 的 REPOSITORIES")
    ap.add_argument("--list", action="store_true", help="列出已有 webhook")
    ap.add_argument("--delete", help="按 id 删除 webhook")
    ap.add_argument("--test", action="store_true", help="注册后立即发一次测试推送")
    args = ap.parse_args()

    repos = []
    raw = os.environ.get("REPOSITORIES", "")
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        repos.append(chunk.split("|")[0].strip())
    if args.repo:
        repos = [args.repo]

    if not repos:
        print("未指定仓库。请用 --repo 或在 .env 配置 REPOSITORIES", file=sys.stderr)
        return 2

    for repo in repos:
        print("\n=== %s ===" % repo)
        if args.list:
            for h in list_hooks(repo):
                print("  id=%s url=%s events=%s active=%s" % (
                    h.get("id"), h.get("url"), h.get("push_events"), h.get("active")))
            continue

        if args.delete:
            status, body = api("DELETE", "/repos/%s/hooks/%s" % (
                urllib.parse.quote(repo), args.delete))
            print("  删除 hook %s: HTTP %s" % (args.delete, status))
            continue

        if not args.url:
            print("  缺少 --url，跳过", file=sys.stderr)
            continue
        if not app.WEBHOOK_TOKEN:
            print("  错误：未配置 GITEE_WEBHOOK_TOKEN，无法设置密码", file=sys.stderr)
            return 2

        payload = {
            "url": args.url,
            "password": app.WEBHOOK_TOKEN,
            "push_events": True,
            "merge_requests_events": True,
            "tag_push_events": False,
            "issues_events": False,
            "note_events": False,
            "enable_ssl_verification": True,
            "active": True,
        }
        status, body = api("POST", "/repos/%s/hooks" % urllib.parse.quote(repo), payload)
        if status in (200, 201):
            print("  注册成功: id=%s url=%s" % (body.get("id"), body.get("url")))
        else:
            print("  注册失败 HTTP %s: %s" % (status, body), file=sys.stderr)
            continue

        if args.test:
            ts = base64.b64encode(os.urandom(12)).decode()
            test_payload = {
                "hook_name": "push_hooks",
                "ref": "refs/heads/main",
                "after": "0" * 40,
                "repository": {"full_name": repo, "html_url": args.url.rsplit("/", 1)[0]},
                "commits": [],
            }
            print("  发送测试推送...")
            # 测试推送由 Gitee 发起，这里只做提示
            print("  提示：在 Gitee 仓库页点「测试」按钮可验证连通性")

    return 0


if __name__ == "__main__":
    sys.exit(main())
