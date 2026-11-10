#!/usr/bin/env python3
"""微信读书扫码登录 + 公众号文章列表抓取（不依赖公众号后台 appmsgpublish 权限）。

背景
----
mp.weixin.qq.com 的 ``/cgi-bin/appmsgpublish`` 对本机未认证订阅号恒定返回
``ret=200013``（静默 420 秒后仍复现，任意 fakeid 同样复现），因此公众号后台的
“插入超链接 / 取他人文章列表”通道不可用。

微信读书提供等价接口：
    https://i.weread.qq.com/mp/chapters?bookId=MP_WXS_{uin}&synckey=0
``bookId`` 由公众号 ``fakeid``（= base64 的 __biz）解码得到。

流程：
  1. 取 wxticket -> 调 qrconnect 得到 {uuid, qrcodebase64}；
  2. 渲染二维码 PNG，长轮询 qrconnect 扫码结果；
  3. 用 auth_code 换取微信读书长效凭据 (vid / skey / accessToken)；
  4. 用凭据抓取指定公众号文章列表。

凭据落盘为本地 600 权限 JSON；token / skey 值不打印到日志。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

APP_HEADERS = {
    "User-Agent": "WeRead/7.5.0 (Android; 33; arm64-v8a)",
    "Content-Type": "application/json; charset=UTF-8",
}
WEREAD_APPID = "wxab9b71ad2b90ff34"
QR_LOGIN_URL = "https://long.open.weixin.qq.com/connect/l/qrconnect"
TICKET_URL = "https://i.weread.qq.com/wxticket?nonceStr=weread"
LOGIN_URL = "https://i.weread.qq.com/login"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _get_text(url: str, headers: dict | None = None, timeout: int = 20) -> str:
    request = urllib.request.Request(url, headers=headers or APP_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def _post_json(url: str, payload: dict, timeout: int = 20) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=APP_HEADERS, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="ignore")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"_raw": body[:400]}


def request_qr() -> dict:
    """获取微信读书扫码登录的 uuid 与二维码图片数据。"""
    ticket = json.loads(_get_text(TICKET_URL))
    if "signature" not in ticket:
        raise RuntimeError(f"获取 wxticket 失败: {sorted(ticket)}")
    params = {
        "appid": WEREAD_APPID,
        "noncestr": "weread",
        "timestamp": ticket["timeStamp"],
        "scope": "snsapi_userinfo,snsapi_timeline,snsapi_friend,snsapi_favorites",
        "signature": ticket["signature"],
    }
    url = "https://open.weixin.qq.com/connect/sdk/qrconnect?" + urllib.parse.urlencode(params)
    payload = json.loads(_get_text(url))
    if not payload.get("uuid"):
        raise RuntimeError(f"获取二维码失败: {payload}")
    return payload


def exchange_token(auth_code: str) -> dict:
    """用扫码回调 auth_code 换取微信读书长效 token。"""
    device_id = base64.b64encode(random.randbytes(16)).decode()[:24]
    ts = int(time.time() * 1000)
    rand_val = random.randint(0, 999)
    signature = hashlib.sha256(f"{ts}{device_id}{rand_val}".encode()).hexdigest()
    payload = {
        "code": auth_code,
        "deviceId": device_id,
        "trackId": "",
        "timestamp": ts,
        "random": rand_val,
        "signature": signature,
        "wxToken": 1,
        "appFirstInstall": 0,
        "virtualChannelId": "",
        "installId": base64.b64encode(random.randbytes(12)).decode()[:16],
        "isFromQrcode": 1,
        "isAutoLogout": 0,
        "deviceName": "Android",
        "deviceType": 1,
    }
    result = _post_json(LOGIN_URL, payload)
    result["deviceId"] = device_id
    result["userAgent"] = APP_HEADERS["User-Agent"]
    return result


def biz_to_book_id(fakeid: str) -> str:
    """base64 的 __biz / fakeid -> 微信读书 bookId (MP_WXS_{uin})。"""
    raw = urllib.parse.unquote(str(fakeid).strip())
    if raw.startswith("MP_"):
        return raw
    if len(raw) % 4:
        raw += "=" * (4 - len(raw) % 4)
    try:
        decoded = base64.b64decode(raw).decode("utf-8", errors="ignore").strip()
        if decoded.isdigit():
            return f"MP_WXS_{decoded}"
    except Exception:
        pass
    if raw.isdigit():
        return f"MP_WXS_{raw}"
    return raw


def weread_headers(creds: dict) -> dict:
    return {
        "User-Agent": creds.get("userAgent") or APP_HEADERS["User-Agent"],
        "Content-Type": "application/json; charset=UTF-8",
        "wr_vid": creds.get("vid", ""),
        "wr_skey": creds.get("skey", ""),
    }


def fetch_chapters(creds: dict, book_id: str, begin: int = 0, count: int = 20) -> dict:
    query = urllib.parse.urlencode({"bookId": book_id, "synckey": 0, "start": begin, "count": count})
    url = "https://i.weread.qq.com/mp/chapters?" + query
    request = urllib.request.Request(url, headers=weread_headers(creds))
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8", errors="ignore")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"_raw": body[:400]}


def write_qr(payload: dict, out_path: Path) -> None:
    """优先用接口返回的 qrcodebase64 渲染 PNG；失败时回退到本地 qrcode 库。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    base64_data = (payload.get("qrcode") or {}).get("qrcodebase64")
    if base64_data:
        raw = base64.b64decode(base64_data)
        out_path.write_bytes(raw)
        return
    import qrcode

    qrcode.make(payload["uuid"]).save(out_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="微信读书扫码登录 / 抓取公众号文章列表")
    parser.add_argument("--qr-out", default="", help="二维码 PNG 输出路径")
    parser.add_argument("--creds-out", default="", help="凭据 JSON 输出路径（本地 600 权限）")
    parser.add_argument("--fetch", default="", help="用已保存凭据抓取该 fakeid 的文章列表")
    parser.add_argument("--begin", type=int, default=0)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    if args.fetch:
        creds = json.loads(Path(args.creds_out).read_text())
        book_id = biz_to_book_id(args.fetch)
        print(f"book_id={book_id}")
        print(json.dumps(fetch_chapters(creds, book_id, args.begin, args.count), ensure_ascii=False)[:4000])
        return 0

    payload = request_qr()
    uuid_value = payload["uuid"]
    print("QR_UUID:", uuid_value)
    if args.qr_out:
        out = Path(args.qr_out)
        write_qr(payload, out)
        print("QR_PNG:", out)

    deadline = time.time() + args.timeout
    last = ""
    while time.time() < deadline:
        poll = f"{QR_LOGIN_URL}?f=json&uuid={uuid_value}&last={last}"
        try:
            body = _get_text(poll, headers={"User-Agent": BROWSER_UA}, timeout=75)
            data = json.loads(body)
        except Exception as error:
            print(f"poll error: {error}", file=sys.stderr)
            time.sleep(2)
            continue
        errcode = str(data.get("wx_errcode", data.get("errcode", "")))
        if errcode == "405":
            wx_code = data.get("wx_code")
            print("SCAN_OK", flush=True)
            result = exchange_token(str(wx_code))
            vid = result.get("vid")
            if not vid or not result.get("accessToken"):
                safe = {k: v for k, v in result.items() if k not in ("accessToken", "refreshToken", "skey")}
                print("TOKEN_FAIL:", json.dumps(safe, ensure_ascii=False))
                return 1
            creds = {
                "vid": str(vid),
                "skey": result.get("skey", ""),
                "accessToken": result.get("accessToken", ""),
                "refreshToken": result.get("refreshToken", ""),
                "deviceId": result.get("deviceId", ""),
                "userAgent": result.get("userAgent", APP_HEADERS["User-Agent"]),
                "nickname": (result.get("user") or {}).get("name", ""),
            }
            if args.creds_out:
                path = Path(args.creds_out)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(creds, ensure_ascii=False, indent=2))
                path.chmod(0o600)
                print("CREDS_SAVED:", path)
            print("LOGIN_SUCCEEDED vid=" + str(vid))
            return 0
        if errcode == "408":
            print("waiting_scan", flush=True)
            time.sleep(3)
            continue
        if errcode not in ("", "201"):
            print(f"scan_status={errcode} {data.get('errmsg', '')}")
            time.sleep(3)
            continue
        last = str(data.get("uuid", last))
        time.sleep(2)
    print("TIMEOUT")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
