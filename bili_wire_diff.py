#!/usr/bin/env python3
"""wire 对账器：把 opencode 真实发出的请求（bili 的 INCOMING 转储）与
我们转换出的 core 列表逐位对照，定位"哪些内容类别的 id 对不上"。

背景：转换器是用 opencode 2.x 的 wire 标定的。在 opencode 1.x 上跑时，
如果某些消息的序列化方式不同，其 core id 就会变 → bili 报
`fold-reconcile ... covered id(s) missing`。本脚本把差异按
(角色, 内容类型) 分类并给出样本，用来找出规则差异。

## 用法

    # 1) 先抓一份真实请求（bili 的请求体转储）
    ACP_DUMP_BODY=1 opencode --session <sid> --prompt "…"        # 跑一次即可
    # 转储默认落在 ~/.local/state/billion-context/dumps/

    # 2) 对账
    python3 bili_wire_diff.py \
        --wire ~/.local/state/billion-context/dumps \
        --db   ~/.local/share/opencode/opencode.db \
        --sid  ses_xxx  [--limit 5]

输出的每一条差异类都会带样本文本，直接看"缺的那类长什么样"即可判断规则。
"""
import argparse
import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bili_convert import build_cores, load_rows  # noqa: E402


def find_dump(root: pathlib.Path, sid: str):
    """找最新的请求转储，返回 (路径, 种类)。

    两种转储（bili 的目录布局）：
      <state>/raw/<ts>-<sid>-INCOMING.txt   ← 客户端**原始** wire（我们要的就是这个）
      <state>/dumps/req-<ts>-<sid>.json     ← 折叠后**转发体**（仅作退路）
    """
    root = root.expanduser()
    if root.is_file():
        return root, ("INCOMING(客户端原始)" if "INCOMING" in root.name else "req(折叠后转发体)")
    cands = [p for p in root.rglob("*") if p.is_file()
             and ("INCOMING" in p.name or (p.name.startswith("req-") and p.suffix == ".json"))]
    if not cands:
        raise SystemExit(
            f"{root} 下没找到转储。先确认：\n"
            f"  · 代理是在设置了 ACP_DUMP_BODY=1 之后启动的（改环境变量后必须重启代理）\n"
            f"  · 原始请求转储在 <state>/raw/，折叠后转发体在 <state>/dumps/\n"
            f"  直接把 <state> 目录（通常是 ~/.local/state/billion-context）传给 --wire 即可")
    inc = [p for p in cands if "INCOMING" in p.name]
    pool = inc or cands                       # 优先原始请求
    pref = [p for p in pool if sid in p.name]
    pool = pref or pool
    p = max(pool, key=lambda x: x.stat().st_mtime)
    return p, ("INCOMING(客户端原始)" if "INCOMING" in p.name else "req(折叠后转发体)")


def parse_dump(p: pathlib.Path) -> dict:
    """INCOMING.txt：`<METHOD> <url>\\n<headers>\\n\\n<body>`；req-*.json：直接是 body。"""
    raw = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix != ".json":
        i = raw.find("\n\n")
        if i < 0:
            raise SystemExit(f"{p}: 找不到 header/body 分隔空行")
        raw = raw[i + 2:]
    try:
        return json.loads(raw)
    except Exception as e:
        raise SystemExit(f"{p}: 请求体不是合法 JSON（{e}）；前 120 字：{raw[:120]!r}")


def scontent(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return ""


def wire_cores(body: dict):
    """按 bili 的 openaiToCore 语义，把真实 wire 折成 core 列表。"""
    out, cl = [], collections.Counter()

    def push(role, ct, text, tcid="", tname=None):
        import hashlib
        seed = f"{role}|{ct}|{tcid}|{tname or ''}|{text}"
        base = "h_" + hashlib.sha256(seed.encode()).hexdigest()[:16]
        n = cl[base]; cl[base] += 1
        cid = base if n == 0 else f"{base}_{n}"
        out.append({"id": cid, "role": role, "ct": ct, "text": text})

    for m in body.get("messages") or []:
        r = m.get("role")
        if r in ("system", "developer"):
            if not out:
                continue                      # 前导 system 被提到 fold space 之外
            push("user", "text", scontent(m.get("content"))); continue
        if r == "user":
            push("user", "text", scontent(m.get("content")))
        elif r == "assistant":
            rc = m.get("reasoning_content")
            rs = rc if isinstance(rc, str) else ""
            tx = scontent(m.get("content"))
            if rs: push("assistant", "reasoning", rs)
            if tx: push("assistant", "text", tx)
            for tc in (m.get("tool_calls") or []):
                f = tc.get("function") or {}
                push("assistant", "tool-call", f.get("arguments") or "", tc.get("id") or "", f.get("name"))
        elif r == "tool":
            push("tool", "tool-result", scontent(m.get("content")), m.get("tool_call_id") or "")
    return out


def build_cores_rule(rows, rule):
    """按候选规则重建"我们的 core 列表"，用于规则搜索。

    rule:
      v2            reasoning → reasoning_content（当前实现的假设，= 2.x 的 wire）
      c<sep>w<sep2> 把一条 assistant 消息的 [reasoning..., text...] 拼成一个 content，
                    c 后是"组之间"的分隔符、w 后是"组内"的分隔符；\\n 表示换行
      例如 c|w      组间用换行、组内直接相连
    """
    import hashlib
    cores, cl = [], collections.Counter()

    def push(role, ct, text, tcid="", tname=None):
        seed = f"{role}|{ct}|{tcid}|{tname or ''}|{text}"
        base = "h_" + hashlib.sha256(seed.encode()).hexdigest()[:16]
        n = cl[base]; cl[base] += 1
        cores.append(base if n == 0 else f"{base}_{n}")

    for mid, typ, data in rows:
        m = json.loads(data)
        if typ == "user":
            push("user", "text", m.get("text", "")); continue
        if typ != "assistant":
            continue
        content = m.get("content") or []
        rs = [p.get("text", "") for p in content if p.get("type") == "reasoning" and p.get("text")]
        tx = [p.get("text", "") for p in content if p.get("type") == "text" and p.get("text")]
        tools = [p for p in content if p.get("type") == "tool"]
        if rule == "v2":
            if rs: push("assistant", "reasoning", "".join(rs))
            if tx: push("assistant", "text", "".join(tx))
        else:
            body = rule[1:]
            between, within = body.split("w", 1)
            between = between.replace("|", "\n")
            within = within.replace("|", "\n")
            parts = [within.join(rs)] if rs else []
            if tx:
                parts.append(within.join(tx))
            joined = between.join([p for p in parts if p])
            if joined:
                push("assistant", "text", joined)
        for x in tools:
            st = x.get("state") or {}
            args = json.dumps(st.get("input") or {}, separators=(",", ":"), ensure_ascii=False)
            push("assistant", "tool-call", args, x.get("id", ""), x.get("name"))
        for x in tools:
            st = x.get("state") or {}
            parts = st.get("content") or []
            out = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
            if st.get("status") == "error":
                out = json.dumps({"error": st.get("error"), "content": parts},
                                 separators=(",", ":"), ensure_ascii=False)
            push("tool", "tool-result", out, x.get("id", ""))
    return cores


def try_rules(rows, wire, limit=10):
    """把几种候选序列化规则各跑一遍，看谁和真实 wire 最对得上。"""
    wset = collections.Counter(c["id"] for c in wire)
    wid = [c["id"] for c in wire]
    rules = ["1x-provider", "v2", "c|w", "cw", "c||w", "c||w||", "c|w|"]
    print("规则搜索（谁能复现 1.19 真实 wire）：")
    print("    （1x-provider = 逐条按消息的 provider/model 判形态，见 bili_convert.py 的 MEASURED_FORMS）")
    print(f"    {'规则':10} {'对得上':>7} {'只在我们':>8} {'只在wire':>8}   {'有序前缀':>8}")
    best = None
    for r in rules:
        try:
            if r == "1x-provider":
                o = [c[0] for c in build_cores(rows, "1x")[0]]
            else:
                o = build_cores_rule(rows, r)
        except Exception as e:
            print(f"    {r:10} 规则解析失败: {e}"); continue
        oc = collections.Counter(o)
        inter = sum(min(n, wset[k]) for k, n in oc.items())
        prefix = 0
        for a, b in zip(o, wid):
            if a != b: break
            prefix += 1
        print(f"    {r:10} {inter:7} {len(oc)-inter:8} {len(wset)-inter:8}   {prefix:8}")
        score = (inter, prefix)
        if best is None or score > best[0]:
            best = (score, r)
    if best:
        print(f"    → 最佳候选：{best[1]}（对得上 {best[0][0]}，有序前缀 {best[0][1]}）")


def main():
    ap = argparse.ArgumentParser(description="wire 对账（真实请求 vs 转换产物）")
    ap.add_argument("--wire", required=True, help="INCOMING 转储文件或所在目录")
    ap.add_argument("--db", required=True, help="内容源库（用来重算我们的 core）")
    ap.add_argument("--sid", required=True, help="会话 id")
    ap.add_argument("--limit", type=int, default=3, help="每类打印几个样本（默认 3）")
    ap.add_argument("--try-rules", action="store_true",
                    help="额外：把几种候选的 assistant 序列化规则各跑一遍，看谁能复现真实 wire")
    args = ap.parse_args()

    dump, kind = find_dump(pathlib.Path(args.wire), args.sid)
    body = parse_dump(dump)
    w = wire_cores(body)
    rows, mode = load_rows(args.db, args.sid)
    # 我们"当前实现"的假设：v1 库 → 1.x 规则，v2 库 → 2.x 规则
    ours, _ = build_cores(rows, "1x" if mode == "v1" else "2x")
    o = [{"id": c[0], "role": c[2], "ct": c[3], "text": c[6]} for c in ours]

    oset, wset = {c["id"] for c in o}, {c["id"] for c in w}
    o_only, w_only = oset - wset, wset - oset

    print(f"转储     : {dump}")
    print(f"转储种类 : {kind}")
    if "req(" in kind:
        print("           ⚠ 这是折叠后的转发体：被折掉的历史已替换成摘要，"
              "『只在我们』一类会被摘要污染，判读时只看样本内容")
    print(f"内容源   : {args.db}  (mode={mode})")
    print(f"wire 条数: {len(body.get('messages') or [])} 条消息 → {len(w)} 个 core")
    print(f"我们产物 : {len(o)} 个 core")
    print(f"对得上   : {len(oset & wset)}")
    print(f"只在 wire（我们没生成）: {len(w_only)}")
    print(f"只在我们（wire 里没有）: {len(o_only)}   ← 这一批就是 bili 报 'covered id(s) missing' 的来源")
    print()

    def report(title, ids, src, limit):
        if not ids:
            print(f"【{title}】无")
            return
        by = collections.Counter((c["role"], c["ct"]) for c in src if c["id"] in ids)
        print(f"【{title}】{len(ids)} 个，按 (角色, 内容类型) 分类：")
        for (role, ct), n in by.most_common():
            print(f"    {role:9} {ct:12} × {n}")
        for c in [x for x in src if x["id"] in ids][:limit]:
            print(f"      e.g. {c['role']}/{c['ct']}: {c['text'][:110]!r}")
        print()

    report("只在我们（wire 里没有）", o_only, o, args.limit)
    report("只在 wire（我们没生成）", w_only, w, args.limit)

    # 顺序对齐：找出第一处位置分歧
    n = min(len(o), len(w))
    for i in range(n):
        if o[i]["id"] != w[i]["id"]:
            print(f"首个位置分歧 @ {i}：")
            print(f"     我们: {o[i]['role']}/{o[i]['ct']}: {o[i]['text'][:90]!r}")
            print(f"     wire: {w[i]['role']}/{w[i]['ct']}: {w[i]['text'][:90]!r}")
            break
    else:
        print("前 %d 个 core 位置完全一致" % n)

    print("\n判读：")
    print("  · 若 '只在我们' 的样本里某个 (角色,内容类型) 占绝大多数 → 就是那类内容的序列化规则不同，把这段发我")
    print("  · 若 '只在 wire' 里主要是较新的消息 → 那只是你转换后又聊过的内容，属正常")

    if args.try_rules:
        print()
        try_rules(rows, w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
