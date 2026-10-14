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
# （校验器的 --acp 与 --acp-dir 等价，写哪个都行）

# ③ 把默认目录里剩余的 acp 状态隔离（关键：不隔离会走 legacy 旁路，bili 不用我们的产物）
#    （用 find 而不是 shell 通配：zsh 下目录为空会报 "no matches found"，find 两种 shell 都安全）
mkdir -p ~/acp-state-quarantine
find ~/.local/share/opencode/storage/plugin/acp -maxdepth 1 -name '*.json' -exec cp -n {} ~/acp-state-quarantine/ \; 2>/dev/null
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
#    （while-read 写法 bash/zsh 通用；xargs -r 是 GNU 专用，macOS 自带的 xargs 不认）
lsof -nP -iTCP:18787 -sTCP:LISTEN -t 2>/dev/null | while read -r p; do kill "$p"; done
lsof -nP -iTCP:8787  -sTCP:LISTEN -t 2>/dev/null | while read -r p; do kill "$p"; done

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

## 测量法（--from-dump）：不猜任何规则，直接从真实请求算指纹

形态规则（上一节）本质是**预测**宿主怎么序列化，覆盖不了所有组合。终极解是**测量**：
让宿主真发一次请求，把请求原文（bili 的 INCOMING 转储）当作指纹来源——转储里每条消息
长什么样，就按 bili 自己的语义算什么 id，与 bili 实际看到的逐字节一致。

```bash
# 1) 抓一次转储（见下一节的正确开法），然后：
python3 bili_convert.py --auto --sid <sid> --acp-dir ~/acp-state-quarantine \
  --from-dump ~/.local/state/billion-context/raw/<时间戳>-<sid>-INCOMING.txt

# 2) 校验同样用测量法重算（产物与校验必须同源）
python3 bili_verify.py --acp ~/acp-state-quarantine/<sid>.json \
  --bili <产物路径> --from-dump <同一个转储> --sid <sid>

# 3) 校验器会打印「锚定统计」：未锚定 = 转储里有、库里对不上宿主消息的条数，
#    新增对话（转储抓取前的正常使用）属于此类，落在尾段，属预期。
```

- `--from-dump` 与形态规则互斥：给了转储就完全不用 1x/2x/catalog。
- protocol/upstream 不给时自动从转储首行 URL 推断（`/messages` → anthropic，否则 openai）。
- 顺序先验：锚定重复文本时取**离当前位置最近**的候选（与 bili 的 order-centric 对账同思想），
  实测能把块级覆盖对到与 acp 完全一致（B2）。
- 适用场景：预测法重转后 drift 仍大的会话；没测过的 provider/model 组合；升级/换配置后。


## 转储开关（ACP_DUMP_BODY）：用完必关

抓真实请求转储用的代理是**手动带环境变量**起的，跟平时自动拉起的不是同一个——**它会一直把
每个请求的原文写盘（大会话一次 ~20MB）**，忘了关就会悄悄吃盘。抓完按下面收尾：

```bash
# 1) 看是否还开着（有没有 raw/ 目录、里面多大）
du -sh ~/.local/state/billion-context/raw 2>/dev/null

# 2) 关掉带转储的代理（bash/zsh 通用），下次 opencode 一跑会自动拉起不带转储的
lsof -nP -iTCP:8787  -sTCP:LISTEN -t 2>/dev/null | while read -r p; do kill "$p"; done
lsof -nP -iTCP:18787 -sTCP:LISTEN -t 2>/dev/null | while read -r p; do kill "$p"; done

# 3) 确认不再需要转储后清理（每次请求一个 INCOMING + REQ + RES，占盘主体是 INCOMING）
rm -rf ~/.local/state/billion-context/raw ~/.local/state/billion-context/dumps
```

需要再抓时的正确开法（输出进日志文件，不用 `/dev/null` 吞错误）：

```bash
nohup env ACP_DUMP_BODY=1 bili start >> ~/.local/state/billion-context/dump-proxy.log 2>&1 &
```

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
