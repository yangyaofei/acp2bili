#!/usr/bin/env python3
"""批量校验 acp2bili 的转换产物（可选：通过的直接装进 bili 的 sessions 目录）。

对每个产物调一次 `bili_verify.py`（A 组结构检查 + B 组内容检查），把结果汇成一张表；
只有全部 PASS 的才会在 --install 时被装进 bili 目录。

用法：
  python3 bili_verify_all.py                       # 校验 <仓库>/runtime/out 下全部产物
  python3 bili_verify_all.py --limit 5             # 只校验前 5 个（冒烟）
  python3 bili_verify_all.py --install             # 校验通过的直接安装
  python3 bili_verify_all.py --report runtime/reports/verify.txt
  python3 bili_verify_all.py --acp <acp目录> --db <opencode.db> --out <产物目录> --wire 1x
                                                  # 手工指定（不在默认位置时）
  # 若转换时用了自定义 provider 规则，这里要带同样的 --fold-providers/--rc-providers

退出码：0 = 全部通过 / 1 = 有失败 / 2 = 没有找到产物。
"""
import argparse, json, os, pathlib, shutil, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent
RUNTIME = pathlib.Path(os.environ.get("ACP2BILI_RUNTIME") or (HERE / "runtime"))
DEFAULT_OUT = str(RUNTIME / "out")
DEFAULT_REPORT_DIR = RUNTIME / "reports"
DEFAULT_ACPDIR = "~/.local/share/opencode/storage/plugin/acp"
DEFAULT_DB = "~/.local/share/opencode/opencode.db"
BILI_SESSIONS = "~/.local/share/billion-context/sessions"


def find_out(root: pathlib.Path):
    """产物：<out>/<protocol>/<host>_<hash>.json（也接受扁平放置）。"""
    out = []
    for p in sorted(root.rglob("*.json")):
        if p.name.endswith(".bak") or ".content-store" in p.name or p.name.startswith("."):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(d, dict) and "payload" in d and "id" in d:
            out.append((p, d))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT, help="转换产物目录（默认 <仓库>/runtime/out）")
    # --acp 与 --acp-dir 等价：转换器叫 --acp-dir，这里历史上叫 --acp，两个都收
    ap.add_argument("--acp", "--acp-dir", dest="acp", default=DEFAULT_ACPDIR,
                    help="acp 状态目录（--acp 与 --acp-dir 等价）")
    ap.add_argument("--db", default=DEFAULT_DB, help="opencode 库（B 组内容检查要用）")
    ap.add_argument("--wire", choices=["", "1x", "2x"], default="",
                    help="wire 规则（默认按库结构自动：v1→1x / v2→2x）")
    ap.add_argument("--fold-providers", default="", help="转换时若自定义过 provider 规则，需两边同步改常量（见下方提示）")
    ap.add_argument("--rc-providers", default="", help="同上")
    ap.add_argument("--limit", type=int, default=0, help="只校验前 N 个")
    ap.add_argument("--install", action="store_true",
                    help="把校验通过（全 PASS）的产物装进 ~/.local/share/billion-context/sessions/")
    ap.add_argument("--report", default="", help="详细报告路径（默认 <仓库>/runtime/reports/verify-<时间>.txt；传空串可关闭）")
    ap.add_argument("--prune", action="store_true",
                    help="删除同会话的陈旧产物文件（去重时保留下来的那个除外）")
    args = ap.parse_args()

    out_root = pathlib.Path(os.path.expanduser(args.out))
    acpdir = pathlib.Path(os.path.expanduser(args.acp))
    db = os.path.expanduser(args.db)
    items = find_out(out_root)

    # 同一会话可能有两个产物文件：转换参数变化会改文件名（如下载 models-dev.json 后
    # 扁平名 <hash>.json 变规范名 <host>_<hash>.json），旧文件留在目录里就成了
    # "陈旧产物"，校验必 FAIL 且误导排查。按记录 id 去重：保留规范名（其次最新 mtime）。
    import re
    by_sid = {}
    for path, d in items:
        by_sid.setdefault(d.get("id") or "", []).append(path)
    deduped, dropped = [], []
    for sid, paths in by_sid.items():
        if len(paths) == 1:
            deduped.append(paths[0])
            continue
        def rank(p):
            canon = 1 if re.match(r"^[A-Za-z0-9.\-]+_[0-9a-f]{24}$", p.stem) else 0
            return (canon, p.stat().st_mtime)
        keep = max(paths, key=rank)
        deduped.append(keep)
        for p in paths:
            if p != keep:
                dropped.append((p, keep))
    if dropped:
        print(f"[去重] 发现 {len(dropped)} 个同会话陈旧产物（保留规范名/最新者，其余不校验、不安装）：")
        for p, keep in dropped:
            print(f"  弃 {p.name}  ←→  用 {keep.name}")
        if args.prune:
            for p, _ in dropped:
                p.unlink()
            print(f"[去重] 已删除 {len(dropped)} 个陈旧产物文件")
        else:
            print("[去重] 加 --prune 可直接删除这些文件")
    item_map = {p: d for p, d in items}
    items = [(p, item_map[p]) for p in deduped]

    if args.limit:
        items = items[:args.limit]
    if not items:
        legacy = os.path.expanduser("~/acp2bili-out")
        if os.path.isdir(legacy):
            print(f"（提示：{os.path.expanduser(DEFAULT_OUT)} 下没有产物，但旧产物目录 {legacy} 存在；"
                  f"要校验旧的加 --out {legacy}）")
        print(f"{out_root} 下没有找到转换产物。先跑 bili_convert.py")
        return 2

    print(f"产物目录 : {out_root}   共 {len(items)} 个")
    print(f"acp 目录 : {acpdir}")
    print(f"内容源库 : {db}   {'(存在)' if os.path.exists(db) else '(不存在！B 组会跳过)'}")
    print(f"安装     : {'开' if args.install else '关'}")
    print("-" * 78)

    ok, bad, skipped, installed, lines_all = [], [], [], 0, []
    t0 = time.time()
    for i, (path, d) in enumerate(items, 1):
        sid = d.get("id") or ""
        acp_file = acpdir / f"{sid}.json"
        if not acp_file.exists():
            skipped.append((sid, str(path)))
            print(f"[{i:>3}/{len(items)}] 跳过 {sid[:34]:<34} 找不到 acp 状态 {acp_file.name}")
            continue
        cmd = [sys.executable, str(HERE / "bili_verify.py"),
               "--acp", str(acp_file), "--bili", str(path), "--sid", sid, "--db", db]
        if args.wire:
            cmd += ["--wire", args.wire]
        if args.fold_providers or args.rc_providers:
            pass  # bili_verify.py 不接受这两个参数；自定义 provider 规则要两边同步改常量
        r = subprocess.run(cmd, capture_output=True, text=True)
        out = r.stdout + r.stderr
        lines_all.append(f"===== {sid} ({path}) =====\n{out}\n")
        passed = ("校验结论: 全部通过" in out)
        blocks = (d.get("payload", {}).get("state", {}) or {}).get("blocks") or []
        nblocks = len(blocks) if isinstance(blocks, list) else len(blocks)
        if passed:
            ok.append(sid)
            print(f"[{i:>3}/{len(items)}] PASS {sid[:34]:<34} blocks={nblocks}")
            if args.install:
                proto = ((d.get("payload", {}).get("meta") or {}).get("protocol")) or "openai"
                dst_dir = pathlib.Path(os.path.expanduser(BILI_SESSIONS)) / proto
                dst_dir.mkdir(parents=True, exist_ok=True)
                dst = dst_dir / path.name
                if dst.exists():
                    shutil.copy2(dst, dst.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
                shutil.copy2(path, dst)
                installed += 1
                print(f"         ↳ 已安装 {dst}")
        else:
            bad.append((sid, str(path)))
            print(f"[{i:>3}/{len(items)}] FAIL {sid[:34]:<34} blocks={nblocks}")
            for ln in out.splitlines():
                if "[FAIL]" in ln or "校验结论" in ln:
                    print(f"         {ln.strip()}")

    print("-" * 78)
    print(f"合计 {len(items)} 个：PASS {len(ok)} / FAIL {len(bad)} / 跳过 {len(skipped)}"
          f"   用时 {time.time() - t0:.1f}s")
    if installed:
        print(f"已安装 {installed} 个到 {os.path.expanduser(BILI_SESSIONS)}/")
    if bad:
        print("\n失败的会话（详细见 --report）：")
        for sid, p in bad:
            print(f"  {sid}  {p}")
    if skipped:
        print("\n跳过的会话（没有 acp 状态，可能是没压过的会话）：")
        for sid, p in skipped[:20]:
            print(f"  {sid}")
        if len(skipped) > 20:
            print(f"  … 另有 {len(skipped) - 20} 个")
    report_path = args.report or str(DEFAULT_REPORT_DIR / f"verify-{time.strftime('%Y%m%d-%H%M%S')}.txt")
    if report_path:
        rp = pathlib.Path(os.path.expanduser(report_path))
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text("\n".join(lines_all), encoding="utf-8")
        print(f"\n详细报告：{rp}")
    flat = sum(1 for p, _ in items if re.match(r"^[0-9a-f]{24}\.json$", p.name))
    print("\n=== 结论 ===")
    print(f"校验        : {len(ok)} 通过 / {len(bad)} 失败 / {len(skipped)} 跳过")
    if installed:
        print(f"安装        : {installed} 个 → {os.path.expanduser(BILI_SESSIONS)}/")
    if flat:
        print(f"文件名      : {flat} 个是 flat 名 <hash>.json（上游 origin 没推断出）")
        print(f"  影响      : bili 接受，功能不受影响；只是文件名少了 host 前缀")
    if skipped:
        print(f"跳过        : {len(skipped)} 个没有 acp 状态（没有压缩块或未隔离），未参与校验")
    print("待确认      : `bili export | grep <sid>` 能看到这些会话与 blocks=<块数>，才算真正生效")
    print("              （bili export 直接读盘，不必先跑会话；若看不到，先确认宿主已重启）")
    problems = []
    if bad:
        problems.append(("校验失败", [x[0].name for x in bad]))
    if skipped:
        problems.append(("跳过（没有 acp 状态）", [x for x in skipped]))
    if flat:
        problems.append(("flat 名（上游 origin 未推断出）", [p.name for p, _ in items
                                                          if re.match(r"^[0-9a-f]{24}\.json$", p.name)]))
    if problems:
        print("\n=== 本次运行发现的问题 ===")
        for kind, names in problems:
            print(f"[{kind}] {len(names)} 个")
            for n in names[:5]:
                print(f"    {n}")
            if len(names) > 5:
                print(f"    …… 其余 {len(names) - 5} 个同类")

    if bad:
        verdict = f"有失败：{len(bad)} 个产物未通过校验 ✗"
    elif flat or skipped:
        verdict = f"通过（有降级）：{len(ok)} 个通过并可用；降级项见上"
    else:
        verdict = f"全部正常 ✓（{len(ok)} 个通过）"
    print("\n=== 批量结论: " + verdict + " ===")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
