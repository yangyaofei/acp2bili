#!/bin/bash
# 校验 acp2bili 各脚本是否是当前版本（在脚本所在目录运行）。用法：bash sha_check.sh
set -u
cd "$(dirname "$0")" || exit 1

expect() {   # expect <文件> <md5> <sha256> <字节数>
  f=$1; want_md5=$2; want_sha=$3; want_size=$4
  if [ ! -f "$f" ]; then
    printf '%-20s [缺失] 这个文件不在当前目录\n' "$f"; return
  fi
  md5=$(md5 -q "$f" 2>/dev/null || md5sum "$f" | cut -d' ' -f1)
  sha=$(shasum -a 256 "$f" | cut -d' ' -f1)
  size=$(wc -c < "$f" | tr -d ' ')
  if [ "$md5" = "$want_md5" ] && [ "$sha" = "$want_sha" ]; then
    printf '%-20s [一致]  md5=%s  sha256=%s  %sB\n' "$f" "$md5" "$sha" "$size"
  else
    printf '%-20s [不一致] —— 不是当前版本（git pull 或重新下载）\n' "$f"
    printf '        本机 md5=%s sha256=%s 字节=%s\n' "$md5" "$sha" "$size"
    printf '        期望 md5=%s sha256=%s 字节=%s\n' "$want_md5" "$want_sha" "$want_size"
  fi
}

# v4.3（2026-10-15）：verify_all 的 --acp 增加 --acp-dir 等价别名（转换器叫 --acp-dir，历史叫 --acp，两个都收）
expect bili_convert.py     58496d1ec57c6bea9a0b80c959f6ce9e 0fc558a475b39f540c7ed077df4f831353790c62220b403d0c6b6b9d6340e920 38350
expect bili_verify.py      fca07b9d4f4c7ac1012cf08d3362ec4e 820a9fcdf76f2c17245e23d0069de85a8b8220dfdd94d990977afdead13d29ef 16071
expect bili_verify_all.py  7a35e0ea3d1975bbc86aad41d7cc213b c6cb115775ceccc93713424855caee6cf589a8fb3ff8b9cdd60b313361ad6dc5 8568
expect bili_wire_diff.py   fb54a913e4eab5c02cdfeadb3d7d75ea b61c75adc0dd8cb4d4a2291248ddfd3b425e00b8a96e85efa518f493418bdf16 12391
expect probe_residual.py   ae5704282a4cd9943f6055644384747f 1b121d8eefeeac8bd580f5550fee9428ab392d8e213f8d566703e67500f27749 3986

echo
echo "全部 [一致] → 直接用；任何 [不一致] → git pull（或重新下载）后再跑"
