#!/usr/bin/env python3
"""判掉 acp2bili 对账的最后残差：逐条给出源头消息的形状与"换个 id 的同文本"。
用法：cd ~/opencode/acp2bili && python3 probe_residual.py <SID> [<opencode.db>]"""
import collections, json, os, sys, pathlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bili_convert as B, bili_wire_diff as D

if len(sys.argv) < 2:
    raise SystemExit("用法：python3 probe_residual.py <SID> [<opencode.db>]")
SID = sys.argv[1]
DB = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser("~/.local/share/opencode/opencode.db")
WIRE_ROOT = os.environ.get("PROBE_WIRE_ROOT", "~/.local/state/billion-context")
wire_path, kind = D.find_dump(pathlib.Path(os.path.expanduser(WIRE_ROOT)), SID)
body = D.parse_dump(wire_path)
wcores = D.wire_cores(body)
rows, mode = B.load_rows(DB, SID)
ours, _ = B.build_cores(rows, wire="1x")
print(f"转储: {wire_path.name}\n库: {DB} (mode={mode})  wire {len(wcores)} core / 我们 {len(ours)} core\n")

def base(i):
    p = i.split("_")
    return i if len(p) <= 2 else i.rsplit("_", 1)[0]

byname = {}                              # mid -> (type, data)
for mid, typ, data in rows:
    byname[mid] = (typ, json.loads(data))
wset = collections.Counter(c["id"] for c in wcores)
oset = collections.Counter(c[0] for c in ours)

def wcount(i):
    return sum(1 for c in wcores if base(c["id"]) == base(i))

def ocount(i):
    return sum(1 for c in ours if base(c[0]) == base(i))

def src(mid):
    if mid not in byname:
        return f"     源: {mid} 不在库里"
    typ, m = byname[mid]
    if typ != "assistant":
        return f"     源: {typ} {json.dumps(m, ensure_ascii=False)[:120]}"
    parts = [(p.get("type"), len(p.get("text") or "")) for p in (m.get("content") or [])]
    out = [f"     源: provider={m.get('providerID')}/{m.get('modelID')} parts={parts}"]
    for p in (m.get("content") or []):
        if p.get("type") == "tool":
            st = p.get("state") or {}
            md = st.get("metadata") or {}
            out.append(f"        tool={p.get('tool')} status={st.get('status')} "
                       f"interrupted={md.get('interrupted')} error={json.dumps(st.get('error'), ensure_ascii=False)[:80]} "
                       f"content={json.dumps(st.get('content'), ensure_ascii=False)[:80]}")
    return "\n".join(out)

print(f"===== 我方独有（wire 里没有）{sum(1 for c in ours if wset[c[0]] == 0)} 条 =====")
for cid, mid, role, ct, tcid, tname, text in ours:
    if wset[cid]:
        continue
    print(f"  {cid}  {role}/{ct}  mid={mid}  len={len(text)}")
    print(f"     前80: {text[:80]!r}")
    print(f"     后60: {text[-60:]!r}")
    same = [(c["id"], c["role"], c["ct"]) for c in wcores if c["text"] == text]
    print(f"     wire 里同文本: {same}   |  同 base 条数: 我们={ocount(cid)} wire={wcount(cid)}")
    print(src(mid))

print(f"\n===== wire 独有（我们没生成）{sum(1 for c in wcores if oset[c['id']] == 0)} 条 =====")
for c in wcores:
    if oset[c["id"]]:
        continue
    t = c["text"]
    print(f"  {c['id']}  {c['role']}/{c['ct']}  len={len(t)}")
    print(f"     前80: {t[:80]!r}")
    print(f"     后60: {t[-60:]!r}")
    same = [(x[0], x[2], x[3], x[1]) for x in ours if x[6] == t]
    print(f"     我们里同文本: {same}  |  同 base 条数: 我们={ocount(c['id'])} wire={wcount(c['id'])}")
    if t:
        for cid, mid, role, ct, tcid, tname, ot in ours:
            if t[:50] and t[:50] in ot and ot != t:
                print(f"     ⊆ 被我们并进了 {cid} ({role}/{ct} mid={mid} len={len(ot)})")
                break

ob = collections.Counter(base(c[0]) for c in ours)
wb = collections.Counter(base(c["id"]) for c in wcores)
drift = sorted(k for k in set(ob) | set(wb) if ob.get(k, 0) != wb.get(k, 0))
print(f"\n===== 同 base 但重数不同（去重计数漂移）{len(drift)} 个 =====")
for k in drift[:20]:
    texts = [x[6][:40] for x in ours if base(x[0]) == k][:1]
    print(f"  {k}  我们={ob[k]} wire={wb[k]}   例: {texts[0]!r}")
