#!/bin/bash
# 校验本目录核心文件与发布版一致（bash sha_check.sh）
fail=0

f="bili_convert.py"
want_md5="8a9825ea07149f58ced92c8e9ab997c9"
want_sha256="923c4b4ab7ef923d6321b159b42089448467ff0f5018da12aa6d7c4264a63de4"
got=$(md5 -q "$f" 2>/dev/null || md5sum "$f" | cut -d" " -f1)
if [ "$got" = "$want_md5" ]; then
  echo "OK   $f  md5=$got"
else
  echo "FAIL $f  md5=$got != $want_md5"; fail=1
fi
got2=$(shasum -a 256 "$f" 2>/dev/null | cut -d" " -f1)
if [ "$got2" = "$want_sha256" ]; then
  echo "OK   $f  sha256=$got2"
else
  echo "FAIL $f  sha256=$got2 != $want_sha256"; fail=1
fi

f="bili_verify.py"
want_md5="6445c60c9bec985748eea39898894a35"
want_sha256="13c53cb791f136db3f59de3f8df196d395b0907c59211fa01f0d0210e8cb83ef"
got=$(md5 -q "$f" 2>/dev/null || md5sum "$f" | cut -d" " -f1)
if [ "$got" = "$want_md5" ]; then
  echo "OK   $f  md5=$got"
else
  echo "FAIL $f  md5=$got != $want_md5"; fail=1
fi
got2=$(shasum -a 256 "$f" 2>/dev/null | cut -d" " -f1)
if [ "$got2" = "$want_sha256" ]; then
  echo "OK   $f  sha256=$got2"
else
  echo "FAIL $f  sha256=$got2 != $want_sha256"; fail=1
fi

f="bili_verify_all.py"
want_md5="7a35e0ea3d1975bbc86aad41d7cc213b"
want_sha256="c6cb115775ceccc93713424855caee6cf589a8fb3ff8b9cdd60b313361ad6dc5"
got=$(md5 -q "$f" 2>/dev/null || md5sum "$f" | cut -d" " -f1)
if [ "$got" = "$want_md5" ]; then
  echo "OK   $f  md5=$got"
else
  echo "FAIL $f  md5=$got != $want_md5"; fail=1
fi
got2=$(shasum -a 256 "$f" 2>/dev/null | cut -d" " -f1)
if [ "$got2" = "$want_sha256" ]; then
  echo "OK   $f  sha256=$got2"
else
  echo "FAIL $f  sha256=$got2 != $want_sha256"; fail=1
fi

f="bili_wire_diff.py"
want_md5="fb54a913e4eab5c02cdfeadb3d7d75ea"
want_sha256="b61c75adc0dd8cb4d4a2291248ddfd3b425e00b8a96e85efa518f493418bdf16"
got=$(md5 -q "$f" 2>/dev/null || md5sum "$f" | cut -d" " -f1)
if [ "$got" = "$want_md5" ]; then
  echo "OK   $f  md5=$got"
else
  echo "FAIL $f  md5=$got != $want_md5"; fail=1
fi
got2=$(shasum -a 256 "$f" 2>/dev/null | cut -d" " -f1)
if [ "$got2" = "$want_sha256" ]; then
  echo "OK   $f  sha256=$got2"
else
  echo "FAIL $f  sha256=$got2 != $want_sha256"; fail=1
fi

f="probe_residual.py"
want_md5="ae5704282a4cd9943f6055644384747f"
want_sha256="1b121d8eefeeac8bd580f5550fee9428ab392d8e213f8d566703e67500f27749"
got=$(md5 -q "$f" 2>/dev/null || md5sum "$f" | cut -d" " -f1)
if [ "$got" = "$want_md5" ]; then
  echo "OK   $f  md5=$got"
else
  echo "FAIL $f  md5=$got != $want_md5"; fail=1
fi
got2=$(shasum -a 256 "$f" 2>/dev/null | cut -d" " -f1)
if [ "$got2" = "$want_sha256" ]; then
  echo "OK   $f  sha256=$got2"
else
  echo "FAIL $f  sha256=$got2 != $want_sha256"; fail=1
fi

f="backup.sh"
want_md5="def948379b2d0cd26a14b1726df4a799"
want_sha256="aeadd1ca43003aa354f2718365a63b96dc9ceed392c445bc7f848d36345b1154"
got=$(md5 -q "$f" 2>/dev/null || md5sum "$f" | cut -d" " -f1)
if [ "$got" = "$want_md5" ]; then
  echo "OK   $f  md5=$got"
else
  echo "FAIL $f  md5=$got != $want_md5"; fail=1
fi
got2=$(shasum -a 256 "$f" 2>/dev/null | cut -d" " -f1)
if [ "$got2" = "$want_sha256" ]; then
  echo "OK   $f  sha256=$got2"
else
  echo "FAIL $f  sha256=$got2 != $want_sha256"; fail=1
fi

[ "$fail" = 0 ] && echo "== 全部一致 ==" || echo "== 有文件不一致 =="
exit $fail
