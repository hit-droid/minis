#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trae 第二账号云端签到（GitHub Actions 版）—— 完全独立于主账号。

与手机版 checkin2.py 的差别：
  * 凭证存 GitHub Gist（cred2.json），refreshToken 轮换后可写回
  * 记账本 status2.json 也存 Gist，做幂等
  * 无需 cookie：refreshToken 有效期到 2027-04

自愈链（与手机版对齐）：
  1. token 过期/失效 -> refreshToken 续期
  2. claim 9074     -> 换新设备号重试（最多 2 次）
  3. 成功后写回 Gist（新 token + 新设备号）

用法:
    python3 checkin_cloud2.py          正常签到
    python3 checkin_cloud2.py status   只读体检（exit 0 已签 / 3 未签）
"""
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

API = "https://api.trae.cn"
CLIENT_ID = "en1oxy7wnw8j9n"
EXCHANGE_PATH = "/cloudide/api/v3/trae/oauth/ExchangeToken"
STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"

GH_TOKEN = os.environ.get("GH_PAT", "")
GIST_ID = os.environ.get("GIST2_ID", "")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def http(method, url, headers=None, body=None, timeout=25):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                                   # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def gh(method, path, payload=None):
    h = {"Authorization": "Bearer " + GH_TOKEN,
         "Accept": "application/vnd.github+json",
         "Content-Type": "application/json"}
    st, raw = http(method, "https://api.github.com" + path, h, payload, 40)
    try:
        return st, json.loads(raw)
    except json.JSONDecodeError:
        return st, {"_raw": raw[:300]}


def gist_load():
    st, j = gh("GET", f"/gists/{GIST_ID}")
    if st != 200:
        return None, None, f"读 Gist 失败 http={st}"
    files = j.get("files") or {}
    cred = log_ = None
    if "cred2.json" in files:
        try:
            cred = json.loads(files["cred2.json"]["content"])
        except json.JSONDecodeError:
            pass
    if "status2.json" in files:
        try:
            log_ = json.loads(files["status2.json"]["content"])
        except json.JSONDecodeError:
            log_ = {"days": {}}
    return cred, (log_ or {"days": {}}), None


def gist_save(cred, log_):
    payload = {"files": {
        "cred2.json": {"content": json.dumps(cred, ensure_ascii=False, indent=2)},
        "status2.json": {"content": json.dumps(log_, ensure_ascii=False, indent=2)},
    }}
    st, _ = gh("PATCH", f"/gists/{GIST_ID}", payload)
    return st == 200


def aha_device_id():
    return "".join([str(random.randint(1, 9))] +
                   [str(random.randint(0, 9)) for _ in range(15)])


def hdrs(c):
    return {"Content-Type": "application/json",
            "Authorization": "Cloud-IDE-JWT " + c["accessToken"],
            "x-device-id": str(c["deviceId"]),
            "X-User-Region": "CN"}


def exchange(rt):
    st, raw = http("POST", API + EXCHANGE_PATH,
                   {"Content-Type": "application/json"},
                   {"ClientID": CLIENT_ID, "RefreshToken": rt,
                    "ClientSecret": "-", "UserID": ""})
    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
        return None, f"ExchangeToken 非 JSON http={st}"
    res = j.get("Result") or j.get("data") or {}
    tok = res.get("Token") or res.get("AccessToken")
    if not tok:
        return None, f"ExchangeToken 无 Token: {json.dumps(j, ensure_ascii=False)[:180]}"
    exp = res.get("TokenExpireAt")
    rexp = res.get("RefreshExpireAt")
    return {"accessToken": tok,
            "refreshToken": res.get("RefreshToken") or rt,
            "expiresAt": (float(exp) / 1000) if exp else time.time() + 3600,
            "refreshExpireAt": (float(rexp) / 1000) if rexp else 0}, None


def trae(path, cred, body=None):
    st, raw = http("POST", API + path, hdrs(cred), body or {})
    try:
        return st, json.loads(raw)
    except json.JSONDecodeError:
        return st, {"_raw": raw[:200]}


def sync_cronjob(cred):
    """把最新 token / 设备号同步到 cron-job.org 的备用任务。

    为什么需要：cron-job.org 的 header 是静态的，不会自己跟着 token 轮换。
    不同步的话，token 一过期（14 天）这条备用路径就静默失效了。
    没配 CRONJOB_API_KEY 就跳过，不影响主流程。
    """
    key = os.environ.get("CRONJOB_API_KEY", "")
    job = os.environ.get("CRONJOB_JOB2_ID", "")
    if not key or not job:
        log("ℹ 未配置 CRONJOB_API_KEY / CRONJOB_JOB2_ID，跳过 cron-job 同步")
        return
    payload = {"job": {"enabled": True, "extendedData": {
        "headers": {
            "Authorization": "Cloud-IDE-JWT " + cred["accessToken"],
            "Content-Type": "application/json",
            "X-User-Region": "CN",
            "x-device-id": str(cred["deviceId"]),
        },
        "body": "{}",
    }}}
    st, raw = http("PATCH", f"https://api.cron-job.org/jobs/{job}",
                   {"Authorization": "Bearer " + key,
                    "Content-Type": "application/json"}, payload, 40)
    log(f"cron-job 同步 http={st}" + ("" if st == 200 else f" {raw[:120]}"))


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "run"
    if not GH_TOKEN or not GIST_ID:
        log("❌ 缺少 GH_PAT 或 GIST2_ID")
        return 1

    cred, logbook, err = gist_load()
    if err:
        log(f"❌ {err}")
        return 1
    if not cred:
        log("❌ Gist 里没有 cred2.json")
        return 1

    today = time.strftime("%Y-%m-%d", time.localtime(time.time() + 8 * 3600))
    log(f"日期(北京): {today} | uid={cred.get('userId')} dev={cred.get('deviceId')}")

    # --- token 有效性 ---
    if time.time() > cred.get("expiresAt", 0) - 600:
        log("token 需续期")
        new, e = exchange(cred["refreshToken"])
        if not new:
            log(f"❌ 续期失败: {e}")
            return 3
        new["deviceId"] = cred.get("deviceId") or aha_device_id()
        new["userId"] = cred.get("userId")
        cred = new
        log("✔ token 已续期")

    st, s = trae(STATUS_PATH, cred)
    log(f"status({st}): {json.dumps(s, ensure_ascii=False)[:160]}")

    if s.get("code") in (1001, 20101):
        log("⚠ token 被拒，续期后重试")
        new, e = exchange(cred["refreshToken"])
        if not new:
            log(f"❌ 续期失败: {e}")
            return 3
        new["deviceId"] = cred.get("deviceId") or aha_device_id()
        new["userId"] = cred.get("userId")
        cred = new
        st, s = trae(STATUS_PATH, cred)
        log(f"续期后 status({st}): {json.dumps(s, ensure_ascii=False)[:160]}")

    if s.get("code") != 0:
        log(f"❌ status 异常 code={s.get('code')}")
        return 3

    if not s.get("enable"):
        log("ℹ 活动未开启")
        return 0

    if s.get("checked_in"):
        log(f"✔ 今日已签到 | credits={s.get('credits')}")
        logbook.setdefault("days", {})[today] = {
            "ok": True, "credits": s.get("credits"),
            "runner": os.environ.get("RUNNER", "gh-actions"),
            "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        logbook["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        gist_save(cred, logbook)
        return 0

    if action == "status":
        log("→ 今日未签到（只读模式）")
        return 3

    # --- claim + 9074 自愈 ---
    log("→ 今日未签到，开始 claim")
    st, r = trae(CLAIM_PATH, cred)
    log(f"claim({st}): {json.dumps(r, ensure_ascii=False)[:160]}")

    if r.get("code") == 9074:
        log("⚠ 9074：设备号被标记，换号重试（最多 2 次）")
        for i in range(2):
            cred["deviceId"] = aha_device_id()
            log(f"   换号 -> {cred['deviceId']}")
            st, r = trae(CLAIM_PATH, cred)
            log(f"   claim -> code={r.get('code')} msg={r.get('message')}")
            if r.get("code") == 0:
                break
            if r.get("code") != 9074:
                break
            time.sleep(3)

    if r.get("code") == 0:
        st2, s2 = trae(STATUS_PATH, cred)
        credits = s2.get("credits") or s.get("credits")
        log(f"✔ 签到成功 | credits={credits} | dev={cred['deviceId']}")
        logbook.setdefault("days", {})[today] = {
            "ok": True, "credits": credits,
            "runner": os.environ.get("RUNNER", "gh-actions"),
            "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        logbook["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if gist_save(cred, logbook):
            log("✔ 凭证与记账本已写回 Gist")
        else:
            log("⚠ 写回 Gist 失败（凭证未持久化，下次仍用旧号）")
        sync_cronjob(cred)
        return 0

    log(f"❌ 签到失败 code={r.get('code')} msg={r.get('message')}")
    logbook.setdefault("days", {})[today] = {
        "ok": False, "code": r.get("code"), "msg": r.get("message"),
        "runner": os.environ.get("RUNNER", "gh-actions"),
        "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    logbook["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    gist_save(cred, logbook)
    return 1


if __name__ == "__main__":
    sys.exit(main())
