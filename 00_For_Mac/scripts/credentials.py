#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
credentials.py — WorkBuddy 积分助手 · 统一登录态读取（Phase 1）

设计要点：
  - 明文登录态优先；v5.6.2+ 起改为解 at-rest 加密信封（见同目录 atrest.py）
  - 提供跨平台路径候选与统一安全规则
  - 登录态结构使用 account.uid + auth.accessToken（含 auth.domain）

登录态只有一个来源：`workbuddy-desktop.info`（纯 Python 读取，无需外部运行时）。
       macOS:   ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
       Windows: %LOCALAPPDATA%（优先）/ %APPDATA% 下的 CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
       Linux:   ~/.config/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
     结构：{ account: { uid, ... }, auth: { accessToken, refreshToken, expiresAt, domain, ... }, ... }

同一个文件里的 `auth.accessToken` 有两种形态，本模块都要能读：
  ① **明文 JWT** —— v5.5.x 及更早，或客户端关闭了字段加密。
  ② **at-rest 加密信封** `{"$wbEncrypted":1,"envelope":"<base64>"}` —— **v5.6.2 起默认开启**，
     委托同目录的 `atrest.py` 解密（AES-256-GCM；密钥获取见 atrest 的密钥定位链）。
     本机实测解密正常。
  ⟹ 两种形态都试；解不开时报**准确原因**（见下），不要笼统地说「未找到登录态」。

兜底（**路线 B**）：若客户端是以 `--remote-debugging-port` 启动的，则改走回环 CDP
直接取**明文** token（模块 `cdp_token.py`）。它不复刻 at-rest 算法，因此**客户端将来
改加密格式也不受影响** —— 这是「抗版本」的保险，不是主路径。

统一返回结构（load_credentials()）：
  {
    "access_token": "...",   # 真实 token。等同账号密码，调用方负责保密：勿打印 / 勿写日志 / 勿落盘
    "uid": "...",            # 可能为空字符串
    "domain": "https://...", # 可能为空字符串；接口 host 以此优先
    "source": "workbuddy-desktop.info" | "cdp-remote-debug"
  }

⚠️ 2026-09-28 移除：旧版 `state.vscdb` + Electron safeStorage 回退链路（与 Windows 侧同步）。
  证据（macOS 本机实测）：
    · `~/Library/Application Support/WorkBuddy/User/globalStorage/state.vscdb` **不存在**；
    · 存在的那个 `.../CodeBuddy/User/globalStorage/state.vscdb` 属于 **CodeBuddy CN**，
      与 WorkBuddy 桌面端登录态无关；
    · 本机既无 `~/.workbuddy/tools/electron`，PATH 里也没有 electron —— 一旦命中那个无关的
      vscdb，就会抛「需要 Electron 运行时解密」，**掩盖真正的失败原因（字段加密）**。
  故整链（sqlite3 读取 + 子进程调 Electron safeStorage + 临时文件）一并移除，
  同时省掉 `shutil`/`sqlite3`/`subprocess`/`tempfile` 四个依赖。
  副作用：`source` 字段不再可能是 `"state.vscdb"`。

安全规则（务必遵守）：
  - access_token 等同账号密码：仅在内存中使用，禁止输出到 stdout/日志、禁止保存副本、禁止提交仓库、禁止上传任何第三方
  - 只读：不修改 WorkBuddy 客户端的任何文件
  - 本模块不发任何外部网络请求（唯一的连接是**可选**的回环 CDP 兜底，且只连 127.0.0.1）
  - 解密得到的明文只在内存中流转，用完即弃，绝不落盘
  - atRestSecretKey 由 atrest.py 取回后**同样不落盘**
"""

from __future__ import annotations

import json
import os
import sys

# 允许在同目录找到 atrest（WorkBuddy 5.6.2+ 登录态字段加密信封的解密模块）。
# checkin.py / main.py 会先把自己目录插入 sys.path，这里再兜底一次，
# 保证 credentials.py 无论被直接运行还是被上层模块 import 都能 import atrest。
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import atrest  # noqa: E402

SOURCE_DESKTOP_INFO = "workbuddy-desktop.info"
SOURCE_CDP = "cdp-remote-debug"

# 候选登录态文件「存在但被系统拒读」（如 macOS 隐私保护/TCC）的记录，
# 仅用于生成准确的错误信息，不含任何敏感内容。
_PERM_DENIED: list[str] = []

# 候选登录态文件「存在、accessToken 是加密信封、但密钥没解出来」的记录，
# 仅用于生成准确的错误信息，不含任何敏感内容。
_ENCRYPTED_FOUND: list[str] = []


class CredentialError(RuntimeError):
    """登录态读取失败的统一异常。"""


# ---------------------------------------------------------------------------
# 平台基础目录
# ---------------------------------------------------------------------------
def _home() -> str:
    return os.path.expanduser("~")


def _appdata() -> str:
    return os.environ.get("APPDATA", "")


def _xdg_config() -> str:
    return os.environ.get("XDG_CONFIG_HOME", os.path.join(_home(), ".config"))


# ---------------------------------------------------------------------------
# 候选路径
# ---------------------------------------------------------------------------
def desktop_info_candidates() -> list[str]:
    """登录态候选路径（按平台）。"""
    rel = os.path.join(
        "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"
    )
    if sys.platform == "darwin":
        return [os.path.join(_home(), "Library", "Application Support", rel)]
    if sys.platform == "win32":
        return [os.path.join(_appdata(), rel)]
    return [os.path.join(_xdg_config(), rel)]


# ---------------------------------------------------------------------------
# 域标准化（登录态里的 auth.domain 才是官方认定的接口 host）
# ---------------------------------------------------------------------------
def normalize_domain(raw) -> str:
    """把登录态里的 domain 标准化为 https://host（去尾斜杠）。非法值返回空串。"""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    d = raw.strip()
    if not d.startswith(("http://", "https://")):
        d = "https://" + d
    return d.rstrip("/")


# ---------------------------------------------------------------------------
# A. 路线 A：明文 JWT 或 at-rest 加密信封（纯 Python + atrest）
# ---------------------------------------------------------------------------
def _load_plaintext() -> dict | None:
    """返回登录态 dict；找不到返回 None；若候选路径存在但被系统拒读，
    通过 _PERM_DENIED 记录路径（供 load_credentials 生成准确的错误信息），
    避免把「权限被拒」误报成「文件不存在」。"""
    for path in desktop_info_candidates():
        try:
            os.stat(path)
        except FileNotFoundError:
            continue
        except PermissionError:
            _PERM_DENIED.append(path)
            continue
        except OSError:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            # 文件损坏 / 写入中 / 权限问题：忽略，尝试下一个候选
            continue
        account = data.get("account") or {}
        auth = data.get("auth") or {}
        token = auth.get("accessToken")
        # ① 明文 JWT（5.5.x 及更早 / 加密未开启）
        if isinstance(token, str) and token:
            return _result(token, account, auth, data)
        # ② 5.6.2+ 字段加密信封：AES-256-GCM 解密后取明文 JWT
        if atrest.is_envelope(token):
            try:
                plain = atrest.decrypt_token(token)
            except atrest.AtRestError:
                _ENCRYPTED_FOUND.append(path)
                continue
            if isinstance(plain, str) and plain:
                return _result(plain, account, auth, data)
    return None


def _result(token: str, account: dict, auth: dict, data: dict) -> dict:
    """按统一结构组装登录态（token 只在内存流转）。"""
    return {
        "access_token": token,
        "uid": account.get("uid") or auth.get("uid") or "",
        "domain": normalize_domain(auth.get("domain") or data.get("domain")),
        "source": SOURCE_DESKTOP_INFO,
    }


# ---------------------------------------------------------------------------
# B. 路线 B 兜底：经回环 CDP 直接取明文 token（抗客户端改版）
# ---------------------------------------------------------------------------
def _load_via_cdp() -> dict | None:
    """客户端以 `--remote-debugging-port` 启动时，走 CDP 直接取**明文** token。

    这是「抗版本」兜底：不解密、不复刻 at-rest 算法，客户端将来改加密格式也不受影响。
    前置条件是客户端带着调试端口启动（端口可用 WORKBUDDY_CDP_PORT 指定）。
    模块 `cdp_token.py` 可能不存在（精简部署），缺失时静默跳过；任何异常都吞掉。
    """
    try:
        import cdp_token
    except ImportError:
        return None
    try:
        port = cdp_token.cdp_port() or cdp_token.probe()
        if not port:
            return None
        cred = cdp_token.load_via_cdp(port)
    except Exception:  # noqa: BLE001  兜底路径绝不因为自身异常打断主流程
        return None
    if not cred:
        return None
    token = cred.get("access_token")
    if not isinstance(token, str) or not token:
        return None
    return {
        "access_token": token,
        "uid": cred.get("uid") or "",
        "domain": normalize_domain(cred.get("domain")),
        "source": SOURCE_CDP,
    }


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def load_credentials() -> dict:
    """
    读取本地登录态，返回统一结构 {"access_token","uid","domain","source"}。
    顺序：路线 A（明文 / 加密信封）→ 路线 B（回环 CDP 兜底）。
    失败时抛出 CredentialError（信息中不含任何 token）。

    ★ 错误信息必须指向**真实原因**（2026-09-28）：以前无论哪种失败都报
      「新版明文文件与旧版 state.vscdb 均未命中，请先安装并登录 WorkBuddy」——
      客户端 5.6.2 引入字段加密后，这句话把人引向「重新登录」，
      而真正该做的是「让脚本能取到解密密钥」。故分三档报错。
    """
    cred = _load_plaintext()
    if cred:
        return cred
    cred = _load_via_cdp()
    if cred:
        return cred
    if _PERM_DENIED:
        raise CredentialError(
            "登录态文件存在但被系统拒绝读取（权限/隐私保护，如 macOS TCC）："
            + "；".join(_PERM_DENIED)
            + "。请给运行终端授权「完全磁盘访问」，或改在 WorkBuddy 自动化/桌面端上下文运行。"
        )
    if _ENCRYPTED_FOUND:
        raise CredentialError(
            "登录态存在但 accessToken 已被 5.6.2+ 客户端加密，且未能取到解密密钥："
            + "；".join(_ENCRYPTED_FOUND)
            + "。macOS 侧请确认 WorkBuddy.app 完整（脚本会启动其自带 Electron 取密钥）；"
            "Windows 侧请确认客户端**正在运行**并已登录、脚本与客户端同一用户；"
            "或设置环境变量 WORKBUDDY_ATREST_KEY / WORKBUDDY_ATREST_KEY_FILE 显式指定密钥。"
        )
    raise CredentialError(
        "未找到登录态文件（workbuddy-desktop.info）。请先安装并登录 WorkBuddy 桌面端，"
        "并确认脚本与客户端以**同一用户**运行。已探测："
        + "；".join(desktop_info_candidates())
    )


# ---------------------------------------------------------------------------
# 脱敏工具（用于安全打印/日志，绝不输出 token 本体）
# ---------------------------------------------------------------------------
def mask_secret(secret: str) -> str:
    """对 access_token 这类高敏感串：不展示任何字符，仅说明已读取及长度。"""
    if not secret:
        return "<空>"
    return "<已读取（不展示）, 长度 {}>".format(len(secret))


def mask_uid(uid: str) -> str:
    """对 uid：保留首尾少量字符，中间打码。"""
    if not uid:
        return "<空>"
    if len(uid) <= 6:
        return "*" * len(uid)
    return uid[:2] + "*" * (len(uid) - 4) + uid[-2:]


def describe(cred: dict) -> dict:
    """把 load_credentials() 的结果转成可安全打印的脱敏描述（不含真实 token）。"""
    return {
        "source": cred.get("source", ""),
        "access_token": mask_secret(cred.get("access_token", "")),
        "uid": mask_uid(cred.get("uid", "")),
        "domain": cred.get("domain", ""),
    }


if __name__ == "__main__":
    # 直接运行时仅打印脱敏结果，绝不打印真实 token。
    try:
        c = load_credentials()
    except CredentialError as e:
        print("❌ 读取登录态失败：{}".format(e))
        sys.exit(1)
    d = describe(c)
    print("✅ 找到登录态来源：{}".format(d["source"]))
    print("✅ access_token：{}".format(d["access_token"]))
    print("✅ uid：{}".format(d["uid"]))
    print("✅ domain：{}".format(d["domain"] or "<空>"))
