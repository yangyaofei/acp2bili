#!/bin/bash
# acp2bili 完整性自检（v20）
set -e
cd "$(dirname "$0")"
python3 - <<'PY'
import hashlib, pathlib, sys
FILES = [
    ("bili_convert.py", 80656, "9c8093dadde07843076f1c9b13713a2a", "6984d574d6c4aa4187f11779c00d1681450bab8b88d746d8e48bd555d09f651e"),
    ("bili_verify.py", 20534, "a254f2b345675613e9f18ec65accf4ce", "cbf45e214e263a1dcdfaa19c40c5ba63730a0a601bfef110ccdd859a548b57c6"),
    ("bili_verify_all.py", 11055, "3107b7be4282945d3aeb8d40b1a4ce0b", "a106652c9937ea333f6ba759e579f12a24c53763bf5f8a4def8795af88e550db"),
    ("bili_wire_diff.py", 12391, "fb54a913e4eab5c02cdfeadb3d7d75ea", "b61c75adc0dd8cb4d4a2291248ddfd3b425e00b8a96e85efa518f493418bdf16"),
    ("bili_wire_filter.py", 9018, "170404b95bd73f13b91fbccd06b440bc", "5799d126762755dbbfe6c7baa1e4a6f0090ed8e6d143fec9ac79b45666323b3a"),
    ("probe_residual.py", 4046, "bc6247bec467786dd8095f54f36f1453", "ac5bee9d4353ae91137eff2c8c34fe5cfd55b152d7efc4aa5c04d016991af53b"),
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
