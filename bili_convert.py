#!/usr/bin/env python3
"""acp -> billion-context 会话状态转换器（opencode-acp → bili）

把 opencode-acp(opencode 1.x 插件) 的会话状态，转换成 billion-context(bili)
能直接加载的会话状态文件。

## 用法

    python3 bili_convert.py --acp <acp状态.json> --db <opencode.db> --sid <ses_xxx> \\
        --out <输出目录> [--protocol openai] [--upstream https://upstream.example] \\
        [--manifest <路径.json>]

输出：`<out>/<protocol>/<host>_<sha256(sid)[:24]>.json`
（`host` 取 `--upstream` 的 hostname；这正是 bili 的 `sessionsDir()` 布局）

## 输入从哪来

- acp 状态：老机 `~/.local/share/opencode/storage/plugin/acp/<sid>.json`
- 内容源：把同一个会话导入 opencode 2.x 之后的库
  （老库的 message/part 也能用，只要表是 v1 的 `message`+`part`）

## 身份模型（本次已用真实 wire 标定，diffs=0）

bili 的 core id 是**内容哈希**，与宿主 id 无关：

    core_id = "h_" + sha256(f"{role}|{contentType}|{toolCallId}|{toolName}|{text}")[:16]

按角色/类型展开：

    user        ("user","text", text)
    assistant   ("assistant","reasoning", 全部 reasoning 拼接)
                ("assistant","text",      全部 text 拼接)
                ("assistant","tool-call", json(args, 紧凑), {toolCallId, toolName})
    tool        ("tool","tool-result", output, {toolCallId})     # 注意：不含 toolName

四条容易踩的规则（都由真实 wire 反推）：

1. **一条 assistant 消息固定排成 reasoning → text → tool_calls**，不保留 part 原始顺序
2. **空 text/reasoning 不产生 core**（bili 侧有 `if (reasoning)` / `if (text)` 守卫）
3. **报错工具结果**序列化成 `{"error":{...},"content":[...]}` 的 JSON，而不是错误文本
4. 同一份身份在一趟里第 N 次出现要加 `_N` 后缀（ClusterCounter）

## 与 acp 的语义差异

- acp 的 ref 是**消息级**（opencode 原生 `msg_...` 的账本），bili 的 core 是**更细粒度**的
  （一条 assistant 消息可能展开成 1 reasoning + 1 text + N tool-call + N tool-result）
- acp 的 raw id 取宿主字段，bili 取内容哈希 → 必须**重算**，不能搬编号
- acp 的 tier-2 用 `startId=b1/endId=b34`（块区间）表达，bili 用 `directBlockIds`（块列表）
- `blockContents` 在 bili 里只是缓存，缺失时会由 `collectBlockContent` 从原始消息重建 → 不必生成
"""
import argparse
import collections
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import sys
import time
from urllib.parse import urlparse


def derive(role, ct, text, tcid="", tname=None):
    """bili 的 core id（与 kernel `deriveMessageId` 逐字节一致）。"""
    seed = f"{role}|{ct}|{tcid}|{tname or ''}|{text}"
    return "h_" + hashlib.sha256(seed.encode()).hexdigest()[:16]


# ── opencode 1.x 的两种 wire 形态（用老机 1.19.0 的真实请求体标定） ──────────
#   "rc"    历史里的 reasoning 单独走 `reasoning_content` → 每条 assistant 产生
#           reasoning core + text core（与 2.x 规则相同）
#   "fold"  历史里的 reasoning 并进 `content`（`reasoning_content` 为空串）→ 只产生
#           1 条 text core，其 text = 该消息的 reasoning 文本 + text 文本（直接拼接，无分隔符）
#
# 实测（老机 1025 条 assistant 消息逐条配对，见 bili_wire_diff.py）：
#   deepseek-litellm/deepseek-flash     → rc   （435 条）
#   opencode-go/deepseek-v4.1-flash     → fold （569 条，另 5 条因 reasoning 为空造成签名歧义）
#   zhipuai[-coding-plan]/glm-5.3       → fold （11 条）
# 形态由「该 provider/model 的 interleaved.field」决定（opencode 1.19.0 源码已确认）：
#   provider.ts:1593  interleaved = model.interleaved ?? existingModel.capabilities.interleaved
#                                 ?? (!existingModel && npm=="@ai-sdk/openai-compatible"
#                                     && apiID.includes("deepseek") ? {field:"reasoning_content"} : false)
#   transform.ts:315  interleaved.field 有值（且 provider 不是 openrouter）→ 把 reasoning part 提到
#                     providerOptions.openaiCompatible[field]（即 rc 形态，且空值也写）
# 所以：interleaved 有值 → rc；否则 reasoning 留在 content → fold。
# ⚠ 形态是 (provider, model) 粒度，不是 provider 粒度：同是 zhipuai-coding-plan，
#   glm-5.2/5.1 实测 fold（204 块会话），glm-5.3 实测 rc（同会话 224 条）。
# ⚠ 形态跟「宿主实际怎么发」走：同一 (zhipuai-coding-plan, glm-5.3) 在 10-13 量到
#   fold（旗舰会话 drift=1 那次）、10-14 量到 rc（204 会话转储）——宿主每次请求都按
#   **当时**读到的元数据重新序列化整条历史。判定顺序：force > 实测表 > catalog > heuristic。
#   实测表放 catalog 前面是 2026-10-15 旗舰复测换来的：buraunkan 缓存刷新后 opencode-go/
#   deepseek-v4.1-flash 与 zhipuai/glm-5.3 被标 rc，宿主 wire 实际仍发 fold（drift 1→986），
#   说明宿主对自定义 provider 不跟 buraunkan 缓存走——实测转储才是 ground truth。
MEASURED_FORMS = {
    # (provider, model): (form, 依据, 日期)
    ("deepseek-litellm", "deepseek-flash"):     ("rc",   "probe 会话 435 条", "2026-10-13"),
    ("opencode-go", "deepseek-v4.1-flash"):     ("fold", "probe 会话 569 条", "2026-10-13"),
    ("zhipuai", "glm-5.3"):                     ("fold", "probe 会话 11 条", "2026-10-13"),
    ("zhipuai-coding-plan", "glm-5.3"):         ("rc",   "204 块会话 224 条", "2026-10-14"),
    ("zhipuai-coding-plan", "glm-5.2"):         ("fold", "204 块会话 15109 条匹配", "2026-10-14"),
    ("zhipuai-coding-plan", "glm-5.1"):         ("fold", "204 块会话 15109 条匹配", "2026-10-14"),
    ("volces-ark", "glm-5.2"):                  ("fold", "204 块会话 15109 条匹配", "2026-10-14"),
    ("aliyun", "glm-5.2"):                      ("fold", "204 块会话 15109 条匹配", "2026-10-14"),
    ("zai-coding-plan", "glm-5.2"):             ("fold", "204 块会话 15109 条匹配", "2026-10-14"),
}


def classify_form(prov, model_id, force_rc=(), force_fold=()):
    """判 (provider, model) 在 1.x wire 上的形态。返回 (form, 依据)。

    优先级：force（--fold/--rc-providers，可写 "provider" 或 "provider/model"）
    > 实测表 MEASURED_FORMS（真实请求转储标定，最可信）
    > 宿主 catalog（load_models_dev：buraunkan 缓存优先，其次 models.dev 快照）
      —— 该模型条目的 interleaved 开关；provider 有而模型无 → 无开关 → fold
    > heuristic：模型 id 含 "deepseek" → rc，否则 fold（provider.ts:1593）。
    实测 > catalog 的理由（2026-10-15 旗舰复测）：老机 buraunkan 缓存刷新后把
    opencode-go/deepseek-v4.1-flash 与 zhipuai/glm-5.3 标成 rc，但宿主真实 wire 仍发
    fold（drift 从 1 涨到 986 ≈ 2×(453+18)），证明宿主对自定义 provider 并不跟
    buraunkan 缓存走——实测转储才是 ground truth，catalog 只用来填没测过的组合。
    """
    for pat in force_rc:
        if pat == prov or pat == f"{prov}/{model_id}":
            return "rc", "force"
    for pat in force_fold:
        if pat == prov or pat == f"{prov}/{model_id}":
            return "fold", "force"
    hit = MEASURED_FORMS.get((prov, model_id))
    if hit:
        return hit[0], "measured"
    entry = load_models_dev().get(prov)
    if entry:
        m = (entry.get("models") or {}).get(model_id)
        if m is not None:
            return ("rc" if m.get("interleaved") else "fold"), "catalog"
        if "deepseek" in model_id.lower():
            return "rc", "heuristic(catalog 无此模型)"
        return "fold", "catalog(无此模型)"
    return ("rc", "heuristic(deepseek)") if "deepseek" in model_id.lower() else ("fold", "default")


def form_contradictions():
    """catalog 与历史实测打架的组合（黄名单）。

    判定按实测走（catalog 不参与），所以冲突**不会**写进产物；此表只提示：
    该组合的 catalog 元数据与实测不一致（宿主行为可能与 buraunkan 缓存不同步），
    若某会话 drift 异常，可用 --fold/--rc-providers 或抓转储复核。
    """
    out = []
    for (prov, model), (form, _, _) in MEASURED_FORMS.items():
        entry = load_models_dev().get(prov)
        if not entry:
            continue
        m = (entry.get("models") or {}).get(model)
        if m is not None and ("rc" if m.get("interleaved") else "fold") != form:
            out.append((prov, model, form, "rc" if m.get("interleaved") else "fold"))
    return out


def build_cores(rows, wire="2x", fold_providers=None, rc_providers=None,
                regime_log=None):
    """rows: [(msg_id, type, data_json)] 按会话顺序。

    wire:
      "2x"  assistant 的 reasoning 一律进 reasoning_content（opencode 2.x 的 wire；已用真实请求标定）
      "1x"  opencode 1.x 的 wire：**逐条消息**判形态（rc / fold，见 MEASURED_FORMS + classify_form）

    fold_providers / rc_providers: 追加/覆盖 provider→形态的判断（rc 优先）
    regime_log: 传入 dict 时，按 provider 累计 {fold: n, rc: n}

    返回 (CORES, cores_of_msg)
      CORES: [(core_id, msg_id, role, contentType, toolCallId, toolName, text)]
    """
    fold_set = set(fold_providers or ())
    rc_set = set(rc_providers or ())
    cores = []
    cores_of_msg = collections.defaultdict(list)
    clusters = collections.Counter()

    def push(base, mid, role, ct, tcid, tname, text):
        n = clusters[base]
        clusters[base] += 1
        cid = base if n == 0 else f"{base}_{n}"
        cores.append((cid, mid, role, ct, tcid, tname, text))
        cores_of_msg[mid].append(cid)

    for mid, typ, data in rows:
        m = json.loads(data)
        if typ == "user":
            push(derive("user", "text", m.get("text", "")),
                 mid, "user", "text", "", None, m.get("text", ""))
            continue
        if typ != "assistant":
            continue
        content = m.get("content") or []
        # 1.x 实测：每个 reasoning part 在 wire 上各算一条（不能合并成一条）
        r_parts = [p.get("text", "") for p in content
                   if p.get("type") == "reasoning" and p.get("text")]
        t_parts = [p.get("text", "") for p in content
                   if p.get("type") == "text" and p.get("text")]
        reasoning = "".join(r_parts)
        text = "".join(t_parts)
        tools = [p for p in content if p.get("type") == "tool"]
        prov = (m.get("providerID") or "").strip()
        model_id = (m.get("modelID") or "").strip()
        if wire == "2x":
            fold = False
        else:
            # v3：按 (provider, model) 判 —— force > MEASURED_FORMS > catalog > heuristic
            form, _basis = classify_form(prov, model_id, rc_set or (), fold_set or ())
            fold = form == "fold"
        if regime_log is not None and wire == "1x":
            key = f"{prov or '(未知)'}/{model_id or '?'}"
            regime_log.setdefault(key, collections.Counter())[
                "fold" if fold else "rc"] += 1
        if wire == "1x" and not text and not tools and not reasoning:
            # 1.x 实测：完全空的 assistant 消息（无 reasoning/text/tool）在 wire 上是
            # {content:"", reasoning_content:""} → bili 侧不产生 core，跳过。
            # 注意：只有 reasoning、没有 text/tool 的消息**会**进 wire（fold→content=R
            # 产生 text core；rc→reasoning_content=R 产生 reasoning core），不能跳。
            continue
        if wire == "1x" and fold:
            # opencode 1.x（fold 形态）：历史里的 reasoning 不进 reasoning_content，
            # 而是并进 content —— 直接拼接（用换行拼是错的，0/1025 命中）
            pooled = reasoning + text
            if pooled:
                push(derive("assistant", "text", pooled),
                     mid, "assistant", "text", "", None, pooled)
        else:
            for rp in (r_parts if wire == "1x" else ([reasoning] if reasoning else [])):
                push(derive("assistant", "reasoning", rp),
                     mid, "assistant", "reasoning", "", None, rp)
            if text:
                push(derive("assistant", "text", text),
                     mid, "assistant", "text", "", None, text)
        for x in tools:
            st = x.get("state") or {}
            args = json.dumps(st.get("input") or {}, separators=(",", ":"), ensure_ascii=False)
            push(derive("assistant", "tool-call", args, x.get("id", ""), x.get("name")),
                 mid, "assistant", "tool-call", x.get("id", ""), x.get("name"), args)
        for x in tools:
            st = x.get("state") or {}
            parts = st.get("content") or []
            out = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
            if st.get("status") == "error" and wire == "2x":
                # 规则 3（**仅 2.x**）：error 工具结果序列化成 {"error":…,"content":[…]}
                # 1.x 实测是把错误文本原样放进 content（外面再包一层 JSON 会算错 id）
                out = json.dumps({"error": st.get("error"), "content": parts},
                                 separators=(",", ":"), ensure_ascii=False)
            elif wire == "1x" and st.get("status") != "completed":
                # 1.x 实测：没跑完的工具（pending/running/无错误文本）在 wire 上是固定占位串
                err = st.get("error")
                msg = err.get("message") if isinstance(err, dict) else (err if isinstance(err, str) else "")
                out = msg or "[Tool execution was interrupted]"
            push(derive("tool", "tool-result", out, x.get("id", "")),
                 mid, "tool", "tool-result", x.get("id", ""), x.get("name"), out)
    return cores, cores_of_msg


def build_candidates(rows):
    """多候选指纹（排列组合法）：对每条消息把**所有可能 wire 形态**的指纹都产出来。

    背景（2026-10-15 定案）：wire 上的内容哈希（h_）由「拼法」决定，而拼法的
    一个条件（catalog 的 interleaved 开关）既不随消息存储、也不必然等于磁盘上
    最新的元数据（opencode-go 实证：buraunkan 缓存已刷新成 rc、宿主实际仍发
    fold，旗舰 drift 986）。与其猜哪种形态会出现，不如把候选全部注册进账本：
      - 宿主实际发出的那个候选 → 命中 → 折叠生效
      - 永远发不出来的候选 → 留在账本里当死账（与宿主 compaction 前缀同类
        的良性 missing，bili 已有处理路径，见 #1921）
    候选维度（每条消息最多 4 个指纹，多数只有 1 个）：
      1. reasoning 摆放：fold（拼进 content，1 条）/ 1x-rc（逐 part 各 1 条）/ 2x-rc（合并 1 条）
      2. 错误工具结果包裹：v1 纯文本/占位串 / v2 JSON 包裹
    同一消息内相同 (role, contentType, toolCallId, toolName, text) 去重——
    避免给同一指纹造出假的 _N 重复后缀。

    返回 (CORES, cores_of_msg)，形状与 build_cores 相同。
    """
    cores = []
    cores_of_msg = collections.defaultdict(list)
    clusters = collections.Counter()

    def push(base, mid, role, ct, tcid, tname, text):
        n = clusters[base]
        clusters[base] += 1
        cid = base if n == 0 else f"{base}_{n}"
        cores.append((cid, mid, role, ct, tcid, tname, text))
        cores_of_msg[mid].append(cid)

    for mid, typ, data in rows:
        m = json.loads(data)
        if typ == "user":
            push(derive("user", "text", m.get("text", "")),
                 mid, "user", "text", "", None, m.get("text", ""))
            continue
        if typ != "assistant":
            continue
        content = m.get("content") or []
        r_parts = [p.get("text", "") for p in content
                   if p.get("type") == "reasoning" and p.get("text")]
        t_parts = [p.get("text", "") for p in content
                   if p.get("type") == "text" and p.get("text")]
        reasoning = "".join(r_parts)
        text = "".join(t_parts)
        tools = [p for p in content if p.get("type") == "tool"]
        if not reasoning and not text and not tools:
            continue                     # 完全空的 assistant：任何形态都不产生 core

        seen = set()                     # 同一消息内去重

        def push1(role, ct, tcid, tname, txt):
            k = (role, ct, tcid or "", tname or "", txt)
            if k in seen:
                return
            seen.add(k)
            # seed 参数与内核标定严格一致：
            #   tool-call    → derive(role, ct, text, toolCallId, toolName)  5 参
            #   tool-result  → derive(role, ct, text, toolCallId)            4 参（不带 toolName）
            #   其余         → derive(role, ct, text)                        3 参
            if ct == "tool-call":
                base = derive(role, ct, txt, tcid or "", tname)
            elif ct == "tool-result":
                base = derive(role, ct, txt, tcid or "")
            else:
                base = derive(role, ct, txt)
            push(base, mid, role, ct, tcid, tname, txt)

        # 候选 1：fold —— 1.x 无开关组合，reasoning 直接拼进 content（实测分隔符=空串）
        pooled = reasoning + text
        if pooled:
            push1("assistant", "text", "", None, pooled)
        # 候选 2：1x-rc —— reasoning 逐 part 各一条（1.x 实测：多 part 各算一条）
        for rp in r_parts:
            push1("assistant", "reasoning", "", None, rp)
        # 候选 3：2x-rc —— reasoning 合并一条（单 part 时与候选 2 天然去重）
        if reasoning:
            push1("assistant", "reasoning", "", None, reasoning)
        if text:
            push1("assistant", "text", "", None, text)
        # 工具调用：各形态一致，单一指纹
        for x in tools:
            st = x.get("state") or {}
            args = json.dumps(st.get("input") or {}, separators=(",", ":"), ensure_ascii=False)
            push1("assistant", "tool-call", x.get("id", ""), x.get("name"), args)
        # 工具结果：completed 单一指纹；非 completed 三候选
        #   v1-错误文本 / v1-占位串（无文本、被中断、未跑完）/ v2-JSON 包裹
        for x in tools:
            st = x.get("state") or {}
            parts = st.get("content") or []
            raw = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
            if st.get("status") != "completed":
                err = st.get("error")
                msg = err.get("message") if isinstance(err, dict) else (err if isinstance(err, str) else "")
                push1("tool", "tool-result", x.get("id", ""), x.get("name"),
                      msg or "[Tool execution was interrupted]")
                if raw and raw != msg:
                    push1("tool", "tool-result", x.get("id", ""), x.get("name"), raw)
                push1("tool", "tool-result", x.get("id", ""), x.get("name"),
                      json.dumps({"error": st.get("error"), "content": parts},
                                 separators=(",", ":"), ensure_ascii=False))
            else:
                push1("tool", "tool-result", x.get("id", ""), x.get("name"), raw)
    return cores, cores_of_msg


def cores_from_dump(dump_path, rows):
    """测量法：从 bili 的 INCOMING 转储（宿主真实发出的请求体）直接折 core。

    不猜任何序列化规则（1x/2x/catalog 全部绕开）：转储里每条消息长什么样，
    就按 bili openaiToCore 的语义算什么 id —— 与 bili 实际看到的逐字节一致。
    同时把每个 core 锚定回宿主消息 id（acp 块的 directMessageIds 要用）。

    转储格式：`<METHOD> <url>\n<headers>\n\n<body>`（bili raw/*-INCOMING.txt）。
    返回 (CORES, cores_of_msg, stats)，CORES 形状与 build_cores 相同。
    """
    raw = pathlib.Path(dump_path).read_text(encoding="utf-8")
    head, _, body = raw.partition("\n\n")
    if not head.strip().startswith(("POST", "GET", "PUT")):   # 已经是纯 JSON 体
        body = raw
    msgs = json.loads(body)["messages"]

    # ---- 锚点索引：文本串 -> [msg_id]（rc 的 R/T 与 fold 的 R+T 都登记）、call_id -> msg_id ----
    text_idx = collections.defaultdict(list)
    tool_idx = {}
    for mid, typ, data in rows:
        m = json.loads(data)
        if typ == "user":
            if m.get("text"):
                text_idx[m["text"]].append(mid)
            continue
        if typ != "assistant":
            continue
        content = m.get("content") or []
        rs = "".join(p.get("text", "") for p in content
                     if p.get("type") == "reasoning" and p.get("text"))
        ts = "".join(p.get("text", "") for p in content
                     if p.get("type") == "text" and p.get("text"))
        # 每个 part 的文本单独登记（build_cores 按 part 逐条产 core，wire 上也可能逐条出现），
        # 同时保留拼接形态（rc 的 R/T 与 fold 的 R+T）
        for p in content:
            if p.get("type") in ("reasoning", "text") and p.get("text"):
                text_idx[p["text"]].append(mid)
        for s in (rs, ts, rs + ts):
            if s:
                text_idx[s].append(mid)
        for p in content:
            if p.get("type") == "tool" and p.get("id"):
                tool_idx[p["id"]] = mid

    def scontent(c):
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "".join(p.get("text", "") for p in c
                           if isinstance(p, dict) and p.get("type") == "text")
        return ""

    cores, cores_of_msg = [], collections.defaultdict(list)
    clusters = collections.Counter()
    stats = collections.Counter()
    used = set()           # (锚点串, mid)：同一宿主消息可锚多个不同串（R/T/tool 各自一条）
    row_order = {mid: i for i, (mid, _, _) in enumerate(rows)}
    last = {"pos": 0}      # 顺序先验：与 bili 的 order-centric 对账同思想
    tc_name = {}          # call_id -> 工具名（展示用；tool-result 的 seed 不带它）

    def anchor(key, cands):
        best, bestd = None, None
        for mid in cands:
            if (key, mid) in used:
                continue
            d = abs(row_order.get(mid, 0) - last["pos"])
            if bestd is None or d < bestd:
                best, bestd = mid, d
        if best is None:
            return None
        used.add((key, best))
        last["pos"] = row_order.get(best, last["pos"])
        return best

    def push(base, mid, role, ct, tcid, tname, text):
        n = clusters[base]
        clusters[base] += 1
        cid = base if n == 0 else f"{base}_{n}"
        cores.append((cid, mid, role, ct, tcid, tname, text))
        if mid:
            cores_of_msg[mid].append(cid)

    lead_done = False
    for m in msgs:
        role = m.get("role")
        if role in ("system", "developer") and not lead_done:
            stats["lead_system(跳过,hoist)"] += 1
            continue                       # 前导 system 被 bili hoist 出 fold 空间
        lead_done = True
        if role in ("system", "developer"):        # 中段 system → 按 user 记
            t = scontent(m.get("content"))
            push(derive("user", "text", t), None, "user", "text", "", None, t)
            stats["mid_system(无锚)"] += 1
        elif role == "user":
            t = scontent(m.get("content"))
            mid = anchor(t, text_idx.get(t, ())) if t else None
            if mid is None:
                stats["user 未锚定"] += 1
            push(derive("user", "text", t), mid, "user", "text", "", None, t)
        elif role == "assistant":
            rc = m.get("reasoning_content") or ""
            cont = scontent(m.get("content"))
            tcs = m.get("tool_calls") or []
            mid = None
            for tc in tcs:                          # 优先 call_id 锚（最硬）
                k = tc.get("id") or ""
                if k in tool_idx:
                    mid = tool_idx[k]
                    last["pos"] = row_order.get(mid, last["pos"])
                    break
            if mid is None and rc:
                mid = anchor(rc, text_idx.get(rc, ()))
            if mid is None and cont:
                mid = anchor(cont, text_idx.get(cont, ()))
            if mid is None and (rc or cont or tcs):
                stats["assistant 未锚定"] += 1
            if rc:
                push(derive("assistant", "reasoning", rc),
                     mid, "assistant", "reasoning", "", None, rc)
            if cont:
                push(derive("assistant", "text", cont),
                     mid, "assistant", "text", "", None, cont)
            for tc in tcs:
                fn = tc.get("function") or {}
                args = fn.get("arguments") or ""
                k = tc.get("id") or ""
                tc_name[k] = fn.get("name") or ""
                push(derive("assistant", "tool-call", args, k, fn.get("name")),
                     mid, "assistant", "tool-call", k, fn.get("name"), args)
        elif role == "tool":
            t = scontent(m.get("content"))
            tcid = m.get("tool_call_id") or ""
            mid = tool_idx.get(tcid)
            if mid is None:
                stats["tool-result 未锚定"] += 1
            # 与 build_cores/内核标定一致：tool-result 的 seed 只带 toolCallId，不带 toolName
            push(derive("tool", "tool-result", t, tcid),
                 mid, "tool", "tool-result", tcid, tc_name.get(tcid), t)
        else:
            stats[f"未知 role:{role}"] += 1
    return cores, cores_of_msg, stats


_CONN_CACHE = {}


def _db(db_path):
    """共享只读连接。批量转换时每个会话都查库，若各自 connect，
    老机默认 ulimit -n 256 会累积到 EMFILE（Too many open files）。"""
    key = str(pathlib.Path(db_path).expanduser().resolve())
    if key not in _CONN_CACHE:
        _CONN_CACHE[key] = sqlite3.connect(f"file:{key}?mode=ro", uri=True)
    return _CONN_CACHE[key]


def load_rows(db_path, sid):
    """从 opencode 库里取该会话的消息；自动识别 v2 / v1 表结构。"""
    db = _db(db_path)
    tables = {r[0] for r in db.execute(
        "select name from sqlite_master where type='table'")}
    if "session_message" in tables and "session_v2" in tables:
        rows = db.execute(
            "select id,type,data from session_message where session_id=? order by seq",
            (sid,)).fetchall()
        return [(r[0], r[1], r[2]) for r in rows], "v2"
    if "message" in tables and "part" in tables:
        parts = collections.defaultdict(list)
        for pid, mid, tc, data in db.execute(
                "select id,message_id,time_created,data from part where session_id=?"
                " order by time_created,id", (sid,)):
            parts[mid].append(json.loads(data))
        out = []
        for mid, tc, data in db.execute(
                "select id,time_created,data from message where session_id=?"
                " order by time_created,id", (sid,)):
            m = json.loads(data)
            role = m.get("role")
            if role == "user":
                txt = "".join(p.get("text", "") for p in parts.get(mid, [])
                              if p.get("type") == "text")
                out.append((mid, "user", json.dumps({"text": txt}, ensure_ascii=False)))
                continue
            content = []
            for p in parts.get(mid, []):
                t = p.get("type")
                if t == "reasoning":
                    content.append({"type": "reasoning", "text": p.get("text", "")})
                elif t == "text":
                    content.append({"type": "text", "text": p.get("text", "")})
                elif t == "tool":
                    st = p.get("state") or {}
                    status = st.get("status", "completed")
                    tool_out = st.get("output")
                    if tool_out is None:
                        tool_out = st.get("raw") or ""
                    if status == "error":
                        interrupted = bool((st.get("metadata") or {}).get("interrupted"))
                        err = st.get("error") or ""
                        if not tool_out and (interrupted or not err):
                            # 被中断 / 没有错误文本的失败：wire 上不是库里的 error 文本
                            # （"Tool execution aborted"），而是固定占位串 —— 用真实请求体标定
                            tool_out = "[Tool execution was interrupted]"
                        elif not tool_out:
                            # v1 的 error 工具没有 output，只有 error 字符串
                            tool_out = err
                    content.append({"type": "tool", "id": p.get("callID", ""),
                                    "name": p.get("tool", ""),
                                    "state": {"status": status,
                                              "input": st.get("input") or {},
                                              "content": [{"type": "text", "text": tool_out}],
                                              "metadata": st.get("metadata") or {},
                                              "error": ({"type": "unknown", "message": str(st.get("error") or "")}
                                                        if status == "error" else None)}})
            out.append((mid, "assistant",
                        json.dumps({"content": content,
                                    "providerID": m.get("providerID"),
                                    "modelID": m.get("modelID")},
                                   ensure_ascii=False)))
        return out, "v1"
    raise SystemExit("无法识别的库结构：既没有 session_message/session_v2，也没有 message/part")


def convert(args):
    rows, mode = load_rows(args.db, args.sid)
    if not rows:
        print("  ✗ 库里已无该会话的消息（可能被 session-reaper 之类清理过）——只剩 acp 状态、"
              "没有原始内容，转换无意义，跳过（不写产物）")
        return
    wire = getattr(args, "wire", "") or ("1x" if mode == "v1" else "2x")
    fold_extra = [s.strip() for s in (getattr(args, "fold_providers", "") or "").split(",") if s.strip()]
    rc_force = [s.strip() for s in (getattr(args, "rc_providers", "") or "").split(",") if s.strip()]
    dump_path = getattr(args, "dump", None)
    method = "predict"
    if dump_path:
        # 测量法：指纹直接来自宿主真实请求，--wire 规则整个绕开
        method = "measure"
        cores, cores_of_msg, dstats = cores_from_dump(dump_path, rows)
        print(f"内容源模式 : {mode} + 转储测量（{dump_path}）  [--wire 规则未参与]")
        for k, v in sorted(dstats.items()):
            if v:
                print(f"   锚定统计 {k}: {v}")
        if not getattr(args, "protocol", None) or not getattr(args, "upstream", None):
            head = pathlib.Path(dump_path).read_text(encoding="utf-8").split("\n", 1)[0]
            parts = head.split()
            if len(parts) >= 2 and parts[1].startswith("http"):
                u = urlparse(parts[1])
                if not getattr(args, "upstream", None):
                    args.upstream = f"{u.scheme}://{u.netloc}"
                if not getattr(args, "protocol", None):
                    args.protocol = "anthropic" if "/messages" in u.path else "openai"
    else:
        regime_log = {}
        if getattr(args, "candidates", True):
            # 多候选指纹：所有可能的 wire 形态全注册，不再猜 fold/rc
            method = "candidates-v1"
            cores, cores_of_msg = build_candidates(rows)
            print(f"内容源模式 : {mode}   → 多候选指纹（fold/1x-rc/2x-rc + 错误双包裹全注册；--wire 规则未参与）")
        else:
            print(f"内容源模式 : {mode}   → wire 规则: {wire}")
            cores, cores_of_msg = build_cores(rows, wire, fold_providers=fold_extra,
                                              rc_providers=rc_force, regime_log=regime_log)
    if regime_log:
        print("1.x wire 形态（按 provider/model 逐条判定）:")
        for p, c in sorted(regime_log.items(),
                           key=lambda kv: -(kv[1]["fold"] + kv[1]["rc"])):
            print(f"   {p:<44} fold={c['fold']:<5} rc={c['rc']:<5}")
        conflicts = form_contradictions()
        if conflicts:
            print("  ⚠ catalog 与历史实测打架（判定按实测走，catalog 只填没测过的组合）:")
            for prov, model, got, want in conflicts:
                print(f"     {prov}/{model}: 实测 {got} / catalog {want}")
    ref_of_core, core_of_ref = {}, {}
    for i, c in enumerate(cores, 1):
        ref = f"m{i:05d}"
        ref_of_core[c[0]] = ref
        core_of_ref[ref] = c[0]

    acp = json.load(open(args.acp))
    prune = acp["prune"]["messages"]
    acp_blocks = prune["blocksById"]

    def expand(msg_ids):
        out = []
        for mid in msg_ids:
            out.extend(cores_of_msg.get(mid, []))
        return out

    def span(msg_ids):
        if not msg_ids:
            return None, None
        first = cores_of_msg.get(msg_ids[0], [])
        last = cores_of_msg.get(msg_ids[-1], [])
        if not first or not last:
            return None, None
        return ref_of_core[first[0]], ref_of_core[last[-1]]

    blocks, max_run = [], 0
    for key in sorted(acp_blocks, key=lambda k: int(k)):
        b = acp_blocks[key]
        direct_msgs = b.get("directMessageIds") or []
        sref, eref = span(direct_msgs)
        run_id = b.get("runId")
        if run_id is None:
            max_run += 1
            run_id = max_run
        else:
            max_run = max(max_run, int(run_id))
        tier = b.get("tier") or 1   # 老版 acp 块可能没有 tier 字段，按 tier-1 处理
        if tier == 1:
            if sref is None and b.get("startId") in core_of_ref:
                sref = b["startId"]
            if eref is None and b.get("endId") in core_of_ref:
                eref = b["endId"]
        else:
            sref, eref = b.get("startId"), b.get("endId")
        blk = {
            "blockId": f"b{b['blockId']}",
            "runId": f"r{run_id}",
            "tier": tier,
            "summary": b.get("summary", ""),
            "directMessageIds": expand(direct_msgs),
            "effectiveMessageIds": expand(b.get("effectiveMessageIds") or []),
            "directBlockIds": [f"b{n}" for n in (b.get("consumedBlockIds") or [])],
            "compressedTokens": b.get("effectiveCompressedTokens") or b.get("compressedTokens") or 0,
            "createdAt": b.get("createdAt") or int(time.time() * 1000),
            "survivedCount": b.get("survivedCount", 0),
            "generation": b.get("generation", "young"),
            "active": bool(b.get("active")),
        }
        if b.get("topic"):
            blk["topic"] = b["topic"]
        if b.get("compressCallId"):
            blk["compressCallId"] = b["compressCallId"]
        if sref:
            blk["startRef"] = sref
        if eref:
            blk["endRef"] = eref
        blocks.append(blk)

    covered = set()
    for b in blocks:
        if b["active"]:
            covered.update(b["effectiveMessageIds"])
    tail = [c for c in cores if c[0] not in covered]
    messages = []
    for cid, mid, role, ct, tcid, tname, text in tail:
        item = {"id": cid, "role": role, "contentType": ct}
        if tname:
            item["toolName"] = tname
        if tcid:
            item["toolCallId"] = tcid
        item["text"] = text
        messages.append(item)

    now = int(time.time() * 1000)
    payload = {
        "version": 3, "savedAt": now, "id": args.sid,
        "meta": {"protocol": args.protocol, "upstreamOrigin": args.upstream,
                 "label": args.sid, "activePack": "default", "title": args.title or ""},
        "stats": {"requests": 0, "tokensSaved": 0, "inputTokens": 0, "cachedTokens": 0,
                  "outputTokens": 0, "cacheSamples": 0, "lastInputTokens": 0,
                  "compressCreditTokens": 0},
        "messages": messages,
        "messagesFolded": True,
        "metadata": {"conversion": method},
        "state": {
            "blocks": blocks,
            "messageRefs": {"byRaw": ref_of_core, "byRef": core_of_ref},
            "tokenSnapshot": {},
            "nudge": {"lastPerMessageNudgeTokens": 0, "lastNudgeShownTokens": 0,
                      "baselineTokens": 0, "anchors": {}, "lastShownByTier": {}},
            "stats": {"tokensCompressed": sum(b["compressedTokens"] for b in blocks if b["active"]),
                      "compressionCount": len(blocks), "absorbedTokens": 0, "imagesShrunk": 0,
                      "imageBytesSaved": 0, "imageTokensSaved": 0, "storedCount": 0,
                      "retrievalCount": 0},
            "absorbed": [], "rules": [], "nextRuleId": 1, "terminalStreak": 0,
            "nextBlockId": len(blocks) + 1, "nextRunId": max_run + 1,
            "hiddenOrphanRefs": [], "lastPassIds": [c[0] for c in tail],
        },
        "blockContents": {},
        "createdAt": now,
    }
    doc = {"version": 3, "savedAt": now, "id": args.sid, "payload": payload}

    # bili 的 hostLabel 用的是 new URL(origin).hostname —— 只有主机名，
    # 不带协议、不带端口（persist.ts: hostLabel）。这里必须同规则，
    # 否则 http://host:port 会生成 "host:port_<hash>.json"，bili 会当成无效文件名。
    #
    # 注意：host 只是**给人看的命名空间**。bili 启动时 loadAll 会扫描整个
    # sessions 目录并按记录里的 id 建索引（kernel/src/persist/store.ts），
    # 只要文件名是"规范名"或"扁平名(<hash>.json)"就能被找到 —— 与请求时的
    # 上游是什么无关（所以一个会话历史上换过多少模型/上游都不影响）。
    host = urlparse(args.upstream).hostname or args.upstream.split("//")[-1].split("/")[0]
    digest = hashlib.sha256(args.sid.encode()).hexdigest()[:24]
    name = f"{digest}.json" if getattr(args, "flat", False) else f"{host}_{digest}.json"
    target = pathlib.Path(args.out) / args.protocol / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, ensure_ascii=False))

    print(f"cores        : {len(cores)}")
    print(f"blocks       : {len(blocks)} (active {sum(1 for b in blocks if b['active'])})")
    print(f"refs         : {len(ref_of_core)}")
    print(f"tail cores   : {len(messages)}")
    print(f"output       : {target}")

    if args.manifest:
        man = pathlib.Path(args.manifest)
        man.parent.mkdir(parents=True, exist_ok=True)
        man.write_text(json.dumps({
            "sid": args.sid, "protocol": args.protocol, "upstream": args.upstream,
            "cores": [{"id": c[0], "msg": c[1], "role": c[2], "ct": c[3]} for c in cores],
            "counts": {"cores": len(cores), "blocks": len(blocks),
                       "active": sum(1 for b in blocks if b["active"]), "tail": len(messages)},
        }, ensure_ascii=False))
        print(f"manifest     : {man}")
    return 0


def default_data_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local/share")
    return os.path.join(base, "opencode")


def default_runtime_dir():
    """所有产物默认放在仓库自己的 runtime/ 下；可用 ACP2BILI_RUNTIME 覆盖。"""
    env = os.environ.get("ACP2BILI_RUNTIME")
    if env:
        return os.path.expanduser(env)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime")


def strip_jsonc(text):
    """够用的 jsonc 预处理：去 // 与 /* */ 注释（不处理字符串内的假注释，风险极低）。"""
    out, i, n = [], 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if ch == '"':
                in_str = False
            i += 1; continue
        if ch == '"':
            in_str = True; out.append(ch); i += 1; continue
        if text.startswith("//", i):
            j = text.find("\n", i); i = n if j < 0 else j; continue
        if text.startswith("/*", i):
            j = text.find("*/", i); i = n if j < 0 else j + 2; continue
        out.append(ch); i += 1
    t = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", t)          # 去尾逗号


def session_provider(db_path, sid):
    """取会话记录里的模型 providerID（v1/v2 表结构都能取）。"""
    db = _db(db_path)
    tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
    table = "session_v2" if "session_v2" in tables else "session"
    row = db.execute(f"select model from {table} where id=?", (sid,)).fetchone()
    if not row or not row[0]:
        return ""
    try:
        return (json.loads(row[0]) or {}).get("providerID", "")
    except Exception:
        return ""


_MODELS_DEV = None
MODELS_DEV_CANDIDATES = [
    "~/.cache/opencode/buraunkan/models-dev.json",   # old 机 & 本机都有（catalog-bridge 落盘）
    "~/.cache/opencode/models-dev.json",
    "./models-dev.json",
    "./tmp/models.dev.json",
]


def load_models_dev():
    """读 models.dev 快照（provider → {api, npm}）。

   为什么需要：老机的 opencode.jsonc 里只写了「用过的」provider 的自定义 baseURL。
    历史会话可能由内置 provider 产生（zhipuai-coding-plan / opencode-go / opencode 等），
    它们的 baseURL 只存在于 models.dev。缺这一步就推断不出上游 origin，产物的文件名
    带不上 host 标签（改走 --flat 也能用，但信息不全）。
    """
    global _MODELS_DEV
    if _MODELS_DEV is not None:
        return _MODELS_DEV
    for p in MODELS_DEV_CANDIDATES:
        p = os.path.expanduser(p)
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p))
            d = d.get("body") or d
            if isinstance(d, dict) and any(
                    isinstance(v, dict) and ("api" in v or "npm" in v) for v in d.values()):
                _MODELS_DEV = d
                return _MODELS_DEV
        except Exception:
            continue
    _MODELS_DEV = {}
    return _MODELS_DEV


def detect_wire(cfg_path, provider_id):
    """从 opencode 配置里推断 (protocol, upstream origin)；配置里没有则回落到 models.dev。"""
    proto, upstream = "", ""
    if provider_id and os.path.exists(cfg_path):
        try:
            cfg = json.loads(strip_jsonc(pathlib.Path(cfg_path).read_text()))
            prov = (cfg.get("provider") or {}).get(provider_id) or {}
            base = ((prov.get("options") or {}).get("baseURL") or "").rstrip("/")
            if base:
                upstream = base.split("//")[0] + "//" + base.split("//")[-1].split("/")[0]
            npm = prov.get("npm") or ""
            if "anthropic" in npm:
                proto = "anthropic"
            elif "google" in npm:
                proto = "google"
            elif "openai" in npm:
                proto = "openai"
        except Exception as e:
            print(f"  (配置解析失败，忽略: {e})")
    if not upstream or not proto:
        md = load_models_dev().get(provider_id or "") or {}
        base = (md.get("api") or "").rstrip("/")
        if base and not upstream:
            upstream = base.split("//")[0] + "//" + base.split("//")[-1].split("/")[0]
        npm = md.get("npm") or ""
        if not proto and npm:
            if "anthropic" in npm:
                proto = "anthropic"
            elif "google" in npm:
                proto = "google"
            elif "openai" in npm or "compatible" in npm:
                proto = "openai"
    return proto or "openai", upstream


def list_convertible(acp_dir):
    rows = []
    for p in sorted(pathlib.Path(acp_dir).glob("*.json")):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        pr = (d.get("prune") or {}).get("messages") or {}
        blocks = pr.get("blocksById") or {}
        mi = d.get("messageIds") or {}
        rows.append({
            "sid": p.stem, "path": str(p), "blocks": len(blocks),
            "active": len(pr.get("activeBlockIds") or []),
            "refs": len(mi.get("byRef") or {}),
            "tier23": sum(1 for b in blocks.values() if int(b.get("tier", 1)) >= 2),
            "model": f"{d.get('modelProviderID')}/{d.get('modelID')}",
        })
    rows.sort(key=lambda r: (-r["blocks"], -r["refs"]))
    return rows


def main():
    p = argparse.ArgumentParser(description="acp -> billion-context 会话状态转换器",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="最省事：python3 bili_convert.py --list   然后   python3 bili_convert.py --auto")
    p.add_argument("--auto", action="store_true",
                   help="全自动：自动定位数据目录/库/acp 目录，选一个（或 --all / --sid）会话并转换")
    p.add_argument("--list", action="store_true", help="列出本机所有可转换的 acp 会话")
    p.add_argument("--all", action="store_true", help="配合 --auto：转换全部有压缩块的会话")
    p.add_argument("--acp", help="acp 状态文件（不给则由 --auto 推断）")
    p.add_argument("--db", help="内容源库（不给则 <data-dir>/opencode.db）")
    p.add_argument("--acp-dir", help="acp 状态目录（不给则 <data-dir>/storage/plugin/acp）")
    p.add_argument("--from-dump", dest="dump",
                   help="bili 的 INCOMING 转储文件（raw/*-INCOMING.txt）：测量法，"
                        "指纹直接按宿主真实请求计算，--wire 规则与形态表全部绕开；"
                        "protocol/upstream 未给时自动从转储 URL 推断")
    p.add_argument("--data-dir", help="opencode 数据目录（默认 ~/.local/share/opencode 或 $XDG_DATA_HOME）")
    p.add_argument("--sid", help="会话 id；--auto 下不给且只有一个候选时自动选")
    p.add_argument("--out", help="输出目录（默认 <仓库>/runtime/out；可用 ACP2BILI_RUNTIME 改 runtime 根目录）")
    p.add_argument("--protocol", help="wire 协议目录名（默认从配置推断，兜底 openai）")
    p.add_argument("--wire", choices=["", "1x", "2x"], default="",
                   help="assistant 的序列化规则：1x=opencode 1.x（逐条按 provider 判 rc/fold 形态），"
                        "2x=opencode 2.x（reasoning 一律走 reasoning_content）。默认按库表结构自动选")
    p.add_argument("--fold-providers", default="",
                   help="1x 下追加判定为 fold 形态的 provider（逗号分隔）；未登记的 provider 默认 fold，"
                        "只有 modelID 含 deepseek 时才按 rc（对应 opencode 1.19 源码 heuristic）")
    p.add_argument("--rc-providers", default="",
                   help="1x 下强制判定为 rc 形态的 provider（逗号分隔，优先级高于 --fold-providers）")
    p.add_argument("--upstream", help="上游 origin；bili 用它算文件名里的 host 标签（默认从配置推断）")
    p.add_argument("--title", default="", help="会话标题（可选，仅元数据）")
    p.add_argument("--flat", action="store_true",
                   help="输出扁平名 <sha256(sid)[:24]>.json（不含 host 标签）。"
                        "bili 的 loadAll/loadSync 明确接受这种旧式扁平名，因此完全不涉及上游语义")
    p.add_argument("--candidates", dest="candidates", action="store_true", default=True,
                   help="多候选指纹（默认开）：每条消息注册所有可能的 wire 形态"
                        "（fold / 1x-rc / 2x-rc + 错误工具结果双包裹），catalog 怎么翻都有候选命中；"
                        "未命中的候选留在账本里当良性死账（与宿主 compaction 前缀同类）")
    p.add_argument("--no-candidates", dest="candidates", action="store_false",
                   help="退回单一预测形态（按 --wire/形态表猜一种；仅用于对照实验）")
    p.add_argument("--manifest", default="", help="可选：写出 core↔消息 映射清单")
    args = p.parse_args()

    data_dir = args.data_dir or default_data_dir()
    db = args.db or os.path.join(data_dir, "opencode.db")
    acp_dir = args.acp_dir or os.path.join(data_dir, "storage", "plugin", "acp")
    out = args.out or os.path.join(default_runtime_dir(), "out")

    if args.list or (args.auto and not args.acp and not args.sid and not args.all):
        if not os.path.isdir(acp_dir):
            raise SystemExit(
                f"找不到 acp 状态目录: {acp_dir}\n"
                f"  说明：这里应存在 opencode-acp 写的 <sessionId>.json。可能原因：\n"
                f"  · 这台机器没装过 opencode-acp（那就没有可转换的会话）；\n"
                f"  · 数据目录不在默认位置 —— 用 --data-dir <目录> 指定，或用 --acp-dir <目录> 直接指定 acp 状态目录。")
        rows = list_convertible(acp_dir)
        usable = [r for r in rows if r["blocks"]]
        print(f"数据目录 : {data_dir}")
        print(f"内容源库 : {db}  ({'存在' if os.path.exists(db) else '缺失！'})")
        print(f"acp 目录 : {acp_dir}  共 {len(rows)} 个状态文件，其中 {len(usable)} 个有压缩块\n")
        print(f"{'session':34} {'blocks':>6} {'tier2+':>6} {'active':>6} {'refs':>6}  model")
        for r in usable[:30]:
            print(f"{r['sid']:34} {r['blocks']:6} {r['tier23']:6} {r['active']:6} {r['refs']:6}  {r['model']}")
        if len(usable) > 30:
            print(f"... 另有 {len(usable)-30} 个")
        print(f"\n下一步：python3 {os.path.basename(__file__)} --auto"
              + ("            # 只有一个候选时自动选" if len(usable) == 1 else " --sid <上面的 session>")
              + "\n       或：--auto --all  # 全部转换")
        if args.list:
            return 0

    # ---- 选会话 ----
    sids = []
    if args.acp:
        sids = [args.sid or pathlib.Path(args.acp).stem]
    elif args.auto:
        rows = [r for r in list_convertible(acp_dir) if r["blocks"]]
        if args.sid:
            rows = [r for r in rows if r["sid"] == args.sid]
            if not rows:
                raise SystemExit(f"acp 目录里没有会话 {args.sid}")
        elif not args.all:
            if len(rows) != 1:
                raise SystemExit(f"有 {len(rows)} 个可转换会话，请加 --sid <id> 或 --all")
        sids = [r["sid"] for r in rows]
    else:
        raise SystemExit("请用 --auto（推荐）或显式给 --acp/--db/--sid")

    ok = 0
    for sid in sids:
        acp_path = args.acp or os.path.join(acp_dir, f"{sid}.json")
        provider = session_provider(db, sid)
        proto, upstream = detect_wire(os.path.join(os.path.expanduser("~"), ".config", "opencode", "opencode.jsonc"), provider)
        protocol = args.protocol or proto
        up = args.upstream or upstream
        flat = args.flat
        note = ""
        if not up:
            # 文件名里的 host 只是给人看的命名空间（kernel 的 loadAll 按记录里的 id 建索引，
            # loadSync 的候选里也显式含 flatFileNameFor(id)）→ 推不出上游时用扁平名，等效可用
            flat = True
            note = "  (上游 origin 推断不出 → 自动用 flat 名 <hash>.json)"
            if not load_models_dev():
                note += "\n            提示：可 curl -s https://models.dev/api.json -o models-dev.json 补全 provider 表"
        print(f"\n=== {sid} ===")
        print(f"  acp      : {acp_path}")
        print(f"  provider : {provider or '(未知)'}  →  protocol={protocol}  upstream={up or '(未推断出)'}{note}")
        ns = argparse.Namespace(acp=acp_path, db=db, sid=sid, out=out, protocol=protocol,
                                upstream=up or "", title=args.title, manifest=args.manifest,
                                flat=flat, wire=args.wire, candidates=args.candidates)
        try:
            convert(ns)
            ok += 1
        except Exception as e:
            print(f"  转换失败: {type(e).__name__}: {e}")
    if len(sids) > 1:
        print(f"\n完成 {ok}/{len(sids)} 个")
    print("\n下一步（必须做）：用内容源把产物校验一遍，通过后再拷进 bili 的 sessions 目录：")
    print(f"  python3 bili_verify.py --acp <acp.json> --bili {out}/<protocol>/<host>_<hash>.json --db {db} --sid <sid>")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
