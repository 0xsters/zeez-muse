"""完整注册 + 兑换流程示例。运行前请填写 config.json（见 config.example.json）。"""

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from musepy import caa, falco
from musepy.client import MuseClient, MuseError, random_dob


def load_config(path="config.json"):
    if not os.path.exists(path):
        raise SystemExit(f"缺少配置文件 {path}，请从 config.example.json 复制并填写")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    cfg = load_config()

    email = cfg["email"]
    proxy = cfg.get("proxy") or None
    invite_code = cfg.get("invite_code") or ""
    otp_file = cfg.get("otp_file")  # 可选：从文件读取当前验证码

    c = MuseClient(proxy=proxy, impersonate=cfg.get("impersonate", "chrome131"),
                   lang=cfg.get("lang", "ja"))

    print(f"[1] 打开首页  {email}")
    c.open_home()

    print("[2] 重启注册流程")
    c.restart()

    # ---- 可选的漏斗埋点 ----
    funnel = caa.Caa(c.fp, c.lang)
    if cfg.get("send_caa", True):
        funnel.page_shown(c.s)
        time.sleep(2)
        funnel.focus(c.s)
        time.sleep(0.5)
        funnel.continue_clicked(c.s)
        funnel.registration_start(c.s)

    print("[3] 发送验证码")
    c.send_otp(email)

    code = otp_file and open(otp_file).read().strip() or input("输入收到的 6 位验证码: ").strip()

    if cfg.get("send_caa", True):
        funnel.otp_typed(c.s)

    print("[4] 校验验证码")
    c.confirm_otp(code)

    first = cfg.get("first_name") or "三郎"    # 名
    last = cfg.get("last_name") or "近藤"      # 姓
    dob = cfg.get("date_of_birth") or random_dob()

    if cfg.get("send_caa", True):
        funnel.otp_success(c.s)
        funnel.birthday_start(c.s)
        time.sleep(3)
        funnel.birthday_next(c.s)

    print(f"[5] 完成注册  {last} {first}  {dob}")
    data = c.register_completion(dob, first, last)
    uid = (data.get("auth_meta_session") or {}).get("frl_account_id")
    print(f"    account_id = {uid}")

    if cfg.get("send_caa", True):
        funnel.create_success(c.s, uid)
        funnel.login_success(c.s, uid)

    # ---- falco 埋点（可选）----
    if cfg.get("send_falco", True):
        falco.send(c.s, c.fp, c.lang, str(uuid.uuid4()), [
            falco.perf("https://muse.ai/"),
            falco.interaction("https://muse.ai/", "page_load", "main_app", "page_load"),
        ])

    print("[6] 激活会话（accept-tos）")
    for _ in range(2):
        c.accept_tos()
        if c.is_live():
            break
        time.sleep(1.2)
    print(f"    invite = {'200 活号' if c.is_live() else '401/403 无 hatch 权限'}")

    print("[7] 唤醒 VM")
    vm = c.wake_vm()
    print(f"    vm_id = {vm}")

    c.stateful_sharing()

    print(f"[8] 兑换邀请码  {invite_code or '(未配置)'}")
    if invite_code:
        ok, reason = c.redeem(invite_code)
        print(f"    redeem = {ok} / {reason}")
        if ok:
            own = c.scratch_code()
            print(f"[9] 自己的邀请码 = {own}")
        else:
            print(f"    失败原因映射: already_redeemed=本账号已用过码  "
                  f"used_up=该码已达使用上限  invalid=码不存在")
    else:
        print("    跳过兑换")

    sess = c.session_cookie()
    print(f"\nhatch_sess 长度 = {len(sess or '')}")
    c.close()


if __name__ == "__main__":
    try:
        main()
    except MuseError as e:
        print(f"协议失败: {e}")
        sys.exit(1)
