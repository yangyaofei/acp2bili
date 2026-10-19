#!/bin/bash
# acp2bili 完整性自检（v8：跨平台，用 python3 计算哈希，不依赖 md5/md5sum）
set -e
cd "$(dirname "$0")"
python3 - <<'PY'
import hashlib, pathlib, sys
FILES = [
    ("bili_convert.py", 54837, "cf6e2304188de190f0543a9c61d6b485", "a7614dc45246a38a1487ca9b593e1460facab7fb50430b2f08c426d1db685608"),
    ("bili_verify.py", 17087, "7756d567d54e8f55867b1a78f8af2c25", "73f5b8c182f4d2d931710465ddcad3b18ae128b19d7b2f25d2ae25e4e1e77bf8"),
    ("bili_verify_all.py", 8925, "1bf1b5b6516d60588bf21fae9ce314a0", "6f4735fca0e5ff6ebe69aa65dd4ce2e2c2a8c8ee023a5e4efd5a26c54530118d"),
    ("bili_wire_diff.py", 12391, "fb54a913e4eab5c02cdfeadb3d7d75ea", "b61c75adc0dd8cb4d4a2291248ddfd3b425e00b8a96e85efa518f493418bdf16"),
    ("probe_residual.py", 3986, "ae5704282a4cd9943f6055644384747f", "1b121d8eefeeac8bd580f5550fee9428ab392d8e213f8d566703e67500f27749"),
    ("backup.sh", 4872, "1dd76dfd7f0d0ba67a0635684fc017a4", "7bc1cdffdc3d1b08c56f946d7a90b2c906b8d39ccaf7aeddc53f23b8a0492b5f"),
]
bad = []
for f, n, md5, sha in FILES:
    b = pathlib.Path(f).read_bytes()
    ok_n = len(b) == n
    ok_md5 = hashlib.md5(b).hexdigest() == md5
    ok_sha = hashlib.sha256(b).hexdigest() == sha
    if ok_n and ok_md5 and ok_sha:
        print(f'{f} ({n}B) md5+sha256 OK')
    else:
        bad.append(f)
        s1 = 'OK' if ok_md5 else 'BAD'
        s2 = 'OK' if ok_sha else 'BAD'
        print(f'{f} MISMATCH: 字节 {len(b)}/{n}  md5 {s1}  sha256 {s2}')
print('全部一致 ✓' if not bad else f'{len(bad)} 个不一致 ✗ —— 别跑，先 git pull 或重传')
sys.exit(0 if not bad else 1)
PY
