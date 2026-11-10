"""微信读书长效凭据的静默续期。

微信读书扫码登录会同时下发两类凭据：

  * ``accessToken`` —— 短效，挂在 ``accessToken`` / ``skey`` 请求头与
    ``wr_vid`` / ``wr_skey`` Cookie 上。过期后所有接口返回
    HTTP 401 + ``errcode=-2012``（登录超时）。
  * ``refreshToken`` —— 长效，可用来静默换一份新的 ``accessToken``，
    无需用户重新扫码。

续期请求需要微信读书客户端的签名算法（``weread::GenSignature``）。
这里用纯 Python 复刻，算法与微信读书 Android 客户端一致：

  1. 每个参数字符串逐字节过 256 项 S-Box 置换；
  2. 追加固定盐值 ``5a6f1``；
  3. 按字节序字典升序排序后拼接（复刻 C++ ``std::sort``）；
  4. 异或累加和取模 11 得到循环移位量，左旋后再 SHA-256；
  5. 对 64 字节的 hex 摘要再做一次同样的移位 + SHA-256。

没有这一步，采集进程跑过凭据有效期后就会全线 401。
"""

from __future__ import annotations

import hashlib
import random
import time
from typing import Any

import requests

APP_USER_AGENT = "WeRead/7.5.0 (Android; 33; arm64-v8a)"
LOGIN_URL = "https://i.weread.qq.com/login"
DEFAULT_DEVICE_ID = "410515152"

# libencrypt.so 内置的 256 字节 S-Box 置换盒
_SBOX = (
    52, 202, 85, 64, 29, 182, 147, 198, 49, 48, 41, 53, 50, 167, 184, 17,
    194, 181, 22, 250, 139, 177, 36, 164, 16, 144, 4, 233, 8, 248, 59, 138,
    156, 140, 68, 249, 188, 92, 105, 226, 161, 218, 210, 211, 117, 137, 247, 30,
    45, 80, 86, 215, 114, 83, 191, 34, 251, 32, 15, 1, 46, 69, 135, 110,
    102, 72, 242, 224, 205, 254, 103, 169, 67, 244, 148, 81, 206, 165, 74, 238,
    19, 38, 142, 204, 170, 51, 20, 93, 14, 57, 187, 207, 145, 43, 129, 77,
    234, 153, 236, 26, 44, 133, 197, 217, 54, 116, 75, 24, 225, 241, 61, 157,
    65, 159, 180, 23, 13, 214, 76, 190, 220, 175, 151, 40, 119, 240, 98, 255,
    113, 193, 200, 39, 143, 108, 104, 168, 155, 230, 89, 28, 27, 18, 9, 152,
    78, 63, 6, 55, 0, 186, 31, 10, 25, 47, 201, 213, 208, 87, 73, 111,
    253, 37, 228, 97, 12, 66, 203, 150, 100, 95, 219, 173, 96, 35, 141, 154,
    109, 195, 196, 94, 62, 185, 146, 106, 189, 91, 7, 127, 118, 149, 237, 79,
    171, 132, 122, 128, 231, 120, 199, 229, 235, 115, 131, 107, 252, 56, 70, 125,
    71, 101, 179, 82, 99, 58, 5, 209, 239, 163, 166, 222, 158, 60, 2, 174,
    178, 123, 160, 246, 243, 42, 192, 172, 134, 3, 90, 84, 11, 245, 130, 212,
    126, 227, 223, 176, 216, 221, 33, 232, 124, 136, 162, 121, 88, 112, 183, 21,
)
_SALT = b"5a6f1"


def _remap_string(value: str) -> bytes:
    """weread::RemapString：逐字节过 S-Box。"""
    return bytes(_SBOX[b] for b in value.encode("utf-8"))


def gen_signature(items: list[str]) -> str:
    """纯 Python 复刻 ``Java_weread_encrypt_EncryptUtils_nativeGetSignatures``。"""
    remapped = [_remap_string(item) for item in items]
    remapped.append(_SALT)
    remapped.sort()  # 复刻 C++ std::string 的字节序字典升序
    joined = b"".join(remapped)
    if not joined:
        return ""

    xor1 = 0
    for byte in joined:
        xor1 ^= byte
    shift1 = xor1 % 11
    shifted1 = bytearray(len(joined))
    for index in range(len(joined)):
        shifted1[(index + shift1) % len(joined)] = joined[index]
    digest1 = hashlib.sha256(shifted1).hexdigest().encode("ascii")

    xor2 = 0
    for byte in digest1:
        xor2 ^= byte
    shift2 = xor2 % 11
    shifted2 = bytearray(len(digest1))
    for index in range(len(digest1)):
        shifted2[(index + shift2) % len(digest1)] = digest1[index]
    return hashlib.sha256(shifted2).hexdigest()


class WeReadRefreshFailed(RuntimeError):
    """静默续期失败（refreshToken 失效或网络错误）。"""


def refresh_credentials(
    credentials: dict[str, Any], timeout_seconds: int = 20
) -> dict[str, Any]:
    """用 refreshToken 换一份新的 accessToken / skey。

    返回**新的**凭据字典（未修改入参）。失败时抛 ``WeReadRefreshFailed``。
    """
    refresh_token = str(credentials.get("refreshToken") or "")
    if not refresh_token:
        raise WeReadRefreshFailed("凭据中没有 refreshToken，无法静默续期")

    device_id = str(credentials.get("deviceId") or DEFAULT_DEVICE_ID)
    now_ms = int(time.time() * 1000)
    random_value = random.randint(0, 999)
    signature = gen_signature(
        ["", device_id, str(now_ms), str(random_value), refresh_token]
    )
    payload = {
        "refreshToken": refresh_token,
        "deviceId": device_id,
        "wxToken": 0,
        "inBackground": 0,
        "trackId": "",
        "kickType": 1,
        "refCgi": "",
        "timestamp": now_ms,
        "random": random_value,
        "signature": signature,
        "virtualChannelId": "",
        "deviceName": "Android",
    }

    session = requests.Session()
    session.trust_env = False
    try:
        response = session.post(
            LOGIN_URL,
            json=payload,
            headers={
                "User-Agent": APP_USER_AGENT,
                "Content-Type": "application/json; charset=UTF-8",
                "Host": "i.weread.qq.com",
            },
            timeout=timeout_seconds,
        )
    except requests.RequestException as error:
        raise WeReadRefreshFailed(f"续期请求失败: {error}") from error

    if response.status_code != 200:
        raise WeReadRefreshFailed(f"续期返回 HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as error:
        raise WeReadRefreshFailed("续期返回的不是 JSON") from error
    if "accessToken" not in data:
        raise WeReadRefreshFailed(f"续期响应缺少 accessToken: {str(data)[:120]}")

    refreshed = dict(credentials)
    refreshed["accessToken"] = str(data["accessToken"])
    if data.get("skey"):
        refreshed["skey"] = str(data["skey"])
    if data.get("vid"):
        refreshed["vid"] = str(data["vid"])
    if data.get("refreshToken"):
        refreshed["refreshToken"] = str(data["refreshToken"])
    refreshed["deviceId"] = device_id
    refreshed["userAgent"] = APP_USER_AGENT
    return refreshed
