#!/usr/bin/env python3
"""按 bili 实际见过的编号过滤转换产物：只保留真实请求里出现过的 core id。

## 为什么需要

转换器算出的 core id 里，有一部分永远不会出现在真实请求里：多候选产物里没被宿主
采用的那种形态、库里有而宿主不再发送的消息。这些 id 留在块里不会让请求出错，但会让
块覆盖"缺一大块"。宿主真发过一次请求之后，bili 的库（index.db 的 messages 表）里就有了
它实际看到的全部编号；拿这份名单过滤产物，块里只剩真实存在的 id。

bili 升级、编号规则可能变化时，这一步也是最快的核对办法：过滤后保留的比例很低，
说明转换器算出的编号和 bili 实际看到的对不上。

## 用法

    python3 bili_wire_filter.py --product <转换产物.json> --out <目录或文件> [--sid <sid>]
        [--bili-db <index.db>] [--ts <请求编号>] [--force]

- 只读 bili 的库，只写 --out。--out 给文件就写那个文件；给目录就按转换器的布局写到
  <目录>/<protocol>/<原文件名>。不能覆盖 --product。
- 默认取该会话在 bili 库里出现过的全部编号；--ts 只取某一次请求里新出现的编号。
- bili 库里该会话一条记录都没有时直接退出：先让宿主发一次请求（请求失败也没关系，
  bili 同样会记下看到的编号），再过滤。
- 过滤后保留的编号不足一半时拒绝写出（--force 强制）：通常说明 bili 只记到了一次很小
  的请求，照这个过滤会把块清空。

装回 bili 之前用 bili_verify.py --acp ... 校验过滤后的产物。
"""

import argparse
import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bili_convert import json_safe  # noqa: E402

DEFAULT_DB = "~/.local/share/billion-context/sessions/index.db"


def wire_ids(db_path, sid, ts=None):
    """bili 库的 messages 表里，该会话出现过的 core id。只读打开。"""
    path = pathlib.Path(db_path).expanduser()
    if not path.is_file():
        raise SystemExit(f"找不到 bili 的库: {path}（用 --bili-db 指定）")
    con = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        if ts is None:
            rows = con.execute("select distinct msg_id from messages where session_id=?",
                               (sid,)).fetchall()
        else:
            rows = con.execute("select distinct msg_id from messages where session_id=? and ts=?",
                               (sid, ts)).fetchall()
    finally:
        con.close()
    return {r[0] for r in rows}


def filter_product(doc, seen):
    """就地过滤 doc['payload']。返回统计 dict。"""
    p = doc["payload"]
    st = p["state"]
    blocks = st.get("blocks") or []
    tail = p.get("messages") or []

    before = set()
    for b in blocks:
        before.update(b.get("directMessageIds") or [])
        before.update(b.get("effectiveMessageIds") or [])
    before.update(m.get("id") for m in tail if m.get("id"))

    kept = set()
    emptied = 0
    for b in blocks:
        b["directMessageIds"] = [i for i in (b.get("directMessageIds") or []) if i in seen]
        b["effectiveMessageIds"] = [i for i in (b.get("effectiveMessageIds") or []) if i in seen]
        kept.update(b["directMessageIds"])
        kept.update(b["effectiveMessageIds"])
        if not b["effectiveMessageIds"]:
            # 一个 id 都不剩的块不会再折叠任何内容；留着过时的 ref 会让校验的 A4 不过
            for k in ("startRef", "endRef"):
                b.pop(k, None)
            if b.get("active"):
                emptied += 1

    tail = [m for m in tail if m.get("id") in seen]
    kept.update(m["id"] for m in tail)

    # ref 账本按保留下来的编号重新连续编号
    old_by_raw = (st.get("messageRefs") or {}).get("byRaw") or {}
    new_by_raw, new_by_ref = {}, {}
    for raw in old_by_raw:
        if raw not in kept:
            continue
        ref = f"m{len(new_by_raw) + 1:05d}"
        new_by_raw[raw] = ref
        new_by_ref[ref] = raw
    for b in blocks:
        ids = b["effectiveMessageIds"]
        if ids and ids[0] in new_by_raw and ids[-1] in new_by_raw:
            b["startRef"] = new_by_raw[ids[0]]
            b["endRef"] = new_by_raw[ids[-1]]
        else:
            for k in ("startRef", "endRef"):
                b.pop(k, None)

    st["messageRefs"] = {"byRaw": new_by_raw, "byRef": new_by_ref}
    st["lastPassIds"] = [m["id"] for m in tail]
    p["messages"] = tail

    return {
        "blocks": len(blocks),
        "active": sum(1 for b in blocks if b.get("active")),
        "emptied_active_blocks": emptied,
        "ids_before": len(before),
        "kept": len(kept),
        "tail": len(tail),
        "refs": len(new_by_raw),
        "refs_dropped": len(old_by_raw) - len(new_by_raw),
    }


def main():
    ap = argparse.ArgumentParser(description="按 bili 实际见过的编号过滤转换产物")
    ap.add_argument("--product", required=True, help="转换产物（bili_convert.py 的输出）")
    ap.add_argument("--out", required=True,
                    help="过滤后的产物：文件路径，或目录（写到 <目录>/<protocol>/<原文件名>）")
    ap.add_argument("--sid", default="", help="会话 id；缺省取产物里的 id")
    ap.add_argument("--bili-db", default=DEFAULT_DB, help="bili 的库（默认 " + DEFAULT_DB + "）")
    ap.add_argument("--ts", type=int, default=None, help="只取这一次请求里新出现的编号；缺省取全部")
    ap.add_argument("--force", action="store_true", help="保留比例不足一半时照样写出")
    args = ap.parse_args()

    src = pathlib.Path(args.product)
    try:
        doc = json.loads(src.read_text(encoding="utf-8"))
    except UnicodeDecodeError as e:
        print(f"✗ 产物不是合法的 UTF-8（{e}）：用当前版本的 bili_convert.py 重新转换")
        return 2

    dst = pathlib.Path(args.out)
    if dst.suffix != ".json":
        # 给的是目录（可以还不存在）：与转换器一致，落在 <目录>/<protocol>/<原文件名>。
        # bili 只认规范名或扁平名，而且规范名必须在 protocol 子目录下。
        proto = ((doc.get("payload") or {}).get("meta") or {}).get("protocol") or "openai"
        dst = dst / proto / src.name
    if src.resolve() == dst.resolve():
        print("✗ --out 不能与 --product 是同一个文件：过滤前后的产物都要留着对照")
        return 2

    sid = args.sid or (doc.get("payload") or {}).get("id") or doc.get("id")
    if not sid:
        print("✗ 产物里没有会话 id，请用 --sid 指定")
        return 2

    seen = wire_ids(args.bili_db, sid, args.ts)
    if not seen:
        scope = f"请求 {args.ts}" if args.ts is not None else "任何请求"
        print(f"✗ bili 的库里没有会话 {sid} 在{scope}里的编号。"
              f"先让宿主对这个会话发一次请求（失败也没关系，bili 会记下看到的编号），再过滤")
        return 1

    stats = filter_product(doc, seen)
    total = stats["ids_before"]
    ratio = stats["kept"] / total if total else 1.0
    print(f"会话        : {sid}")
    print(f"bili 见过   : {len(seen)} 个编号（{'请求 ' + str(args.ts) if args.ts is not None else '全部请求'}）")
    print(f"块          : {stats['blocks']} 个（active {stats['active']}，过滤后变空的 active 块 {stats['emptied_active_blocks']}）")
    print(f"编号        : 产物原有 {total}，保留 {stats['kept']}（{ratio:.1%}），去掉 {total - stats['kept']}")
    print(f"尾段        : {stats['tail']} 条")
    print(f"ref 账本    : {stats['refs']} 条（去掉 {stats['refs_dropped']}）")

    if ratio < 0.5 and not args.force:
        print(f"✗ 保留比例只有 {ratio:.1%}，没有写出。常见原因：bili 只记到了一次很小的请求"
              f"（去掉 --ts 用全部编号再试），或者转换器的编号规则和当前 bili 不一致"
              f"（看 bili_convert.py 结尾的 bili 版本一行）。确认无误可加 --force")
        return 1

    label = args.ts if args.ts is not None else "all"
    (doc["payload"].setdefault("metadata", {}))["wireFilter"] = (
        f"ts={label}; kept={stats['kept']}; dropped_blocks={stats['emptied_active_blocks']}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(json_safe(doc), ensure_ascii=False), encoding="utf-8")
    print(f"输出        : {dst}")
    if stats["emptied_active_blocks"]:
        print(f"⚠ 有 {stats['emptied_active_blocks']} 个 active 块过滤后一个编号都不剩，"
              f"这部分内容不会再被折叠")
    print("下一步      : python3 bili_verify.py --acp <acp 状态.json> --bili "
          f"{dst} --db <opencode.db> --sid {sid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
