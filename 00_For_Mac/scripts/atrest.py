#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atrest.py — WorkBuddy 5.6.2+ AtRestEncryption（AES-256-GCM 信封）解密

背景
----
WorkBuddy 桌面端从 5.6.2 起强制开启登录态静态加密（AtRestEncryption），
`workbuddy-desktop.info` 里的 `auth.accessToken` 不再是明文 JWT，而是：

    {"$wbEncrypted": 1, "envelope": "<base64>"}

其中 `envelope` 解 base64 后是一个 JSON：

    {"suite": 1, "keyId": "16hex", "nonce": "<b64>", "authTag": "<b64>", "ciphertext": "<b64>"}

本模块实现完整的解密链（全部只用 Python 标准库，零第三方依赖）：

  1. 密钥派生  key    = SHA256(atRestSecretKey 的 UTF-8 字节)   → 32 字节
               keyId  = SHA256(key).hex()[:16]                   → 16 hex 字符
     （注意：SHA256 的输入是 44 字符 base64 字符串本身，不是解码后的 32 字节）
  2. AAD 构造  "WB-AAD\\0" + 版本 + 各字段按「4 字节大端长度 + UTF-8」前缀编码，
               具体见 build_aad()，与客户端 at-rest-crypto 的 sym-v1 field 加密一致
  3. 解密      AES-256-GCM，nonce=12 字节、authTag=16 字节

密钥 atRestSecretKey（44 字符规范 base64）不落盘、由客户端构建期内置，经其原生模块
在运行时提供。本模块按以下顺序定位（见 locate_secret_key）：

  1. 环境变量 WORKBUDDY_ATREST_KEY         —— 直接给 44 字符密钥串
  2. 环境变量 WORKBUDDY_ATREST_KEY_FILE    —— 指向含明文 44 字符密钥的文件
  3. Windows：扫描登录态所在 Data 目录的 DPAPI blob，解开后用 keyId 校验
  4. Windows：ReadProcessMemory 扫描运行中 WorkBuddy.exe 进程内存（Windows 主路径）
  5. macOS：借客户端自带的 Electron 二进制，以 ELECTRON_RUN_AS_NODE 方式启动并向其
     内置原生绑定 electron_browser_workbuddy_storage.loggerGet() 索取密钥
     （macOS 主路径；2026-09-28 本机实测成功，无需 root / 注入 / 读内存）

安全约束：解密得到的明文 JWT 只在内存中流转，绝不打印 / 写日志 / 落盘 / 上传。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys

# ===========================================================================
# 一、AES-256（只需「加密」方向：GCM 的 CTR 与 GHASH 子密钥都用加密方向）
# ===========================================================================

# AES S-Box（FIPS-197，256 字节查表）
_SBOX = bytes([
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
])

# 轮常量 Rcon（AES-256 用到前 7 个）
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40)

_NR = 14  # AES-256 轮数


def _gmul(a: int, b: int) -> int:
    """GF(2^8) 乘法，约减多项式 x^8 + x^4 + x^3 + x + 1（0x11b）。"""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _sub_word(word: list) -> list:
    return [_SBOX[b] for b in word]


def _rot_word(word: list) -> list:
    return word[1:] + word[:1]


def _expand_key(key: bytes) -> list:
    """AES-256 密钥扩展，返回 15 个轮密钥（每个 16 字节）。"""
    nk = 8  # 256 位 = 8 个 32 位字
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (_NR + 1)):
        temp = list(w[i - 1])
        if i % nk == 0:
            temp = _sub_word(_rot_word(temp))
            temp[0] ^= _RCON[i // nk - 1]
        elif i % nk == 4:
            temp = _sub_word(temp)
        w.append([w[i - nk][j] ^ temp[j] for j in range(4)])
    round_keys = []
    for r in range(_NR + 1):
        rk = []
        for j in range(4):
            rk += w[4 * r + j]
        round_keys.append(bytes(rk))
    return round_keys


def _sub_bytes(s: list) -> list:
    return [_SBOX[b] for b in s]


def _shift_rows(s: list) -> list:
    """按列主序（s[c*4+r] 是第 r 行第 c 列）做 ShiftRows。"""
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            out[c * 4 + r] = s[((c + r) % 4) * 4 + r]
    return out


def _mix_columns(s: list) -> list:
    out = [0] * 16
    for c in range(4):
        a0, a1, a2, a3 = s[c * 4], s[c * 4 + 1], s[c * 4 + 2], s[c * 4 + 3]
        out[c * 4 + 0] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
        out[c * 4 + 1] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
        out[c * 4 + 2] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
        out[c * 4 + 3] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)
    return out


def _encrypt_block(block: bytes, round_keys: list) -> bytes:
    """加密单个 16 字节块（正向，AES-256）。"""
    s = list(block)
    for i in range(16):
        s[i] ^= round_keys[0][i]
    for r in range(1, _NR):
        s = _sub_bytes(s)
        s = _shift_rows(s)
        s = _mix_columns(s)
        for i in range(16):
            s[i] ^= round_keys[r][i]
    s = _sub_bytes(s)
    s = _shift_rows(s)
    for i in range(16):
        s[i] ^= round_keys[_NR][i]
    return bytes(s)


# ===========================================================================
# 二、GCM（GHASH + CTR，均只用 AES 加密方向）
# ===========================================================================

def _gf_mult(x: bytes, h: bytes) -> bytes:
    """GF(2^128) 乘法：返回 x·h mod (x^128 + x^7 + x^2 + x + 1)。

    采用 NIST SP 800-38D 的位序：x 从最高有效位（MSB）开始逐位处理，
    v 整体右移、最低位溢出时约减 R（R = 0xE1 || 0^120，落在最高字节）。
    """
    z = bytearray(16)
    v = bytearray(h)
    for i in range(128):
        # x 的第 i 位（MSB first：i=0 → x[0] 的 bit7，… i=127 → x[15] 的 bit0）
        if x[i // 8] & (0x80 >> (i % 8)):
            for j in range(16):
                z[j] ^= v[j]
        # v 右移 1 位（big-endian），记录最低位是否溢出
        odd = v[15] & 1
        for j in range(15, 0, -1):
            v[j] = (v[j] >> 1) | ((v[j - 1] & 1) << 7)
        v[0] >>= 1
        if odd:
            v[0] ^= 0xE1
    return bytes(z)


def _ghash(h: bytes, data: bytes) -> bytes:
    """GHASH：对已补齐到 16 字节倍数的 data 做认证哈希。"""
    y = bytearray(16)
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        for j in range(16):
            y[j] ^= block[j]
        y = bytearray(_gf_mult(bytes(y), h))
    return bytes(y)


def _ctr_xor(key: bytes, nonce: bytes, data: bytes, round_keys: list) -> bytes:
    """CTR 模式：keystream = E(nonce || counter)，counter 从 2 起（inc32(J0)）。

    GCM 中 J0 = nonce || 00000001 专用于计算认证标签；加密明文从 inc32(J0)
    （计数器=2）开始，避免与 J0 冲突。
    """
    out = bytearray(len(data))
    counter = 2
    for off in range(0, len(data), 16):
        ctr_block = nonce + counter.to_bytes(4, "big")
        keystream = _encrypt_block(ctr_block, round_keys)
        chunk = data[off:off + 16]
        for k in range(len(chunk)):
            out[off + k] = chunk[k] ^ keystream[k]
        counter += 1
    return bytes(out)


def _pad16(data: bytes) -> bytes:
    if len(data) % 16 == 0:
        return data
    return data + b"\x00" * (16 - len(data) % 16)


def _len_block(n: int) -> bytes:
    return (n * 8).to_bytes(8, "big")  # GHASH 里长度用「bit 长度」64 位大端


def _gcm_tag(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes, round_keys: list) -> bytes:
    h = _encrypt_block(b"\x00" * 16, round_keys)
    ghash_in = _pad16(aad) + _pad16(ciphertext) + _len_block(len(aad)) + _len_block(len(ciphertext))
    s = _ghash(h, ghash_in)
    j0 = nonce + b"\x00\x00\x00\x01"
    ek_j0 = _encrypt_block(j0, round_keys)
    return bytes(a ^ b for a, b in zip(s, ek_j0))


def aes_gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """返回 ciphertext + authTag（16 字节）。"""
    if len(nonce) != 12:
        raise ValueError("GCM nonce 必须为 12 字节")
    round_keys = _expand_key(key)
    ciphertext = _ctr_xor(key, nonce, plaintext, round_keys)
    auth_tag = _gcm_tag(key, nonce, ciphertext, aad, round_keys)
    return ciphertext + auth_tag


class IntegrityError(ValueError):
    """GCM 认证失败：密文 / authTag / AAD 被篡改。"""


def aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, auth_tag: bytes, aad: bytes) -> bytes:
    """解密并校验 authTag，认证失败抛 IntegrityError。返回明文。"""
    if len(nonce) != 12:
        raise ValueError("GCM nonce 必须为 12 字节")
    if len(auth_tag) != 16:
        raise IntegrityError("authTag 长度异常")
    round_keys = _expand_key(key)
    expected = _gcm_tag(key, nonce, ciphertext, aad, round_keys)
    if expected != auth_tag:
        raise IntegrityError("ciphertext authentication failed")
    return _ctr_xor(key, nonce, ciphertext, round_keys)


# ===========================================================================
# 三、信封解析 / 密钥派生 / AAD 构造
# ===========================================================================

class AtRestError(RuntimeError):
    """AtRestEncryption 解密失败。"""


def is_envelope(value) -> bool:
    """判断 accessToken 值是否为加密信封（dict 且带 $wbEncrypted 标记）。"""
    return isinstance(value, dict) and value.get("$wbEncrypted") == 1 and isinstance(value.get("envelope"), str)


def parse_envelope(value: dict) -> dict:
    """解析加密信封，返回 {suite, keyId, nonce, authTag, ciphertext}（后三者已解 base64）。"""
    env_b64 = value.get("envelope")
    if not isinstance(env_b64, str):
        raise AtRestError("信封缺少 envelope 字段")
    try:
        raw = base64.b64decode(env_b64)
        inner = json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise AtRestError("信封不是合法的 base64+JSON：{}".format(e))
    try:
        nonce = base64.b64decode(inner["nonce"], validate=True)
        auth_tag = base64.b64decode(inner["authTag"], validate=True)
        ciphertext = base64.b64decode(inner["ciphertext"], validate=True)
    except (KeyError, ValueError) as e:
        raise AtRestError("信封字段缺失或非法：{}".format(e))
    key_id = inner.get("keyId")
    suite = inner.get("suite", 1)
    if not isinstance(key_id, str) or not re.fullmatch(r"[0-9a-f]{16}", key_id):
        raise AtRestError("信封 keyId 非法")
    if len(nonce) != 12:
        raise AtRestError("信封 nonce 长度异常（应 12 字节）")
    if len(auth_tag) != 16:
        raise AtRestError("信封 authTag 长度异常（应 16 字节）")
    return {"suite": suite, "keyId": key_id, "nonce": nonce, "authTag": auth_tag, "ciphertext": ciphertext}


def derive_key(secret_str: str):
    """由 atRestSecretKey（44 字符 base64 字符串）派生 (key_bytes, key_id_hex)。"""
    key = hashlib.sha256(secret_str.encode("utf-8")).digest()
    key_id = hashlib.sha256(key).hexdigest()[:16]
    return key, key_id


def _length_prefixed(s: str) -> bytes:
    b = s.encode("utf-8")
    return len(b).to_bytes(4, "big") + b


def build_aad(key_id: str, suite: int = 1) -> bytes:
    """构造 sym-v1「field」加密的 AAD（与客户端 at-rest-crypto 完全一致）。"""
    # 常量（来自 app.asar 的 at-rest-crypto 模块）
    #   AAD_DOMAIN="WB-AAD\0", FRAMING_CODE.field=2, STANDARD_FORMAT_ID.field="WBEV1", scheme="sym-v1"
    aad = b"WB-AAD\x00"
    aad += b"\x01"                      # 版本
    aad += _length_prefixed("WBEV1")    # STANDARD_FORMAT_ID.field
    aad += _length_prefixed("sym-v1")   # scheme
    aad += suite.to_bytes(4, "big")     # suite (uint32 BE)
    aad += _length_prefixed(key_id)     # keyId
    aad += b"\x02"                      # FRAMING_CODE.field
    aad += b"\x00"                      # sequence 未定义
    aad += b"\x00"                      # final 未定义
    return aad


# ===========================================================================
# 四、解密入口
# ===========================================================================

def decrypt_token(token_value, secret_key: str | None = None) -> str:
    """解密 accessToken。若非信封则原样返回；是信封则用密钥解密出明文 JWT。"""
    if not is_envelope(token_value):
        return token_value
    env = parse_envelope(token_value)
    if secret_key is None:
        secret_key = locate_secret_key(env["keyId"])
    if not secret_key:
        raise AtRestError(
            "无法获取 atRestSecretKey：请确保 WorkBuddy 客户端已启动并登录、脚本与客户端同一用户，"
            "或用环境变量 WORKBUDDY_ATREST_KEY / WORKBUDDY_ATREST_KEY_FILE 显式指定。"
        )
    key, key_id = derive_key(secret_key)
    if key_id != env["keyId"]:
        raise AtRestError("密钥 keyId 不匹配（期望 {}，得到 {}）".format(env["keyId"], key_id))
    aad = build_aad(env["keyId"], env["suite"])
    try:
        plaintext = aes_gcm_decrypt(key, env["nonce"], env["ciphertext"], env["authTag"], aad)
    except IntegrityError as e:
        raise AtRestError("解密认证失败：{}".format(e))
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as e:
        raise AtRestError("解密结果不是合法 UTF-8：{}".format(e))


# ===========================================================================
# 五、密钥定位
# ===========================================================================

# 进程内缓存：命中即复用，避免同一次运行里反复扫进程内存。
#
# ★ 为什么必须缓存（2026-09-28 实测）：定位一次要扫 WorkBuddy.exe 的进程内存，
#   本机耗时 **~6.5 秒**；而一次 catchup 运行会多次读凭据（预检 / 签到 / 旅行 /
#   推送 / renew）。不缓存 = 每次调用都重扫一遍。
# ★ 只缓存**命中**结果：未命中通常意味着「客户端没起 / 没登录 / 还没到该扫的时候」，
#   下次运行应当重新尝试，缓存空值会让一次偶发失败永久卡住。
# ★ 键用 keyId：客户端轮换密钥时 keyId 随之变化，天然不会误用旧密钥；
#   万一服务端复用同一个 keyId 换密钥，解密会因 GCM 认证失败而报错，
#   不会静默给出错误明文（fail-closed）。
_SECRET_CACHE: dict[str, str] = {}


def locate_secret_key(key_id: str) -> str | None:
    """按顺序定位 atRestSecretKey，返回 44 字符 base64 字符串或 None。

    命中结果按 keyId 缓存在进程内（见 `_SECRET_CACHE` 的说明）。
    """
    cached = _SECRET_CACHE.get(key_id)
    if cached:
        return cached
    key = _locate_secret_key_uncached(key_id)
    if key:
        _SECRET_CACHE[key_id] = key
    return key


def _locate_secret_key_uncached(key_id: str) -> str | None:
    """真正的定位实现（无缓存）：环境变量 → 密钥文件 → Windows DPAPI / 进程内存。"""
    # 1) 环境变量：直接给密钥串
    env_key = os.environ.get("WORKBUDDY_ATREST_KEY")
    if env_key and _is_valid_secret(env_key):
        return env_key

    # 2) 环境变量：密钥文件（明文）
    env_file = os.environ.get("WORKBUDDY_ATREST_KEY_FILE")
    if env_file and os.path.isfile(env_file):
        try:
            content = open(env_file, "rb").read()
        except OSError:
            content = b""
        cand = content.decode("utf-8", errors="ignore").strip()
        if _is_valid_secret(cand):
            return cand

    # 3/4) 平台相关
    if sys.platform == "win32":
        key = _locate_key_windows(key_id)
        if key:
            return key
    elif sys.platform == "darwin":
        key = _locate_key_macos(key_id)
        if key:
            return key
    return None


def _is_valid_secret(s: str) -> bool:
    """校验候选密钥是否为 44 字符规范 base64 的 32 字节。"""
    if not isinstance(s, str) or len(s) != 44 or not s.endswith("="):
        return False
    try:
        raw = base64.b64decode(s, validate=True)
    except ValueError:
        return False
    return len(raw) == 32 and base64.b64encode(raw).decode("ascii") == s


def _secret_matches(secret: str, key_id: str) -> bool:
    """校验候选密钥派生的 keyId 是否命中目标。"""
    try:
        _, kid = derive_key(secret)
    except Exception:  # noqa: BLE001
        return False
    return kid == key_id


# ---------------------------------------------------------------------------
# Windows 专属：DPAPI blob + 进程内存扫描
# ---------------------------------------------------------------------------

def _locate_key_windows(key_id: str) -> str | None:
    """Windows 下定位密钥：先扫 DPAPI blob，再扫 WorkBuddy.exe 进程内存。"""
    key = _scan_dpapi_blobs(key_id)
    if key:
        return key
    return _scan_process_memory(key_id)


def _scan_dpapi_blobs(key_id: str) -> str | None:
    """扫描登录态所在 Data 目录下可能的 DPAPI blob（客户端曾落盘的密钥缓存）。"""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None

    # 候选目录：%APPDATA%/CodeBuddyExtension、%LOCALAPPDATA%/CodeBuddyExtension
    bases = []
    for env in ("APPDATA", "LOCALAPPDATA"):
        v = os.environ.get(env)
        if v:
            bases.append(os.path.join(v, "CodeBuddyExtension"))
    for base in bases:
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for fn in files:
                low = fn.lower()
                # 只对疑似密钥/缓存文件尝试，跳过登录态明文与已知无关文件
                if low in ("workbuddy-desktop.info", "workbuddy-desktop-ai.info"):
                    continue
                if not (low.endswith((".key", ".blob", ".bin")) or "key" in low or "secret" in low):
                    continue
                path = os.path.join(root, fn)
                try:
                    if os.path.getsize(path) > 4096:  # DPAPI blob 一般只有几百字节
                        continue
                    blob = open(path, "rb").read()
                except OSError:
                    continue
                secret = _dpapi_unprotect(blob, ctypes, wintypes)
                if secret and _secret_matches(secret, key_id):
                    return secret
    return None


def _dpapi_unprotect(blob: bytes, ctypes, wintypes) -> str | None:
    """尝试用 CryptUnprotectData 解 DPAPI blob，返回明文 44 字符密钥或 None。"""
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    in_blob = DATA_BLOB()
    in_blob.cbData = len(blob)
    in_buf = ctypes.create_string_buffer(blob, len(blob))
    in_blob.pbData = ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_byte))
    out_blob = DATA_BLOB()
    try:
        crypt32 = ctypes.windll.crypt32
        if not crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
        ):
            return None
        try:
            raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    except Exception:  # noqa: BLE001
        return None
    text = raw.decode("utf-8", errors="ignore").strip()
    return text if _is_valid_secret(text) else None


def _scan_process_memory(key_id: str) -> str | None:
    """ReadProcessMemory 扫描运行中 WorkBuddy.exe 的进程内存，搜索密钥。

    分两阶段：先定向搜 keyId（ASCII/UTF-16LE）命中点附近提取密钥；若未命中，
    再做全内存 44 字符 base64 候选兜底。要求客户端已启动并登录、脚本与客户端
    同一 Windows 用户（否则 OpenProcess 会被拒）。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None

    pids = _find_workbuddy_pids(ctypes, wintypes)
    if not pids:
        return None

    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010

    for pid in pids:
        hproc = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not hproc:
            continue
        try:
            result = _scan_one_process(hproc, key_id, ctypes, wintypes, kernel32)
        finally:
            kernel32.CloseHandle(hproc)
        if result:
            return result
    return None


def _find_workbuddy_pids(ctypes, wintypes) -> list:
    """用 Toolhelp32 快照枚举匹配 WorkBuddy 的进程 PID。"""
    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x00000002
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1 or snap == 0xFFFFFFFFFFFFFFFF:
        return []
    pids = []
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if kernel32.Process32First(snap, ctypes.byref(entry)):
            while True:
                name = entry.szExeFile.decode("mbcs", errors="ignore").lower()
                if name.startswith("workbuddy"):
                    pids.append(entry.th32ProcessID)
                if not kernel32.Process32Next(snap, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snap)
    return pids


_MEM_COMMIT = 0x1000
# 可读内存保护类型：PAGE_READONLY/READWRITE/WRITECOPY + 对应的 EXECUTE_* 变体
_MEM_READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}


def _iter_readable_chunks(hproc, ctypes, wintypes, kernel32, max_bytes=512 * 1024 * 1024):
    """生成器：遍历进程已提交且可读的内存块，逐个 yield 读出的字节。"""
    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("PartitionId", wintypes.WORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    address = 0
    scanned = 0
    while scanned < max_bytes:
        mbi = MEMORY_BASIC_INFORMATION()
        ret = kernel32.VirtualQueryEx(
            ctypes.c_void_p(hproc), ctypes.c_void_p(address),
            ctypes.byref(mbi), ctypes.sizeof(mbi),
        )
        if ret == 0:
            return
        region_addr = mbi.BaseAddress or 0
        region_size = mbi.RegionSize or 0
        address = region_addr + region_size
        if mbi.State != _MEM_COMMIT:
            continue
        if (mbi.Protect & 0xFF) not in _MEM_READABLE:
            continue
        if region_size <= 0 or region_size > 64 * 1024 * 1024:
            continue  # 单块过大通常是保留区/大映射，跳过
        buf = ctypes.create_string_buffer(region_size)
        bytes_read = ctypes.c_size_t(0)
        if not kernel32.ReadProcessMemory(
            hproc, ctypes.c_void_p(region_addr), buf, region_size, ctypes.byref(bytes_read)
        ):
            continue
        chunk = buf.raw[: bytes_read.value]
        scanned += len(chunk)
        yield chunk


def _extract_b64_candidates(buf: bytes, seen: set) -> list:
    """从内存块提取 44 字符 base64 候选（ASCII 与 UTF-16LE），去重后返回新候选。"""
    out = []
    for m in re.finditer(rb"[A-Za-z0-9+/]{43}=", buf):
        s = m.group().decode("ascii")
        if s not in seen:
            seen.add(s)
            out.append(s)
    try:
        text = buf.decode("utf-16-le", errors="ignore")
    except Exception:  # noqa: BLE001
        text = ""
    for m in re.finditer(r"[A-Za-z0-9+/]{43}=", text):
        s = m.group()
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _scan_one_process(hproc, key_id, ctypes, wintypes, kernel32) -> str | None:
    """扫描单个进程内存：阶段1 定向搜 keyId 附近，阶段2 全内存 base64 兜底。"""
    key_id_ascii = key_id.encode("ascii")
    key_id_utf16 = key_id.encode("utf-16-le")

    # 阶段1：定向搜索 keyId，命中点 ±2KB 内找密钥并立即校验（密钥与 keyId 同属
    # key payload，通常相邻；命中即可返回，避免全量扫描）
    for chunk in _iter_readable_chunks(hproc, ctypes, wintypes, kernel32):
        for marker in (key_id_ascii, key_id_utf16):
            start = 0
            while True:
                idx = chunk.find(marker, start)
                if idx < 0:
                    break
                near = chunk[max(0, idx - 2048): idx + 2048]
                for s in _extract_b64_candidates(near, set()):
                    if _is_valid_secret(s) and _secret_matches(s, key_id):
                        return s
                start = idx + len(marker)

    # 阶段2：全内存 base64 兜底（keyId 可能在内存中被拆分/转码，定向搜不到时用）
    seen = set()
    for chunk in _iter_readable_chunks(hproc, ctypes, wintypes, kernel32):
        for s in _extract_b64_candidates(chunk, seen):
            if _is_valid_secret(s) and _secret_matches(s, key_id):
                return s
    return None


# ---------------------------------------------------------------------------
# macOS 专属：借客户端自带 Electron 二进制取密钥（ELECTRON_RUN_AS_NODE）
# ---------------------------------------------------------------------------
#
# ★ 为什么这条路可行（2026-09-28 本机实测，macOS + 客户端 5.6.2）：
#   WorkBuddy 桌面端的 atRestSecretKey 是**构建期内置**的常量，经原生绑定
#   `electron_browser_workbuddy_storage` 的 `loggerGet()` 暴露出来。把客户端自带的
#   Electron 二进制**以纯 Node 方式**启动（ELECTRON_RUN_AS_NODE=1），在普通用户权限
#   下就能直接取回这份 payload ——
#     · 不需要 root；
#     · 不需要 lldb / task_for_pid（hardened runtime 下连 root 也会被 AMFI 拒）；
#     · 不需要 DYLD 注入（entitlements 里没有 allow-dyld-environment-variables）；
#     · 也不需要客户端正在运行。
#
#   实测返回：
#     {"version":1,"atRestSecretKey":"<44字符base64>","atRestDeveloperPublicKey":{...}}
#
#   唯一会让这条路失效的前提是 Electron fuse `RunAsNode` 被禁用
#   （可用 `npx @electron/fuses read --app /Applications/WorkBuddy.app` 查看）。
#   5.6.2 未禁用，故当前可用。
#
# ★ 注意：`CFBundleExecutable` 实际是 `Electron`，不是 `WorkBuddy`；且**不要**去动
#   `WorkBuddy AI.app`（BundleId `com.workbuddy.workbuddy-ai`）——那是另一款应用。

# 客户端可执行文件常见位置（首个是默认安装位置）
_MAC_APP_EXECS = (
    "/Applications/WorkBuddy.app/Contents/MacOS/Electron",
    os.path.join(os.path.expanduser("~/Applications"), "WorkBuddy.app", "Contents", "MacOS", "Electron"),
)

# 在客户端 Node 环境里执行的取密钥脚本：把 loggerGet() 的返回值原样写到 stdout。
# 只读，不改动客户端任何状态；失败信息走 stderr，不污染 stdout。
_NODE_GET_KEY_JS = (
    "const v=process._linkedBinding('electron_browser_workbuddy_storage').loggerGet();"
    "process.stdout.write(typeof v==='string'?v:JSON.stringify(v));"
)


def _read_bundle_plist(plist_path: str):
    """读取 .app 的 Info.plist，返回 (CFBundleIdentifier, CFBundleExecutable)。"""
    try:
        with open(plist_path, "rb") as f:
            d = plistlib.load(f)
    except (OSError, ValueError):
        return None, None
    return d.get("CFBundleIdentifier"), d.get("CFBundleExecutable")


def _scan_app_bundles() -> list:
    """扫描常见应用目录，返回 WorkBuddy 桌面端（**排除 AI 版**）的可执行文件路径。"""
    out = []
    roots = ("/Applications", os.path.join(os.path.expanduser("~"), "Applications"))
    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".app"):
                continue
            app_dir = os.path.join(root, name)
            bid, exe = _read_bundle_plist(os.path.join(app_dir, "Contents", "Info.plist"))
            if not bid or "workbuddy" not in bid.lower():
                continue
            if bid.lower().endswith("workbuddy-ai"):
                continue  # WorkBuddy AI 是另一款应用，不碰
            out.append(os.path.join(app_dir, "Contents", "MacOS", exe or "Electron"))
    return out


def _find_macos_app_exec() -> str:
    """定位可用于 ELECTRON_RUN_AS_NODE 的客户端二进制；找不到返回空串。

    顺序：环境变量 WORKBUDDY_APP_EXEC → 常见安装位置 → 扫描应用目录（排除 AI 版）。
    """
    env_path = os.environ.get("WORKBUDDY_APP_EXEC")
    cands = [env_path] if env_path else []
    cands += list(_MAC_APP_EXECS)
    cands += _scan_app_bundles()
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ""


def _extract_secret_candidates(text: str) -> list:
    """从 Node 输出里提取候选密钥：先按 JSON 字段取，再退化为 44 字符 base64 扫描。"""
    cands = []
    for m in re.finditer(r'"atRestSecretKey"\s*:\s*"([^"]{1,128})"', text):
        cands.append(m.group(1))
    for m in re.finditer(r"[A-Za-z0-9+/]{43}=", text):
        cands.append(m.group())
    seen = set()
    out = []
    for s in cands:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _macos_payload() -> str:
    """启动客户端自带 Electron（纯 Node 模式）取其密钥 payload；失败返回空串。"""
    exe = _find_macos_app_exec()
    if not exe:
        return ""
    env = dict(os.environ)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        proc = subprocess.run(
            [exe, "-e", _NODE_GET_KEY_JS],
            capture_output=True, text=True, timeout=20, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def _locate_key_macos(key_id: str) -> str | None:
    """macOS 下定位密钥：借客户端自带 Electron 二进制取 payload，按 keyId 校验。"""
    for cand in _extract_secret_candidates(_macos_payload()):
        if _is_valid_secret(cand) and _secret_matches(cand, key_id):
            return cand
    return None


# ===========================================================================
# 六、离线自测（NIST 向量 + 随机往返）
# ===========================================================================

def self_test() -> bool:
    """离网自测 AES-256-GCM 正确性，返回是否全部通过。"""
    ok = True

    # NIST GCM 测试向量（AES-256）
    # 用例 13：空明文/空 AAD（此时 encrypt 返回的只有 16 字节 authTag）
    key = bytes(32)
    nonce = bytes(12)
    ct_tag = aes_gcm_encrypt(key, nonce, b"", b"")
    expected_tag_13 = bytes.fromhex("530f8afbc74536b9a963b4f1c4cb738b")
    if ct_tag[-16:] != expected_tag_13:
        ok = False
        print("  ✗ NIST 用例 13 失败")
    else:
        print("  ✓ NIST 用例 13 通过（AES-256-GCM 空明文）")

    # 用例 14：单块明文
    pt_14 = bytes(16)
    ct_14 = aes_gcm_encrypt(key, nonce, pt_14, b"")
    expected_ct_14 = bytes.fromhex("cea7403d4d606b6e074ec5d3baf39d18")
    expected_tag_14 = bytes.fromhex("d0d1c8a799996bf0265b98b5d48ab919")
    if ct_14[:16] != expected_ct_14 or ct_14[16:] != expected_tag_14:
        ok = False
        print("  ✗ NIST 用例 14 失败")
    else:
        print("  ✓ NIST 用例 14 通过（AES-256-GCM 单块明文）")

    # 随机往返（带 AAD，覆盖 GHASH 的 AAD 分支）
    import random
    rnd = random.Random(20260928)
    for i in range(8):
        k = bytes(rnd.getrandbits(8) for _ in range(32))
        n = bytes(rnd.getrandbits(8) for _ in range(12))
        pt = bytes(rnd.getrandbits(8) for _ in range(rnd.randint(0, 80)))
        aad = bytes(rnd.getrandbits(8) for _ in range(rnd.randint(0, 60)))
        enc = aes_gcm_encrypt(k, n, pt, aad)
        ct, tag = enc[:-16], enc[-16:]
        dec = aes_gcm_decrypt(k, n, ct, tag, aad)
        if dec != pt:
            ok = False
            print("  ✗ 随机往返 {} 失败".format(i))
            break
        # 篡改检测
        try:
            aes_gcm_decrypt(k, n, ct, bytes([tag[0] ^ 1]) + tag[1:], aad)
            ok = False
            print("  ✗ 篡改 authTag 未被拒绝")
            break
        except IntegrityError:
            pass
    else:
        print("  ✓ 随机往返 + 篡改检测通过（含 AAD）")

    return ok


def keycheck(key_id: str) -> int:
    """`--keycheck <keyId>`：只报告「密钥能否定位」。

    keyId 本身是**公开指纹**（明文写在登录态信封里），打印它不敏感；
    密钥本体**绝不打印**。返回 0=命中，1=未命中，2=参数非法。
    """
    if not re.fullmatch(r"[0-9a-f]{16}", key_id or ""):
        print("用法：python3 atrest.py --keycheck <16位小写hex的keyId>")
        print("（keyId 明文写在 workbuddy-desktop.info 的 envelope 里，可让 credentials.py 打印）")
        return 2
    key = locate_secret_key(key_id)
    # ★ 必须再校验一次 keyId（2026-09-28 对抗式验证发现）：环境变量 / 密钥文件这两条
    #   路径是「直接返回」的短路实现，**不校验 keyId**；若它们给的是过期或写错的密钥，
    #   locate_secret_key 照样返回非空 → 本自检会报绿色，而真实解密会因 keyId 不匹配
    #   失败。主解密路径本身是 fail-closed（decrypt_token 会校验并报错），
    #   但这个自检不能骗人，故这里显式复核。
    if key and _secret_matches(key, key_id):
        print("可定位：keyId = {} 已命中（密钥本体不打印）".format(key_id))
        return 0
    if key:
        print("未命中：所提供的密钥与 keyId = {} 不匹配（密钥本体不打印）".format(key_id))
        return 1
    print("无法定位：keyId = {} 未命中（平台 {}）".format(key_id, sys.platform))
    return 1


if __name__ == "__main__":
    if "--keycheck" in sys.argv[1:]:
        _i = sys.argv.index("--keycheck")
        _arg = sys.argv[_i + 1] if _i + 1 < len(sys.argv) else ""
        sys.exit(keycheck(_arg))
    print("=== atrest.py 离线自测 ===")
    passed = self_test()
    print("结果：{}".format("全部通过" if passed else "存在失败"))
    sys.exit(0 if passed else 1)
