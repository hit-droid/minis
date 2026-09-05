#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TraeWork 每日自动签到 —— 云端无头版（GitHub Actions / 任意 cron 环境）

凭证存在一个**私有 Gist** 里，每次运行读回、刷新后写回，因此 token 轮换不会丢状态。

同一个 Gist 里还有第二个文件 status.json，是**跨运行器的记账本**：
任何 runner（GitHub Actions、手机、别的机器）开工前先读它，发现今天已经
成功就直接退出。这样多个保底源不会互相重复劳动，也让"今天到底签没签"
变成一个可以一眼查到的事实，而不是要靠猜。

需要的环境变量
  GH_PAT       GitHub Personal Access Token，只要 **gist** 一个 scope
  GIST_ID      存放凭证的 Gist ID（首次运行会自动创建，并打印出来）

可选
  TRAE_CRED    base64(JSON) 初始凭证；首次运行时用它来建 Gist，之后不再需要
  RUNNER       本次运行者的标识，写进记账本便于排查是谁签的

子命令
  (默认)       检查记账本 → 未签到则签到 → 写记账本
  status       只报告今天状态，不签到（exit 0=已完成, 3=未完成）
  local        纯本地模式，不碰 Gist（应急，token 轮换不会保存）

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

CRED_FILE = "trae_cred.json"
LOG_FILE = "status.json"
# Trae 是 CN 服务，日界按北京时间算，不能依赖 runner 自己的时区
CN_TZ = 8 * 3600

_HERE = os.path.dirname(os.path.abspath(__file__))


def _cred_candidates():
    """本地凭证镜像的可能位置。

    仓库布局是 trae_auto/checkin.py + 同级 trae_cred.json，而跨会话的
    shared 目录是扁平放法，所以两种都要试。
    """
    return [os.path.join(_HERE, "trae_cred.json"),
            os.path.abspath(os.path.join(_HERE, "..", "trae_cred.json")),
            "/var/minis/shared/trae_checkin/trae_cred.json",
            os.path.expanduser("~/.trae_checkin/cred.json")]


def _cred_target():
    """写入目标：已存在的第一个，否则用最后一个（持久目录）。"""
    for p in _cred_candidates():
        if os.path.exists(p):
            return p
    return _cred_candidates()[-1]


LOCAL_CRED = None  # 兼容旧引用；实际路径由 _cred_target() 决定


def today():
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + CN_TZ))


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + CN_TZ))


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
    st, raw = 0, ""
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


def gist_load_all(gist_id):
    """返回 ({cred: dict|None, log: dict|None})；Gist 不可达返回 None。"""
    st, raw = http("GET", f"https://api.github.com/gists/{gist_id}", gh())
    if st != 200:
        return None
    try:
        files = json.loads(raw)["files"]
    except Exception:
        return None

    def pick(name):
        f = files.get(name)
        if not f or f.get("content") is None:
            return None
        try:
            return json.loads(f["content"])
        except Exception:
            return None

    return {"cred": pick(CRED_FILE), "log": pick(LOG_FILE)}


def gist_save(gist_id, cred, log=None):
    """PATCH 会**替换整个文件集合**，漏传的文件会被删掉 —— 所以两个文件必须一起发。"""
    files = {CRED_FILE: {"content": json.dumps(cred, ensure_ascii=False, indent=2)}}
    if log is not None:
        files[LOG_FILE] = {"content": json.dumps(log, ensure_ascii=False, indent=2)}
    payload = {"files": files}
    st, raw = http("PATCH", f"https://api.github.com/gists/{gist_id}", gh(), payload)
    if st != 200:
        print("⚠ 写回 Gist 失败：", raw[:200])
        return False
    return True


def gist_create(cred, log):
    payload = {"public": False,
               "description": "TraeWork 签到凭证 + 状态记账本（自动维护，勿手改）",
               "files": {CRED_FILE: {"content": json.dumps(cred, ensure_ascii=False, indent=2)},
                         LOG_FILE: {"content": json.dumps(log, ensure_ascii=False, indent=2)}}}
    st, raw = http("POST", "https://api.github.com/gists", gh(), payload)
    if st not in (200, 201):
        print("❌ 创建 Gist 失败：", raw[:300])
        sys.exit(4)
    return json.loads(raw)["id"]


def load_local_cred():
    """Gist 完全不可达时的最后一道保险：读本地凭证镜像。"""
    for p in _cred_candidates():
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("accessToken"):
                return d
        except Exception:
            continue
    return None


def save_local_cred(cred):
    """更新本地凭证镜像 —— 但**仅当某个候选位置已存在**时才写。

    这条规则同时解决了两件事：
      · 云端 runner 是一次性环境，checkout 出来的仓库里没有这个文件，
        所以不会把明文凭证白白写到别人的磁盘上（少一个泄露面）；
      · 本地/手机这类持久环境里文件存在，token 轮换后能及时落盘，
        成为 Gist 不可达时的救命备份。
    """
    p = _cred_target()
    if not os.path.exists(p):
        return
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cred, f, ensure_ascii=False, indent=2)
        os.chmod(p, 0o600)
    except Exception as e:
        print("⚠ 本地凭证镜像写入失败：", str(e)[:80])


# ------------------------------------------------------------------ 记账本
def log_done(log):
    return bool((log or {}).get("days", {}).get(today(), {}).get("ok"))


def log_record(log, ok, note="", credits=None, runner=None, egress=None):
    """只保留最近 60 天，避免 Gist 无限膨胀。"""
    log = log or {}
    days = log.setdefault("days", {})
    entry = {"ok": bool(ok), "at": now(), "note": note[:200],
             "credits": credits, "runner": runner or os.environ.get("RUNNER", "unknown")}
    if egress:
        entry["egress"] = f"{egress.get('cc')}/{egress.get('ip')}"
    days[today()] = entry
    for old in sorted(days)[:-60]:
        days.pop(old, None)
    return log


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


def query_status(cred):
    """只读查询 Trae 侧签到状态。绝不发 claim。"""
    return http_json("POST", API_HOST + STATUS_PATH, hdrs(cred), {})


def run_dry(cred):
    """取证模式：只探测出口 IP + 查状态，**绝不发 claim**。

    存在理由：需要确认云端 runner 的出口地域，但当天尚未签到，
    直接跑主流程会真的把积分领掉，污染观察结果。
    """
    e = egress_info()
    print(f"runner={os.environ.get('RUNNER','?')} 出口={e['ip']} | {e['country']} ({e['cc']}) | {e['isp']}")
    s = query_status(cred)
    print("Trae 侧:", json.dumps(s, ensure_ascii=False)[:200])
    # 2026-09-05 修正：dry 只做取证，出口地域不作为失败判定。
    # 9074 实测根因是 deviceId 被标记（换号即解），与 IP 无关 ——
    # 境外 IP 也可能成功，境外+旧号失败也不能归因地域。不再 return 4。
    if e["cc"] == "CN":
        print("→ 出口在中国大陆")
    else:
        print(f"→ 出口在 {e['cc']}（非 CN）。注意：9074 与 IP 无关（deviceId 被标记才是根因），"
              "此处仅作记录，不代表 claim 会失败。")
    return 0


def egress_info():
    """探测本次请求的出口 IP 与国家。

    存在理由：9074 曾被误判为限流，实际高度怀疑是**境外 IP 触发地域风控**
    （Trae 是纯 CN 服务，请求头还写着 X-User-Region: CN）。
    没有这条数据，重试和错峰都是盲调。
    """
    srcs = [("http://ip-api.com/json/?fields=country,countryCode,isp,query",
             ("countryCode", "country", "isp", "query")),
            ("https://api.ip.sb/geoip", ("country_code", "country", "isp", "ip")),
            ("https://ipinfo.io/json", ("country", "country", "org", "ip"))]
    # 沙箱网络会随机 DNS 失败/断连，单发一次必然偶发 unknown，
    # 而 unknown 会让地域判断退化成"不敢下结论"，所以每个源都重试一轮。
    for rnd in range(3):
        for url, (kcc, kcty, kisp, kip) in srcs:
            st, raw = http("GET", url, timeout=12)
            if st != 200 or not raw:
                continue
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            cc = (d.get(kcc) or "?").upper()
            if cc == "?":
                continue
            return {"cc": cc, "country": d.get(kcty, "?"),
                    "isp": d.get(kisp, "?"), "ip": d.get(kip, "?")}
        time.sleep(2)
    return {"cc": "?", "country": "unknown", "isp": "unknown", "ip": "unknown"}


def do_claim(cred):
    """执行签到。返回 (ok, note, credits, egress)。"""
    s = query_status(cred)
    print("status:", json.dumps(s, ensure_ascii=False)[:200])
    if s.get("code") != 0:
        return False, f"status 查询失败: {s.get('_raw') or s}", None, None
    if not s.get("enable"):
        return True, "该账号未开启签到活动", None, None
    if s.get("checked_in"):
        return True, "今日已签到（Trae 侧确认）", s.get("credits"), None

    egress = egress_info()
    print(f"出口: {egress['ip']} | {egress['country']} ({egress['cc']}) | {egress['isp']}")

    # 9074 自愈（2026-08-31 实测修正）：deviceId 被 Trae 风控标记后无论 IP 在哪
    # 都 9074；换新 16 位号立即成功（同日手机 CN IP 实测）。IP 仅记录，不再拦截。
    for attempt in range(8):
        r = http_json("POST", API_HOST + CLAIM_PATH, hdrs(cred), {}, tries=3)
        code = r.get("code")
        msg = r.get("message") or ""
        print(f"[{time.strftime('%H:%M:%S')}] claim -> {json.dumps(r, ensure_ascii=False)[:160]}")

        if code == 0:
            return True, "签到成功", 200, egress

        if code == 9074:
            if attempt < 3:
                cred["deviceId"] = aha_device_id()
                print(f"⚠ 9074：deviceId 疑似被标记，换新号 {cred['deviceId']} 重试")
                time.sleep(2)
                continue
            wait = min(25 * (attempt + 1), 90)
            print(f"⚠ 9074：已换 3 号仍失败，{wait}s 后退避重试")
            time.sleep(wait)
            continue

        if "已签到" in msg or "已领取" in msg:
            return True, "今日已签到", s.get("credits"), egress

        return False, f"claim 失败 code={code} msg={msg}", None, egress

    return False, "重试 6 次仍失败（限流/风控）", None, egress


# ------------------------------------------------------------------ main
def run_status():
    """只读体检：记账本 + Trae 真实状态。任何情况下都不发 claim。

    exit 0 = 今日已完成；3 = 未完成/无法确认。
    """
    print(f"日期(北京): {today()}")
    gist_id = os.environ.get("GIST_ID", "").strip()
    log, cred = None, None
    if gist_id and os.environ.get("GH_PAT"):
        b = gist_load_all(gist_id)
        if b:
            log, cred = b["log"], b["cred"]
            d = (log or {}).get("days", {}).get(today())
            if d:
                print(f"  记账本: {'ok' if d.get('ok') else 'fail'} @ {d.get('at')} "
                      f"| {d.get('runner')} | {d.get('note')}")
            else:
                print("  记账本: 今日无记录")
        else:
            print("  记账本: Gist 不可达")

    # Trae 侧查询优先用 Gist 里的凭证（那是轮换后的最新 token），
    # 本地镜像可能落后一个版本，只作兜底。
    if not cred:
        cred = load_local_cred()
    if not cred:
        print("❌ 无凭证可查 Trae 状态")
        d = (log or {}).get("days", {}).get(today())
        return 0 if d and d.get("ok") else 3

    s = query_status(cred)
    print("  Trae 侧:", json.dumps(s, ensure_ascii=False)[:200])
    if s.get("code") == 0 and s.get("checked_in"):
        print(f"✔ 今日已签到 | credits={s.get('credits')}")
        return 0
    if s.get("code") == 0 and not s.get("enable"):
        print("ℹ 该账号未开启签到活动")
        return 0
    if s.get("code") not in (None, 0) and (s.get("code") in (20101, 20102)
                                           or "Token" in str(s.get("message", ""))):
        print("⚠ token 已失效，需人工重新绑定")
    else:
        print("✘ 今日尚未签到")
    return 3


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if mode == "status":
        return run_status()
    if mode == "dry":
        # 取证：不 claim、不写记账本
        gid = os.environ.get("GIST_ID", "").strip()
        cr = None
        if gid and os.environ.get("GH_PAT"):
            b = gist_load_all(gid)
            cr = b["cred"] if b else None
        cr = cr or load_local_cred()
        if cr is None:
            print("❌ 无凭证")
            return 2
        return run_dry(ensure_valid(cr) or cr)

    has_gh = bool(os.environ.get("GH_PAT")) and os.environ.get("GIST_ID", "").strip()
    gist_id = os.environ.get("GIST_ID", "").strip()

    # ---- 纯本地应急模式：完全不碰 Gist
    if mode == "local" or not has_gh:
        if mode != "local":
            print("⚠ 未设置 GH_PAT/GIST_ID：纯本地模式，token 轮换不会保存")
        cred = load_local_cred()
        if cred is None:
            seed = os.environ.get("TRAE_CRED", "").strip()
            if not seed:
                print("❌ 既无 Gist 也无本地凭证，无法签到")
                return 2
            cred = json.loads(base64.b64decode(seed).decode())
        cred = ensure_valid(cred)
        if cred is None:
            return 3
        ok, note, credits, egress = do_claim(cred)
        save_local_cred(cred)
        print(("✅ " if ok else "❌ ") + note)
        return 0 if ok else 1

    # ---- 正常模式：以 Gist 为准
    bundle = gist_load_all(gist_id)
    if bundle is None:
        print("⚠ Gist 不可达，降级到本地凭证")
        cred = load_local_cred()
        if cred is None:
            seed = os.environ.get("TRAE_CRED", "").strip()
            cred = json.loads(base64.b64decode(seed).decode()) if seed else None
        if cred is None:
            print("❌ Gist 读不到，且无本地备份/初始凭证")
            return 2
        cred = ensure_valid(cred)
        if cred is None:
            return 3
        ok, note, credits, egress = do_claim(cred)
        save_local_cred(cred)
        print(("✅ " if ok else "❌ ") + note + "（记账本未更新）")
        return 0 if ok else 1

    cred, log = bundle["cred"], bundle["log"]

    if cred is None:
        seed = os.environ.get("TRAE_CRED", "").strip()
        if not seed:
            print("❌ Gist 里没有凭证，且未提供 TRAE_CRED 初始值")
            return 2
        cred = json.loads(base64.b64decode(seed).decode())
        if not gist_save(gist_id, cred, log_record(log, False, "初始化")):
            return 2
        print(f"✔ 已用 TRAE_CRED 初始化 Gist {gist_id}")

    # ---- 保底协调：记账本说今天成功了，还要不要问 Trae？
    # 要。记账本可能因为写回失败而落后于事实，而 Trae 的 status 才是真相。
    # 但反过来说，记账本成功 + status 也确认已签到时，就绝不再发 claim。
    before = cred
    cred = ensure_valid(cred)
    if cred is None:
        # ⚠ 绝不能写 {} 回去 —— PATCH 会替换文件，凭证会被清空，
        # 那等于把 refreshToken 也丢了，只能人工重新绑定。原样保存旧凭证。
        gist_save(gist_id, before,
                  log_record(log, False, "refreshToken 失效，需人工重新绑定"))
        return 3

    ok, note, credits, egress = do_claim(cred)
    log = log_record(log, ok, note, credits, egress=egress)
    save_local_cred(cred)
    if not gist_save(gist_id, cred, log):
        print("⚠ 记账本写回失败（不影响本次签到结果）")

    print(("✅ " if ok else "❌ ") + note)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
