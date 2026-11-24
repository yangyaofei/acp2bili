#!/usr/bin/env python3
"""编号规则的单测：拿 bili 自己算出的结果对照本仓库的 Python 实现。

为什么需要：转换器和校验器用同一套算法，算法错了两边一起错。bili 0.1.202 改编号规则时，
旧产物就是这样带着"校验通过"装进去的。所以这里的期望值不是本仓库算的。

期望值怎么来的：从 bili 0.1.216 的 dist/index.js 里原样取出 canonicalToolArgs、canonicalize、
hashId2、deriveCanonicalMessageId 四个函数，在 Node 里对下面的输入各算一次。
bili 换版本后要重新生成：用同样的方法取函数、算一遍、替换期望值，并把 bili_convert.py 的
BILI_ID_RULE_VERSIONS 改成新验证过的范围。

运行：python3 tests/test_ids.py
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bili_convert as B  # noqa: E402

# ((role, contentType, text, toolName), core id)。tool-call 的 text 是未规范化的 args 原文。
ID_VECTORS = (
    (("assistant", "tool-call", '{"command":"echo hi","n":1.0,"opts":{"b":2,"a":1}}', "bash"),
     "h_6d45e19bb8cfa206"),
    (("tool", "tool-result", "ok \ud83d", "bash"), "h_9654ca04ef5a936e"),
    (("assistant", "reasoning", "think", None), "h_8a858c71e0710e70"),
    (("assistant", "text", "world", None), "h_8a312c34980daa6b"),
    (("assistant", "text", "thinkworld", None), "h_feae31ac076b5442"),
    (("assistant", "tool-call", '{"a":1,"b":100,"f":1e-7,"k":"bad \\ud83d end","u":100000000000000000000}', "t"),
     "h_283367019b28d5d3"),
)

# (args 原文, bili 的 canonicalToolArgs 结果)。标"原样"的是 bili 解析失败、原样返回。
CANON_VECTORS = (
    ('{"z":1,"a":{"y":[3,{"b":2,"a":1}],"x":null},"m":"中文 é ü \\ \\" \n\t"}',
     '{"z":1,"a":{"y":[3,{"b":2,"a":1}],"x":null},"m":"中文 é ü \\ \\" \n\t"}'),          # 原样（字符串转义）
    ("not json at all", "not json at all"),                                             # 原样
    (' { "b" : 1 , "a" : 2 } ', '{"a":2,"b":1}'),
    ('{"a":1,"a":2}', '{"a":2}'),                                                       # 重复键取后一个
    ('\ufeff{"a":1}\xa0', '{"a":1}'),                                                   # 首尾的 BOM、不换行空格要 trim
    ('{"n":1e400,"m":9007199254740993,"o":0.1,"p":1e-6,"q":123e-20,"r":1.7976931348623157e308,'
     '"s":5e-324,"t":-1.5,"u":1e20,"v":1e15,"w":123456.7e3,"y":-0,"z":0.000001234,'
     '"aa":-123.456e-9,"ab":4503599627370497.5,"ac":0.30000000000000004,'
     '"ad":123456789012345678901234567890}',
     '{"aa":-1.23456e-7,"ab":4503599627370498,"ac":0.30000000000000004,"ad":1.2345678901234568e+29,'
     '"m":9007199254740992,"n":null,"o":0.1,"p":0.000001,"q":1.23e-18,"r":1.7976931348623157e+308,'
     '"s":5e-324,"t":-1.5,"u":100000000000000000000,"v":1000000000000000,"w":123456700,"y":0,'
     '"z":0.000001234}'),                                                               # 数字按 JS 规则打印
    ('{"\\ud83d\\ude00":1,"\\ue000":1,"b":1,"B":1,"10":2,"2":1,"1":1,"\\u00e9":1,"\\uffff":1,'
     '"\\ud7ff":1,"\\ue000\\ud83d\\ude00":1}',
     '{"1":1,"2":1,"10":2,"B":1,"b":1,"é":1,"\ud7ff":1,"😀":1,"\ue000":1,"\ue000😀":1,"\uffff":1}'),
    ('[3,{"b":1,"a":2},[1.0,"x\\u0001\\u2028\\ud83d"]]',
     '[3,{"a":2,"b":1},[1,"x\\u0001\u2028\\ud83d"]]'),                                  # 孤立代理项输出成转义
    ('{"huge":' + "9" * 400 + "}", '{"huge":null}'),                                   # 超出双精度 → Infinity → null
    ('{"ok":NaN}', '{"ok":NaN}'),                                                       # 原样（NaN 不是合法 JSON）
    ('{"s":"tab\\t\\b\\f\\r\\/\\\\"}', '{"s":"tab\\t\\b\\f\\r/\\\\"}'),
)


def main():
    ok = True

    def report(name, bad):
        nonlocal ok
        print(("PASS " if not bad else "FAIL ") + name)
        for b in bad[:3]:
            print("      " + b)
        ok = ok and not bad

    report("自检样例（bili_convert.ID_SELFTEST）", B.selftest_ids())

    bad = []
    for (role, ct, text, tname), want in ID_VECTORS:
        got = B.derive(role, ct, B.canonical_tool_args(text) if ct == "tool-call" else text, "", tname)
        if got != want:
            bad.append(f"{role}/{ct} {text[:40]!r}：算出 {got}，bili 是 {want}")
    report(f"core id 与 bili 一致（{len(ID_VECTORS)} 条）", bad)

    bad = []
    for args, want in CANON_VECTORS:
        got = B.canonical_tool_args(args)
        if got != want:
            bad.append(f"{args[:50]!r}：算出 {got[:80]!r}，bili 是 {want[:80]!r}")
    report(f"canonicalToolArgs 与 bili 一致（{len(CANON_VECTORS)} 条）", bad)

    # toolCallId 不进 seed：同一个工具调用换 call id，编号不变
    a = B.derive("assistant", "tool-call", '{"command":"ls -la"}', "call_a", "bash")
    b = B.derive("assistant", "tool-call", '{"command":"ls -la"}', "call_b", "bash")
    report("toolCallId 不参与编号", [] if a == b else [f"{a} ≠ {b}"])

    # 孤立代理项换成 U+FFFD、能按 UTF-8 写盘；成对的代理项（emoji）保持原样
    pair = "\U0001F600"          # 😀；写成相邻的两个代理项码点时也算它（JS 的 UTF-16 语义）
    cleaned = B.json_safe({"a": ["x\ud83dy", {"k\udc00": 1}], "s": pair + " ok", "p": "\ud83d\ude00!"})
    try:
        blob = json.dumps(cleaned, ensure_ascii=False).encode("utf-8")
        bad = [] if (cleaned["a"][0] == "x\ufffdy" and list(cleaned["a"][1]) == ["k\ufffd"]
                     and cleaned["s"] == pair + " ok" and cleaned["p"] == pair + "!") \
            else ["替换结果不对：" + ascii(cleaned)]
    except UnicodeEncodeError as e:
        bad, blob = [f"仍无法编码：{e}"], b""
    report(f"孤立代理项替换为 U+FFFD（{len(blob)} 字节可写盘）", bad)

    print("\n编号单测：" + ("全部通过 ✓" if ok else "有失败 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
