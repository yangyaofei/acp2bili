#!/usr/bin/env python3
"""bili_convert.py 产出物的**离线校验器**（不需要 opencode、不需要插件、不碰网络）

只用两/三样东西：
    1) 老会话的 acp 状态 JSON        （转换的来源）
    2) 转换生成的 bili 状态 JSON      （被检对象）
    3) 内容源 SQLite 库（可选）       （能把 core id 重算一遍，做最强校验）

## 用法

    python3 bili_verify.py --acp <acp.json> --bili <bili状态.json> \
        [--db <opencode.db>] [--sid ses_xxx] [--out 报告.md]

## 检什么

A 组（只靠两份 JSON，永远可跑）
  A1 信封与元数据            version / id / payload / meta
  A2 文件名规则               <host>_<sha256(sid)[:24]>.json（给了 --bili 路径就查）
  A3 ref 账本                  byRaw/byRef 双射、ref 从 m00001 起连续无洞
  A4 块结构                    blockId 唯一、tier 合法、directBlockIds 指向存在、
                              startRef/endRef 在账本内且有序、direct ⊆ effective
  A5 折叠一致性                tier-2 active 的 effective == 子块 effective ∪ 自身 direct；
                              active 块之间 effective 若重叠只报 WARN（bili 按并集处理，不是错误）
  A6 与 acp 的块级对应          块数/块号/tier/active 一一对应

B 组（需要 --db：把内容重算一遍，这是"产物对不对"的硬校验）
  B1 core 重算                 按身份规则重算全量 core，与账本**逐位**比对（含顺序）
  B2 覆盖集合逐块对应            acp 块直接覆盖的消息集合 == bili 块 direct 反推的消息集合
  B3 尾段一致                   payload.messages == 未被 active 块覆盖的 core，顺序一致
  B4 汇总一致                   cores / blocks / refs / tail 四个数字两侧对得上

退出码：0 = 全通过；1 = 有失败项（打印每一项的证据）；2 = 编号自检失败（结果不可信）

## 与转换器同源的局限

B 组是用 bili_convert.py 的算法把 core 重算一遍再比。算法错了，两边一起错，照样 PASS
（bili 0.1.202 改编号规则时就是这样漏过去的）。所以开头先跑 selftest_ids()：拿 bili 自己
算出的编号对照算法本身。它只能证明"算法与 bili 0.1.216 一致"；bili 升级后规则是否又变，
要用 bili_wire_filter.py 按真实请求核对。
"""
import argparse
import collections
import hashlib
import json
import pathlib
import re
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bili_convert import (build_cores, build_candidates, load_rows, cores_from_dump,  # noqa: E402
                          selftest_ids, bili_version_status, BILI_ID_RULE_VERSIONS)

REF_RE = re.compile(r"^m\d{5}$")
BLOCK_RE = re.compile(r"^b[1-9]\d*$")
FAILS = []
WARNS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILS.append(f"{name}: {detail}")
    return ok


def warn(msg):
    print(f"  [WARN] {msg}")
    WARNS.append(msg)


def main():
    ap = argparse.ArgumentParser(description="bili 转换产物离线校验")
    ap.add_argument("--acp", required=True)
    ap.add_argument("--bili", required=True)
    ap.add_argument("--db", default="", help="内容源库；给了才能跑 B 组")
    ap.add_argument("--sid", default="", help="会话 id；缺省从 bili 状态里读")
    ap.add_argument("--out", default="", help="把报告同时写到该文件")
    ap.add_argument("--wire", choices=["", "1x", "2x"], default="",
                    help="assistant 序列化规则（默认按库表结构自动选：v1→1x，v2→2x）")
    ap.add_argument("--from-dump", dest="dump", default="",
                    help="bili 的 INCOMING 转储：B 组改用测量法重算（与 --from-dump 转换的产物配套）")
    args = ap.parse_args()

    # 先确认编号算法本身没走样。B 组是用 bili_convert.py 的算法重算一遍再比，转换器和校验器
    # 同源：算法错了两边一起错，照样 PASS（bili 0.1.202 改编号规则时就是这样漏过去的）。
    bad = selftest_ids()
    check("编号自检（与 bili 0.1.216 自己算出的编号逐字节一致）", not bad,
          f"{len(bad)} 条不一致，例如 {bad[0]}" if bad else "")
    if bad:
        print("\n=== 校验结论: 编号自检失败，下面的结果不可信，不做 ✗ ===")
        return 2

    acp = json.load(open(args.acp))
    bili = json.load(open(args.bili))
    sid = args.sid or bili.get("id") or ""
    payload = bili.get("payload", {})
    state = payload.get("state", {})
    blocks = state.get("blocks", [])
    refs = state.get("messageRefs", {})
    by_raw, by_ref = refs.get("byRaw", {}), refs.get("byRef", {})

    print(f"会话: {sid}")
    print(f"acp 状态: {args.acp}")
    print(f"bili 状态: {args.bili}")
    print(f"内容源: {args.db or '(未提供，跳过 B 组)'}")
    print()

    # ---------------- A1 ----------------
    print("A. 结构性检查（只靠两份 JSON）")
    meta = payload.get("meta", {})
    a1 = (bili.get("version") == 3 and bili.get("id") == sid
          and isinstance(payload.get("messages"), list)
          and meta.get("protocol")
          and isinstance(meta.get("upstreamOrigin"), str)
          and meta.get("label") == sid)
    check("A1 信封与元数据 (version=3 / id / payload.messages / meta.protocol+label；upstreamOrigin 允许空)",
          bool(a1), "" if a1 else f"keys={list(bili)} meta={payload.get('meta')}")
    if a1 and not meta.get("upstreamOrigin"):
        # 上游推不出时转换器写扁平名（<hash>.json），meta.upstreamOrigin 只能是空串。
        # 内核按记录里的 id 建索引、host 只是给人看的命名空间，所以这是合法形态，
        # 不当失败；但值得提示一句，方便事后补 provider 映射。
        print("         [INFO] meta.upstreamOrigin 为空（扁平名形态）：该会话的 provider 未登记且 models.dev 查不到。")

    # ---------------- A2 ----------------
    # 按 bili 的真实规则：文件名必须是「规范名」(<host>_<hash>.json，host 取自文件内
    # meta.upstreamOrigin 的 hostname) 或「扁平名」(<hash>.json)；其它名字会被
    # loadAll/loadSync 当成无效文件忽略。规范名要求落在对应的 protocol 子目录下，
    # 扁平名不挑目录（内核是递归扫描）。
    up = payload.get("meta", {}).get("upstreamOrigin", "")
    host = urlparse(up).hostname or up.split("//")[-1].split("/")[0]
    digest = hashlib.sha256(sid.encode()).hexdigest()[:24]
    canonical, flat = f"{host}_{digest}.json", f"{digest}.json"
    actual = pathlib.Path(args.bili).name
    protocol_dir = pathlib.Path(args.bili).parent.name
    if actual == canonical:
        a2 = protocol_dir == payload.get("meta", {}).get("protocol")
        form = f"规范名（host={host}）"
    elif actual == flat:
        a2 = True
        form = "扁平名（不含 host，bili 明确接受）"
    else:
        a2 = False
        form = "非法命名"
    check("A2 文件名符合 bili 可识别规则（规范名 或 扁平名）", a2,
          f"{form}：{protocol_dir}/{actual}" + ("" if a2 else f"  期望 {canonical} 或 {flat}"))

    # ---------------- A3 ----------------
    a3 = True
    why = []
    if set(by_raw.values()) != set(by_ref.keys()):
        a3 = False; why.append("byRaw 值集合 ≠ byRef 键集合")
    if len(by_raw) != len(by_ref):
        a3 = False; why.append(f"数量不等 byRaw={len(by_raw)} byRef={len(by_ref)}")
    ref_list = sorted(by_ref, key=lambda r: int(r[1:]) if REF_RE.match(r) else 10**9)
    bad = [r for r in ref_list if not REF_RE.match(r)]
    if bad:
        a3 = False; why.append(f"非法 ref 格式 {bad[:3]}")
    if ref_list and ref_list != [f"m{i:05d}" for i in range(1, len(ref_list) + 1)]:
        a3 = False; why.append("ref 不连续或未从 m00001 开始")
    if len(set(by_raw)) != len(by_raw):
        a3 = False; why.append("byRaw 有重复键（不可能，字典）")
    check("A3 ref 账本双射 + 连续 (m00001..)", a3, "；".join(why))

    # ---------------- A4 ----------------
    a4 = True; why = []
    ids = [b.get("blockId") for b in blocks]
    if ids != [f"b{i}" for i in range(1, len(blocks) + 1)]:
        a4 = False; why.append("blockId 不连续/不唯一")
    for b in blocks:
        if (b.get("tier") or 1) not in (1, 2, 3):
            a4 = False; why.append(f"{b.get('blockId')} tier 非法")
        for t in b.get("directBlockIds", []):
            if not BLOCK_RE.match(t) or t not in set(ids):
                a4 = False; why.append(f"{b.get('blockId')} directBlockIds 指向不存在 {t}")
        d, e = set(b.get("directMessageIds", [])), set(b.get("effectiveMessageIds", []))
        if not d <= e:
            a4 = False; why.append(f"{b.get('blockId')} direct ⊄ effective")
        for cid in e | d:
            if cid not in by_raw:
                a4 = False; why.append(f"{b.get('blockId')} 引用了账本外的 id {cid}")
        for k in ("startRef", "endRef"):
            v = b.get(k)
            if v and REF_RE.match(v) and v not in by_ref:
                a4 = False; why.append(f"{b.get('blockId')} {k}={v} 不在账本内")
        if b.get("startRef", "").startswith("m") and b.get("endRef", "").startswith("m"):
            try:
                if int(b["startRef"][1:]) > int(b["endRef"][1:]):
                    a4 = False; why.append(f"{b.get('blockId')} startRef>endRef")
            except ValueError:
                a4 = False; why.append(f"{b.get('blockId')} ref 非数字")
        if not b.get("summary"):
            warn(f"{b.get('blockId')} summary 为空")
    check("A4 块结构（id/tier/directBlockIds/ref/direct⊆effective）", a4, "；".join(why[:4]))

    # ---------------- A5 ----------------
    a5 = True; why = []
    act = [b for b in blocks if b.get("active")]
    seen = {}
    overlaps = []
    for b in act:
        for cid in set(b.get("effectiveMessageIds", [])):
            if cid in seen:
                overlaps.append(f"{cid} ∈ {seen[cid]} ∩ {b['blockId']}")
            else:
                seen[cid] = b["blockId"]
    if overlaps:
        # bili 的「被覆盖消息」是按并集算的（kernel: coveredMessageIds / collectVisible
        # 都是 Set 累加），active 块之间重叠不会让它漏内容或重复隐藏；acp 本身就会产生
        # 重叠覆盖（实测 active 块的 direct 互不重叠，但 effective 会因「块消费了早先的块」
        # 而与更早的 active 块重叠）。因此这里只报 WARN。
        warn(f"active 块的 effective 有重叠（bili 按并集处理，非错误）：{len(overlaps)} 个 core，"
             f"例如 {overlaps[0]}")
    by_id = {b["blockId"]: b for b in blocks}
    for b in act:
        if b.get("tier", 1) >= 2 and b.get("directBlockIds"):
            union = set(b.get("directMessageIds", []))
            for t in b["directBlockIds"]:
                union |= set(by_id[t].get("effectiveMessageIds", []))
            if union != set(b.get("effectiveMessageIds", [])):
                a5 = False
                why.append(f"{b['blockId']} effective ≠ 子块 effective ∪ 自身 direct"
                           f"（差 {len(union ^ set(b.get('effectiveMessageIds', [])))} 个）")
    check("A5 折叠一致性（tier2 effective = 子树并集；active 重叠仅 WARN）", a5, "；".join(why[:3]))

    # ---------------- A6 ----------------
    ablocks = acp.get("prune", {}).get("messages", {}).get("blocksById", {})
    a6 = True; why = []
    if len(ablocks) != len(blocks):
        a6 = False; why.append(f"块数不同 acp={len(ablocks)} bili={len(blocks)}")
    else:
        for key in sorted(ablocks, key=lambda k: int(k)):
            a, b = ablocks[key], by_id.get(f"b{key}")
            if b is None:
                a6 = False; why.append(f"b{key} 缺失"); continue
            if (a.get("tier") or 1) != (b.get("tier") or 1):
                a6 = False; why.append(f"b{key} tier {a.get('tier')}≠{b.get('tier')}")
            if bool(a.get("active")) != bool(b.get("active")):
                a6 = False; why.append(f"b{key} active 不一致")
    check("A6 与 acp 的块级一一对应（数量/tier/active）", a6, "；".join(why[:4]))

    # ---------------- B 组 ----------------
    if args.db:
        print("\nB. 内容级检查（用内容源把 core 重算一遍）")
        rows, mode = load_rows(args.db, sid)
        if mode == "v1" and not args.dump and (payload.get("metadata") or {}).get("conversion") != "candidates-v1":
            print("  v1 模式：assistant 的 wire 形态**逐条按 provider/model 判定**（fold=reasoning 并进 content；"
                  "rc=reasoning 走 reasoning_content；表见 bili_convert.py 的 MEASURED_FORMS / classify_form）")
        wire = args.wire or ("1x" if mode == "v1" else "2x")
        conv_method = (payload.get("metadata") or {}).get("conversion", "predict")
        regime_log = {}
        if args.dump:
            print(f"  (测量法：转储 {args.dump}；--wire 规则未参与)")
            cores, cores_of_msg, dstats = cores_from_dump(args.dump, rows)
            for k, v in sorted(dstats.items()):
                if v:
                    print(f"    锚定统计 {k}: {v}")
        elif conv_method == "candidates-v1":
            print("  (多候选指纹：fold / 1x-rc / 2x-rc + 错误工具结果双包裹，全形态重算)")
            cores, cores_of_msg = build_candidates(rows)
        else:
            print(f"  (wire 规则: {wire})")
            cores, cores_of_msg = build_cores(rows, wire, regime_log=regime_log)
        if not args.dump and conv_method != "candidates-v1":
            for prov, c in sorted(regime_log.items(), key=lambda kv: -(kv[1]["fold"] + kv[1]["rc"])):
                print(f"    {prov:<44} fold={c['fold']:<5} rc={c['rc']:<5}")
            from bili_convert import form_contradictions
            for prov, model, got, want in form_contradictions():
                print(f"    ⚠ {prov}/{model}: 实测 {got} / catalog {want}（按实测走；catalog 仅填空）")
        core_to_msg = {c[0]: c[1] for c in cores}
        ordered = [c[0] for c in cores]
        ref_in_order = [by_ref[r] for r in sorted(by_ref, key=lambda r: int(r[1:]))]

        # 经 bili_wire_filter.py 过滤的产物只含 bili 实际见过的编号：账本是重算结果的有序子集，
        # 块覆盖、尾段也相应是子集。仍然要求文件里的每个编号都重算得出来、顺序不变。
        filtered = bool((payload.get("metadata") or {}).get("wireFilter"))
        if filtered:
            print(f"  (按 bili 实际见过的编号过滤过：{payload['metadata']['wireFilter']}；B 组按有序子集比对)")

        if filtered:
            place = {cid: i for i, cid in enumerate(ordered)}
            missing = [c for c in ref_in_order if c not in place]
            places = [place[c] for c in ref_in_order if c in place]
            b1 = not missing and places == sorted(places)
            det = (f"账本 {len(ref_in_order)} 条里有 {len(missing)} 条重算不出来，例如 {missing[0]}" if missing
                   else "" if b1 else "账本顺序与重算结果不一致")
            check("B1 账本是重算结果的有序子集（过滤产物）", b1, det)
        else:
            b1 = ordered == ref_in_order
            # 一方是另一方的前缀时 zip 里找不到差异位，差异位就是较短一方的长度
            first = next((i for i, (x, y) in enumerate(zip(ordered, ref_in_order)) if x != y),
                         min(len(ordered), len(ref_in_order)))
            det = "" if b1 else f"首个差异位 {first}（重算 {len(ordered)} vs 账本 {len(ref_in_order)}）"
            check("B1 core 逐位重算一致（含顺序、含重复 _N 后缀）", b1, det)

        b2 = True; why = []
        dropped_total = 0

        def with_cores(msg_ids):
            """acp 的覆盖集合里，只有会产出 core 的消息才可能在 bili 侧存在。

            内容为空的 assistant 消息（content=[]）在 bili 里不产生任何 core——
            它既不占 wire、也无需折叠，属于**语义等价的丢弃**，不是错误。
            """
            return {m for m in msg_ids if cores_of_msg.get(m)}

        for key in sorted(ablocks, key=lambda k: int(k)):
            a, b = ablocks[key], by_id.get(f"b{key}")
            if b is None:
                b2 = False; why.append(f"b{key} 缺失"); continue
            want = with_cores(a.get("directMessageIds") or [])
            dropped_total += len(a.get("directMessageIds") or []) - len(want)
            got = {core_to_msg[c] for c in b.get("directMessageIds", []) if c in core_to_msg}
            if not ((got <= want) if filtered else (got == want)):
                b2 = False
                why.append(f"b{key} direct 覆盖消息不一致（应有 {len(want)} vs 实际 {len(got)}，"
                           f"差集 {sorted(want ^ got)[:2]}）")
            want_e = with_cores(a.get("effectiveMessageIds") or [])
            got_e = {core_to_msg[c] for c in b.get("effectiveMessageIds", []) if c in core_to_msg}
            if not ((got_e <= want_e) if filtered else (got_e == want_e)):
                b2 = False
                why.append(f"b{key} effective 覆盖消息不一致（差集 {len(want_e ^ got_e)}）")
        check("B2 逐块覆盖集合与 acp 一致" + ("（过滤产物：子集）" if filtered else "") + "（direct + effective）",
              b2, "；".join(why[:3]))
        if dropped_total:
            print(f"  [INFO] 另有 {dropped_total} 处「消息条数」差异，来自内容为空的消息"
                  f"（bili 侧不产生 core，语义等价）")

        covered = set()
        for b in blocks:
            if b.get("active"):
                covered |= set(b.get("effectiveMessageIds", []))
        want_tail = [c[0] for c in cores if c[0] not in covered]
        if filtered:                      # 被 bili 过滤掉的编号不在账本里，尾段同样只比子集
            want_tail = [c for c in want_tail if c in by_raw]
        got_tail = [m.get("id") for m in payload.get("messages", [])]
        check("B3 尾段 = 未被 active 覆盖的 core，且顺序一致", want_tail == got_tail,
              f"重算 {len(want_tail)} vs 文件 {len(got_tail)}")

        # 过滤产物的账本只会比重算结果少；多出来就是有编号不是重算得来的
        ledger_ok = len(by_ref) <= len(cores) if filtered else len(cores) == len(by_ref)
        b4 = (ledger_ok
              and len(blocks) == len(ablocks)
              and len(payload.get("messages", [])) == len(want_tail))
        check("B4 汇总数字一致（cores/blocks/tail）", b4,
              f"cores {len(cores)}/{len(by_ref)} blocks {len(blocks)}/{len(ablocks)} tail {len(payload.get('messages', []))}/{len(want_tail)}")

    # 编号规则只在 BILI_ID_RULE_VERSIONS 范围内用真实请求验证过。这里只看磁盘上的 bili 副本，
    # 不发网络请求（本校验器约定不碰网络）。
    ver, status = bili_version_status(probe_http=False)
    lo, hi = BILI_ID_RULE_VERSIONS
    if status == "old":
        check(f"bili 版本不早于 {lo}（编号规则的起点）", False, f"检测到 bili {ver}，产物装不上")
    elif status == "new":
        warn(f"bili {ver} 比已验证的 {hi} 新：编号规则可能变过，PASS 不代表在它上面生效")

    print()
    verdict = "全部通过 ✓" if not FAILS else f"有 {len(FAILS)} 项失败 ✗"
    print(f"=== 校验结论: {verdict} ===")
    if WARNS:
        print(f"（另有 {len(WARNS)} 条警告，不构成失败）")
    for f in FAILS:
        print(f"  ! {f}")

    if args.out:
        pathlib.Path(args.out).write_text(
            f"# bili 转换产物校验报告\n\n- 会话: {sid}\n- acp: {args.acp}\n- bili: {args.bili}\n"
            f"- 内容源: {args.db or '(未提供)'}\n- 结论: {verdict}\n\n"
            + "\n".join(f"- 失败: {f}" for f in FAILS)
            + "\n" + "\n".join(f"- 警告: {w}" for w in WARNS) + "\n")
        print(f"报告已写入 {args.out}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
