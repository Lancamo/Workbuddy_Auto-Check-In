# WorkBuddy 自动签到领积分（macOS 版）

> **本文件夹是自包含的**：拷到任意位置，执行 `bash install.sh` 即可安装并运行。
> 它只依赖 WorkBuddy 桌面端（读登录态）与一个 ≥3.10 的 Python。
> 与它平级的是 `../00_For_Win/`（Windows 版，同名文件尽量逐字一致，同步规则见那份 README 第 5 节）；
> 上级目录的 `Plan/`、`Knowledge/` 是项目文档，**不参与运行**。

## 简介

**每天自动替你在 WorkBuddy 里签到领积分、派遣旅行并领取奖励，结果推到微信；漏跑自动补，做完即停。**

它具体替你做的事：

- **签到**：每天 `07:00` 后自动完成 Buddy 加油站签到（+100 积分）。
- **旅行**：同一时刻把猫派出去；**猫到达后自动领取奖励**（+7 积分）—— 不设固定兜底时刻，到点才领。
- **通知**：每笔到账各推一条微信（签到、旅行分开推，不合并）；故障类通知当天只推一条。
- **补跑**：错过的时间窗在开机 / 睡眠唤醒后自动补上（launchd 会把过期的触发补发过来）。
- **安静且免费**：当天做完后，后续触发全部空转、**零网络请求**；不走 WorkBuddy 自动化，**不消耗任何额度**。

## 安装

前置：macOS + 已登录 WorkBuddy 桌面端 + 一个 ≥3.10 的 Python（`install.sh` 会自动挑，无需你操心）。

```bash
cd "<本项目路径>/00_For_Mac"   # 本文件夹，拷到任意位置都行
bash install.sh                # 注册主任务（每 5 分钟）+ watchdog（每 30 分钟），并做一次验收
```

> ⚠️ **必须在 Terminal.app 里执行一次**。在 WorkBuddy 或其它沙箱环境里跑会报
> `Bootstrap failed: 5: Input/output error` —— 注册系统服务需要图形登录会话，这是 macOS 的限制。

`install.sh` 做三件事：① 自动挑一个 ≥3.10 的解释器；② 按**本文件夹的真实路径**现场生成两个 plist
（所以拷到哪都能用）；③ 加载后**回读确认**，不会给你一个假的 ✓。

## 使用

装好之后**不需要你日常操作**。需要时用这几条：

```bash
cd "<本项目路径>/00_For_Mac"

bash install.sh status     # 两个任务是否在跑 + watchdog 自己的存活判定
bash install.sh uninstall  # 卸载（项目文件保留）

# 手动跑一次、看结果（幂等：今天做过就什么都不做，安全）
<解释器> catchup.py
```

`<解释器>` 用 `install.sh` 打印出来的那个绝对路径即可（必须 ≥3.10；系统自带的
`/usr/bin/python3` 是 3.9，**不能用**）。常见排查命令见下文「测试与排查」。

**怎么确认它在正常工作**：`bash install.sh status` 显示两个任务 `state = active`、
`last exit code = 0`，且 watchdog 判定 `healthy: true`。

## 文档维护约定

**本文件与代码同步维护，这是硬要求。** 以后任何一次优化调整 —— 逻辑变更、配置新增、
目录移动、默认值改动 —— 都必须**同步更新本 README**（mac 版改 `00_For_Mac/README.md`，
win 版改 `00_For_Win/README.md`；**跨平台的功能改动两份都要改**，
不要只改一侧、留下一条过期的谎言）。

变更历史不在这里维护：大改动的方案与执行记录见上级 `Plan/`，每日工作日志见项目内 `.workbuddy/memory/`。

---

> 以下为深入说明与排查细节。装好后日常不需要读；出问题时按目录找对应章节即可。

## 工作原理

| 项 | 说明 |
|---|---|
| 触发者 | macOS LaunchAgent（`com.workbuddy.wb-reward-catchup`） |
| 触发时机 | 登录 / launchd 加载即跑一次；此后每 **5 分钟**一次；**睡眠唤醒后 launchd 立即补发过期任务** |
| 效果 | 时间点一到，最迟 5 分钟内自动补跑 → 「打开电脑即补」 |
| 额度消耗 | **0**（纯本地脚本，不触发 WorkBuddy 模型推理） |
| 静默 | **00:00–07:00** 与「当日签到+领取都到手后」：只读一次系统时间即退出 —— 不抢锁、不写日志、不做续期检查与端点探测（2026-09-19 起） |
| 静默期外每次触发做三件事 | ① **接口发现**（`api_discovery.py`，纯本地）；② 积分派发（带闸门，派发前再做一次探活校验）；③ **续期守护**（`renew.py`，与闸门无关，见下文） |

> **为什么是 5 分钟**：触发周期的唯一价值是「事件发生后多快被发现」。闸门未开时，这次触发
> 只读/写一次本地 `state.json` 就退出，**零网络请求** —— 所以提高频率的代价只是每天多几百次
> 极轻量的本地唤醒（约 0.1 秒/次），换来的是「07:00 签到」和「猫到达就领」都收敛到 5 分钟以内。
> 这也**就是「猫到达即触发领取」的实现方式**：不新增定时器，提高基础采样率。
>
> 配套：为避免「重试」在密集触发下退化成「刷请求」，`catchup.py` 加了
> `MIN_RETRY_GAP_MIN = 20`（两次真实尝试至少间隔 20 分钟），12 次重试约 4 小时，
> 节奏与旧的 30 分钟触发基本一致。

**静默（2026-09-19 新增）**

触发大多由**系统的维护唤醒**带起来（见下一节），其间常常无事可做。两条规则让这些触发
变得几乎免费：

| 情形 | 行为 |
|---|---|
| `00:00–07:00` | `main()` 里只读一次系统时间即返回：不抢锁、不读 state、不写日志 |
| 当日签到与旅行奖励**都到手后** | 读一次 `state.json` 判定后返回：不做续期检查、不做端点探测、不写盘 |
| 其他时间 | 照常全量运行，闸门逻辑一个字未改 |

`QUIET_FROM = 700` 与签到闸门 `WINDOW1 = 700` **同值** —— 闸门几点开门，静默就几点结束，
不留「醒了但不该干活」的中间态。

> ⚠️ **静默不减少系统的唤醒次数。** 那些唤醒是 macOS 自带的 **Power Nap + 系统维护任务**
> （`com.apple.dasd` 心跳、mDNSResponder、searchd、powerd）发起的，与本项目无关：
> 本机 `pmset -g log` 显示 **09-13 起每天就有 63 次 DarkWake**，而本任务的 LaunchAgent
> **09-18 18:00 才装上**。我们的 plist 里没有任何唤醒能力，只是**搭便车**。
> 想减少系统唤醒得动 `sudo pmset -a powernap 0` 这类系统设置，那会影响邮件/日历/备份/查找，
> 不在本项目范围内，**不建议为了这个脚本去关**。

配套地，`watchdog.py` 也学会了识别这两段静默（读 `state.json` 的内容，而不是只看 mtime），
否则每次长时间合盖再开盖都会收到一条「主脚本可能已停摆」的**假警报** ——
2026-09-19 就实际发生过一次（连续睡眠 122 分钟 > 阈值 90 分钟）。

**时间窗（写死在 `catchup.py`）**
- `≥ 07:00`：签到（即领取 100 积分）+ 旅行派遣
- **猫到达即领**：到达时间由接口的 `travel.status.arrive_at` 给出，派猫那一次运行就记进
  `state.json`；到点才开窗领取。**没有固定时刻兜底** —— 到点去领的唯一正当理由是「猫真的到了」。
  缺失到达时间时（`state` 被删、或猫是你在 App 里手动派的）不猜时间，而是**去问一次接口**
  补齐（受 `MAX_TRIES` + 20 分钟间隔约束，且要求已过 07:00）。

> **通知拆成两条（2026-09-18）**：签到到账推「WorkBuddy 签到到账：+100 积分」，
> 旅行领取推「WorkBuddy 旅行奖励到账：+7 积分」，**各自独立成条，不合并**。
> 它们实际相隔 1–2 小时发生，合并成「+107 到账」会让人分不清哪笔是哪笔。
> 同一次运行里两件都成，就推两条；失败类仍合并成一条（一次故障不该刷两条）。
>
> `state.json` 有 `arrive_at`、`checkin_last_try`、`claim_last_try`；
> `catchup.py` 的 JSON 摘要里有 `claim_open` / `arrive_at`，
> 排查「为什么还没领」时一眼能看出是闸门没开、还是领取失败了。

> 微信推送通道（ClawBot）的登录会话会失效，所以每次运行还会顺带做一次**连接守护**检查 ——
> 详见下文「连接守护（renew.py）」。

## ⚠️ 2026-09-17 修复：活动接口变更 + 前置校验工作流（勿回退）

**症状**：每天早上推送「当前没有进行中的活动」，而 WorkBuddy 积分页里活动明明在跑，
导致整天一粒积分都没进账。

**根因**：活动/签到状态接口换了，脚本还在调旧的。

| | 旧接口（脚本原用） | 新接口（桌面端在用） |
|---|---|---|
| 路径 | `POST /billing/meter/checkin-status` | `POST /v2/billing/meter/checkin-activity-status` |
| `code` / HTTP | **200 / 0（看起来完全成功）** | 200 / 0 |
| 字段结构 | 与新接口**完全相同** | 完整 |
| 字段值 | **全为零**：`active=false`、`streak_days=0`、`total_credits=0`、`checkin_dates=null`、`activity_name=""` | 真实值：`active=true`、`activity_name="高校新生攻略"`、`end_time="2026-09-29 23:59:59"` 等 |

**这是最危险的一类失效**：不报错、HTTP 200、业务码 0、字段结构合法 —— 只是全为零。
脚本「成功」读到一份合法却空洞的数据，如实报告「没有活动」。日志全绿，积分归零。

**为什么没被发现**：`catchup.py` 把 `no_activity` 也计入「当日完成」（这个设计本身是对的，
避免空档期白白重试几十次），于是当天直接不再重试 —— 静默失效。

### 接口是什么 / 会不会再换

端点会换，而且**没有固定周期** —— 它跟腾讯的发版节奏走，不是定时轮换。
两种变化性质不同（客户端源码 `app.asar` 里明文写着）：

| 变化 | 机制 | 说明 |
|---|---|---|
| 前缀分叉 | `get billingPrefix()` | Web 端是空串 → `/billing/meter/...`（cookie 认证）；**Desktop 端覆写为 `/v2`** → `/v2/billing/meter/...`（IDE 网关 Bearer token）。本脚本用 Bearer token，属 Desktop 那一支。结构性，几乎不变 |
| 接口改名 / 替换 | 产品迭代 | `checkin-status` → `checkin-activity-status`（新版多返回活动名、主题、赛季、结束时间、可领积分）。**跟客户端发版走**，新旧会并存一段时间，旧路径由网关兜底并返回退化数据 |

**结论：不要猜周期，也不要硬编码。** 权威来源就在本机 —— 客户端自己的 `app.asar`
（`/Applications/WorkBuddy.app/Contents/Resources/app.asar`）里明文包含当前端点。
每次运行前现读一遍，就永远不会落后。

### 前置校验工作流（`scripts/api_discovery.py`）

每次真正领取**之前**，先回到权威来源核对端点，再探活：

```
① 接口发现  读 app.asar → billingPrefix + 所有 billing/meter 端点名
             按语义排序（名字含 activity 的最优），组合出候选路径
             按「客户端版本 + asar 大小 + asar mtime」指纹缓存 → 指纹没变不重扫
② 探活     逐个候选发一次**只读**状态查询，取第一个返回可信数据的
③ 退化检测 载荷全为零 ⇒ 不采信，继续试下一个
             全部候选都退化 ⇒ 返回 suspect（而非 no_activity）并立刻告警
④ 变更检测 端点相对上次变了 ⇒ 写进 api_endpoints.json 的 history 并推送告知
             （即使当天无事可做也报，不等明天）
```

**为什么这样设计**：

- **不硬编码**：候选端点按语义规则从客户端现读。将来再改名（比如加 `-v3` 后缀），
  只要名字里还有 `checkin` / `activity` 就能自动认出来，不用改代码。
- **不做"探针式领取"**：领取接口一旦调用就可能真把积分领掉，所以**绝不拿它做探针**。
  领取路径的正确性由客户端源码背书，并由幂等码 `10001` 兜底。
- **全零载荷判据**：只有"真的没活动"和"接口退化"两种情况需要区分。
  活动**空档期**返回的 `active` 也是 false，但空档期里用户的历史累计积分 / 签到日期
  仍然存在 → 不会是全零。所以"全零"能干净地把两者分开，误报率极低。
- **降级不阻断**：读不到客户端（未安装 / 非 macOS）就退回内置候选，`source="fallback"`，
  **绝不因为校验失败而放弃签到**。

### 修复清单

1. `scripts/api_discovery.py`（新增）：接口发现 + 探活 + 退化检测 + 变更历史。
2. `scripts/checkin.py`：候选端点改为动态获取（`_candidates()` → 客户端现读，内置兜底）；
   `query_activity_status()` 按**载荷可信度**择优，全零时打 `__degraded__` 标记；
   `run_checkin()` 在退化时返回 `suspect`（**不再**返回 `no_activity`）。
3. `scripts/main.py`：`all` 改为 **前置校验 → 签到 → 旅行**，输出含 `preflight` 报告；
   新增 `preflight` 子命令；`status` 新增 `degraded` 字段。
4. `catchup.py`：每次触发都做接口发现（纯本地，缓存后开销近 0）；`suspect` 计入当日完成
   （全零载荷重试也不会变好，该做的是**立刻告警**而不是反复重试刷屏）并推 `failure` 级告警；
   端点变更时推送告知。
5. `scripts/http_client.py`：User-Agent 改为跟随**本机客户端真实版本**（原先硬编码
   `WorkBuddy/5.3.14`，客户端已升到 5.5.6，请求还在伪装成老版本）。

**幂等**：重复领取返回 HTTP 400 + `code=10001`（`msg="今天已签到，请明天再来"`），
按 `already_checked` 处理，不计失败。
> 注意：客户端源码里的 `mapCheckinStatus` 只映射 1001/1002/1003，**与实测的 10001 不同**，
> 以实测为准，不要照抄客户端那张表。

**如何自查**（两条命令）：
```bash
python3 scripts/api_discovery.py          # 前置校验：当前端点 + 探活 + 是否变更
python3 scripts/main.py status | python3 -m json.tool
# 看 checkin_status.endpoint / active / degraded
# degraded=true ⇒ 接口返回了全零数据，端点可能又变了
```

## 文件

代码、配置和运行数据分层如下：代码保持在平台目录根部；用户配置在 `runtime/config/`；
状态与凭据在 `runtime/state/`；接口缓存、日志和 launchd 输出在 `runtime/cache/`、`runtime/logs/`。

| 路径 | 说明 |
|---|---|
| `catchup.py` | 补跑器：闸门 + 幂等 + 当日去重；结果经 `notify.py` 推送 |
| `paths.py` | runtime 路径、日志裁剪和 macOS 文件权限的唯一收口 |
| `notify.py` | 通知模块：通道选择 + 去重 + 每日配额 + macOS 通知兜底 |
| `renew.py` | **连接守护**：只读桌面端游标文件，监测 ClawBot 是否仍在被维护；停摆超阈值即提醒 |
| `clawbot.py` | **微信直推模块**：腾讯 iLink / ClawBot 官方个人微信通道（仅标准库） |
| `runtime/config/notify_config.json` | 通知开关与备用通道凭据（**默认 auto，无需填写任何凭据**） |
| `runtime/config/renew_config.json` | 连接守护配置（首次运行自动生成，默认 24 小时阈值） |
| `scripts/` | **内联引擎**（6 个脚本）：`main.py`（入口）、`checkin.py`（签到）、`travel.py`（旅行）、`api_discovery.py`（**接口前置校验**）、`credentials.py`（读登录态 + 域名）、`http_client.py`（HTTP 封装） |
| `install.sh` | **安装 / 查看 / 卸载**两个 LaunchAgent。plist 不放进仓库 —— 它必须写死本文件夹的绝对路径，作为文件存在就会「拷到别处即失效」，所以改为安装时现场生成（含「回读确认」，不会报假 ✓） |
| `watchdog.py` | **存活监控**：独立于主脚本，只查心跳与计划任务是否还在，异常时弹本机通知 |
| `runtime/state/state.json` / `runtime/state/state.bak.json` | 运行后生成：当日进度与备份（损坏时自动回落） |
| `runtime/logs/watchdog.log` / `runtime/state/watchdog_state.json` | 运行后生成：watchdog 自己的日志 / 告警冷却记录（可安全删除） |
| `runtime/logs/catchup.log` | 运行后生成：主任务日志，超过 512 KB 自动保留最近 800 行 |
| `runtime/cache/api_endpoints.json` | 运行后生成：当前客户端端点 + 变更历史（**可安全删除**，删了会重新扫描客户端） |
| `runtime/state/notify_state.json` | 运行后生成：通知去重记录 + 每日配额计数（可安全删除） |
| `runtime/state/renew_state.json` | 运行后生成：检查节流时间 + 提醒记录（可安全删除） |
| `runtime/state/clawbot_state.json` | 运行后生成：本地扫码凭据（**当前已清空，改用桌面端凭据**）+ 最近发送时间（含 token 时勿外传） |
| `clawbot_login.html` | `clawbot.py login` 生成：扫码登录页面（含二维码，扫完可删） |
| `runtime/logs/result.log` | 运行后生成：引擎每次执行的摘要 |
| `runtime/logs/launchd.out.log` / `runtime/logs/launchd.err.log` | 运行后生成：launchd 捕获的**主任务** stdout / stderr（路径由 plist 指定；可安全删除） |
| `runtime/logs/watchdog.out.log` / `runtime/logs/watchdog.err.log` | 运行后生成：同上，**watchdog** 那份（可安全删除） |
| `~/Library/LaunchAgents/com.workbuddy.wb-reward-catchup.plist` | **实际生效**的 LaunchAgent（主任务，必须留在该路径） |
| `~/Library/LaunchAgents/com.workbuddy.wb-reward-watchdog.plist` | **实际生效**的 LaunchAgent（watchdog，同样必须留着） |

依赖：WorkBuddy 桌面端已登录（引擎读登录态）+ ClawBot 通道可用 + Python 3.10+（**仅标准库，零第三方包**）。
不再依赖任何 Skill —— 引擎脚本已从 `workbuddy-reward-helper` 内联到本目录，可完全独立运行。
微信推送与 WorkBuddy 桌面端**共用同一个 ClawBot**（2026-09-16 起，详见下文「为什么改为共用桌面端 bot」）。

## 通知（微信推送）

结果分四级，由 `notify_config.json` 控制：

| 级别 | 触发条件 | 默认 |
|---|---|---|
| `success` | 有积分到账。**签到与旅行各推一条，互不合并** | 开 |
| `failure` | 签到或旅行失败（含令牌过期）；**接口返回全零载荷**（`suspect`）；前置校验找不到任何可用端点；连续 3 天报「无活动」 | 开 |
| `warning` | 当前没有进行中的活动（空档期）；**检测到接口端点已切换**（含空转期） | 开 |
| `info` | 无事发生（今日已领过 / 猫在路上） | 关 |

**实际收到的消息长这样**

```
WorkBuddy 签到到账：+100 积分
签到成功，+100 积分（高校新生攻略）
连续签到：3 天
时间：2026-09-18 07:00
今日进度：签到 ✓ / 旅行 ✓
```

```
WorkBuddy 旅行奖励到账：+7 积分
猫已到达，旅行奖励领取成功：+7 积分
时间：2026-09-18 09:00
今日进度：签到 ✓ / 旅行 ✓
```

### 通道：默认走 ClawBot（无需任何第三方服务）

`channel = "auto"` 时会按 **clawbot → pushplus → serverchan** 顺序自动选择可用的通道：

| 通道 | 说明 | 是否需要额外配置 |
|---|---|---|
| **clawbot**（默认） | 腾讯 iLink 官方个人微信通道。凭据来源见下 | **不需要**（本机已登录） |
| pushplus | 第三方代发，借其公众号下发到个人微信 | 需注册 + 实名 + 填 token |
| serverchan | 第三方代发（Server 酱） | 需注册 + 填 SendKey |

**为什么默认用 ClawBot 而不是 PushPlus**：个人微信没有对个人开放的 API，PushPlus / Server 酱 本质是"借它的公众号给你发模板消息"，属第三方中转，且需要实名。而 ClawBot 是腾讯 2026-03 通过 OpenClaw 框架开放的**官方个人微信 Bot 通道**（协议名 iLink），直连腾讯域名 `ilinkai.weixin.qq.com`，无第三方、免注册、免实名。

**凭据优先级**（`clawbot.load_channel()`）

1. `clawbot_state.json → credentials`（本机 `clawbot.py login` 扫码得到的）
2. `~/.workbuddy/settings.json → claw.users.<uid>.channels.weixinClawBot`（WorkBuddy 桌面端绑定的）

> **当前实际使用的是第 2 项** —— 桌面端绑定的 bot `example-current-bot@im.bot`。
> 第 1 项（本机扫码自建的 `example-legacy-bot@im.bot`）已于 2026-09-16 主动清空，原因见下文
> 「为什么改为共用桌面端 bot」。
>
> 优先级机制本身保留，价值在于 `clawbot.py login` 可作**逃生通道**：万一桌面端通道彻底不可用，
> 扫一次码就能让脚本立刻独立工作，不必等桌面端恢复。

**协议要点**（核对自 WorkBuddy 自身实现 `weixin-api.ts` + 官方 iLink 文档）

| 项 | 值 |
|---|---|
| 发消息 | `POST https://ilinkai.weixin.qq.com/ilink/bot/sendmessage` |
| 鉴权 | `Authorization: Bearer {bot_token}` + `AuthorizationType: ilink_bot_token` |
| 公共头 | `X-WECHAT-UIN`（base64 随机数，防重放）。**不发** `iLink-App-Id`（WorkBuddy 原生实现也不发） |
| `channel_version` | `workbuddy-desktop-1.0.0`（对齐 WorkBuddy 原生；旧版误用 `2.1.1`） |
| 取二维码 | `GET /ilink/bot/get_bot_qrcode?bot_type=3` |
| 扫码状态 | `GET /ilink/bot/get_qrcode_status?qrcode=…`（头 `iLink-App-ClientVersion: 1`） |
| 成功标志 | 响应体含 `message_id`（如 `example-message-id`）＝已受理 |

> ⚠️ **踩坑记录**：腾讯把错误放在 HTTP 200 的响应体里（如 `{"errcode":-14,"errmsg":"session timeout"}`）。
> 早期版本只检查 `ret` 字段，导致**会话失效被误判为发送成功**，日志一直显示"已送达"而微信收不到。已修。
> 现在成功会带上 `message_id` 作为送达凭证。

### 四个必须知道的硬限制

1. **配额：每人 24 小时内最多 10 条主动推送**，超出返回 `429`。失败请求同样计入配额。
   → 本模块用 `max_pushes_per_day`（默认 **8**）本地封顶，留余量；触顶后自动降级为 macOS 通知，信息不丢。
2. **格式：不支持表格 / 图片 / 代码块**，Markdown 渲染不稳定 → 一律推送纯文本（emoji 可用）。
3. **登录会话会过期（最容易踩）**：`errcode=-14 / session timeout` 的真正含义是
   **微信侧登录会话已失效**，不是"你没发消息"。
   - **发消息救不回来**。首选在 WorkBuddy 设置 → 远程通道里**重新连接「微信助理」**；
     只想让脚本单独恢复时才用 `python3 clawbot.py login` 自建凭据。
   - **判定证据**：连伪造 token / 不带鉴权头的请求也返回同样的 -14 → 服务端对该 bot 一律拒绝，
     与请求格式无关，属账号级会话失效。
   - **冷却**：识别到会话失效后进入 `clawbot_cooldown_minutes`（默认 120 分钟）冷却，
     期间直接走 macOS 通知，不再做无意义的重复请求（iLink 失败也计入配额）。
4. **`context_token` 是必需的 —— 此前「可选」的结论有误（2026-09-17 更正）**

   `clawbot.py` 模块文档早已写明「**主动推送必须携带**从入站消息捕获的 context_token」，
   但代码里的成功判据与它矛盾：只要服务端返回 `message_id` 就记为"已送达"，
   于是**永远走不到"需要抓令牌"的分支**，`has_context_token` 长期为 `false`。

   **实测对照（2026-09-17）**：

   | 发送方 | 是否带 token | 服务端响应 | 微信侧 |
   |---|---|---|---|
   | 桌面端 `sendReply`（23:46:24） | ✅ `hasContextToken: true` | `delivered` | 可见 |
   | 脚本 `notify.send`（23:59 / 00:00） | ❌ `has_context_token: false` | 返回 `message_id` | **收不到** |

   → **`message_id` 只代表「服务端受理」，不代表「微信已投递」。** 不带令牌的主动推送
   会被服务端收下并计数（照样吃配额），但不进微信。

   **修复方向**：缺令牌时必须先抓取 —— 让用户在微信里给该 bot 发一条消息，
   然后 `python3 clawbot.py wait 60` 长轮询捕获并持久化。
   ⚠️ 代价：`wait` 期间会与桌面端**抢同一条消息**（iLink 对同一 bot 只有一个消费位），
   所以抓取窗口内只让用户发无关测试消息。令牌「不保证长期有效」，失效后需重新捕获。

   > 这正是「共用桌面端 bot」架构的固有代价：脚本不能常驻轮询，就无法自动维持会话令牌。
   > 若抓取方案反复失效，换 pushplus / serverchan（无 session 概念）才是真正的无人值守路径。

### 登录会话会过期（必读）

**ClawBot 作为"无人值守每日推送"通道有结构性弱点**：登录会话会失效，且恢复必须人工扫码，
失效期间所有推送静默丢失。实测这台机器上，上一个 bot 从 **2026-08-19** 起就没再活动过，
游标文件停更近一个月 —— 期间推送全是无声失败。

**关键机制**（实测确认）：

- **主动推送不会续期** —— 推送多少条都不影响会话寿命
- **会话靠「持续轮询」保活** —— WorkBuddy 桌面端在运行时不断轮询该 bot，这才是会话活着的真正原因
- **桌面端停止轮询 = 会话开始倒计时** —— 游标文件停滞正是失效的前兆，这是 `renew.py` 的监测依据

### 为什么改为共用桌面端 bot（2026-09-16）

脚本原先用 `clawbot.py login` 自建了一个独立 bot（`example-legacy-bot`），设计上"两边各走各的、互不影响"。
**实测证明这个设计在微信侧不成立** —— 重新绑定会顶掉旧 bot：

| 时间 | 事件 |
|---|---|
| 23:02 | 脚本自建 bot 推送成功（`last_send_ok_ts`） |
| 23:24 | 在 WorkBuddy 设置里重新绑定「微信助理」 |
| 23:27 | 脚本自建 bot 自测 → `errcode=-14` 失效 |

中间只隔 22 分钟，这不是自然过期。**结论：同一微信号下，桌面端重新绑定会挤掉脚本自建的 bot。**

于是统一为一条通道：脚本改用桌面端绑定的 bot，自建凭据清空。**收益**是你微信里只会有一个
「WorkBuddy」对话，积分提醒都在这一个窗口里，不会混淆。

**代价与应对**：脚本与桌面端从此共用同一个 `bot_id`，因此**脚本必须避免抢消息**。
iLink 的消息队列对同一 bot 只有一个消费位，两边同时 `getupdates` 会互相抢 ——
脚本每轮询一次就推进自己的游标，发给该 bot 的消息可能被脚本半路消费掉。这就是 `renew.py`
在 v2 里彻底放弃主动轮询的原因。

> **本通道的用途边界**：只承担积分签到 / 旅行领取这类个人提醒。
> 工作性质的通知（日报、告警、审批等）**一律走企业微信**，不依赖本通道。

### 连接守护（renew.py）

v2 改为**只读桌面端的游标文件**，零网络请求、零抢消息：

```
读 ~/.workbuddy/claw-state/weixin/<bot_id>.cursor.json 的 mtime
   ├─ 新鲜（< 24 小时）→ 桌面端在正常轮询 → 会话健康 → 静默
   └─ 陈旧（≥ 24 小时）→ 桌面端已停止维护 → 推送提醒（每天最多 1 条，直到恢复）
```

该文件由 WorkBuddy 桌面端在**每次成功轮询后**写出，所以它的 mtime 就是「桌面端最后一次
维持会话」的时间戳。

**关于「18 秒一次」—— 已定位到根因（2026-09-17 核实）**

实测刷新间隔固定 **18.1 秒**（两次独立观测：08-19 版 18.1/18.1/18.2/18.1；
09-17 版 18.18/17.98/18.12/18.13/18.14，均值 18.11）。这不是我们设定的，也**不是客户端写死的**：

> 在 WorkBuddy 桌面端 `app.asar` → `packages/workbuddy-server/src/claw/plugins/weixin/weixin-api.ts`
> 中查到权威实现：
>
> ```js
> var DEFAULT_POLL_TIMEOUT_MS = 35e3;              // 客户端兜底默认＝35 秒
> var DEFAULT_MAX_RECONNECT_DELAY_MS = 6e4;        // 异常退避上限＝60 秒
>
> async getUpdates(cursor, timeoutMs, signal) {
>     const longPollMs = typeof timeoutMs === "number" && timeoutMs > 0
>         ? timeoutMs : DEFAULT_POLL_TIMEOUT_MS;
>     return await this.post("getupdates", { get_updates_buf: cursor },
>                            longPollMs + 1e4, signal);   // abort 加 10 秒 headroom
> }
> ```
>
> 以及 `pollLoop()` 中每轮的覆盖逻辑：
>
> ```js
> const result = await this.api.getUpdates(this.pollCursor, this.nextPollTimeoutMs, ...);
> if (typeof result.longpolling_timeout_ms === "number" && result.longpolling_timeout_ms > 0)
>     this.nextPollTimeoutMs = result.longpolling_timeout_ms;   // ← 服务端说了算
> ```

**结论：18 秒是腾讯 iLink 服务端每轮响应下发的 `longpolling_timeout_ms`**，
客户端只是跟随。所以：

- 它是**长轮询超时**，不是"每 18 秒新发一次请求" —— 请求挂起等 18 秒，无消息则超时返回，
  客户端**立刻重连**（正常路径无 sleep，无退避），实测 18s + 网络往返 ≈ 18.1s 完全吻合。
- 任一时刻只有 **1 个挂起连接**（`nextPollTimeoutMs + 10s` 是 fetch abort 上限，不是频率）。
- 真有消息时立即返回，不等满 18 秒。
- 客户端默认其实是 35 秒；只有在服务端不下发该字段时才会退到 35 秒。
- **异常才退避**：`backoffDelay()` 走指数退避 `min(1000·2^n, 60s) + 随机 0–1s`，
  仅在 `ret != 0` / 非 0 `errcode` 时触发，正常轮询不经过它。

> **对「能不能改成更稀疏」的回答**：不能，也不该改。
> ① 该值由服务端下发，客户端没有配置项；② 改 `app.asar` 会被签名校验 / 自动更新覆盖；
> ③ 就算改了，影响的是「你在微信里发指令 → 桌面端收到」的延迟，**不影响脚本主动推送**
> （推送是独立的 `POST /ilink/bot/sendmessage`，与轮询频率无关）。
> 唯一的微小成本是每 18 秒写一次 126 字节的游标文件（一天约 4800 次小写入，可忽略）。
> 真正的风险不是"太频繁"，而是**桌面端一退出，会话就开始倒计时** —— 这正是 `renew.py` 的职责。

**提醒行为**：进入停摆状态后**每天最多 1 条**，恢复后计数归零。文案同时给出两个恢复动作
（打开桌面端 / 给 bot 发条消息），做任一个都有效。

配置见 `renew_config.json`：

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `cursor_stale_hours` | `24` | 游标超过这个小时数没刷新就提醒 |
| `check_interval_minutes` | `30` | 检查间隔（纯本地读文件，开销可忽略） |

```bash
cd "<project>/00_For_Mac"
PY=python3   # 需 ≥3.10
$PY renew.py status   # 游标路径 / 最后活动时间 / 是否停摆
$PY renew.py          # 执行一次检查（纯本地，无网络）
$PY renew.py --force  # 忽略节流强制执行
$PY renew.py reset    # 清空提醒记录
```

> **为什么阈值是 24 小时**：桌面端正常轮询间隔 18.1 秒，所以 24 小时无活动只可能是真的停了。
> 阈值必须大于一次电脑睡眠时长，否则合盖过夜就会误报。
>
> **40 天寿命数据的出处**（用于说明"游标停滞"这条判据为何可信）：
>
> | 事件 | 时间 | 依据 |
> |---|---|---|
> | 绑定（会话开始） | 2026-07-10 11:47 | `dffb797a0545_im.bot.cursor.json` 创建时间 |
> | 最后一次成功轮询 | 2026-08-19 11:11 | 同文件最后写入时间（**此后桌面端不再活动**） |
> | 确认失效（-14） | 2026-09-16 | 实测返回 `errcode=-14` |
>
> 即：**会话在"有人持续轮询"期间至少健康存活 40 天**；游标一停，失效就只是时间问题。
>
> **失效的兜底识别**：若会话因其它原因失效，日常推送本身会立刻返回 -14。此时 `notify.py` 会
> **额外弹一条明确的 macOS 告警**（「⚠️ 微信推送通道已失效，积分通知发不出去」，每天最多 1 次），
> 并进入 120 分钟冷却。游标判据 + 推送结果两条合并，不留盲区。
>
> 为什么需要这条单独告警：失效后所有微信通知只会「降级成 macOS 通知」，而通知内容是
> "签到成功 +10 积分"之类 —— **用户容易误以为通道正常**，实际只是本地弹窗，
> 手机微信什么都收不到。这条告警把真实原因说清楚。

**换主通道的备选**：配置 `pushplus_token`（需实名）走服务号模板消息，**无 session 概念**，真正无人值守。
日常推送量很小（每天 1–3 条），`max_pushes_per_day` 默认 8，留足余量。

### 会话失效时怎么恢复

**首选（推荐）：在 WorkBuddy 里重连，脚本自动跟随**

设置 → 远程通道 → 「微信助理」→ 重新连接 → 扫码确认。
脚本读的是 `settings.json`，会**自动改用新下发的 bot_id，无需任何本地操作**。
这是当前架构下最省事、也最一致的恢复方式。

**备选：让脚本自建独立凭据**（桌面端暂时不想动时用）

```bash
cd "<project>/00_For_Mac"
PY=python3   # 需 ≥3.10
$PY clawbot.py login            # 默认：等扫码 1500s，等消息 600s
$PY clawbot.py login 300 120    # 自定义：等扫码 300s，等消息 120s
```

流程：取二维码 → 渲染 `clawbot_login.html` → 轮询扫码 → 存本地凭据 →
**立即试推一条测试消息** → 顺带短探 15 秒抓 `context_token`（抓不到也无妨）。

**你通常只需要做一步**：

**打开 `clawbot_login.html`，用手机微信扫码并确认。**
- 二维码约 5 分钟过期，**过期会自动换码并重写同一个文件** → 刷新浏览器页面即可看到新码
- 页面顶部有时间戳，可判断看到的是新码还是旧码

> ⚠️ **2026-09-17 更正 —— 不带 `context_token` 推送不会到达微信。**
> 服务端仍会返回 `message_id`（并照样占用当日配额），但消息不会出现在微信里。
> `clawbot.py` 已据此把「服务端受理」与「真正送达」拆开：
> 缺令牌时 `send_text` 返回 `False`，`notify.py` 会降级成 macOS 通知，
> 并在说明里提示「请在微信给机器人发一条消息刷新令牌」。
> 所以扫码后**必须再发一条消息**，脚本才有投递能力。

> ⚠️ 扫码会创建一个**新的 bot_id**，并在凭据优先级上**压过桌面端绑定的那个**（见上文「凭据优先级」）。
> 副作用是微信里会多出一个「WorkBuddy」对话，且它随时可能被下次桌面端重连顶掉（见上文实测）。
> 若想回到「共用桌面端 bot」的状态，清空 `clawbot_state.json` 里的 `credentials` 字段即可。

### ⚠️ 2026-09-18：`prepare failed` —— 与「登录失效」完全不同的第二种通道故障

**症状**：签到和领奖都成功了（服务端可查证），但微信一条消息都没收到。

**根因**：发消息时服务端返回 `{"ret":-2,"errmsg":"prepare failed"}`。经过 A/B 对照确认：

| 探测 | 结果 | 说明 |
|---|---|---|
| `getconfig`（带当时的 context_token） | `ret:0` + `typing_ticket` | 凭据和令牌**都有效** |
| `sendtyping` | `ret:0` | 会话也没问题 |
| `sendmessage`（带 token） | `ret:-2 prepare failed` | 只有「投递」这一步被拒 |
| `sendmessage`（不带 token） | `ret:-2 prepare failed` | 说明**不是 token 的问题** |

结论：这是服务端拒绝**为这条主动消息建立会话**。但**成因尚未确认** —— 见下方更正。

**恢复动作**：**在微信里给机器人随便发一条消息（例如「1」）**，或**保持 WorkBuddy 桌面端运行**，
推送都会恢复 —— **不需要重新扫码**。这两类故障的恢复动作完全不同，务必分清：

| | 登录会话失效 | 投递被拒 |
|---|---|---|
| 响应 | `errcode=-14 / session timeout` | `ret=-2 / errmsg=prepare failed` |
| 恢复 | **重新扫码登录** | **发一条消息** / **保持桌面端运行**即可 |

#### ⚠️ 2026-09-18 更正：不要把 `prepare failed` 说成「会话窗口 N 小时过期」

早先（就在当天上午）我们把它归因为「距你上次给机器人发消息超过会话窗口时长」，并写下了
「9 小时内一定能发、32 小时后发不出」这样的边界。**当天下午的数据就把这个模型否掉了：**

| 时刻 | 距上次用户发消息 | 结果 |
|---|---|---|
| 09-17 09:30 | 9.1 小时 | ✅ 送达 |
| 09-18 09:14 | 32.8 小时 | ❌ `prepare failed`（**刚过夜休眠醒来**） |
| 09-18 11:20 | 34.9 小时 | ❌ `prepare failed` |
| 09-18 15:12 | **38.7 小时** | ✅ **送达**（`message_id=example-message-id`） |

**距上次发消息更久反而发成功了**，所以「按时间过期」的说法站不住。
两个观测还有个被忽略的共同点：失败那两次都发生在机器**过夜休眠醒来后不久**（`catchup.log`
里 00:42 → 09:14 有 8.5 小时空档）；而成功那次，桌面端的游标文件（`~/.workbuddy/claw-state/
weixin/*.cursor.json`）正在被**持续刷新** —— `renew.py` 的说明里写着「桌面端正常轮询间隔约 18 秒」。

**所以目前最合理的推测是**：这条通道能不能发，取决于**本机客户端是否在持续轮询这个 bot**
（也就是服务端那边这个「会话」还是不是活的），而不是「你多久没发过消息」。
样本仍然太少，这仍是推测，不是结论。

**实践含义**：

- 保持 WorkBuddy 桌面端在运行，比「记得偶尔发条消息」更可能让推送持续可用；
- 排查时别拿「多久没发消息」当判据 —— 它可能就是错的；
- 代码里那些熔断时长（120/60/20 分钟）只是**防止把 iLink 每日配额烧在注定失败的请求上**
  的工程取舍，**不代表我们已知道成因**。

**本次改动（避免再次「静默失效」）**

1. `clawbot._explain_error()` 不再把 `ret=-2` 一律翻译成「频率限制」——
   **所有分支都把服务端的 `errmsg` 原样带上**。之前那句自编的「频率限制」把真实原因
   盖掉了，排查时只能看到我们自己的说法，方向被完全带偏。
2. 新增 `DELIVER_BLOCKED_MARK = "投递被拒"`，`notify.py` 据此区分三类故障并分别熔断/告警。
3. **本机通知不再「闷声降级」**：投递被拒时额外弹一条明确告警，说清「发条消息就能恢复」。
4. `catchup.py` 的通知结果**写入 `catchup.log`**（`notify[级别] sent=… channel=… reason=…`）。
   以前返回值被直接丢掉，导致「微信没收到」时日志里看不出任何异常。

### 去重与熔断（为什么需要）

脚本每 5 分钟触发一次。若某类失败持续存在，同一内容会被反复推送 —— 既骚扰用户，又会白耗 iLink 每日 10 条配额（**失败的请求同样计入配额**）。

因此 `notify.py` 本地先拦两道：

- **去重**：同一 `(级别 + 标题 + 内容)` 在 `dedupe_window_minutes`（默认 360 分钟）内只推一次
- **熔断**：微信通道失败后进入冷却，冷却期内直接走本机通知，不做无谓请求。按失败类型分三档 ——

| 失败类型 | 默认熔断 | 为什么是这个时长 |
|---|---|---|
| 登录会话失效（`-14`） | 120 分钟 | 必须人工扫码，重试没有任何意义 |
| 投递被拒（`ret=-2 prepare failed`） | 60 分钟 | 要等用户主动发消息开窗，重试也没意义，但恢复后要尽快能发出去 |
| 其它（网络抖动等） | 20 分钟 | 下次或下下次触发就能重试 |

> **为什么把熔断从「只对 -14」扩大到「任何失败」（2026-09-18）**：触发频率从 30 分钟提到
> 5 分钟后，一次持续故障若每 5 分钟重试一次，**一小时就能把 iLink 每日 10 条的配额烧光** ——
> 等通道真恢复时反而没额度了。熔断是提高频率的必要配套。

记录写在 `notify_state.json`，可安全删除（删后最多重复推一次）。最后一次失败原因也记在这里，
用 `python3 notify.py status` 可看 `last_send_error`。

#### 去重记两张表（2026-09-18 修的真实缺陷）

**旧行为**：推送失败 → 降级成本机通知 → **但去重指纹照样被记成「已送达」**。
后果是：等你后来发了消息把会话窗口重新打开，那条消息**再也不会被尝试推送** ——
「降级过一次」变成了「永久放弃」。积分确实到手了，但那一条到账消息你永远看不到。

**现在的语义**：指纹按**通道**分开记，两张表各管一件事 ——

| 表 | 什么时候写 | 拦住什么 | 放行什么 |
|---|---|---|---|
| `sent` | **只有真的送到微信**时 | 拦住所有后续重试（含本机，避免重复弹） | — |
| `local_sent` | 只弹过本机通知时 | 只拦住本机重复弹窗 | **放行微信通道** → 通道恢复后自动补发 |
| （都不写） | 微信和本机**都失败**时 | — | 留给下一次运行继续补发 |

一句话：**只有真的送到微信，才配记「已送达」。** 这条不变量由自检第 8f 节强制守着
（整个 `notify.py` 只有一处写 `sent` 指纹，且必须被 `if wechat_delivered` 守住）。

另外，熔断期内**只要你给机器人发了一条消息**，冷却会立刻失效（而不是傻等满 60/120 分钟）——
因为告警文案承诺的是「发一条消息即刻恢复」，代码必须真的做到。

### 配置字段

| 字段 | 默认 | 说明 |
|---|---|---|
| `channel` | `auto` | `auto` / `clawbot` / `pushplus` / `serverchan` |
| `pushplus_token` | 空 | 备用通道，留空即不启用 |
| `serverchan_key` | 空 | 备用通道，留空即不启用 |
| `notify_success` | `true` | 领到积分时推送 |
| `notify_failure` | `true` | 签到/旅行失败时推送 |
| `notify_warning` | `true` | 警告类（如当前无签到活动）推送 |
| `notify_noop` | `false` | 无事发生时不推送，保持 false 以免打扰 |
| `macos_notification` | `true` | 微信通道不可用时补一条 macOS 通知兜底 |
| `dedupe_window_minutes` | `360` | 同一内容多少分钟内只推一次；设 `0` 关闭去重 |
| `max_pushes_per_day` | `8` | 微信通道每日推送上限（iLink 硬上限 10） |
| `clawbot_cooldown_minutes` | `120` | 登录失效后的熔断时长 |
| `clawbot_blocked_cooldown_minutes` | `60` | 投递被拒后的熔断时长 |
| `clawbot_failure_cooldown_minutes` | `20` | 其它失败后的熔断时长 |

> 这三档熔断键**即使配置文件里没写也会生效** —— `notify.py` 的 `DEFAULT_CONFIG` 里三档都有，
> `load_config()` 把文件覆盖在默认值之上。所以「漏配一个键 → 冷却退化成 0」这种事不会发生。

### 存活监控（watchdog，2026-09-18 新增）

**它解决什么问题**：`catchup.py` 的所有告警都建立在「它自己跑起来了」这个前提上。
如果 LaunchAgent 没被加载、plist 被删、Python 被卸载、项目文件夹被移走 ——
主脚本会**连告警机制一起静默死掉**，你不会有任何感知。这是整套系统最大的盲区，
**也是唯一无法靠 `catchup.py` 自己解决的故障**。

**它怎么做**：一个**完全独立**的计划任务，只做两件事 ——

1. `state.json` 的 mtime 是不是太旧了（主脚本每轮都写它，所以它就是心跳；阈值 **90 分钟**）。
   判定前会先读 `state.json` 的**内容**：主脚本在「凌晨静默期」与「当日收工后」是**故意
   不写心跳**的，这两段内的陈旧属于设计预期，不算故障（2026-09-19 修 —— 此前每次长时间
   合盖再开盖都会收到一条假警报，当天实测误报了一次「已 122 分钟没有运行」）
2. LaunchAgent（Windows 上是计划任务）是不是还挂着

发现问题就弹一条**本机通知**（每类问题最多每 6 小时提醒一次）。

**为什么它必须「愚蠢」**：`watchdog.py` **不 import 任何项目模块**（连发通知都自己内联实现），
免得同一个故障把主脚本和监控一起干掉。它甚至刻意不复用 `notify.py` —— 那正是可能已经坏掉的组件。
这条约束由自检第 8d 节用 AST 检查真实的 `import` 语句来守着。

**它自己也无人监控** —— 这是原理性的（无限套娃）。所以它不是「万能兜底」，
而是「把最可能发生的静默死亡变成一条你看得见的本地通知」。

```bash
# mac：装两个 LaunchAgent（必须在 Terminal.app 里跑一次，见下节说明）
cd "<本文件夹>"
bash install.sh            # 主任务 5 分钟 + watchdog 30 分钟，一起装
bash install.sh status     # 看是否真的加载了（含 watchdog 自己的判定）

# Windows：重跑一次安装即可，会自动注册两个计划任务
python install.py install
python install.py status     # 能看到 WorkBuddyRewardCatchup + WorkBuddyRewardWatchdog

# 两边都能手动验一次（只读，不通知任何东西）
python3 watchdog.py status
```

### 测试与排查

```bash
cd "<project>/00_For_Mac"
# 解释器：必须 ≥3.10（系统自带的 /usr/bin/python3 是 3.9，**不能用**）。
# install.sh 会自动挑一个，照抄它打印出来的那个即可，例如：
PY=python3
$PY notify.py status    # 通道状态 + 凭据 + context_token + 冷却 + 今日已用配额（不发送）
$PY notify.py           # 真实推送一条测试通知
$PY notify.py localtest # 只测本机兜底通知这一级：查通知开关 + 专注模式 + 实弹一条（不碰微信、不耗配额）
$PY watchdog.py status  # 看主脚本的存活判定（只读，不通知）
$PY clawbot.py status   # 只看 ClawBot 凭据与 context_token（脱敏）
$PY clawbot.py login    # 会话失效且不想动桌面端时：自建独立凭据（会压过桌面端凭据）
$PY clawbot.py test     # 绕过 notify 直接实测 ClawBot 通道
```

> ⚠️ **不要随便跑 `clawbot.py wait`**：它是主动长轮询，会与 WorkBuddy 桌面端**抢消息**，
> 可能让你发给桌面端的指令丢失。仅在排障时短时使用（`clawbot.py wait 15`）。

**故障对照**

| 现象 | 原因与处理 |
|---|---|
| `errcode=-14` / `session timeout` | **微信登录会话失效** → 首选在 WorkBuddy 设置 → 远程通道里**重新连接「微信助理」**（脚本会自动改用它下发的新凭据）；只想让脚本单独恢复时才跑 `clawbot.py login`。冷却期内自动走 macOS 通知 |
| 日志显示"已送达"但微信收不到 | 旧版判定 bug（只查 `ret` 未查 `errcode`），已修；新版成功会带 `message_id`。若仍出现请贴 `python3 clawbot.py test` 的完整输出 |
| `HTTP 429` | 当日主动推送配额用尽（24h/10 条），等次日恢复 |
| `HTTP 401 / 403` | 凭据被拒 → 跑 `clawbot.py login` 重新扫码 |
| 凭据找不到 | 既无 `clawbot_state.json` 本地凭据，`settings.json` 里也没有 `weixinClawBot` → 跑 `clawbot.py login` |
| 扫码页显示旧码 | 等自动换码后**刷新浏览器**；或重跑 `login` |


## 激活（安装 LaunchAgent）

两个 LaunchAgent 都由 `install.sh` **一并安装**，必须在 **Terminal.app** 里执行一次：

```bash
cd "<本文件夹>"            # 例如 .../00_Workbuddy自动签到领积分/00_For_Mac
bash install.sh            # 安装（已存在则重装；主任务 5 分钟 + watchdog 30 分钟）
bash install.sh status     # 复查：是否真的加载了 + watchdog 自己的判定
```

**为什么必须手动、且必须在 Terminal.app**：WorkBuddy 的 agent 进程不在你的图形登录会话里，
对**任何** plist 执行 `launchctl bootstrap` 都会报
`Bootstrap failed: 5: Input/output error`（连只跑 `/bin/echo` 的空 plist 也一样，已实测）。
这是环境限制，不是脚本问题。

**为什么 plist 不作为文件放进本文件夹**：plist 里必须写死本文件夹的绝对路径，
一旦作为文件存在，把文件夹拷到别处就会指向不存在的路径 —— 那正是「拷走即失效」的根因。
`install.sh` 改为**安装时按实际路径现场生成**（并自动探测一个 ≥3.10 的解释器），
所以本文件夹可以整体拷到任何位置，重新 `bash install.sh` 即可。

**安装后的验收判据**（三项都要满足，只看「已加载」不够）：

1. `launchctl print` 显示 `runs >= 1` 且 `last exit code = 0` —— 证明它**真的被调度过并跑成功了**；
2. `watchdog.log` 出现 `ok state_age=… job_loaded=True` —— 证明它的判定是「健康」而不是「没吭声」；
3. 手工构造异常（心跳过期 / 任务卸载）时确实弹通知，且同一问题 6 小时内不重复打扰 —— 证明它的告警链路是通的。

> ⚠️ **两个任务共用同一个解释器**：`install.sh` 自动挑一个 ≥3.10 的（优先托管运行时的
> `current` 指针）。项目大量使用 `X | None` 注解，语法下限是 Python 3.10 ——
> 系统自带的 `/usr/bin/python3` 是 3.9.6，**不能用**（导入即 `TypeError`）。
> 若哪天那个版本目录被清理，**两个任务会一起失效**（`last exit code` 变成非 0）。
> 5 秒自检：`launchctl print gui/$(id -u)/com.workbuddy.wb-reward-catchup | grep "last exit"`，
> 正常应为 `0`；若报错，在该文件夹里重新 `bash install.sh` 即可 —— 它会自动挑一个现存可用的。

## 验证

```bash
cd "<本文件夹>"
bash install.sh status      # 两个任务是否加载 + watchdog 自己的存活判定
```

## 卸载

```bash
cd "<本文件夹>"
bash install.sh uninstall   # 两个 LaunchAgent 一起卸载，项目文件保留
```

## 注意

- **本文件夹是自包含的**：拷到任意位置后 `bash install.sh` 即可用（plist 按实际路径现场生成）。
  唯一的例外是**改名/移动后要重装一次** —— LaunchAgent 里记的是当时的绝对路径，
  不重装就还指向老位置。运行时文件集中在 `runtime/`，跟着文件夹一起走；安装脚本会自动迁移旧布局并收敛权限。
- **本目录与 `../00_For_Win/` 是同一套逻辑的两个平台实现**，改名/同步规则见 `../00_For_Win/README.md` 第 5 节。
- 需已登录 WorkBuddy 桌面端。长期不开，token 可能过期 → 补跑报 401，打开一次 WorkBuddy 即恢复。
- 签到按自然日结算：电脑整天关着补不到当日签到，连续天数会重置（接口规则）。
- 现已**不使用** WorkBuddy 自动化（避免每次触发消耗额度），仅靠本机 LaunchAgent + 通知模块。
- **微信推送默认已就绪**：走腾讯 iLink 官方通道，**开箱即用、免注册免实名**。
  若某天收到"需重新登录"的提示，跑一次 `python3 clawbot.py login` 扫码即可恢复（见上文「重新登录」）。
- **微信通道已统一为一条**：脚本与 WorkBuddy 桌面端**共用同一个 ClawBot**
  （`example-current-bot@im.bot`，2026-09-16 起）。所以微信里只有一个「WorkBuddy」对话，不会混淆。
  代价是脚本不能再主动轮询（会抢同一个 bot 的消息队列），连接守护改为只读桌面端游标文件。
- **用途边界**：本通道只发积分提醒。工作性质的通知走**企业微信**，不受本通道状态影响。
- **iLink 每日 10 条主动推送上限**：本模块封顶 8 条/日，触顶后自动降级 macOS 通知。签到+旅行正常一天只推 1–3 条，余量充足。
- **推送内容限纯文本**：iLink 不支持表格 / 图片 / 代码块，Markdown 渲染不稳定。
- `scripts/` 内的引擎是从 Skill `workbuddy-reward-helper` 复制的**冻结副本**：不再跟随该 Skill 更新。
  若 WorkBuddy 官方接口改版（尤其 Buddy 旅行活动接口下线），需自行改引擎或重新安装该 Skill。
