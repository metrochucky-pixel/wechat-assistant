#!/usr/bin/env python3
"""测试飞书告警通道(走「飞书机器人」应用私聊机主)+ 补推历史积压的「需人工」消息。

用法:
    python3 ~/wechatbot/tools/test_feishu.py           # 只发一条测试消息
    python3 ~/wechatbot/tools/test_feishu.py --backlog # 顺便把 decisions.jsonl 里
                                                       # 历史 flag_需人工 汇总推一条

前提:~/.config/wechatbot.env 里有(从 VPS /opt/飞书-feishu/飞书-feishu.env 拿):
    FEISHU_APP_ID=cli_...
    FEISHU_APP_SECRET=...
    FEISHU_TO_OPEN_ID=ou_...      # 可选,默认用 机主自己的 open_id
⚠️ 不打印 app_secret。
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wechat_autoreply as wa  # noqa: E402

if not (wa.FEISHU_APP_ID and wa.FEISHU_APP_SECRET):
    sys.exit("❌ 没读到 FEISHU_APP_ID / FEISHU_APP_SECRET。请写进 ~/.config/wechatbot.env"
             "(⚠️ 追加前确认文件末尾有换行,否则会粘到上一行的 key 上)")
print(f"✅ app_id={wa.FEISHU_APP_ID}  secret 长度={len(wa.FEISHU_APP_SECRET)}")
print(f"   收件人 open_id={wa.FEISHU_TO_OPEN_ID}")
if not wa._feishu_token():
    sys.exit("❌ 取 tenant_access_token 失败 —— app_id/secret 不对,或应用没启用")
print("✅ tenant_access_token 拿到了")

ok = wa.notify_feishu("✅ 微信分身:飞书告警通道测试",
                      ["这条是测试,收到说明通道打通了。",
                       "以后涉及钱/报价/约见面/承诺的消息会推到这里。",
                       f"时间:{datetime.now().strftime('%m-%d %H:%M')}"])
print("✅ 测试消息已发出,去飞书群里看" if ok else "❌ 发送失败(看上面的报错)")

if "--backlog" in sys.argv and ok:
    path = os.path.join(wa.HERE, "decisions.jsonl")
    items = []
    for line in open(path, encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("_action") == "flag_需人工":
            items.append((d.get("ts", "")[:16].replace("T", " "),
                          d.get("conversation", "?"),
                          (d.get("last_customer_msg") or "")[:60]))
    print(f"\n📦 历史积压 flag_需人工:{len(items)} 条")
    if items:
        recent = items[-15:]
        lines = [f"共 {len(items)} 条,以下是最近 {len(recent)} 条:"]
        lines += [f"· [{t}] {c} — {m}" for t, c, m in recent]
        print("已推送汇总" if wa.notify_feishu("📦 微信分身:历史积压的「需人工」消息", lines)
              else "汇总推送失败")
