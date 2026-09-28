# WorkBuddy Auto Check-In

WorkBuddy 桌面端的本地签到与旅行奖励补跑工具，支持 macOS 与 Windows，零第三方 Python 依赖。

## 功能

- 每日 07:00 后自动签到并派遣旅行。
- 旅行奖励到达后立即领取，不等固定时间。
- 错过触发时间后按自然日补跑，当日重复执行会自动去重。
- 从本机 WorkBuddy 客户端读取接口与登录态，接口变更时优先自动发现新端点。
- 兼容 WorkBuddy 客户端 5.6.2 起的登录态字段加密：登录凭据被 AES-256-GCM 信封加密时自动解密后再取用（Windows 侧，详见下方平台现状）。
- WorkBuddy 官方接口默认直连；显式 `HTTP_PROXY` / `HTTPS_PROXY` 仍生效，设置 `WORKBUDDY_PROXY_MODE=system` 可跟随系统代理。
- 签到、领取、失败和接口异常可推送通知；ClawBot 微信通知不可用时降级为本机通知。
- 独立 watchdog 监控主任务是否停止运行。
- 00:00–07:00 与当日签到、领取都完成后立即退出，避免无意义空转。
- Windows 增加每日 07:00 与白天每小时唤醒点，电脑睡眠时也能完成当日签到；可用 `--no-wake` / `--no-daywake` 关闭。
- Windows 本机通知改为右下角卡片，并避免 watchdog 子进程弹出控制台窗口。
- watchdog 识别预期的静默期，不把正常收工误报为停摆。
- 区分网络层与业务层失败：断网或代理拦截不会消耗当日重试额度，网络恢复后自动继续。
- 微信推送凭据过期时自动重新获取并重发，无需人工干预。
- 关键通知未能送达微信时进入补发队列，后续自动补发；已过时的提醒不再推送。
- 运行状态、配置、缓存和日志集中在各平台目录的 `runtime/`，不写入仓库。

> **平台现状**：WorkBuddy 客户端 5.6.2 起把登录态里的凭据字段改为加密存储，旧版读取方式会失效。
> 该适配目前只在 **Windows** 侧完成（已真机实测，签到与领取正常）；
> **macOS** 侧沿用既有实现，本次未含该适配，在 5.6.2 客户端上仍会因凭据加密而无法取到登录态。

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

微信主动推送依赖 WorkBuddy 桌面端维持会话。桌面端停止运行、会话失效或用户重新绑定机器人时，通知可能失败；脚本会将失败写入日志并通过本机通知提示。

会话凭据失效导致推送被拒时，脚本会自动重新获取凭据并重发。仍未送达的关键通知会进入补发队列，在后续运行中自动补发；超过 48 小时的记录不再补发。

## 安全与隐私

- 不提交、不上传账号 token、context token、运行状态或日志。
- 访问令牌只在内存中传递，不会写入输出日志。
- `runtime/` 已被 `.gitignore` 排除；不要把其中的状态文件复制或分享给别人。
- 仅访问实现签到、旅行状态、领取和通知所需的本地 WorkBuddy 接口。

## 免责声明

本项目用于个人设备的本地自动化，不与 WorkBuddy 官方关联。使用前请确认并遵守 WorkBuddy 服务条款；接口可能随客户端更新变化，使用风险由使用者自行承担。

## License

[MIT](LICENSE)
