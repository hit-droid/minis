# TraeWork 每日自动签到（无需电脑常开）

原理：GitHub Actions 每天定时跑一次 `checkin.py`，直接调用 Trae 的签到接口。
GitHub Actions 跑在微软的服务器上，**你的手机关机、断网都照常签到**。

## 一次性配置（约 5 分钟，全程手机浏览器）

### 1. 注册 GitHub，建一个私有仓库
- 打开 https://github.com/join 注册（或用微信/邮箱登录）
- 新建仓库：https://github.com/new
  - 名字随意，例如 `trae-checkin`
  - 选 **Private**（必须私有，因为要存凭证）
  - 点 Create repository

### 2. 建一个私有 Gist 用来存凭证
- 打开 https://gist.github.com/
- 随便建一个 **Secret** 的 Gist，文件名填 `trae_cred.json`，内容先写 `{}`
- 建好后看浏览器地址栏：`gist.github.com/<用户名>/`**`abc123...`**
  最后那串就是 **GIST_ID**，记下来

### 3. 生成 Fine-grained PAT
- 打开 https://github.com/settings/personal-access-tokens/new
- 设置：
  - Name：`trae-checkin`
  - Expiration：**Custom → 1 year**（到期前记得换）
  - Repository access → **Only select repositories** → 选第 1 步那个仓库，权限给 **Contents: Read and write**
  - 勾选 **Account permissions → Gists: Read and write**
- 生成后复制那串 `github_pat_...`

### 4. 把凭证传到仓库 Secrets
仓库 → Settings → Secrets and variables → Actions → New repository secret，建三个：

| Name | Value |
|---|---|
| `GH_PAT` | 第 3 步的 token |
| `GIST_ID` | 第 2 步那串 gist id |
| `TRAE_CRED` | 见下方 |

`TRAE_CRED` 的值 = 把 `trae_cred.json` 做 base64。在 Minis 里执行：

```sh
cat /var/minis/workspace/trae_cred.json | base64 -w 0
```

把输出整串粘进去。

### 5. 放工作流文件
仓库 → Add file → Create a new file，路径**必须**填：

```
.github/workflows/checkin.yml
```

内容粘贴本目录的 `checkin.yml`，Commit。

### 6. 开启 Actions 并手动验证一次
仓库 → Actions 标签页 → 如果提示要启用工作流，点 **I understand my workflows, go ahead and enable them**
→ 点 "TraeWork 每日自动签到" → Run workflow → Run workflow

几秒后点进那次运行，日志里看到下面任一行即成功：

```
✔ 今日已签到
✔ 签到成功
status: {"checked_in": true, ...}
```

配完就再也不用管了。每天北京时间 09:00 自动执行。

---

## 关键坑（已在本脚本中处理）

**`code 9074「当前参与用户太多」不是限流，是设备号格式错误。**

签到接口只接受 **16 位十进制的 Aha 设备号**。传 32 位 hex 或 UUID，
服务端会判定为非官方客户端设备，返回这个极具误导性的错误码——
无论重试多久都不会成功。

`x-device-id` 必须与 `Authorization` 里的 token 属于同一次登录会话。

## 关于 token 有效期

- access token 约 14 天，脚本会在剩 2 天时自动用 refreshToken 续期
- refreshToken 有效期到 **2027-02**（见 `expiresAt`），到期后需重新走一次绑定
- 每次续期 Trae 都可能轮换 refreshToken，所以凭证必须存在可写的 Gist 里，
  而不是写死在 Secret 中

## 手动跑一次（本地调试）

```sh
export TRAE_CRED=$(base64 -w0 /var/minis/workspace/trae_cred.json)
python3 checkin.py        # 无 GH_PAT 时为纯本地模式，不回写 Gist
```

## 换手机 / 重装

凭证都在 Gist 里，GitHub Actions 会自己读。你只需要保住 GitHub 账号。
