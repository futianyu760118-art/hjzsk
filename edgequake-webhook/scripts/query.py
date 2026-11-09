#!/usr/bin/env python3
"""查询 EdgeQuake 图谱 —— 查看实体、关系与检索路径。

用法:
    python3 scripts/query.py "恒剑知识库的核心实体是什么"
    python3 scripts/query.py --mode local "BOM 涉及哪些模块"
    python3 scripts/query.py --docs                 # 列出已摄入文档
    python3 scripts/query.py --stats                # 查看文档状态统计
"""

import argparse
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webhook import app  # noqa: E402


def show_query(q, mode):
    status, body = app.http_json(
        "POST", app.EDGEQUAKE_URL + "/api/v1/query",
        {"query": q, "mode": mode}, timeout=120)

    if status != 200:
        print("查询失败 HTTP %s" % status)
        print(json.dumps(body, ensure_ascii=False, indent=2)[:2000])
        return 1

    if not isinstance(body, dict):
        print(json.dumps(body, ensure_ascii=False, indent=2)[:4000])
        return 0

    print("\n" + "=" * 60)
    print("回答")
    print("=" * 60)
    print(body.get("answer") or body.get("response") or "(无回答字段)")

    ents = body.get("entities") or []
    rels = body.get("relationships") or body.get("relations") or []
    srcs = body.get("sources") or []

    if ents:
        print("\n" + "-" * 60)
        print("实体 (%d)" % len(ents))
        print("-" * 60)
        for e in ents[:30]:
            name = e.get("name") if isinstance(e, dict) else str(e)
            typ = e.get("type") if isinstance(e, dict) else ""
            print("  - %s%s" % (name, "  [%s]" % typ if typ else ""))

    if rels:
        print("\n" + "-" * 60)
        print("关系 (%d)" % len(rels))
        print("-" * 60)
        for r in rels[:30]:
            if isinstance(r, dict):
                s = r.get("source") or r.get("from") or r.get("entity1") or "?"
                t = r.get("target") or r.get("to") or r.get("entity2") or "?"
                p = r.get("type") or r.get("predicate") or r.get("relation") or ""
                print("  - %s --%s--> %s" % (s, ("%s " % p) if p else "", t))
            else:
                print("  - %s" % r)

    if srcs:
        print("\n" + "-" * 60)
        print("来源溯源 (%d)" % len(srcs))
        print("-" * 60)
        for s in srcs[:20]:
            if isinstance(s, dict):
                print("  - %s" % (s.get("title") or s.get("source") or s.get("path") or s))
                txt = s.get("text") or s.get("content") or s.get("snippet")
                if txt:
                    print("      %s" % str(txt)[:150].replace("\n", " "))
            else:
                print("  - %s" % s)

    print("\n" + "=" * 60 + "\n")
    return 0


def show_docs():
    status, body = app.http_json(
        "GET", app.EDGEQUAKE_URL + "/api/v1/documents?page=1&page_size=100", timeout=60)
    if status != 200:
        print("查询失败 HTTP %s: %s" % (status, str(body)[:500]))
        return 1
    docs = body.get("documents", []) if isinstance(body, dict) else []
    print("\n已摄入文档 (%d / total=%s)" % (len(docs), body.get("total") if isinstance(body, dict) else "?"))
    print("-" * 70)
    for d in docs:
        print("  %-10s %-6s chunks=%-5s ent=%-5s %s" % (
            d.get("status", "?"), d.get("id", "")[:6],
            d.get("chunk_count", "?"), d.get("entity_count", "?"),
            d.get("title", "")))
    counts = body.get("status_counts") if isinstance(body, dict) else None
    if counts:
        print("\n状态统计: %s" % json.dumps(counts, ensure_ascii=False))
    print()
    return 0


def show_stats():
    status, body = app.http_json("GET", app.EDGEQUAKE_URL + "/health", timeout=30)
    print("EdgeQuake 健康检查: HTTP %s" % status)
    if isinstance(body, dict):
        print(json.dumps(body, ensure_ascii=False, indent=2)[:1500])
    print()

    st_dir = app.STATE_DIR
    print("本地摄入状态:")
    print("-" * 70)
    if not os.path.isdir(st_dir):
        print("  (状态目录不存在)")
        return 0
    for fn in sorted(os.listdir(st_dir)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(st_dir, fn), "r", encoding="utf-8") as fh:
                st = json.load(fh)
        except Exception:
            continue
        print("  %s" % fn[:-5])
        print("      sha    : %s" % (st.get("sha") or "(无)"))
        print("      更新于 : %s" % (st.get("updated_at") or "-"))
        print("      文档映射: %d 条" % len(st.get("docs") or {}))
        print("      上次统计: %s" % json.dumps(st.get("last_ingested") or {}, ensure_ascii=False))
    print()
    return 0


def main():
    ap = argparse.ArgumentParser(description="EdgeQuake 图谱查询")
    ap.add_argument("query", nargs="?", help="查询内容")
    ap.add_argument("--mode", default="hybrid",
                    choices=["naive", "local", "global", "hybrid", "mix", "bypass"],
                    help="检索模式，默认 hybrid")
    ap.add_argument("--docs", action="store_true", help="列出已摄入文档")
    ap.add_argument("--stats", action="store_true", help="显示健康检查与摄入状态")
    args = ap.parse_args()

    if args.stats:
        return show_stats()
    if args.docs:
        return show_docs()
    if not args.query:
        ap.print_help()
        return 2
    return show_query(args.query, args.mode)


if __name__ == "__main__":
    sys.exit(main())
