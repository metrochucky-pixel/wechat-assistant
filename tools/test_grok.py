#!/usr/bin/env python3
"""探测 xAI Grok:key 通不通、有哪些模型、哪个能读图。

用法:  python3 ~/wechatbot/tools/test_grok.py
前提:  ~/.config/wechatbot.env 里有 XAI_API_KEY=...(chmod 600)

⚠️ 本脚本**不打印 key**,只打印遮蔽后的长度和前后各2位,方便确认"加对了没"。
探到能读图的模型后,把它写进 ~/.config/wechatbot.env 的 XAI_MODEL=xxx 即可,
主程序 _grok_vision_decide() 会自动用它。
"""
import base64
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wechat_autoreply import _load_env_file, XAI_BASE  # noqa: E402

_load_env_file()
KEY = os.environ.get("XAI_API_KEY")
if not KEY:
    sys.exit("❌ 没读到 XAI_API_KEY。请把 XAI_API_KEY=... 写进 ~/.config/wechatbot.env(等号两边别留空格)")
print(f"✅ 读到 key:{KEY[:2]}…{KEY[-2:]}(长度 {len(KEY)})")

from openai import OpenAI  # noqa: E402
c = OpenAI(api_key=KEY, base_url=XAI_BASE)

# ── 1. 列模型 ──
try:
    models = sorted(m.id for m in c.models.list().data)
    print(f"\n📋 账号可用模型({len(models)}):")
    for m in models:
        print("   ", m)
except Exception as e:
    sys.exit(f"❌ 列模型失败(key 无效 / 网络不通 / 没充值?):{str(e)[:200]}")

# ── 2. 纯文本连通 ──
def _try_text(model):
    try:
        r = c.with_options(timeout=60.0).chat.completions.create(
            model=model, max_tokens=30,
            messages=[{"role": "user", "content": "只回两个字:通了"}])
        return (r.choices[0].message.content or "").strip()[:20]
    except Exception as e:
        return f"❌ {str(e)[:90]}"

# ── 3. 读图能力(造一张带字的小图,看它能不能认出来)──
def _probe_img():
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (420, 160), (255, 255, 255))
    ImageDraw.Draw(im).rectangle([30, 40, 390, 120], fill=(7, 193, 96))
    buf = io.BytesIO(); im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

IMG = _probe_img()

def _try_vision(model):
    try:
        r = c.with_options(timeout=90.0).chat.completions.create(
            model=model, max_tokens=60,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{IMG}"}},
                {"type": "text", "text": "这张图里主要是什么颜色的方块?只答颜色。"}]}])
        return (r.choices[0].message.content or "").strip()[:40]
    except Exception as e:
        return f"❌ {str(e)[:90]}"

# 优先测像 grok-4 / vision 的,其余也过一遍
cands = [m for m in models if "image-gen" not in m and "embed" not in m]
cands.sort(key=lambda m: (0 if ("vision" in m or m.startswith("grok-4")) else 1, m))

print("\n🔌 连通性 + 读图能力:")
best = None
for m in cands[:8]:
    t = _try_text(m)
    v = _try_vision(m)
    ok = not v.startswith("❌")
    if ok and best is None:
        best = m
    print(f"  {'✅' if ok else '⚠️ '} {m:28s} 文本:{t:12s} 读图:{v}")

print()
if best:
    print(f"👉 建议用:{best}")
    print(f"   把这行加进 ~/.config/wechatbot.env:   XAI_MODEL={best}")
    print("   然后重载:launchctl unload/load ~/Library/LaunchAgents/com.chuck.wechatbot.plist")
else:
    print("⚠️ 没有模型能读图 —— Grok 就只能当文本兜底,视觉兜底继续用 Claude。")
