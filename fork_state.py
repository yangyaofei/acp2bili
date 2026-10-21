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


def main():
    ap = argparse.ArgumentParser(description="把父会话的 bili 状态复制给 fork 会话")
    ap.add_argument("--parent", required=True, help="父会话 id（ses_...）")
    ap.add_argument("--child", required=True, help="fork 出来的会话 id（ses_...）")
    ap.add_argument("--sessions-dir", default=default_sessions_dir(),
                    help="bili 会话目录（默认 ~/.local/share/billion-context/sessions）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
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
