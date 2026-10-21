#!/bin/bash
# acp2bili 完整性自检（v19）
set -e
cd "$(dirname "$0")"
python3 - <<'PY'
import hashlib, pathlib, sys
FILES = [
    ("bili_convert.py", 67812, "a342568153d55404ce1f770f524e9c12", "69556b2654df72e505c16fbbb8fe9ce00276431033e35d09849b5e900758a7b8"),
    ("bili_verify.py", 17087, "7756d567d54e8f55867b1a78f8af2c25", "73f5b8c182f4d2d931710465ddcad3b18ae128b19d7b2f25d2ae25e4e1e77bf8"),
    ("bili_verify_all.py", 11055, "3107b7be4282945d3aeb8d40b1a4ce0b", "a106652c9937ea333f6ba759e579f12a24c53763bf5f8a4def8795af88e550db"),
    ("bili_wire_diff.py", 12391, "fb54a913e4eab5c02cdfeadb3d7d75ea", "b61c75adc0dd8cb4d4a2291248ddfd3b425e00b8a96e85efa518f493418bdf16"),
    ("probe_residual.py", 3986, "ae5704282a4cd9943f6055644384747f", "1b121d8eefeeac8bd580f5550fee9428ab392d8e213f8d566703e67500f27749"),
    ("backup.sh", 4872, "1dd76dfd7f0d0ba67a0635684fc017a4", "7bc1cdffdc3d1b08c56f946d7a90b2c906b8d39ccaf7aeddc53f23b8a0492b5f"),
    ("fork_state.py", 6786, "b299b282a762e5a29bd2a38ef9d0c1e2", "f0b5a78661cff28551c786892574e78a18bf41d8aaef05f39ba480cc7cddccd9"),
]
bad = []
for f, n, md5, sha in FILES:
    b = pathlib.Path(f).read_bytes()
    ok = len(b) == n and hashlib.md5(b).hexdigest() == md5 and hashlib.sha256(b).hexdigest() == sha
    print(f'{f} ({n}B) md5+sha256 ' + ('OK' if ok else 'MISMATCH'))
    if not ok: bad.append(f)
print('全部一致 ✓' if not bad else f'{len(bad)} 个不一致 ✗')
sys.exit(0 if not bad else 1)
PY
