#!/bin/bash
# acp2bili 完整性自检（v7：runtime/ 布局）
# 逐文件 md5 + sha256 双校验；任何不一致立即退出。
set -e
cd "$(dirname "$0")"
FAIL=0

echo -n "bili_convert.py (53414B) md5: ";  [ "$(md5 -q bili_convert.py)" = "4b355dbae9c6311d3f851aaffa7ddf70" ] && echo OK || { echo "MISMATCH（期望 4b355dbae9c6311d3f851aaffa7ddf70）"; FAIL=1; }
echo -n "bili_convert.py sha256: "; [ "$(shasum -a 256 bili_convert.py | cut -d" " -f1)" = "062cc497d055970e40d4d2505d6a05aabf925e567fc1e0af9643ce5810d6f1ff" ] && echo OK || { echo "MISMATCH（期望 062cc497d055970e40d4d2505d6a05aabf925e567fc1e0af9643ce5810d6f1ff）"; FAIL=1; }

echo -n "bili_verify.py (17087B) md5: ";  [ "$(md5 -q bili_verify.py)" = "7756d567d54e8f55867b1a78f8af2c25" ] && echo OK || { echo "MISMATCH（期望 7756d567d54e8f55867b1a78f8af2c25）"; FAIL=1; }
echo -n "bili_verify.py sha256: "; [ "$(shasum -a 256 bili_verify.py | cut -d" " -f1)" = "73f5b8c182f4d2d931710465ddcad3b18ae128b19d7b2f25d2ae25e4e1e77bf8" ] && echo OK || { echo "MISMATCH（期望 73f5b8c182f4d2d931710465ddcad3b18ae128b19d7b2f25d2ae25e4e1e77bf8）"; FAIL=1; }

echo -n "bili_verify_all.py (8925B) md5: ";  [ "$(md5 -q bili_verify_all.py)" = "1bf1b5b6516d60588bf21fae9ce314a0" ] && echo OK || { echo "MISMATCH（期望 1bf1b5b6516d60588bf21fae9ce314a0）"; FAIL=1; }
echo -n "bili_verify_all.py sha256: "; [ "$(shasum -a 256 bili_verify_all.py | cut -d" " -f1)" = "6f4735fca0e5ff6ebe69aa65dd4ce2e2c2a8c8ee023a5e4efd5a26c54530118d" ] && echo OK || { echo "MISMATCH（期望 6f4735fca0e5ff6ebe69aa65dd4ce2e2c2a8c8ee023a5e4efd5a26c54530118d）"; FAIL=1; }

echo -n "bili_wire_diff.py (12391B) md5: ";  [ "$(md5 -q bili_wire_diff.py)" = "fb54a913e4eab5c02cdfeadb3d7d75ea" ] && echo OK || { echo "MISMATCH（期望 fb54a913e4eab5c02cdfeadb3d7d75ea）"; FAIL=1; }
echo -n "bili_wire_diff.py sha256: "; [ "$(shasum -a 256 bili_wire_diff.py | cut -d" " -f1)" = "b61c75adc0dd8cb4d4a2291248ddfd3b425e00b8a96e85efa518f493418bdf16" ] && echo OK || { echo "MISMATCH（期望 b61c75adc0dd8cb4d4a2291248ddfd3b425e00b8a96e85efa518f493418bdf16）"; FAIL=1; }

echo -n "probe_residual.py (3986B) md5: ";  [ "$(md5 -q probe_residual.py)" = "ae5704282a4cd9943f6055644384747f" ] && echo OK || { echo "MISMATCH（期望 ae5704282a4cd9943f6055644384747f）"; FAIL=1; }
echo -n "probe_residual.py sha256: "; [ "$(shasum -a 256 probe_residual.py | cut -d" " -f1)" = "1b121d8eefeeac8bd580f5550fee9428ab392d8e213f8d566703e67500f27749" ] && echo OK || { echo "MISMATCH（期望 1b121d8eefeeac8bd580f5550fee9428ab392d8e213f8d566703e67500f27749）"; FAIL=1; }

echo -n "backup.sh (4872B) md5: ";  [ "$(md5 -q backup.sh)" = "1dd76dfd7f0d0ba67a0635684fc017a4" ] && echo OK || { echo "MISMATCH（期望 1dd76dfd7f0d0ba67a0635684fc017a4）"; FAIL=1; }
echo -n "backup.sh sha256: "; [ "$(shasum -a 256 backup.sh | cut -d" " -f1)" = "7bc1cdffdc3d1b08c56f946d7a90b2c906b8d39ccaf7aeddc53f23b8a0492b5f" ] && echo OK || { echo "MISMATCH（期望 7bc1cdffdc3d1b08c56f946d7a90b2c906b8d39ccaf7aeddc53f23b8a0492b5f）"; FAIL=1; }

if [ "$FAIL" = 0 ]; then echo "全部一致 ✓"; else echo "存在不一致 ✗ —— 别跑，先重新 git pull / 重传"; exit 1; fi
