# acp2bili —— 把 opencode-acp 的压缩状态迁移给 billion-context

把 **opencode v1 + opencode-acp** 旧会话的压缩账本，转换成 **billion-context（bili）** 能直接使用的会话状态文件。转换后的旧会话在 bili 接管时，**已压缩的历史继续折叠生效**（实测 21.4MiB 请求 → 628KiB 出站），不必从零重建。

## 原理（一分钟版）

| | opencode-acp | billion-context |
|---|---|---|
| 消息身份 | 宿主原生 id（`msg_…`） | 内容哈希 `h_<sha256[:16]>`（`role\|contentType\|toolCallId\|toolName\|text`） |
| ref 账本 | `messageIds.byRawId/byRef`（`mNNNNN`） | `state.messageRefs.byRaw/byRef`（`mNNNNN`，同格式不同键空间） |
| 压缩块 | `blocksById`（数字 id，tier1/2/3） | `state.blocks`（字符串 id，`directMessageIds/directBlockIds`） |
| 存放 | `~/.local/share/opencode/storage/plugin/acp/<sid>.json` | `~/.local/share/billion-context/sessions/<protocol>/<host>_<sha256(sid)[:24]>.json` |

转换 = **按 bili 的算法重算每条消息的 `h_` 身份 → 重排 ref → 把 acp 的块映射成 bili 的块**。原始对话永远在 opencode 自己的库里，本工具只新增 bili 状态文件，**不改 opencode 的任何数据**；删掉产物即回到未迁移状态。

## 依赖

- python3（≥3.9，仅标准库：sqlite3 / hashlib / json）——无需 pip
- 机器上已装 billion-context 且 `bili plugin install opencode` 已执行

## 文件清单

| 文件 | 作用 |
|---|---|
| `bili_convert.py` | 转换器。`--auto --all` 批量 / `--auto --sid <sid>` 单会话；自动定位数据目录、库、acp 目录；从 `~/.config/opencode/opencode.jsonc` + models.dev 快照推断 protocol/upstream，推不出时用扁平名（bili 明确接受） |
| `bili_verify.py` | 单产物离线校验：A 组结构（信封/文件名/ref 账本/块结构/折叠一致性/与 acp 块级对应）+ B 组内容（core 逐位重算、逐块覆盖、尾段、汇总） |
| `bili_verify_all.py` | 批量校验 + `--install` 只装全 PASS 的产物到 bili sessions 目录（同名先备份 `.bak`） |
| `bili_wire_diff.py` | 取证：把真实请求转储折成 core，与我们的账本逐位比，按类（角色×内容类型）给差异样本；`--try-rules` 可对比多种形态规则 |
| `probe_residual.py` | 残差取证：对对不上的 core 打印源消息形状（parts/status/provider） |
| `backup.sh` | 一站式备份到 `~/OpenCode_bak/<时间戳>_<主机>/`：opencode 库三件套+配置+acp 状态+bili 会话+迁移产物；`--full` 加整个 storage 与 cache。还原步骤见脚本头注释与 MANIFEST.txt |
| `sha_check.sh` | 核对本机脚本是否当前版本（md5+sha256+字节） |

## 快速开始（在跑过 opencode-acp 的 opencode v1 机器上）

```bash
git clone https://github.com/yangyaofei/acp2bili.git ~/opencode/acp2bili
cd ~/opencode/acp2bili && bash sha_check.sh        # 确认版本一致

# ① 批量转换（只处理有压缩块的会话；acp 状态已被挪走的加 --acp-dir）
python3 bili_convert.py --auto --all --acp-dir ~/acp-state-quarantine

# ② 校验 + 安装（只装全 PASS 的）
python3 bili_verify_all.py --install --acp ~/acp-state-quarantine --report ~/verify-report.txt

# ③ 把默认目录里剩余的 acp 状态隔离（关键：不隔离会走 legacy 旁路，bili 不用我们的产物）
mkdir -p ~/acp-state-quarantine
cp -n ~/.local/share/opencode/storage/plugin/acp/*.json ~/acp-state-quarantine/ 2>/dev/null
python3 - <<'PY'
import shutil, pathlib, os
bili = pathlib.Path.home()/".local/share/billion-context/sessions"
import hashlib
done = {f.name.rsplit("_",1)[-1].split(".")[0] for d in bili.iterdir() if d.is_dir() for f in d.glob("*.json")}
src = pathlib.Path.home()/".local/share/opencode/storage/plugin/acp"
for f in src.glob("ses_*.json"):
    if hashlib.sha256(f.stem.encode()).hexdigest()[:24] in done:
        shutil.move(str(f), str(pathlib.Path.home()/"acp-state-quarantine"/f.name)); print("隔离", f.name)
PY

# ④ 重启 bili 代理（精确杀 18787/8787 的监听进程，让新产物生效）
lsof -nP -iTCP:18787 -sTCP:LISTEN -t | xargs -r kill
lsof -nP -iTCP:8787  -sTCP:LISTEN -t | xargs -r kill

# ⑤ 抽跑验收：挑最大的会话发一句
cd <该会话的项目目录>
opencode run --session <sid> "只回复两个字：收到"
grep -E "drifted|outbound=" ~/.local/state/billion-context/bili.log | tail -4
```

**判定标准**：模型正常回复 + `outbound` 是 KiB~MiB 级（比 inbound 小一个量级）= 成功。`drifted` 的含义见下表。

## 在另一台机器上跑（换机器迁移）

前置：目标机器装好 `billion-context`（`npm i -g billion-context --prefix=~/.local`）+ `bili plugin install opencode`，且它就是**那台跑过 acp 的 opencode v1 机器**（消息库在本机）。然后：

1. `git clone` 本仓库；
2. 与上面「快速开始」①–⑤ 完全相同（工具会自动定位 `~/.local/share/opencode` 的库与 acp 目录）；
3. 如果数据目录不在默认位置：`--db ~/.local/share/opencode/opencode.db --acp-dir <acp 目录>` 手工指定；
4. wire 规则自动选择：转换器读库表结构，v1（`message`+`part` 表）→ 1x 规则，v2（`session_message` 表）→ 2x 规则，无需参数。

注意：**跨机器拷 opencode.db 不在本文范围**——会话内容必须已在目标机器的 opencode 库里（它就是原机器，或你已用 `opencode session export/import` 迁好）。

## 1.x wire 形态规则（为什么需要、怎么判）

opencode 1.x 发历史时，assistant 的 reasoning 有两种形态：**rc**（单独走 `reasoning_content`）与 **fold**（并进 `content`，直接拼接无分隔符）。形态由 (provider, model) 决定，**错了 core id 就全错**。

判定优先级（`classify_form`）：

```
force（--rc/--fold-providers，可写 "provider" 或 "provider/model"）
> MEASURED_FORMS（真实请求转储标定的实测表，最可信）
> 宿主 catalog（~/.cache/opencode/buraunkan/models-dev.json → models.dev 快照；模型条目的 interleaved 开关）
> heuristic（模型 id 含 "deepseek" → rc，否则 fold；对应 opencode provider.ts:1593）
```

> 实测 > catalog 是 2026-10-15 的教训：buraunkan 缓存刷新后把 opencode-go、zhipuai 标成 rc，
> 但宿主真实 wire 仍发 fold（旗舰 drift 1→986）；宿主对自定义 provider 不跟该缓存走。
> 遇到没测过的组合 drift 异常时：抓一次转储（`ACP_DUMP_BODY=1` 重启代理后跑一句），
> 用 `bili_wire_diff.py --try-rules` 定形态，再用 `--fold-providers provider/model` 覆盖并重转。

## 三层验证

| 层 | 命令 | 看什么 |
|---|---|---|
| 离线 | `bili_verify_all.py --report` | `PASS n / FAIL 0`；只装全 PASS 的 |
| 静态 | `BILI_SESSIONS_DIR=<产物目录> bili export` | 能列出/渲染 = bili 认得出（文件名/id 匹配） |
| 真跑 | `opencode run --session <sid> …` 后 grep 日志 | `outbound` 小一个量级、drift 在预期内 |

## 日志警告释义（`~/.local/state/billion-context/bili.log`）

| 日志 | 含义 | 处置 |
|---|---|---|
| `drifted: N covered id(s) missing`（N 很大且稳定，如 6316） | 宿主自己的 `/compact` 压缩把前缀吃了，那段永远不再发（良性，acp 活着也一样） | 不用管 |
| `drifted: N`（N ≈ 2×某 provider 的消息数） | 该 provider 的 rc/fold 形态判错 | 按上节抓转储定形态，覆盖重转 |
| `drifted: N`（个位数） | 尾部 churn（正常消息增删） | 不用管 |
| `preflight fail-fast 502 → EMERGENCY T1` | 首次接管的一次性开销：折叠后仍超窗→自动紧急压缩→转发 | 只出现一次属正常 |
| verifier 的 `[WARN] active 块 effective 重叠` | acp 的块语义（消费了更早的块），bili 按并集处理 | 非错误 |
| verifier 的 `[INFO] 空消息差异` | 空 assistant 在 bili 侧不产生 core | 语义等价 |
| verifier 的 `⚠ 实测 X / catalog Y` | 判定按实测走；catalog 只是元数据不同步的提示 | 无需处理 |

## 回滚

```bash
# 单会话：删掉对应产物即回到「未迁移」（bili 会当全新会话）
python3 -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:24])" <sid>
rm ~/.local/share/billion-context/sessions/openai/*_<hash>.json

# 全部：清空 sessions 目录 + 把 acp 状态从隔离目录拷回原位 + 重启代理
```

## 已知边界

- 只处理**有压缩块**的 acp 状态（`--list` 看清单）；没压过的会话无需迁移，bili 自己从零建。
- 库里已无消息行的空壳状态自动跳过（不写产物）。
- acp 块缺 `tier` 字段时按 tier=1、缺 `runId` 时自动补号（兼容 acp 后期版本的 null tier）。
- `acp.jsonc` 里自定义 `storagePath` 的机器：bili 不探测该路径，隔离时记得用 `--acp-dir` 指向它。
