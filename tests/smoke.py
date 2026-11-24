#!/usr/bin/env python3
"""冒烟测试：三种输入布局下 bili_convert.py 的行为，外加 bili_wire_filter.py。

覆盖的回归点（都踩过）：
   A 新机器：状态还在默认目录 → --quarantine 搬过来并转换；
     产物的编号与 bili 自己算的一致，文本里有残缺 emoji 也能写出合法的 UTF-8
   B 已经隔离过的机器：默认目录空、状态在 ~/acp-state-quarantine
     → 未显式给 --acp-dir 时应自动回退（commit ef351be 修的）
   C 两处都空：应退出并打印三处计数（不能静默成功）
   D --dry-run + --compare：同一批产物应判「一致」，且不落盘
   E bili_wire_filter.py：只保留 bili 实际见过的编号，ref 账本重新连续编号；
     bili 库里没有这个会话时拒绝写出

运行：python3 tests/smoke.py           （用临时目录，不碰本机数据）
"""
import json, os, pathlib, shutil, sqlite3, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SID = "ses_smoke0000000000000000000"

# 产物里应当出现的全部编号。期望值不是本仓库算的，生成方法见 tests/test_ids.py。
EXPECTED_IDS = {
    "h_2708c80e9ecf5970",  # user "hello"
    "h_8a858c71e0710e70",  # reasoning "think"
    "h_8a312c34980daa6b",  # text "world"
    "h_feae31ac076b5442",  # fold 候选：reasoning 与 text 拼成一条
    "h_6d45e19bb8cfa206",  # tool-call：args 规范化后 {"command":"echo hi","n":1,"opts":{"a":1,"b":2}}
    "h_9654ca04ef5a936e",  # tool-result "ok <残缺 emoji>"：按 U+FFFD 计
}


def make_db(path):
    db = sqlite3.connect(path)
    db.executescript("""
      create table message(id text primary key, session_id text, time_created int, time_updated int, data text);
      create table part(id text primary key, message_id text, session_id text, time_created int, time_updated int, data text);
      create table session(id text primary key, project_id text, model text, title text);
    """)
    msgs = [
        ("msg_u1", "user", 1, [{"type": "text", "text": "hello"}]),
        ("msg_a1", "assistant", 2, [
            {"type": "reasoning", "text": "think"},
            {"type": "text", "text": "world"},
            # 工具调用 + 结果：编号里最容易算错的部分（args 规范化、toolName 进 seed）。
            # 结果里放一个残缺的 emoji（孤立代理项），确认整条链路不会因此写不出文件。
            {"type": "tool", "callID": "call_1", "tool": "bash",
             "state": {"status": "completed",
                       "input": {"command": "echo hi", "n": 1.0, "opts": {"b": 2, "a": 1}},
                       "output": "ok \ud83d"}},
        ]),
    ]
    for i, (mid, role, t, parts) in enumerate(msgs):
        db.execute("insert into message values (?,?,?,?,?)",
                   (mid, SID, t, t, json.dumps(
                       {"role": role, "providerID": "deepseek-litellm", "modelID": "deepseek-flash"})))
        for j, p in enumerate(parts):
            db.execute("insert into part values (?,?,?,?,?,?)",
                       (f"prt_{mid}_{j}", mid, SID, t, t, json.dumps(p)))
    db.execute("insert into session values (?,?,?,?)",
               (SID, "p1", json.dumps({"providerID": "deepseek-litellm", "modelID": "deepseek-flash"}), "smoke"))
    db.commit()
    db.close()


def make_state(directory):
    pathlib.Path(directory).mkdir(parents=True, exist_ok=True)
    state = {
        "prune": {"messages": {
            # 只折叠 user 那条：assistant 消息（带工具结果）留在尾段，才能查到写出的文本
            "blocksById": {"1": {"blockId": 1, "runId": 1, "tier": 1, "active": True,
                                 "summary": "s", "directMessageIds": ["msg_u1"],
                                 "startId": "m00001", "endId": "m00001"}},
            "activeBlockIds": [1], "nextBlockId": 2, "nextRunId": 2}},
        "stats": {"totalPruneTokens": 10},
        "messageIds": {"byRawId": {"msg_u1": "m00001", "msg_a1": "m00002"},
                       "byRef": {"m00001": "msg_u1", "m00002": "msg_a1"}, "nextRef": 3},
    }
    p = pathlib.Path(directory) / f"{SID}.json"
    p.write_text(json.dumps(state))
    return p


def run(args, env):
    return subprocess.run([sys.executable, str(ROOT / "bili_convert.py")] + args,
                          capture_output=True, text=True, env=env, cwd=ROOT)


def check_product(tmp):
    """A 的产物：编号与 bili 一致、残缺 emoji 已替换、文件是合法 UTF-8。返回是否通过。"""
    files = sorted((tmp / "runtime" / "out").rglob("*.json"))
    if len(files) != 1:
        print(f"      产物数不对：{len(files)}")
        return False
    try:
        doc = json.loads(files[0].read_bytes().decode("utf-8"))     # 非法 UTF-8 在这一步就会暴露
    except UnicodeDecodeError as e:
        print(f"      产物不是合法 UTF-8：{e}")
        return False
    payload = doc["payload"]
    ids = set(payload["state"]["messageRefs"]["byRaw"])
    if ids != EXPECTED_IDS:
        print(f"      编号不一致：多 {sorted(ids - EXPECTED_IDS)}，少 {sorted(EXPECTED_IDS - ids)}")
        return False
    last = payload["messages"][-1]
    if last["contentType"] != "tool-result" or last["text"] != "ok \ufffd" or last["id"] != "h_9654ca04ef5a936e":
        print(f"      工具结果不对：{ascii(last)}")
        return False
    return True


def check_wire_filter(tmp):
    """E：bili_wire_filter.py。返回 (是否通过, 说明)。"""
    wf = tmp / "wire-filter"
    wf.mkdir()
    src = wf / "in.json"
    doc = {"version": 3, "id": SID, "payload": {
        "version": 3, "id": SID,
        "messages": [{"id": "h_a", "role": "user", "contentType": "text", "text": "a"},
                     {"id": "h_dead", "role": "user", "contentType": "text", "text": "x"}],
        "metadata": {"conversion": "predict"},
        "state": {
            "blocks": [
                # b1 两个编号 bili 都见过，全部保留
                {"blockId": "b1", "tier": 1, "active": True, "summary": "s",
                 "directMessageIds": ["h_a"], "effectiveMessageIds": ["h_a", "h_dead"],
                 "startRef": "m00001", "endRef": "m00002"},
                # b2 的编号 bili 从没见过：过滤后变空，ref 要摘掉
                {"blockId": "b2", "tier": 1, "active": True, "summary": "s",
                 "directMessageIds": ["h_gone"], "effectiveMessageIds": ["h_gone"],
                 "startRef": "m00003", "endRef": "m00003"}],
            "messageRefs": {"byRaw": {"h_a": "m00001", "h_dead": "m00002", "h_gone": "m00003"},
                            "byRef": {"m00001": "h_a", "m00002": "h_dead", "m00003": "h_gone"}},
            "lastPassIds": ["h_a", "h_dead"]}}}
    src.write_text(json.dumps(doc))
    original = src.read_bytes()
    bdb = wf / "bili.db"
    con = sqlite3.connect(bdb)
    con.execute("create table messages(session_id text, seq int, msg_id text, ts int)")
    con.executemany("insert into messages values (?,?,?,?)",
                    [(SID, 0, "h_a", 111), (SID, 1, "h_dead", 111), ("ses_other", 0, "h_a", 111)])
    con.commit()
    con.close()

    def filt(out, *extra):
        return subprocess.run([sys.executable, str(ROOT / "bili_wire_filter.py"),
                               "--product", str(src), "--out", str(out), "--sid", SID,
                               "--bili-db", str(bdb), *extra],
                              capture_output=True, text=True, cwd=ROOT)

    out = wf / "out.json"
    r = filt(out, "--ts", "111")
    if r.returncode != 0 or not out.exists():
        return False, f"退出码 {r.returncode}\n{r.stdout[-600:]}{r.stderr[-400:]}"
    p = json.loads(out.read_text())["payload"]
    b1, b2 = p["state"]["blocks"]
    got = (b1["effectiveMessageIds"], (b1["startRef"], b1["endRef"]),
           b2["effectiveMessageIds"], "startRef" in b2,
           p["state"]["messageRefs"], p["state"]["lastPassIds"],
           [m["id"] for m in p["messages"]], p["metadata"].get("wireFilter"))
    want = (["h_a", "h_dead"], ("m00001", "m00002"), [], False,
            {"byRaw": {"h_a": "m00001", "h_dead": "m00002"},
             "byRef": {"m00001": "h_a", "m00002": "h_dead"}},
            ["h_a", "h_dead"], ["h_a", "h_dead"], "ts=111; kept=2; dropped_blocks=1")
    if got != want:
        return False, f"过滤结果不对：\n  得到 {got}\n  应为 {want}"
    if src.read_bytes() != original:
        return False, "输入产物被改动了"
    # bili 库里没有这次请求的编号 → 拒绝写出
    out2 = wf / "refused.json"
    r = filt(out2, "--ts", "222")
    if r.returncode == 0 or out2.exists():
        return False, "没有编号时应当拒绝写出"
    # 输出不能覆盖输入
    r = filt(src)
    if r.returncode != 2 or src.read_bytes() != original:
        return False, "--out 与 --product 相同时应当拒绝"
    return True, ""


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="acp2bili-smoke-"))
    ok = True
    try:
        db = tmp / "opencode.db"; make_db(db)
        env = dict(os.environ)
        env.update({"HOME": str(tmp / "home"), "XDG_DATA_HOME": str(tmp / "xdg"),
                    "ACP2BILI_RUNTIME": str(tmp / "runtime"),
                    "XDG_CONFIG_HOME": str(tmp / "cfgnone")})
        (tmp / "home").mkdir(); (tmp / "xdg").mkdir()

        # A：状态在默认目录，--quarantine 搬运
        default_dir = pathlib.Path(env["XDG_DATA_HOME"]) / "opencode/storage/plugin/acp"
        make_state(default_dir)
        r = run(["--auto", "--all", "--quarantine", "--no-fetch-models", "--db", str(db),
                 "--acp-dir", str(tmp / "state-a")], env)
        a_ok = r.returncode == 0 and "已隔离 1 个" in r.stdout
        print(("PASS " if a_ok else "FAIL ") + "A 新机器：--quarantine 搬运并转换")
        if not a_ok:
            print(r.stdout[-800:], r.stderr[-400:]); ok = False

        # A+：产物本身——编号、残缺 emoji、UTF-8
        p_ok = check_product(tmp)
        print(("PASS " if p_ok else "FAIL ") + "A+ 产物的编号与 bili 一致，含残缺 emoji 也能写出合法 UTF-8")
        if not p_ok:
            ok = False

        # B：已经隔离过，默认目录空；不给 --acp-dir
        shutil.rmtree(default_dir, ignore_errors=True)
        make_state(pathlib.Path(env["HOME"]) / "acp-state-quarantine")
        shutil.rmtree(tmp / "runtime", ignore_errors=True)
        r = run(["--auto", "--all", "--no-fetch-models", "--db", str(db)], env)
        b_ok = r.returncode == 0 and "自动改用旧隔离目录" in r.stdout and "产物        : 1" in r.stdout
        print(("PASS " if b_ok else "FAIL ") + "B 已隔离过：自动回退旧目录")
        if not b_ok:
            print(r.stdout[-800:], r.stderr[-400:]); ok = False

        # C：两处都空
        shutil.rmtree(pathlib.Path(env["HOME"]) / "acp-state-quarantine", ignore_errors=True)
        r = run(["--auto", "--all", "--no-fetch-models", "--db", str(db)], env)
        c_ok = r.returncode != 0 and "没有可转换的会话" in (r.stdout + r.stderr) \
            and "旧隔离目录" in (r.stdout + r.stderr)
        print(("PASS " if c_ok else "FAIL ") + "C 两处都空：退出并打印三处计数")
        if not c_ok:
            print(r.stdout[-800:], r.stderr[-400:]); ok = False
        # D：--dry-run + --compare（同一批产物应判「一致」，且不落盘）
        before = set((tmp / "runtime" / "out").rglob("*.json"))
        r = run(["--auto", "--all", "--no-fetch-models", "--dry-run",
                 "--compare", str(tmp / "runtime" / "out"), "--acp-dir", str(tmp / "state-a"),
                 "--db", str(db)], env)
        after = set((tmp / "runtime" / "out").rglob("*.json"))
        d_ok = r.returncode == 0 and "对比结论   : 一致" in r.stdout and before == after
        print(("PASS " if d_ok else "FAIL ") + "D --dry-run + --compare 判一致且不写盘")
        if not d_ok:
            print(r.stdout[-800:], r.stderr[-400:]); ok = False
        # E：bili_wire_filter.py
        e_ok, why = check_wire_filter(tmp)
        print(("PASS " if e_ok else "FAIL ") + "E bili_wire_filter：只留见过的编号、ref 重排、无编号时拒绝")
        if not e_ok:
            print(why); ok = False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n冒烟测试：" + ("全部通过 ✓" if ok else "有失败 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
