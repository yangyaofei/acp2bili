#!/usr/bin/env python3
"""把父会话的 bili 压缩状态复制给一个 fork 出来的会话。

背景：bili 的会话状态按「会话 id 的哈希」命名。fork 出来的会话是一个新 id，
拿不到父会话的状态，于是它从零开始：没有已压缩的块，整段历史原样发给模型，
大会话会直接超过窗口被拒绝（preflight fail-fast），表现就是「fork 之后不能用」。

为什么可以直接复用：块和编号都是**按消息内容**算的（内容哈希），fork 的前缀
内容与父会话逐字相同，所以父会话的块、编号、覆盖关系对 fork 同样成立。

用法：
    python3 fork_state.py --parent ses_xxx --child ses_yyy
    python3 fork_state.py --parent ses_xxx --child ses_yyy --dry-run
"""
import argparse
import glob
import hashlib
import json
import os
import pathlib
import time


def default_sessions_dir():
    return os.path.expanduser("~/.local/share/billion-context/sessions")


def find_state(sessions_dir, sid):
    """按 sha256(sid)[:24] 找该会话的状态文件（规范名/扁平名都能匹配）。"""
    h = hashlib.sha256(sid.encode()).hexdigest()[:24]
    hits = sorted(glob.glob(os.path.join(sessions_dir, "*", f"*{h}.json")))
    hits = [h for h in hits if not h.endswith(".content-store.json")]
    return hits[0] if hits else None


def state_path_for(sessions_dir, sid, protocol="openai", host_label=None):
    d = hashlib.sha256(sid.encode()).hexdigest()[:24]
    name = f"{host_label}_{d}.json" if host_label else f"{d}.json"
    return pathlib.Path(sessions_dir) / protocol / name


def trim_state(doc, present_ordered):
    """只保留子会话里真实存在的覆盖：前缀分叉时，父块里超出分叉点的部分要裁掉。

    present_ordered: 子会话按消息顺序算出来的 core id 列表
                     （用 bili_convert.build_candidates 得到，顺序即消息顺序）。
    返回 (doc, kept, dropped)。
    """
    state = doc["payload"]["state"]
    blocks = state.get("blocks") or []
    present = set(present_ordered)
    kept, dropped = [], []
    for b in blocks:
        direct = [i for i in (b.get("directMessageIds") or []) if i in present]
        eff0 = [i for i in (b.get("effectiveMessageIds") or []) if i in present]
        if not direct and not eff0:
            dropped.append(b)
            continue
        b["directMessageIds"] = direct
        b["effectiveMessageIds"] = eff0 or direct
        kept.append(b)
    state["blocks"] = kept

    # 编号按子会话顺序重排（父会话的编号可能包含子会话里不存在的消息）
    covered = set()
    for b in kept:
        covered.update(b["directMessageIds"])
        covered.update(b["effectiveMessageIds"])
    by_raw = {}
    ordered = [i for i in present_ordered if i in covered]
    by_ref = {}
    for n, cid in enumerate(ordered, 1):
        ref = f"m{n:05d}"
        by_raw[cid] = ref
        by_ref[ref] = cid
    state["messageRefs"] = {"byRaw": by_raw, "byRef": by_ref}
    # 块的起止编号跟着重算
    for b in kept:
        ids = b["directMessageIds"] or b["effectiveMessageIds"]
        if ids:
            b["startRef"] = by_raw.get(ids[0], b.get("startRef", ""))
            b["endRef"] = by_raw.get(ids[-1], b.get("endRef", ""))
    state["nextBlockId"] = len(kept) + 1
    return doc, len(kept), len(dropped)


def main():
    ap = argparse.ArgumentParser(description="把父会话的 bili 状态复制给 fork 会话")
    ap.add_argument("--parent", required=True, help="父会话 id（ses_...）")
    ap.add_argument("--child", required=True, help="fork 出来的会话 id（ses_...）")
    ap.add_argument("--sessions-dir", default=default_sessions_dir(),
                    help="bili 会话目录（默认 ~/.local/share/billion-context/sessions）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--trim-db", default="",
                    help="按子会话的库裁剪：只保留子会话里真实存在的覆盖（前缀分叉必须加）")
    args = ap.parse_args()

    src = find_state(args.sessions_dir, args.parent)
    if not src:
        raise SystemExit(f"找不到父会话 {args.parent} 的状态文件（目录 {args.sessions_dir}）")
    doc = json.loads(pathlib.Path(src).read_text())
    state = (doc.get("payload") or {}).get("state") or {}
    blocks = state.get("blocks") or []
    refs = (state.get("messageRefs") or {}).get("byRaw") or {}

    parent_proto = (doc.get("payload") or {}).get("meta", {}).get("protocol") or "openai"
    host_label = None
    basename = os.path.basename(src)
    if "_" in basename:
        host_label = basename.rsplit("_", 1)[0]

    target = state_path_for(args.sessions_dir, args.child, parent_proto, host_label)
    print(f"父会话状态 : {src}")
    print(f"             blocks={len(blocks)} refs={len(refs)} protocol={parent_proto}")
    print(f"目标文件   : {target}")

    existing = find_state(args.sessions_dir, args.child)
    if existing:
        print(f"注意       : 子会话已有状态文件 {existing}，将被覆盖（原文件另存 .bak）")

    if args.trim_db:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import bili_convert as bc
        rows, mode = bc.load_rows(args.trim_db, args.child)
        if not rows:
            raise SystemExit(f"库里没有子会话 {args.child} 的消息，无法裁剪")
        cores, _ = bc.build_candidates(rows)
        present_ordered = [c[0] for c in cores]
        present = set(present_ordered)
        existing_ids = set((state.get("messageRefs") or {}).get("byRaw") or {})
        if present >= existing_ids:
            print(f"裁剪       : 子会话 core={len(present)} 已覆盖父状态全部编号，无需裁剪")
        else:
            doc, kept, dropped = trim_state(doc, present_ordered)
            print(f"裁剪       : 子会话 core={len(present)} / 父状态 {len(existing_ids)}；"
                  f"保留块 {kept} 个，丢弃 {dropped} 个")

    if args.dry_run:
        print("dry-run    : 未写盘")
        return

    doc["id"] = args.child
    doc["savedAt"] = int(time.time() * 1000)
    payload = doc.setdefault("payload", {})
    payload["id"] = args.child
    meta = payload.setdefault("meta", {})
    meta["label"] = args.child
    meta["title"] = meta.get("title") or ""

    if existing:
        pathlib.Path(existing).with_suffix(".json.bak").write_text(pathlib.Path(existing).read_text())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, ensure_ascii=False))
    print("已写入     : 完成（重启宿主后生效）")


if __name__ == "__main__":
    main()
