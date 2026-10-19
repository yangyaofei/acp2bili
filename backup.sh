#!/bin/bash
# opencode_bak —— opencode v1（含 acp 迁移现场）一站式备份，产物默认在 <仓库>/runtime/backup/<时间戳>_<主机>/
#
# 用法：
#   bash backup.sh            # 标准备份（库三件套 + 配置 + acp 状态 + bili 会话 + 迁移产物）
#   bash backup.sh --full     # 额外含整个 storage/ 与 ~/.cache/opencode（大，老机可能几十 GB）
#
# 备份四条铁律（来自 V1→V2 迁移调研，恢复时照做）：
#   1) 备份前退出 opencode / OpenChamber（本脚本会检测并拒绝正在写的库）
#   2) opencode.db 必须与 -wal/-shm 一起拷（或先 checkpoint）
#   3) 还原前先把现存的三个库文件删掉再覆盖（半新半旧会损坏）
#   4) 还原期间不要启动 opencode
set -eu

FULL=0; [ "${1:-}" = "--full" ] && FULL=1
STAMP=$(date "+%Y-%m-%dT%H%M%S")
HOST=$(hostname -s)
REPO="$(cd "$(dirname "$0")" && pwd)"
RUNTIME="${ACP2BILI_RUNTIME:-$REPO/runtime}"
ROOT="${ACP2BILI_BACKUP_DIR:-$RUNTIME/backup}/${STAMP}_${HOST}"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/opencode"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
BILI_DATA="${HOME}/.local/share/billion-context"
BILI_STATE="${HOME}/.local/state/billion-context"
BILI_CONF="${HOME}/.config/billion-context"
CLONE_OPT=""; [ "$(uname)" = "Darwin" ] && CLONE_OPT="-c"   # APFS 克隆，秒级不占额外空间

mkdir -p "$ROOT"/{opencode,bili,migration}
echo "== opencode_bak → $ROOT =="

# 0) 库是否正在被写
if lsof -nP "$DATA/opencode.db" >/dev/null 2>&1; then
  echo "✗ opencode.db 正被进程打开（lsof 命中）。请先退出 opencode / OpenChamber 再备份。" >&2
  exit 1
fi

# 1) SQLite 三件套（先尝试 checkpoint，让 wal 并入主库）
if [ -f "$DATA/opencode.db" ]; then
  sqlite3 "$DATA/opencode.db" "PRAGMA wal_checkpoint(PASSIVE);" 2>/dev/null || true
  for f in opencode.db opencode.db-wal opencode.db-shm; do
    [ -f "$DATA/$f" ] && cp $CLONE_OPT "$DATA/$f" "$ROOT/opencode/$f"
  done
  echo "[ok] opencode/ 库三件套 $(du -sh "$ROOT/opencode" | cut -f1)"
else
  echo "[skip] 未找到 $DATA/opencode.db"
fi

# 2) opencode 配置（含 opencode.jsonc / acp.jsonc / 插件配置；内含 apiKey，目录勿外传）
[ -d "$CONF" ] && { mkdir -p "$ROOT/opencode/config"; cp -R "$CONF"/. "$ROOT/opencode/config/"; \
  rm -rf "$ROOT/opencode/config/node_modules" "$ROOT/opencode/config/logs"; \
  echo "[ok] opencode/config/（已剔除 node_modules 与 logs）"; }

# 3) acp 状态（默认目录 + 隔离目录，两处都收）
for d in "$DATA/storage/plugin/acp" "$RUNTIME/acp-state" "$HOME/acp-state-quarantine"; do
  if [ -d "$d" ] && ls "$d"/*.json >/dev/null 2>&1; then
    n=$(ls "$d"/*.json | wc -l | tr -d ' ')
    tag=""; case "$d" in "$RUNTIME/acp-state"|"$HOME/acp-state-quarantine") tag="-quarantine";; esac
    mkdir -p "$ROOT/opencode/acp-states$tag"
    cp $CLONE_OPT "$d"/*.json "$ROOT/opencode/acp-states$tag/" 2>/dev/null || true
    echo "[ok] acp 状态 $n 个 ← $d"
  fi
done

# 4) --full：整个 storage/ 与 cache（大）
if [ "$FULL" = "1" ]; then
  [ -d "$DATA/storage" ] && { cp -R $CLONE_OPT "$DATA/storage" "$ROOT/opencode/storage"; echo "[ok] storage/ $(du -sh "$ROOT/opencode/storage" | cut -f1)"; }
  [ -d "$HOME/.cache/opencode" ] && { cp -R $CLONE_OPT "$HOME/.cache/opencode" "$ROOT/opencode/cache"; echo "[ok] cache/opencode"; }
fi

# 5) bili：会话产物（迁移成果本体）+ 配置
[ -d "$BILI_DATA/sessions" ] && { cp -R $CLONE_OPT "$BILI_DATA/sessions" "$ROOT/bili/sessions"; echo "[ok] bili/sessions $(find "$ROOT/bili/sessions" -name '*.json' | wc -l | tr -d ' ') 个文件"; }
[ -d "$BILI_CONF" ] && { cp -R "$BILI_CONF" "$ROOT/bili/config"; echo "[ok] bili/config"; }

# 6) 迁移中间产物：转换输出 + 校验报告（工具本身在 git 仓库里，不用备）
for d in "$RUNTIME/out" "$RUNTIME/reports" "$HOME/acp2bili-out" "$HOME/opencode/acp2bili.old"; do
  [ -d "$d" ] && { cp -R $CLONE_OPT "$d" "$ROOT/migration/$(basename "$d")"; echo "[ok] migration/$(basename "$d")"; }
done
i=0; for f in "$RUNTIME"/reports/*.txt "$HOME"/verify-report*.txt; do [ -f "$f" ] && { cp "$f" "$ROOT/migration/"; i=$((i+1)); }; done
[ "$i" -gt 0 ] && echo "[ok] verify 报告 ×$i"

# 7) 自检 + 清单
if [ -f "$ROOT/opencode/opencode.db" ]; then
  echo "PRAGMA integrity_check;" | sqlite3 "$ROOT/opencode/opencode.db" | head -1 | sed 's/^/    integrity: /'
fi
{
  echo "backup: $STAMP"; echo "host: $(hostname)"; echo "full: $FULL"
  echo "opencode 库三件套 / 配置 / acp 状态 / bili sessions / 迁移产物"
  echo "总计: $(du -sh "$ROOT" | cut -f1)"
  echo; echo "还原（在目标机）：退出 opencode → 删现存 db/-wal/-shm → 拷回 opencode/ 下同名文件 → 拷回 config → 再启动"
} > "$ROOT/MANIFEST.txt"
echo "== 完成：$ROOT（$(du -sh "$ROOT" | cut -f1)），清单见 MANIFEST.txt =="
