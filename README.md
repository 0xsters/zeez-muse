# muse.ai 注册 / 邀请码兑换 协议分析

补充一下，记得用JP 节点， JP 不需要绑卡，直接过

纯 HTTP 实现 muse.ai 的注册与邀请码兑换流程，不依赖浏览器。本文记录的是通过抓包逐字段比对得出的协议细节。

## 快速开始

```bash
pip install -r requirements.txt
cp config.example.json config.json   # 填入邮箱与可选代理
python example_register.py
```

## 目录结构

```
musepy/
  client.py    注册 + 兑换的完整协议客户端
  falco.py     行为埋点上报（可选）
  caa.py       注册漏斗事件上报（可选）
example_register.py   端到端示例
config.example.json   配置模板
test_onboarding_smoke.py   离线冒烟（披露解析纯函数，不联网）
```

## 协议流程

```
GET  /                                   拿初始 cookie
POST /api/auth/native/restart            → 201 {csrf_token}
POST /api/auth/native/send-otp           → 201
POST /api/auth/native/confirm-otp        → 200 {registration_state:"completion_required",
                                               tos:{content_version}}（动态 TOS 版本）
POST /api/auth/native/register-completion→ 200 {auth_meta_session:{frl_access_token, frl_account_id}}
POST auth.meta.com/hatch/session/bootstrap/ → 302…→ muse.ai 200 HTML（会话引导，必须）
     └ 从 muse.ai 文档解析动态披露 disclosure_text/disclosure_version 与状态门
       （resolvedDestination/isGeoGated/waitlist → 地区受限）
POST /api/hatch/accept-tos               → 200（单次调用；正文用服务端动态披露，非硬编码）
GET  /api/hatch/invite                   → 200 表示账号有效
POST /api/hatch/vm/wake                  → 200（必须，否则兑换误报 invalid；
                                             wake_trigger=mount + open_id + away_ms=0）
POST /api/hatch/stateful-sharing         → 200
POST /api/hatch/invite-code/redeem       → 200 {redemptionStatus:"redeemed"}
POST /api/hatch/invite                   → 200 {code}（取回自己的码，不等于页面刮开 reveal）
```

## 几个容易踩的坑

### 1. `confirm-otp` 必须带 `supports_muse_link_transparency: true`

```json
{"otp_code":"123456","remember_device":true,"supports_muse_link_transparency":true}
```

### 2. `register-completion` 的姓名字段语义

```json
{
  "date_of_birth": "1993-11-14",
  "first_name": "三郎",     // 名（given name）
  "last_name": "近藤",      // 姓（family name）
  "tos_content_version": "ntm_create_account:default_v1_meta_account_v3+docs_v2",
  "remember_device": true
}
```

`first_name` 是名、`last_name` 是姓，与直觉相反。生日早于约 18 年前会返回 403——同一会话同一份姓名，只把生日从 2013 改成 1993 就能从 403 变 200。

### 3. `restart` 的 body 不是空对象

```json
{"resume": false}
```

### 4. `accept-tos` 是会话激活开关，且必须先引导会话

注册完成后**必须先走 `POST auth.meta.com/hatch/session/bootstrap/`**（form 表单只带
`frl_access_token`，手动跟随 302/301/303 跳转落到 muse.ai 200 文档），否则后续
hatch 接口一律 403。引导落地的 muse.ai 文档里带有 Next Flight 序列化的动态披露
（`disclosureText` / `disclosureVersion`，当前实测版本为 `HATCH_ROW_v0`），`accept-tos`
的正文必须用服务端返回的这份动态文案（**不是**硬编码常量），`Referer` 用披露页
URL；单次调用即可，不重试，403 时把响应体前 200 字符带出来便于诊断。

引导后若服务端判定 `resolvedDestination == "waitlist"` 或 `isGeoGated == true`，
属于**地区受限**：注册已完成、账号保留，但未证明已加入名单。

### 5. `vm/wake` 缺失会让兑换误报

不调 `vm/wake` 直接兑换，即使是有效码也会返回：

```json
{"success":false,"reason":"invalid","detail":"この招待コードは存在しません"}
```

需要先从已登录页面提取 `vmId`：

```python
re.search(r'vmId[\\"]*:[\\"]*([0-9a-fA-F-]{36})', html)
```

`wake` 的 body 实测为：`wake_trigger: "mount"`（首连）、`open_id`（新 uuid）、
`away_ms: 0`，而不是早期的 `resume`。

### 6. 兑换失败原因的含义

| reason | 含义 |
|---|---|
| `redeemed` | 成功 |
| `already_redeemed` | 本账号已经兑换过一个码 |
| `used_up` | 该码达到 30 次使用上限 |
| `invalid` | 码不存在，或没调 `vm/wake` |

## Cookie

注册成功后 `hatch_sess` 由 `register-completion` 的响应头下发（约 886 字符），这是后续所有 hatch 接口的凭证。`hatch_vml` 在注册阶段一直是被清空状态（`Max-Age=0`），不属于必需项。

## 埋点说明

站点前端会上报两类遥测：

- **falco**（`POST /api/falco`）—— 性能指标与业务交互事件，`machineId` 固定为 `not_set`
- **caa-events**（`POST /api/auth/caa-events`）—— 注册漏斗，通过 `navigator.sendBeacon` 逐步骤上报，同一 `waterfall_id` 贯穿全程

两者都不参与注册判定，缺省不影响流程能跑通。`musepy/falco.py` 与 `musepy/caa.py` 保留了完整实现，可通过配置关闭。

## 使用声明

本项目仅供个人、非商业的学习与研究使用，不采用 MIT 等开源许可证。不得用于商业用途或再分发。实际使用时请遵守目标站点的服务条款与当地法律法规。

交流群 t.me/muihbb
