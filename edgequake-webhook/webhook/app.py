#!/usr/bin/env python3
"""
EdgeQuake Webhook 接收服务 —— Gitee Push/Merge Request -> 增量摄入知识图谱

设计要点
--------
1. 零框架依赖：仅用 Python 标准库 (http.server / json / subprocess / threading)。
2. 5 秒硬超时：Gitee 要求 Webhook 在 5 秒内返回。因此接收线程只做
   "校验 + 入队"，git pull / diff / LLM 抽取全部在后台工作线程执行。
3. 增量摄入：git diff --name-status 拿到 A/M/D，只把新增+修改的文件喂给
   EdgeQuake；删除的文件触发图谱侧删除（反向删除）。
4. 幂等：同一 (repo, sha) 重复推送直接跳过，Gitee 重试不会重复扣 token。
5. 多仓库同名隔离：路径使用 full_name.replace("/", "__")。

运行：python3 webhook/app.py
"""

import hashlib
import hmac
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ─────────────────────────── 配置加载 ───────────────────────────

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dotenv(path=None):
    """极简 .env 解析器：KEY=VALUE，支持 # 注释，不覆盖已有环境变量。"""
    path = path or os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


load_dotenv()

GITEE_TOKEN = os.environ.get("GITEE_TOKEN", "")
GITEE_USERNAME = os.environ.get("GITEE_USERNAME", "")
WEBHOOK_TOKEN = os.environ.get("GITEE_WEBHOOK_TOKEN", "")
EDGEQUAKE_URL = os.environ.get("EDGEQUAKE_URL", "http://127.0.0.1:8080").rstrip("/")
EDGEQUAKE_API_KEY = os.environ.get("EDGEQUAKE_API_KEY", "")
EDGEQUAKE_WORKSPACE_ID = os.environ.get("EDGEQUAKE_WORKSPACE_ID", "")

WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "9000"))
WEBHOOK_HOST = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
WORKER_COUNT = int(os.environ.get("WEBHOOK_WORKERS", "2"))

STATE_DIR = os.environ.get("STATE_DIR", os.path.join(BASE_DIR, "state"))
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", os.path.join(BASE_DIR, "workspace"))
LOG_DIR = os.environ.get("LOG_DIR", os.path.join(BASE_DIR, "logs"))

# 目录黑名单：这些目录不参与摄入
BLACKLIST_DIRS = {
    ".git", "node_modules", "dist", "build", ".venv", "venv", "__pycache__",
    ".idea", ".vscode", "target", "vendor", ".next", ".nuxt", "coverage",
    ".pytest_cache", ".mypy_cache", "logs", "state", "workspace",
}
BLACKLIST_EXTS = {
    ".min.js", ".min.css", ".map", ".lock", ".png", ".jpg", ".jpeg", ".gif",
    ".ico", ".svg", ".pdf", ".zip", ".tar", ".gz", ".exe", ".dll", ".so",
    ".dylib", ".pyc", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3",
}
# 整名匹配的文件（依赖锁文件、构建产物等）
BLACKLIST_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "composer.lock",
    "poetry.lock", "cargo.lock", "gemfile.lock", "go.sum",
    "pipfile.lock", "npm-shrinkwrap.json",
}

# 只摄入这些扩展名（代码仓库场景）
ALLOWED_EXTS = {
    ".md", ".markdown", ".rst", ".txt", ".adoc",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".sql", ".sh", ".bash", ".yml", ".yaml",
    ".json", ".xml", ".toml", ".ini", ".conf", ".vue", ".html", ".css", ".scss",
}

MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", str(1024 * 1024)))  # 1 MiB
MAX_FILES_PER_PUSH = int(os.environ.get("MAX_FILES_PER_PUSH", "200"))
INGEST_TIMEOUT = int(os.environ.get("INGEST_TIMEOUT", "120"))

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(WORKSPACE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "webhook.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("edgequake-webhook")

# ─────────────────────────── 工具函数 ───────────────────────────


def repo_slug(full_name):
    """ningbohengjianguangdian/hengjian-knowledge-hub -> ningbohengjianguangdian__hengjian-knowledge-hub

    用 __ 分隔以避免同名冲突；额外做一次可逆编码，防止原始名字里本就含 __ 时撞车。
    """
    name = full_name or "unknown/unknown"
    if "__" in name:
        # 原始名含 __ 时，先做转义再拼接，保证映射一一对应
        name = name.replace("_", "-")
    return name.replace("/", "__")


def state_path(full_name):
    return os.path.join(STATE_DIR, repo_slug(full_name) + ".json")


def load_state(full_name):
    p = state_path(full_name)
    if not os.path.exists(p):
        return {"sha": None, "docs": {}, "updated_at": None}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        log.warning("状态文件损坏，重建: %s (%s)", p, exc)
        return {"sha": None, "docs": {}, "updated_at": None}


def save_state(full_name, state):
    """原子写入，避免进程中断导致状态文件半截。"""
    p = state_path(full_name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def run_git(args, cwd=None, timeout=120):
    """执行 git 命令，返回 (returncode, stdout, stderr)。"""
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    try:
        proc = subprocess.run(
            ["git"] + args, cwd=cwd, env=env, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return (proc.returncode,
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        return (124, "", "git timeout")
    except FileNotFoundError:
        return (127, "", "git not found")


def should_ingest(path):
    """判断文件是否参与摄入。"""
    base = os.path.basename(path)
    if base in BLACKLIST_DIRS:
        return False
    if base.lower() in BLACKLIST_NAMES:
        return False
    for ext in BLACKLIST_EXTS:
        if base.endswith(ext):
            return False
    lowered = path.lower()
    for seg in lowered.split("/"):
        if seg in BLACKLIST_DIRS:
            return False
    ext = os.path.splitext(base)[1].lower()
    if ext not in ALLOWED_EXTS:
        return False
    return True


def _build_opener(url):
    """构造 urllib opener。

    本机/内网地址（127.0.0.1、localhost、10./172./192. 私有段）必须绕过系统代理，
    否则环境里存在 http_proxy 时，请求本机 EdgeQuake 会被送到代理上导致 502。
    """
    host = urllib.parse.urlparse(url).hostname or ""
    bypass = (
        host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")
        or host.startswith("10.")
        or host.startswith("192.168.")
        or (host.startswith("172.") and host.split(".")[1:2] and 16 <= int(host.split(".")[1]) <= 31)
    )
    if bypass:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def http_json(method, url, payload=None, headers=None, timeout=60):
    """调用 EdgeQuake API，返回 (status_code, parsed_body_or_text)。"""
    hdrs = {"Accept": "application/json"}
    if EDGEQUAKE_API_KEY:
        hdrs["X-API-Key"] = EDGEQUAKE_API_KEY
    if EDGEQUAKE_WORKSPACE_ID:
        hdrs["X-Workspace-ID"] = EDGEQUAKE_WORKSPACE_ID
    if headers:
        hdrs.update(headers)

    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    opener = _build_opener(url)
    try:
        with opener.open(req, timeout=timeout) as resp:
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


# ─────────────────────────── 摄入核心 ───────────────────────────


def _authenticated_url(clone_url):
    """给 Gitee 私有仓 URL 注入凭据，避免交互式提示。

    Gitee 的 HTTPS 认证要求 "用户名:token" 形式（只给 token 会报
    "The token username invalid"）。因此这里注入 GITEE_USERNAME:GITEE_TOKEN。
    若未配置 GITEE_USERNAME，则退回用 token 作为用户名（Gitee 部分接口兼容）。

    用字符串拼接而非 re.sub —— token 中若含 \\1、\\g 之类的字符，
    会被正则当成反向引用而抛异常。
    """
    if not clone_url:
        return clone_url
    if not GITEE_TOKEN:
        return clone_url
    if "gitee.com" not in clone_url:
        return clone_url
    scheme, sep, rest = clone_url.partition("://")
    if not sep:
        return clone_url
    hostpart = rest.split("/")[0]
    if "@" in hostpart:
        # 已含认证信息
        return clone_url
    user = GITEE_USERNAME or "oauth2"
    cred = urllib.parse.quote(user, safe="") + ":" + urllib.parse.quote(GITEE_TOKEN, safe="")
    return "%s://%s@%s" % (scheme, cred, rest)


def ensure_repo(full_name, clone_url):
    """clone 或 fetch 目标仓库，返回本地路径。失败返回 None。

    注意：必须使用完整克隆（不用 --depth 浅克隆）。
    增量摄入依赖 git diff <old_sha> <new_sha>，浅克隆的历史深度不足会导致
    diff 失败或误判为全量变更。
    """
    slug = repo_slug(full_name)
    path = os.path.join(WORKSPACE_DIR, slug)

    url = _authenticated_url(clone_url)

    if not os.path.isdir(os.path.join(path, ".git")):
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        log.info("clone %s -> %s", full_name, path)
        rc, _, err = run_git(["clone", "--no-single-branch", url, path], timeout=600)
        if rc != 0:
            log.error("clone 失败 %s: %s", full_name, err.strip()[:300])
            return None
        return path

    # 已有仓库：更新 remote 后 fetch 全部分支
    rc, _, err = run_git(["remote", "set-url", "origin", url], cwd=path)
    if rc != 0:
        log.warning("设置 origin 失败: %s", err.strip()[:200])
    rc, _, err = run_git(["fetch", "--prune", "--tags", "origin",
                          "+refs/heads/*:refs/remotes/origin/*"], cwd=path, timeout=600)
    if rc != 0:
        log.warning("fetch 失败 %s: %s", full_name, err.strip()[:300])
        return None
    return path


def resolve_head(repo_path, full_name):
    """解析远端 main/master 的最新提交 sha。

    优先用远端跟踪引用；失败则回退到本地 HEAD。
    """
    for ref in ("origin/main", "origin/master"):
        rc, out, _ = run_git(["rev-parse", "--verify", ref], cwd=repo_path, timeout=30)
        if rc == 0 and out.strip():
            return out.strip()
    rc, out, err = run_git(["rev-parse", "HEAD"], cwd=repo_path, timeout=30)
    if rc == 0:
        return out.strip()
    log.warning("[%s] 无法解析 HEAD: %s", full_name, err.strip()[:200])
    return None


def diff_files(repo_path, old_sha, new_sha):
    """返回 (added_or_modified, deleted) 两个路径列表。"""
    added, modified, deleted = [], [], []
    if old_sha and old_sha != new_sha:
        rc, out, err = run_git(
            ["diff", "--name-status", "--find-renames", old_sha, new_sha],
            cwd=repo_path, timeout=60)
        if rc == 0:
            for line in out.splitlines():
                parts = line.split("\t")
                if not parts:
                    continue
                code = parts[0][0]
                path = parts[-1]
                if code == "A":
                    added.append(path)
                elif code in ("M", "R", "T"):
                    modified.append(path)
                elif code == "D":
                    deleted.append(path)
        else:
            log.warning("diff 失败 %s..%s: %s", old_sha[:8], new_sha[:8], err.strip()[:200])
    elif not old_sha:
        # 首次全量：列出该 commit 的所有文件
        rc, out, _ = run_git(["ls-tree", "-r", "--name-only", new_sha], cwd=repo_path, timeout=60)
        if rc == 0:
            added = [p for p in out.splitlines() if p.strip()]

    return added + modified, deleted


def ingest_file(repo_path, rel_path, full_name, doc_id=None):
    """把单个文件摄入 EdgeQuake。返回 (ok, document_id_or_None, message)。"""
    abs_path = os.path.join(repo_path, rel_path)
    if not os.path.isfile(abs_path):
        return False, None, "文件不存在: %s" % rel_path

    try:
        size = os.path.getsize(abs_path)
    except OSError as exc:
        return False, None, "无法读取 %s: %s" % (rel_path, exc)

    if size > MAX_FILE_BYTES:
        return False, None, "超过大小上限 (%d > %d): %s" % (size, MAX_FILE_BYTES, rel_path)

    try:
        with open(abs_path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except UnicodeDecodeError:
        return False, None, "非 UTF-8 文本，跳过: %s" % rel_path
    except OSError as exc:
        return False, None, "读取失败 %s: %s" % (rel_path, exc)

    if not content.strip():
        return False, None, "空文件，跳过: %s" % rel_path

    title = "%s :: %s" % (full_name, rel_path)
    payload = {
        "content": content,
        "title": title,
        "metadata": {
            "repo": full_name,
            "path": rel_path,
            "source": "gitee-webhook",
        },
        "chunk_strategy": "markdown",
    }

    status, body = http_json("POST", EDGEQUAKE_URL + "/api/v1/documents", payload, timeout=INGEST_TIMEOUT)
    if status in (200, 202):
        did = None
        if isinstance(body, dict):
            did = body.get("document_id")
        return True, did, "ok"
    return False, None, "HTTP %s: %s" % (status, str(body)[:200])


def delete_document(doc_id):
    """从图谱中删除文档（反向删除）。"""
    if not doc_id:
        return False, "无 document_id"
    status, body = http_json(
        "DELETE", EDGEQUAKE_URL + "/api/v1/documents/" + urllib.parse.quote(str(doc_id)),
        timeout=INGEST_TIMEOUT)
    if status in (200, 202, 404):
        return True, "ok" if status != 404 else "文档已不存在"
    return False, "HTTP %s: %s" % (status, str(body)[:200])


def ingest_repo(full_name, clone_url, new_sha, force_full=False):
    """对单个仓库执行增量摄入。"""
    state = load_state(full_name)
    old_sha = state.get("sha")
    if old_sha == new_sha and not force_full:
        log.info("[%s] sha 未变 (%s)，跳过", full_name, (new_sha or "")[:8])
        return {"skipped": True, "reason": "sha 未变"}

    repo_path = ensure_repo(full_name, clone_url)
    if not repo_path:
        return {"ok": False, "error": "clone/fetch 失败"}

    # 切到目标提交
    if new_sha:
        rc, _, err = run_git(["checkout", "--force", new_sha], cwd=repo_path, timeout=60)
        if rc != 0:
            # 该 sha 可能还没被 fetch 到，单独取一次
            rc, _, err = run_git(["fetch", "origin", new_sha], cwd=repo_path, timeout=300)
            if rc == 0:
                rc, _, err = run_git(["checkout", "--force", new_sha], cwd=repo_path, timeout=60)
            if rc != 0:
                log.error("checkout %s 失败: %s", new_sha[:8], err.strip()[:200])
                return {"ok": False, "error": "checkout 失败"}

        # 校验工作区确实处于目标提交（避免 diff 与文件内容不一致）
        rc, cur, _ = run_git(["rev-parse", "HEAD"], cwd=repo_path, timeout=30)
        if rc != 0 or cur.strip() != new_sha:
            log.error("HEAD 校验失败：期望 %s，实际 %s", new_sha[:8], (cur.strip() or "?")[:8])
            return {"ok": False, "error": "HEAD 校验失败"}

    changed, deleted = diff_files(repo_path, old_sha, new_sha)
    log.info("[%s] 变更 %d 个文件 (A/M)，删除 %d 个 (D)", full_name, len(changed), len(deleted))

    if len(changed) > MAX_FILES_PER_PUSH:
        log.warning("[%s] 变更文件数 %d 超过上限 %d，截断", full_name, len(changed), MAX_FILES_PER_PUSH)
        changed = changed[:MAX_FILES_PER_PUSH]

    docs = dict(state.get("docs") or {})
    ok_count, fail_count = 0, 0

    for rel in changed:
        if not should_ingest(rel):
            continue
        ok, did, msg = ingest_file(repo_path, rel, full_name)
        if ok:
            ok_count += 1
            if did:
                docs[rel] = did
            log.info("[%s] 摄入成功 %s", full_name, rel)
        else:
            fail_count += 1
            log.warning("[%s] 摄入失败 %s: %s", full_name, rel, msg)

    # 反向删除：文件从仓库消失 -> 从图谱移除
    del_count = 0
    for rel in deleted:
        did = docs.pop(rel, None)
        if did:
            ok, msg = delete_document(did)
            if ok:
                del_count += 1
                log.info("[%s] 反向删除 %s (doc=%s)", full_name, rel, did)
            else:
                log.warning("[%s] 反向删除失败 %s: %s", full_name, rel, msg)
        else:
            log.info("[%s] 删除文件 %s 无 doc 映射，跳过图谱删除", full_name, rel)

    state = {
        "sha": new_sha,
        "docs": docs,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "last_ingested": {"ok": ok_count, "failed": fail_count, "deleted": del_count},
    }
    save_state(full_name, state)
    log.info("[%s] 完成：成功 %d，失败 %d，删除 %d", full_name, ok_count, fail_count, del_count)
    return {"ok": True, "ingested": ok_count, "failed": fail_count, "deleted": del_count}


# ─────────────────────────── 任务队列 ───────────────────────────

TASK_QUEUE = queue.Queue()
TASK_STATS = {"processed": 0, "failed": 0, "last_error": None}
STATS_LOCK = threading.Lock()


def worker_loop(idx):
    log.info("worker-%d 启动", idx)
    while True:
        item = TASK_QUEUE.get()
        try:
            if item is None:
                break
            full_name = item.get("full_name") or ""
            clone_url = item.get("clone_url") or ""
            sha = item.get("sha")
            log.info("[worker-%d] 开始处理 %s @ %s", idx, full_name, (sha or "")[:8])
            result = ingest_repo(full_name, clone_url, sha, force_full=item.get("force_full", False))
            with STATS_LOCK:
                TASK_STATS["processed"] += 1
                if not result.get("ok") and not result.get("skipped"):
                    TASK_STATS["failed"] += 1
                    TASK_STATS["last_error"] = str(result.get("error"))
        except Exception as exc:
            log.exception("worker-%d 处理异常: %s", idx, exc)
            with STATS_LOCK:
                TASK_STATS["failed"] += 1
                TASK_STATS["last_error"] = str(exc)
        finally:
            TASK_QUEUE.task_done()
    log.info("worker-%d 退出", idx)


# ─────────────────────────── HTTP 处理 ───────────────────────────


def parse_push_payload(payload):
    """从 Gitee Push 事件里提取仓库与提交信息。"""
    full_name = ""
    clone_url = ""
    sha = ""

    repo = payload.get("repository") or {}
    full_name = repo.get("full_name") or repo.get("path_with_namespace") or ""
    # 优先用 clone_url（Gitee 会同时给 clone_url 与 html_url，clone_url 已是 .git 结尾）
    clone_url = repo.get("clone_url") or repo.get("html_url") or repo.get("url") or ""
    if clone_url and clone_url.startswith(("http://", "https://")) and not clone_url.endswith(".git"):
        clone_url += ".git"

    after = payload.get("after")
    head_commit = payload.get("head_commit") or {}
    sha = after or head_commit.get("id") or ""

    # 分支过滤：只处理 main/master
    ref = payload.get("ref") or ""
    if ref.startswith("refs/heads/"):
        branch = ref[len("refs/heads/"):]
        allowed = os.environ.get("ALLOWED_BRANCHES", "main,master").split(",")
        if branch.strip() not in [b.strip() for b in allowed]:
            log.info("分支 %s 不在允许列表，跳过", branch)
            return None

    if not full_name or not sha:
        return None
    return {"full_name": full_name, "clone_url": clone_url, "sha": sha}


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "EdgeQuakeWebhook/1.0"

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/health", "/healthz", "/live"):
            self._send_json(200, {"status": "ok", "service": "edgequake-webhook"})
        elif path == "/stats":
            with STATS_LOCK:
                snap = dict(TASK_STATS)
            snap["queue"] = TASK_QUEUE.qsize()
            self._send_json(200, snap)
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/webhook", "/webhook/", "/hooks/gitee"):
            self._send_json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""

        # 校验 Webhook 密码（Gitee 用 X-Gitee-Token 头传递）
        provided = (self.headers.get("X-Gitee-Token")
                    or self.headers.get("X-Hub-Signature")
                    or self.headers.get("X-Gitee-Password") or "")
        if WEBHOOK_TOKEN:
            if not hmac.compare_digest(str(provided), str(WEBHOOK_TOKEN)):
                log.warning("Webhook 密码校验失败")
                self._send_json(401, {"error": "invalid token"})
                return
        elif not provided:
            log.warning("未配置 GITEE_WEBHOOK_TOKEN，且请求未带令牌 —— 拒绝")
            self._send_json(401, {"error": "token required"})
            return

        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            log.warning("无法解析 payload: %s", exc)
            self._send_json(400, {"error": "invalid json"})
            return

        hook = (self.headers.get("X-Gitee-Event")
                or self.headers.get("X-GitHub-Event")
                or payload.get("hook_name") or "")
        log.info("收到事件 %s", hook or "(未知)")

        event = (payload.get("hook_name")
                 or payload.get("event_name")
                 or payload.get("object_kind")
                 or "")

        if event in ("merge_request", "MergeRequest"):
            action = payload.get("action") or ""
            if action not in ("merge", "open", "update"):
                self._send_json(200, {"status": "ignored", "event": event, "action": action})
                return

        item = parse_push_payload(payload)
        if not item:
            self._send_json(200, {"status": "ignored", "reason": "无法解析仓库/提交信息"})
            return

        # 先查状态：同一 sha 直接跳过，避免重复扣 token
        st = load_state(item["full_name"])
        if st.get("sha") == item["sha"]:
            log.info("[%s] sha 已处理 (%s)，跳过", item["full_name"], item["sha"][:8])
            self._send_json(200, {"status": "skipped", "reason": "sha 已处理"})
            return

        TASK_QUEUE.put(item)
        # 关键：立刻返回，不能让 Gitee 等超过 5 秒
        self._send_json(200, {"status": "accepted", "queued": True,
                              "repo": item["full_name"], "sha": item["sha"][:8]})
        log.info("已入队 %s @ %s (队列长度 %d)", item["full_name"], item["sha"][:8], TASK_QUEUE.qsize())


def main():
    if not WEBHOOK_TOKEN:
        log.warning("未设置 GITEE_WEBHOOK_TOKEN —— 所有请求将被拒绝。请在 .env 中配置随机串。")
    if not EDGEQUAKE_API_KEY:
        log.warning("未设置 EDGEQUAKE_API_KEY —— 若 EdgeQuake 开启认证，摄入会失败。")

    for i in range(WORKER_COUNT):
        threading.Thread(target=worker_loop, args=(i,), daemon=True).start()

    srv = ThreadingHTTPServer((WEBHOOK_HOST, WEBHOOK_PORT), WebhookHandler)
    srv.daemon_threads = True
    log.info("EdgeQuake Webhook 接收服务已启动 http://%s:%d/webhook", WEBHOOK_HOST, WEBHOOK_PORT)
    log.info("EdgeQuake 目标: %s", EDGEQUAKE_URL)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("收到中断，退出")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
