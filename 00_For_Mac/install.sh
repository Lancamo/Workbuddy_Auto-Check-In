#!/bin/bash
# ============================================================
# install.sh —— 安装 / 查看 / 卸载本文件夹的两个 LaunchAgent
#
#   bash install.sh            # 安装（已存在则重装）
#   bash install.sh status     # 看是否真的加载了
#   bash install.sh uninstall  # 卸载（不删项目文件）
#
# 为什么 plist 不作为文件放进仓库：
#   plist 里必须写死「本文件夹的绝对路径」和「解释器路径」。一旦它作为文件存在，
#   把本文件夹拷到别处（或换台机器）就会指向不存在的路径 —— 这正是「拷走即不能用」
#   的根因。所以改为**安装时按实际路径现场生成**，本文件夹才能真正自包含。
#
# 为什么必须由你在 Terminal.app 里手动跑：
#   WorkBuddy 的 agent 进程不在你的图形登录会话里，对**任何** plist 执行
#   `launchctl bootstrap` 都会报 `Bootstrap failed: 5: Input/output error`
#   （连只跑 /bin/echo 的空 plist 也一样，已实测）。
#
# 安装后两个任务：
#   com.workbuddy.wb-reward-catchup    每 5 分钟 —— 主脚本（签到 + 旅行领奖 + 推送）
#   com.workbuddy.wb-reward-watchdog   每 30 分钟 —— 存活监控（只查主脚本还活着没）
# ============================================================
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL_MAIN="com.workbuddy.wb-reward-catchup"
LABEL_WD="com.workbuddy.wb-reward-watchdog"
DEST="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
RUNTIME="$DIR/runtime"
STATE_DIR="$RUNTIME/state"
CONFIG_DIR="$RUNTIME/config"
LOG_DIR="$RUNTIME/logs"

migrate_runtime() {
  mkdir -p "$STATE_DIR" "$CONFIG_DIR" "$LOG_DIR"
  chmod 700 "$RUNTIME" "$STATE_DIR" "$CONFIG_DIR" "$LOG_DIR"

  # One-time migration from the old layout; existing state and credentials stay usable.
  for name in state.json state.bak.json catchup.lock notify_state.json \
              renew_state.json watchdog_state.json clawbot_state.json; do
    if [ -f "$DIR/$name" ] && [ ! -f "$STATE_DIR/$name" ]; then
      mv "$DIR/$name" "$STATE_DIR/$name"
    fi
  done
  for name in notify_config.json renew_config.json; do
    if [ -f "$DIR/$name" ] && [ ! -f "$CONFIG_DIR/$name" ]; then
      mv "$DIR/$name" "$CONFIG_DIR/$name"
    fi
  done
  for name in catchup.log launchd.out.log launchd.err.log \
              watchdog.log watchdog.out.log watchdog.err.log; do
    if [ -f "$DIR/$name" ] && [ ! -f "$LOG_DIR/$name" ]; then
      mv "$DIR/$name" "$LOG_DIR/$name"
    fi
  done

  find "$STATE_DIR" "$CONFIG_DIR" -type f -exec chmod 600 {} +
}

# ---------------------------------------------------------------------------
# 解释器探测
# ---------------------------------------------------------------------------
# 项目大量使用 `X | None` 注解，语法下限是 Python 3.10，所以不能随便挑一个 python3
# （macOS 自带的 /usr/bin/python3 是 3.9，导入即 TypeError）。
# 优先级：环境变量 WB_PYTHON → 托管运行时的 current 指针 → 托管目录里任一可用版本
#         → PATH 里的 python3。第一个「存在且 ≥3.10」的胜出。
_py_ok() {
  [ -x "${1:-}" ] || return 1
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null
}

detect_python() {
  local cand
  if [ -n "${WB_PYTHON:-}" ]; then
    if _py_ok "$WB_PYTHON"; then printf '%s\n' "$WB_PYTHON"; return 0; fi
    echo "✗ WB_PYTHON 指定的解释器不可用或低于 3.10：$WB_PYTHON" >&2
    return 1
  fi

  local root="$HOME/.workbuddy/binaries/python/versions"
  if [ -f "$root/current" ]; then
    local v
    v="$(tr -d '[:space:]' < "$root/current" 2>/dev/null)"
    cand="$root/$v/bin/python3"
    if _py_ok "$cand"; then printf '%s\n' "$cand"; return 0; fi
  fi

  if [ -d "$root" ]; then
    local d
    for d in "$root"/*/; do
      cand="${d%/}/bin/python3"
      if _py_ok "$cand"; then printf '%s\n' "$cand"; return 0; fi
    done
  fi

  cand="$(command -v python3 2>/dev/null || true)"
  if _py_ok "$cand"; then printf '%s\n' "$cand"; return 0; fi

  return 1
}

# ---------------------------------------------------------------------------
# 生成 plist
# ---------------------------------------------------------------------------
gen_plist() {
  # $1=Label $2=脚本绝对路径 $3=间隔秒 $4=stdout 日志 $5=stderr 日志 $6=用途说明
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$1</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$2</string>
    </array>

    <!-- 加载即跑一次；睡眠唤醒后 launchd 会把过期的间隔任务立即补发 -->
    <key>RunAtLoad</key>
    <true/>

    <!-- $6 -->
    <key>StartInterval</key>
    <integer>$3</integer>

    <key>ProcessType</key>
    <string>Background</string>

    <!-- 日志放在 runtime/logs；install.sh 已先建好目录并收敛权限 -->
    <key>StandardOutPath</key>
    <string>$4</string>
    <key>StandardErrorPath</key>
    <string>$5</string>
</dict>
</plist>
EOF
}

# ---------------------------------------------------------------------------
# 安装一个 LaunchAgent（含回读确认）
# ---------------------------------------------------------------------------
install_one() {
  # $1=Label $2=plist 内容
  local label="$1" body="$2" path="$DEST/$1.plist"

  printf '%s\n' "$body" > "$path" || return 1

  # 先用 plutil 验一遍 XML —— 结构错误会让 launchctl 报难懂的错
  if ! plutil -lint "$path" >/dev/null 2>&1; then
    echo "  ✗ 生成的 plist 不是合法 XML：$path"
    plutil -lint "$path"
    return 1
  fi

  launchctl bootout "$DOMAIN/$label" 2>/dev/null

  # ⚠️ 「load 返回 0」不等于「已加载」—— 实测 `launchctl load -w` 在失败时也返回 0。
  # 所以必须回读确认，否则会报一个假的 ✓。
  launchctl bootstrap "$DOMAIN" "$path" 2>/dev/null \
    || launchctl load -w "$path" >/dev/null 2>&1 \
    || true
  sleep 1

  if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    local runs
    runs="$(launchctl print "$DOMAIN/$label" 2>/dev/null | sed -n 's/^[[:space:]]*runs = //p')"
    echo "  ✓ $label 已加载（已执行 ${runs:-0} 次）"
    return 0
  fi
  echo "  ✗ $label 未能加载 —— 回读 launchd 查不到这个任务"
  return 1
}

show_one() {
  launchctl print "$DOMAIN/$1" 2>/dev/null \
    | grep -E "state = |runs = |last exit code|run interval" \
    | sed 's/^[[:space:]]*/    /' | sort -u
}

# ---------------------------------------------------------------------------
case "${1:-install}" in
  install)
    echo "项目目录：$DIR"
    echo

    # 入口脚本必须在位 —— 否则装了也是空跑
    local_ok=1
    for f in catchup.py watchdog.py notify.py clawbot.py renew.py; do
      [ -f "$DIR/$f" ] || { echo "✗ 缺少 $f —— 本文件夹不完整"; local_ok=0; }
    done
    [ -d "$DIR/scripts" ] || { echo "✗ 缺少 scripts/ 目录"; local_ok=0; }
    [ "$local_ok" -eq 1 ] || exit 1

    PYTHON="$(detect_python)" || {
      cat >&2 <<'HINT'
✗ 找不到可用的 Python（要求 ≥3.10）。

  项目里用了 `X | None` 这类注解，语法下限是 3.10；macOS 自带的
  /usr/bin/python3 是 3.9，**不能用**（导入即 TypeError）。

  常用做法：先启动一次 WorkBuddy 桌面端（它会带出托管运行时），
  或显式指定：WB_PYTHON=/path/to/python3 bash install.sh
HINT
      exit 1
    }
    echo "解释器：$PYTHON"
    "$PYTHON" -V | sed 's/^/        /'
    echo

    # Stop old jobs before moving their active state/log files.
    launchctl bootout "$DOMAIN/$LABEL_MAIN" >/dev/null 2>&1 || true
    launchctl bootout "$DOMAIN/$LABEL_WD" >/dev/null 2>&1 || true
    migrate_runtime

    mkdir -p "$DEST"
    ok=1
    install_one "$LABEL_MAIN" "$(gen_plist "$LABEL_MAIN" "$DIR/catchup.py" 300 \
      "$LOG_DIR/launchd.out.log" "$LOG_DIR/launchd.err.log" \
      "每 5 分钟轮询一次。闸门未开时这次触发只做本地判断、零网络请求，所以高频代价极小")" || ok=0
    install_one "$LABEL_WD" "$(gen_plist "$LABEL_WD" "$DIR/watchdog.py" 1800 \
      "$LOG_DIR/watchdog.out.log" "$LOG_DIR/watchdog.err.log" \
      "每 30 分钟查一次主脚本心跳与计划任务是否还在，异常时弹本机通知")" || ok=0

    echo
    if [ "$ok" -ne 1 ]; then
      cat <<'HINT'
✗ 有任务没装上。若报 “Bootstrap failed: 5: Input/output error”，说明当前进程
  不在你的图形登录会话里 —— 请在 **Terminal.app** 里直接重跑本脚本。

  装完用 `bash install.sh status` 复查。
HINT
      exit 1
    fi

    echo "— launchd 中的状态 —"
    show_one "$LABEL_MAIN"
    show_one "$LABEL_WD"
    echo
    echo "完成。之后："
    echo "  手动跑一次看输出：$PYTHON \"$DIR/catchup.py\""
    echo "  看主脚本存活判定：$PYTHON \"$DIR/watchdog.py\" status"
    echo "  卸载：bash \"$DIR/install.sh\" uninstall"
    ;;

  status)
    for label in "$LABEL_MAIN" "$LABEL_WD"; do
      echo "▸ $label"
      if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
        show_one "$label"
      else
        echo "    （未加载）"
      fi
    done
    echo
    echo "▸ 主脚本存活判定（watchdog 自己的结论）"
    PYTHON="$(detect_python 2>/dev/null || echo python3)"
    if [ -f "$DIR/watchdog.py" ]; then
      "$PYTHON" "$DIR/watchdog.py" status 2>&1 | sed 's/^/    /'
    else
      echo "    （找不到 watchdog.py）"
    fi
    ;;

  uninstall)
    for label in "$LABEL_MAIN" "$LABEL_WD"; do
      launchctl bootout "$DOMAIN/$label" 2>/dev/null && echo "✓ 已卸载 $label" || echo "（$label 本来就没加载）"
      rm -f "$DEST/$label.plist"
    done
    echo "项目文件未删除，只是取消了自动触发。"
    ;;

  *)
    echo "用法：bash $(basename "$0") [install|status|uninstall]"
    exit 2
    ;;
esac
