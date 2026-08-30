#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TraeWork 每日自动签到 —— 云端无头版（GitHub Actions / 任意 cron 环境）

与本地 trae_checkin.py 的区别：凭证不存文件，而是存在一个**私有 Gist** 里，
每次运行读回、刷新后写回，因此 token 轮换不会丢状态。

需要的环境变量
  GH_PAT       GitHub Personal Access Token，只要 **gist** 一个 scope
  GIST_ID      存放凭证的 Gist ID（首次运行会自动创建，并打印出来）

可选
  TRAE_CRED    base64(JSON) 初始凭证；首次运行时用它来建 Gist，之后不再需要

纯标准库，无第三方依赖。
"""
import base64
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request

CLIENT_ID = "en1oxy7wnw8j9n"
AUTH_BASE = "https://api.trae.com.cn"
API_HOST = "https://api.trae.cn"
CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"
STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
EXCHANGE = f"{AUTH_BASE}/cloudide/api/v3/trae/oauth/ExchangeToken"
GIST_FILE = "trae_cred.json"


# ------------------------------------------------------------------ http
def http(method, url, headers=None, body=None, timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception as e:
        return 0, json.dumps({"_err": str(e)[:200]})


def http_json(method, url, headers=None, body=None, tries=4):
    """带重试的 JSON 请求 —— 这个 API 网关经常直接断连，必须重试。"""
    for i in range(tries):
        st, raw = http(method, url, headers, body)
        if st and raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
        if i < tries - 1:
            time.sleep(4 * (i + 1))
    return {"_status": st, "_raw": raw[:200] if raw else ""}


# ------------------------------------------------------------------ gist
def gh(headers=None):
    return {"Authorization": f"Bearer {os.environ['GH_PAT']}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28", **(headers or {})}


def gist_load(gist_id):
    st, raw = http("GET", f"https://api.github.com/gists/{gist_id}", gh())
    if st != 200:
        return None
    try:
        content = json.loads(raw)["files"][GIST_FILE]["content"]
        return json.loads(content)
    except Exception:
        return None


def gist_create(cred):
    payload = {"public": False,
               "description": "TraeWork 签到凭证（自动维护，勿手改）",
               "files": {GIST_FILE: {"content": json.dumps(cred, ensure_ascii=False, indent=2)}}}
    st, raw = http("POST", "https://api.github.com/gists", gh(), payload)
    if st not in (200, 201):
        print("❌ 创建 Gist 失败：", raw[:300]); sys.exit(4)
    return json.loads(raw)["id"]


def gist_save(gist_id, cred):
    payload = {"files": {GIST_FILE: {"content": json.dumps(cred, ensure_ascii=False, indent=2)}}}
    st, raw = http("PATCH", f"https://api.github.com/gists/{gist_id}", gh(), payload)
    if st != 200:
        print("⚠ 写回 Gist 失败：", raw[:200])
        return False
    return True


def persist(gist_id, has_gh, cred):
    """把（可能已轮换的）凭证写回 Gist；本地模式静默跳过。"""
    if has_gh and gist_id:
        gist_save(gist_id, cred)
    else:
        print("ℹ 本地模式：跳过 Gist 回写")


# ------------------------------------------------------------------ trae
def aha_device_id():
    """16 位十进制 Aha 设备号。

    ⚠ claim 接口只认这个格式。传 32 位 hex / UUID 会被风控判定为非官方
    客户端设备，返回极具误导性的 code 9074「当前参与用户太多」。
    """
    return "".join([str(random.randint(1, 9))] + [str(random.randint(0, 9)) for _ in range(15)])


def hdrs(c):
    return {"Content-Type": "application/json",
            "Authorization": f"Cloud-IDE-JWT {c['accessToken']}",
            "x-device-id": c["deviceId"],
            "X-User-Region": "CN"}


def refresh(c):
    r = http_json("POST", EXCHANGE, {"Content-Type": "application/json"},
                  {"ClientID": CLIENT_ID, "RefreshToken": c["refreshToken"],
                   "ClientSecret": "-", "UserID": ""})
    res = r.get("Result") or r.get("data") or {}
    tok = res.get("Token") or res.get("AccessToken")
    if not tok:
        print("❌ refreshToken 已失效，需要人工重新绑定一次。响应：",
              json.dumps(r, ensure_ascii=False)[:300])
        return None
    c["accessToken"] = tok
    c["refreshToken"] = res.get("RefreshToken") or c["refreshToken"]
    bound = res.get("BoundDeviceID")
    if bound and re.fullmatch(r"\d{16}", str(bound)):
        c["deviceId"] = str(bound)
    exp = res.get("TokenExpireAt")
    try:
        exp = float(exp) if exp else None
    except (TypeError, ValueError):
        exp = None
    c["expiresAt"] = (exp / 1000 if exp and exp > 1e12 else exp) if exp else time.time() + 3600
    print("↻ token 已续期至",
          time.strftime("%Y-%m-%d %H:%M", time.localtime(c["expiresAt"])))
    return c


def ensure_valid(c):
    if time.time() > c.get("expiresAt", 0) - 600:
        return refresh(c)
    return c


def main():
    has_gh = bool(os.environ.get("GH_PAT"))
    if not has_gh:
        print("⚠ 未设置 GH_PAT：本次为纯本地模式，不会读写 Gist，"
              "轮换后的 token 也不会保存")

    gist_id = os.environ.get("GIST_ID", "").strip() if has_gh else ""
    cred = gist_load(gist_id) if gist_id else None

    if cred is None:
        seed = os.environ.get("TRAE_CRED", "").strip()
        if not seed:
            print("❌ Gist 里没有凭证，且未提供 TRAE_CRED 初始值")
            print("   先在本地跑 trae_checkin.py 绑定，再把 trae_cred.json base64 后填入 secret")
            return 2
        cred = json.loads(base64.b64decode(seed).decode())
        if not has_gh:
            print("✔ 已从 TRAE_CRED 载入凭证（本地模式）")
        elif gist_id:
            if not gist_save(gist_id, cred):
                return 2
            print(f"✔ 已用 TRAE_CRED 初始化 Gist {gist_id}")
        else:
            gist_id = gist_create(cred)
            print(f"✔ 已自动创建私有 Gist，GIST_ID = {gist_id}")
            print(f"   请把它填到仓库 secret：GIST_ID={gist_id}")
            return 0

    cred = ensure_valid(cred)
    if cred is None:
        return 3

    s = http_json("POST", API_HOST + STATUS_PATH, hdrs(cred), {})
    print("status:", json.dumps(s, ensure_ascii=False)[:200])
    if s.get("code") != 0:
        print("❌ 状态查询失败"); return 1
    if not s.get("enable"):
        print("ℹ 该账号未开启签到活动"); return 0
    if s.get("checked_in"):
        print("✔ 今日已签到，跳过")
        persist(gist_id, has_gh, cred)
        return 0

    ok = False
    for attempt in range(6):
        r = http_json("POST", API_HOST + CLAIM_PATH, hdrs(cred), {}, tries=3)
        code = r.get("code")
        msg = r.get("message") or ""
        print(f"[{time.strftime('%H:%M:%S')}] claim -> {json.dumps(r, ensure_ascii=False)[:160]}")

        if code == 0:
            print("✅ 签到成功"); ok = True; break

        if code == 9074:
            if attempt == 0 and not re.fullmatch(r"\d{16}", str(cred.get("deviceId", ""))):
                cred["deviceId"] = aha_device_id()
                print("⚠ 9074：设备号格式不对，已换成 Aha 16 位号重试")
                continue
            wait = min(25 * (attempt + 1), 90)
            print(f"⚠ 限流/风控，{wait}s 后重试"); time.sleep(wait); continue

        if "已签到" in msg or "已领取" in msg:
            print("✔ 今日已签到"); ok = True; break

        print("❌ 签到失败"); break

    if gist_id:
        gist_save(gist_id, cred)
    elif not has_gh:
        print("ℹ 本地模式：跳过 Gist 回写")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
