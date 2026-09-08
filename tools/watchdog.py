#!/usr/bin/env python3
"""微信分身看门狗 —— 独立进程,专抓「bot 自己死了/被 unload 了」这类内部告警发不出来的情况。

历史教训:决策日志里有过 96 小时、43 小时两段完全静默,机主全是靠问 Claude 才发现的。
bot 内部的掉线告警只能覆盖「bot 活着但手机连不上」;进程本身没了就哑火了,所以必须有外部看门狗。

判定:bot.log 超过 STALE_MIN 分钟没更新 → 认为停摆 → 飞书告警(只报一次,恢复时报恢复)。

⚠️ 刻意【不 import wechat_autoreply】:主程序万一有语法错/依赖坏,看门狗必须还能报警。
   所以这里自带一份最小的 env 加载和飞书发送(约30行重复代码,值得)。

由 launchd 每 30 分钟拉起一次:~/Library/LaunchAgents/com.chuck.wechatbot-watchdog.plist
手动测:python3 ~/wechatbot/tools/watchdog.py --check   (只打印判断,不发飞书)
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT_LOG = os.path.join(HERE, "bot.log")
HEALTH = os.path.join(HERE, "health.json")
STATE = os.path.join(HERE, "watchdog_state.json")
STALE_MIN = 25          # 心跳超过这么久没更新 = 停摆
DOWN_MIN = 15           # adb 断连要持续这么久才算"真掉了"
# ⚠️ 为什么要有 DOWN_MIN:WiFi 每天会抖几次(实测 874 轮里 4 次),每次 10~20 秒就自愈。
#    早期判据是"ok=false 就报停摆",看门狗恰好在抖动那一瞬间采样就误报了(2026-09-01 真实误报)。
#    误报比漏报更伤——报几次机主就不信这个告警了。bot 自己的掉线告警是 5 分钟,这里放到 15 分钟兜底。
PLIST_LABEL = "com.chuck.wechatbot"


def _load_env(path="~/.config/wechatbot.env"):
    p = os.path.expanduser(path)
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
TO = os.environ.get("FEISHU_TO_OPEN_ID", "")


def _post(url, body, token=""):
    h = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=h)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def feishu(title, lines):
    if not (APP_ID and APP_SECRET):
        print("(没配飞书凭据,跳过发送)")
        return False
    try:
        d = _post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                  {"app_id": APP_ID, "app_secret": APP_SECRET})
        tok = d.get("tenant_access_token")
        if not tok:
            print(f"取token失败:{d}")
            return False
        r = _post("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                  {"receive_id": TO, "msg_type": "text",
                   "content": json.dumps({"text": title + "\n" + "\n".join(lines)},
                                         ensure_ascii=False)}, tok)
        return r.get("code") == 0
    except Exception as e:
        print(f"飞书发送异常:{e}")
        return False


def _state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {"down": False}


def _save(s):
    json.dump(s, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)


def main():
    dry = "--check" in sys.argv
    now = time.time()

    # ① launchd 里还在不在(被 unload 了就永远不会自己起来)
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    managed = PLIST_LABEL in out

    # ② 心跳:bot 每跑完一轮【且 adb 是通的】才写 health.json
    # ⚠️ 绝不能再拿 bot.log 的 mtime 当判据 —— 2026-08-28→31 真实事故:adb 掉线后
    #    日志一直在刷"未连接"报错,mtime 一直新,看门狗一路报"正常",分身瞎了 3 天没人知道。
    #    **日志在动 ≠ 分身活着。**
    hb_min, hb_ok, hb_note, hb_down = 9999.0, False, "(没有 health.json)", 99999
    if os.path.exists(HEALTH):
        try:
            h = json.load(open(HEALTH, encoding="utf-8"))
            hb_min = (now - os.path.getmtime(HEALTH)) / 60
            hb_ok = bool(h.get("ok"))
            hb_note = h.get("note") or ""
            hb_down = int(h.get("down_secs") or 0)
        except Exception as e:
            hb_note = f"(health.json 读不了:{e})"

    log_min = (now - os.path.getmtime(BOT_LOG)) / 60 if os.path.exists(BOT_LOG) else 9999

    # 宽限:health.json 还没生成、但进程在跑且日志很新 = 刚启动,给它时间,别误报
    booting = (not os.path.exists(HEALTH)) and managed and log_min < 5
    if booting:
        print(f"[{datetime.now():%m-%d %H:%M}] 刚启动中(还没写心跳,日志 {log_min:.1f}min)→ 先不判定")
        return

    # 只有"断连已持续 ≥DOWN_MIN"才算停摆;短暂抖动不报
    really_disconnected = (not hb_ok) and hb_down >= DOWN_MIN * 60
    down = (not managed) or hb_min > STALE_MIN or really_disconnected
    why = []
    if not managed:
        why.append(f"launchd 里已经没有 {PLIST_LABEL}(被 unload 了,不会自动恢复)")
    if hb_min > STALE_MIN:
        why.append(f"心跳已 {hb_min:.0f} 分钟没更新(阈值 {STALE_MIN} 分钟)· {hb_note}")
    elif really_disconnected:
        why.append(f"进程还活着,但**手机连不上**已 {hb_down//60} 分钟:{hb_note}")

    print(f"[{datetime.now():%m-%d %H:%M}] managed={managed} 心跳={hb_min:.1f}min ok={hb_ok} "
          f"断连={hb_down//60}min 日志={log_min:.1f}min → {'停摆' if down else '正常'}")
    if (not hb_ok) and not really_disconnected:
        print(f"   · adb 抖了一下({hb_down}s),未达 {DOWN_MIN} 分钟阈值 → 不报")
    for w in why:
        print("   ·", w)
    if dry:
        return

    s = _state()
    # 停摆期间每 6 小时重播一次:只报一次的话,那条被漏看就再也没有第二次提醒(踩过)
    replay = False
    if down and s.get("down") and s.get("last_alert"):
        try:
            replay = (datetime.now() - datetime.fromisoformat(s["last_alert"])).total_seconds() >= 21600
        except Exception:
            replay = False
    if down and (not s.get("down") or replay):
        ok = feishu("🔴 微信分身:停摆了", why + [
            "分身现在完全不工作,群里没人替你说话。",
            "手机连不上时:检查手机开机/在WiFi上;若手机重启过会丢 adb tcpip,插一次USB线即可自愈。",
            "进程没了时:launchctl load ~/Library/LaunchAgents/com.chuck.wechatbot.plist",
            f"时间:{datetime.now():%m-%d %H:%M}"])
        s["down"] = True
        s.setdefault("since", datetime.now().isoformat(timespec="seconds"))
        s["last_alert"] = datetime.now().isoformat(timespec="seconds")
        _save(s)
        print("已推飞书" if ok else "推送失败")
    elif not down and s.get("down"):
        feishu("🟢 微信分身:已恢复", [f"从 {s.get('since','?')} 起的停摆已结束,分身继续工作。",
                                     f"时间:{datetime.now():%m-%d %H:%M}"])
        _save({"down": False})
        print("已推恢复通知")
    else:
        print("状态无变化,不打扰")


if __name__ == "__main__":
    main()
