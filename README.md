# WorkBuddy Auto Check-In

WorkBuddy 桌面端的本地签到与旅行奖励补跑工具，支持 macOS 与 Windows，零第三方 Python 依赖。

## 功能

- 每日 07:00 后自动签到并派遣旅行。
- 旅行奖励到达后立即领取，不等固定时间。
- 错过触发时间后按自然日补跑，当日重复执行会自动去重。
- 从本机 WorkBuddy 客户端读取接口与登录态，接口变更时优先自动发现新端点。
- 兼容 WorkBuddy 客户端 5.6.2 起的登录态字段加密：登录凭据被 AES-256-GCM 信封加密时自动解密后再取用（macOS 与 Windows 均已支持，详见下方平台现状）。
- WorkBuddy 官方接口默认直连；显式 `HTTP_PROXY` / `HTTPS_PROXY` 仍生效，设置 `WORKBUDDY_PROXY_MODE=system` 可跟随系统代理。
- 签到、领取、失败和接口异常可推送通知；ClawBot 微信通知不可用时降级为本机通知。
- 独立 watchdog 监控主任务是否停止运行。
- 00:00–07:00 与当日签到、领取都完成后立即退出，避免无意义空转。
- Windows 增加每日 07:00 与白天每小时唤醒点，电脑睡眠时也能完成当日签到；可用 `--no-wake` / `--no-daywake` 关闭。
- macOS 提供 `daily_wake.sh`，用系统电源计划额外设一条每日保底唤醒（默认 07:05），把合盖时「能否签到全看系统当次给的维护唤醒窗口」变成有保底；需手动 `sudo` 执行一次。
- Windows 本机通知改为右下角卡片，并避免 watchdog 子进程弹出控制台窗口。
- watchdog 识别预期的静默期与「刚睡眠唤醒」两种情形，不把正常收工或睡眠后的补跑误报为停摆。
- 区分网络层与业务层失败：断网或代理拦截不会消耗当日重试额度，网络恢复后自动继续。
- 微信推送被服务端拒绝时，会在后续运行中自动重试并补发，无需人工干预。
- 投递被拒后按间隔限流重试（失败请求同样计入每日推送配额），避免把当日额度烧光。
- 关键通知未能送达微信时进入补发队列，后续自动补发；已送达或已过时的条目不再重复推送。
- 运行状态、配置、缓存和日志集中在各平台目录的 `runtime/`，不写入仓库。

> **平台现状**：WorkBuddy 客户端 5.6.2 起把登录态里的凭据字段改为加密存储（AES-256-GCM 信封），
> 旧版读取方式会失效。**Windows 与 macOS 两侧均已适配并实测通过**：
>
> - **Windows**：从运行中的客户端进程内存取回密钥，再本地解密；
> - **macOS**：借客户端自带的运行时取回密钥，再本地解密 —— 无需 root、无需注入、
>   也无需读取其它进程的内存；另提供一条「回环 CDP 直取明文」的兜底路径
>   （客户端以 `--remote-debugging-port` 启动时可用），该路径不复刻加密算法，
>   客户端将来更改加密格式也不受影响。
>
> 两条路径取到的密钥都只在内存中使用，不落盘。

## 目录

```text
00_For_Mac/   macOS 交付物与安装入口
00_For_Win/   Windows 交付物、诊断与自检入口
```

## 安装

### macOS

要求：Python 3.10 或更高版本，WorkBuddy 桌面端已登录。

```bash
cd 00_For_Mac
python3 -m py_compile *.py scripts/*.py
bash install.sh
bash install.sh status
```

`install.sh` 会注册两个 LaunchAgent：

- `com.workbuddy.wb-reward-catchup`：每 5 分钟触发主任务。
- `com.workbuddy.wb-reward-watchdog`：每 30 分钟检查主任务状态。

合盖时 launchd 不会执行任务，当天能否签到取决于系统当次给的维护唤醒窗口（实测 45 秒的能跑完、
2 秒的来不及）。想加一条保底，用 `daily_wake.sh` 额外设一个每日固定唤醒点：

```bash
sudo bash daily_wake.sh enable        # 默认 07:05，可带 HH:MM 改时刻
bash daily_wake.sh status             # 查看当前设置（无需 sudo）
sudo bash daily_wake.sh disable       # 取消
```

它只写系统电源计划（`pmset repeat`），不注册 LaunchAgent、不写项目状态。

### Windows

要求：Python 3.10 或更高版本并加入 PATH，WorkBuddy 桌面端已登录。

```cmd
cd 00_For_Win
py -3 -m py_compile *.py scripts/*.py
install.cmd
doctor.cmd
```

`install.cmd` 会注册四个计划任务：

- `WorkBuddyRewardCatchup`：每 5 分钟触发主任务。
- `WorkBuddyRewardWatchdog`：每 30 分钟检查主任务状态。
- `WorkBuddyRewardWake`：每天 07:00 唤醒电脑执行主任务；安装时可使用 `--no-wake` 禁用。
- `WorkBuddyRewardDayWake`：07:00–23:00 每小时一个唤醒点；安装时可使用 `--no-daywake` 禁用。

绑定微信通知后，可运行 `check_channel.cmd` 验证消息是否真能送达微信——「已绑定」不等于「可送达」。

## 微信通知

项目优先使用 WorkBuddy 桌面端已绑定的 ClawBot / iLink 通道发送微信文本通知。没有可用通道时，通知会降级为本机通知。

微信主动推送受服务端「会话窗口」约束：只有在你近期给该机器人发过消息之后，主动消息才可能被送达。窗口关闭期间推送会被拒绝（**失败请求同样计入每日配额**），脚本会将失败写入日志并通过本机通知提示；窗口重开后自动补发，无需重新扫码。

推送被拒或缺少会话凭据时，脚本会在后续运行中先检查是否有新的入站消息（即窗口是否已重开），再决定是否补发。仍未送达的关键通知进入补发队列，后续运行自动补发；已确认送达的条目自动作废，超过 48 小时的记录不再补发。

## 安全与隐私

- 不提交、不上传账号 token、context token、运行状态或日志。
- 访问令牌只在内存中传递，不会写入输出日志。
- `runtime/` 已被 `.gitignore` 排除；不要把其中的状态文件复制或分享给别人。
- 仅访问实现签到、旅行状态、领取和通知所需的本地 WorkBuddy 接口。

## 免责声明

本项目用于个人设备的本地自动化，不与 WorkBuddy 官方关联。使用前请确认并遵守 WorkBuddy 服务条款；接口可能随客户端更新变化，使用风险由使用者自行承担。

## License

[MIT](LICENSE)
