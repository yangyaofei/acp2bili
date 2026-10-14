#!/bin/bash
# acp2bili 完整性自检（v6：修 EMFILE）
# 逐文件 md5 + sha256 双校验；任何不一致立即退出。
set -e
cd "$(dirname "$0")"
FAIL=0

echo -n "bili_convert.py (53144B) md5: ";  [ "$(md5 -q bili_convert.py)" = "9ed985b421dc6342258c0ea187c05474" ] && echo OK || { echo "MISMATCH（期望 9ed985b421dc6342258c0ea187c05474）"; FAIL=1; }
echo -n "bili_convert.py sha256: "; [ "$(shasum -a 256 bili_convert.py | cut -d" " -f1)" = "c05de6489b7c22e2f9b5f0ed7ff1327ce54d7e4bf43f4e686119ad252ddec18e" ] && echo OK || { echo "MISMATCH（期望 c05de6489b7c22e2f9b5f0ed7ff1327ce54d7e4bf43f4e686119ad252ddec18e）"; FAIL=1; }

echo -n "bili_verify.py (17087B) md5: ";  [ "$(md5 -q bili_verify.py)" = "7756d567d54e8f55867b1a78f8af2c25" ] && echo OK || { echo "MISMATCH（期望 7756d567d54e8f55867b1a78f8af2c25）"; FAIL=1; }
echo -n "bili_verify.py sha256: "; [ "$(shasum -a 256 bili_verify.py | cut -d" " -f1)" = "73f5b8c182f4d2d931710465ddcad3b18ae128b19d7b2f25d2ae25e4e1e77bf8" ] && echo OK || { echo "MISMATCH（期望 73f5b8c182f4d2d931710465ddcad3b18ae128b19d7b2f25d2ae25e4e1e77bf8）"; FAIL=1; }

echo -n "bili_verify_all.py (8568B) md5: ";  [ "$(md5 -q bili_verify_all.py)" = "7a35e0ea3d1975bbc86aad41d7cc213b" ] && echo OK || { echo "MISMATCH（期望 7a35e0ea3d1975bbc86aad41d7cc213b）"; FAIL=1; }
echo -n "bili_verify_all.py sha256: "; [ "$(shasum -a 256 bili_verify_all.py | cut -d" " -f1)" = "c6cb115775ceccc93713424855caee6cf589a8fb3ff8b9cdd60b313361ad6dc5" ] && echo OK || { echo "MISMATCH（期望 c6cb115775ceccc93713424855caee6cf589a8fb3ff8b9cdd60b313361ad6dc5）"; FAIL=1; }

echo -n "bili_wire_diff.py (12391B) md5: ";  [ "$(md5 -q bili_wire_diff.py)" = "fb54a913e4eab5c02cdfeadb3d7d75ea" ] && echo OK || { echo "MISMATCH（期望 fb54a913e4eab5c02cdfeadb3d7d75ea）"; FAIL=1; }
echo -n "bili_wire_diff.py sha256: "; [ "$(shasum -a 256 bili_wire_diff.py | cut -d" " -f1)" = "b61c75adc0dd8cb4d4a2291248ddfd3b425e00b8a96e85efa518f493418bdf16" ] && echo OK || { echo "MISMATCH（期望 b61c75adc0dd8cb4d4a2291248ddfd3b425e00b8a96e85efa518f493418bdf16）"; FAIL=1; }

echo -n "probe_residual.py (3986B) md5: ";  [ "$(md5 -q probe_residual.py)" = "ae5704282a4cd9943f6055644384747f" ] && echo OK || { echo "MISMATCH（期望 ae5704282a4cd9943f6055644384747f）"; FAIL=1; }
echo -n "probe_residual.py sha256: "; [ "$(shasum -a 256 probe_residual.py | cut -d" " -f1)" = "1b121d8eefeeac8bd580f5550fee9428ab392d8e213f8d566703e67500f27749" ] && echo OK || { echo "MISMATCH（期望 1b121d8eefeeac8bd580f5550fee9428ab392d8e213f8d566703e67500f27749）"; FAIL=1; }

echo -n "backup.sh (4675B) md5: ";  [ "$(md5 -q backup.sh)" = "def948379b2d0cd26a14b1726df4a799" ] && echo OK || { echo "MISMATCH（期望 def948379b2d0cd26a14b1726df4a799）"; FAIL=1; }
echo -n "backup.sh sha256: "; [ "$(shasum -a 256 backup.sh | cut -d" " -f1)" = "aeadd1ca43003aa354f2718365a63b96dc9ceed392c445bc7f848d36345b1154" ] && echo OK || { echo "MISMATCH（期望 aeadd1ca43003aa354f2718365a63b96dc9ceed392c445bc7f848d36345b1154）"; FAIL=1; }

if [ "$FAIL" = 0 ]; then echo "全部一致 ✓"; else echo "存在不一致 ✗ —— 别跑，先重新 git pull / 重传"; exit 1; fi
