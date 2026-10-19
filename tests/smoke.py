#!/usr/bin/env python3
"""冒烟测试：三种输入布局下 bili_convert.py 的行为。

覆盖的回归点（都踩过）：
  A 新机器：状态还在默认目录 → --quarantine 搬过来并转换
  B 已经隔离过的机器：默认目录空、状态在 ~/acp-state-quarantine
    → 未显式给 --acp-dir 时应自动回退（commit ef351be 修的）
  C 两处都空：应退出并打印三处计数（不能静默成功）

运行：python3 tests/smoke.py           （用临时目录，不碰本机数据）
"""
import json, os, pathlib, shutil, sqlite3, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SID = "ses_smoke0000000000000000000"


def make_db(path):
    db = sqlite3.connect(path)
    db.executescript("""
      create table message(id text primary key, session_id text, time_created int, time_updated int, data text);
      create table part(id text primary key, message_id text, session_id text, time_created int, time_updated int, data text);
      create table session(id text primary key, project_id text, model text, title text);
    """)
    msgs = [
        ("msg_u1", "user", 1, [{"type": "text", "text": "hello"}]),
        ("msg_a1", "assistant", 2, [{"type": "reasoning", "text": "think"},
                                    {"type": "text", "text": "world"}]),
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
            "blocksById": {"1": {"blockId": 1, "runId": 1, "tier": 1, "active": True,
                                 "summary": "s", "directMessageIds": ["msg_u1", "msg_a1"],
                                 "startId": "m00001", "endId": "m00002"}},
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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n冒烟测试：" + ("全部通过 ✓" if ok else "有失败 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
