#!/usr/bin/env python3
"""
wechat_autoreply.py — 微信自动回复(本机 Mac 跑,本地 adb 控 OnePlus 9R)

今天(2026-08-18)端到端验证过的正解已全部固化:
  · 微信屏蔽无障碍树 → 读消息走截图 + 视觉模型
  · 中文输入 → ADBKeyboard broadcast(phone_type 的 ADB-only 模式打不了中文)
  · 发送键定位 → 扫微信绿 (7,193,96) 求质心,别写死坐标
  · 截图原生 1080×2400,adb 抓的图坐标直接用,不缩放
  · 不需要 VPS,手机连这台 Mac 本地 adb 即可

三件套:
  ① 影子模式(DRY_RUN=True):只打印"会怎么回",不真发,零风险攒数据
  ② 多会话轮询:扫会话列表红点,逐个进去处理
  ③ 知识库外挂:话术从 kb.md 加载,改话术不用动代码

依赖:  pip install openai pillow
密钥:  把 key 写进 ~/.config/wechatbot.env(每行 KEY=值),脚本自动加载;
       key 不入代码/不入聊天/不入 shell 历史。例:
           MINIMAX_API_KEY=你的key
运行:  python wechat_autoreply.py

注:视觉模型必须能读图。默认用 MiniMax-M3(2026-06 起原生多模态,OpenAI 兼容)。
"""

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime

from PIL import Image
# anthropic 懒加载:adb 和颜色检测不需要它,没装也能跑那部分

sys.stdout.reconfigure(line_buffering=True)   # 日志实时刷到 bot.log(否则重定向会缓冲)

# ── 配置 ────────────────────────────────────────────────────────────
# WiFi ADB(根治 USB 隔夜硬掉线):USB 线只管充电,adb 走无线
# 手机侧需 WLAN 常连(设置→WLAN→高级→睡眠时保持WLAN=始终);IP 变了改这里
# ⚠️ 这里【不能】直接读环境变量:_load_env_file() 在下面很远才执行,
#    此刻 ~/.config/wechatbot.env 还没加载,写在配置文件里的 ADB_SERIAL 会被完全忽略
#    (2026-08-31 踩到:手机换 IP 后往配置里写了新地址,启动却还在连旧的 .3)。
#    真正的取值在 _load_env_file() 之后、main() 之前统一做,见 _resolve_adb_serial()。
ADB_SERIAL   = os.environ.get("ADB_SERIAL", "127.0.0.1:5555")   # 占位;实际值放 env,或靠 mDNS 自动发现
ADB_USB_FALLBACK = os.environ.get("ADB_USB_FALLBACK", "")   # 手机 USB serial(adb devices 里那串);无线连不上时回退


def _discover_via_mdns():
    """走「无线调试」的 mDNS 广播找手机(_adb-tls-connect._tcp),**不需要插 USB**。
    前提:手机 开发者选项→无线调试 已开启,且当初配对时勾了"始终允许通过此网络进行调试"
    (2026-08-31 已在机主手机上开好)。手机重启后 `adb tcpip 5555` 会丢,但这条还在,
    所以这是重启后**唯一能自动自愈**的路径——没有它,开那个开关等于白开。
    返回 '<ip>:<port>' 或 None。"""
    try:
        out = subprocess.run(["adb", "mdns", "services"], capture_output=True,
                             text=True, timeout=25).stdout
        for line in out.splitlines():
            # ⚠️ 两种广播都要认(2026-09-07 踩过):
            #   _adb-tls-connect._tcp = 「无线调试」开关广播的(端口动态)
            #   _adb._tcp             = `adb tcpip 5555` 模式自己广播的(端口 5555)
            # 第一版只认 TLS 那种;手机没重启、tcpip 还在,只是换了 IP(.60→.13),
            # 它广播的是 _adb._tcp —— 我的代码视而不见,自愈 173 分钟没成功,只能等人来。
            if "_adb-tls-connect._tcp" in line or "_adb._tcp" in line:
                m = re.search(r"(\d+\.\d+\.\d+\.\d+:\d+)", line)
                if not m:
                    continue
                addr = m.group(1)
                subprocess.run(["adb", "connect", addr], capture_output=True, timeout=20)
                time.sleep(1.5)
                ok = subprocess.run(["adb", "-s", addr, "get-state"], capture_output=True,
                                    text=True, timeout=15).stdout.strip() == "device"
                if ok:
                    print(f"🔄 通过无线调试(mDNS)找到手机:{addr}")
                    return addr
    except Exception as e:
        print(f"    [mDNS 发现失败] {str(e)[:60]}")
    return None


def autodiscover_wifi():
    """手机重启/换网后 IP 会变、tcpip 模式也会丢。
    ① 先试 mDNS(无线调试,不用插线)② 再退回借 USB 重开 tcpip。
    返回可用地址,失败返回 None。"""
    addr = _discover_via_mdns()
    if addr:
        return addr
    try:
        st = subprocess.run(["adb", "-s", ADB_USB_FALLBACK, "get-state"],
                            capture_output=True, text=True).stdout.strip()
        if st != "device":
            return None                      # USB 不在,没法自动发现
        out = subprocess.run(["adb", "-s", ADB_USB_FALLBACK, "shell",
                              "ip", "-f", "inet", "addr", "show", "wlan0"],
                             capture_output=True, text=True).stdout
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        if not m:
            return None
        addr = f"{m.group(1)}:5555"
        subprocess.run(["adb", "-s", ADB_USB_FALLBACK, "tcpip", "5555"], capture_output=True)
        time.sleep(3)
        subprocess.run(["adb", "connect", addr], capture_output=True)
        time.sleep(1.5)
        ok = subprocess.run(["adb", "-s", addr, "get-state"],
                            capture_output=True, text=True).stdout.strip() == "device"
        if ok:
            print(f"🔄 自动发现手机新地址:{addr}")
            return addr
    except Exception as e:
        print(f"    [自动发现失败] {str(e)[:60]}")
    return None
POLL_INTERVAL = 10         # 每轮轮询间隔(秒);每轮含一次 M3 读列表
PATROL_EVERY  = 6          # 每 N 轮做一次"巡逻":顶部几个会话不论有无红点都进去看一眼
DRY_RUN      = False       # ⚠️ False=实发!会真的往微信发消息。改回 True 则只打印不发

# ── LLM 供应商(OpenAI 兼容)────────────────────────────────────────
PROVIDER = "minimax"       # 读图 + 回复都用 MiniMax-M3(原生多模态)
PROVIDERS = {
    "minimax": {           # MiniMax-M3:多模态,OpenAI 兼容 /chat/completions
        "base_url": "https://api.minimaxi.com/v1",   # 国内;国际用 https://api.minimax.io/v1
        "model":    "MiniMax-M3",
        "key_env":  "MINIMAX_API_KEY",
    },
    "kimi-code": {         # Kimi Code(纯文本好,视觉存疑)
        "base_url": "https://api.kimi.com/coding/v1",
        "model":    "k3",
        "key_env":  "KIMI_CODE_API_KEY",
    },
    "kimi": {              # Moonshot 视觉平台(另一套 key)
        "base_url": "https://api.moonshot.cn/v1",
        "model":    "kimi-latest",
        "key_env":  "MOONSHOT_API_KEY",
    },
}
_P    = PROVIDERS[PROVIDER]
MODEL = _P["model"]

HERE         = os.path.dirname(os.path.abspath(__file__))
PERSONA_PATH = os.path.join(HERE, "persona.md")        # 人设/语气
ABOUT_PATH   = os.path.join(HERE, "about_me.md")       # 我是谁(身份/生意/立场)
PEOPLE_PATH  = os.path.join(HERE, "people.md")         # 我认识的人
VOICE_PATH   = os.path.join(HERE, "voice.md")          # 机主本人真实语料(最准的口吻标尺)
LOG_PATH     = os.path.join(HERE, "decisions.jsonl")   # 决策日志(影子模式看这个)
SEEN_PATH    = os.path.join(HERE, "seen.json")         # 已处理消息指纹表

# 输入法包名(今天实测)
IME_ADB   = "com.android.adbkeyboard/.AdbIME"
IME_SOGOU = "com.sohu.inputmethod.sogouoem/.SogouIME"

# 内置人设:不要死板知识库,用大脑生成有人情味的回复
DEFAULT_PERSONA = """你在替机主本人回复微信消息。像真人朋友那样聊天,有温度、有分寸。

原则:
- 自然、口语、有人情味,别像客服模板,别官腔,别一堆敬语
- 顺着对方的语气和上下文接话;对方轻松你就轻松,对方认真你就认真
- 简短,一般一两句,像平时发微信那样,可带个合适的 emoji 但别泛滥
- 用中文,符合机主日常说话习惯(看聊天记录里我方的历史发言口吻)
- 拿不准对方意图、涉及钱/见面/重要决定的,别自作主张,needs_human=true 交给本人
- 绝不编造事实(具体时间、金额、承诺);不确定就用"我看下再回你"这类稳妥说法
"""

def _load_env_file(path="~/.config/wechatbot.env"):
    """从本地私密文件加载 API key 到环境变量(key 不入代码、不入聊天、不入 shell 历史)。
    文件格式每行 KEY=值,例如:
        MINIMAX_API_KEY=你的minimax_key
        KIMI_CODE_API_KEY=你的kimi_key
    建议 chmod 600。"""
    p = os.path.expanduser(path)
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env_file()

def _resolve_adb_serial():
    """配置文件加载完之后再定 ADB_SERIAL(env > 配置文件 > 占位默认)。"""
    v = os.environ.get("ADB_SERIAL")
    if v:
        globals()["ADB_SERIAL"] = v.strip()

_resolve_adb_serial()

def _direct_http_client():
    """不走系统代理的 httpx 客户端。
    ⚠️ 机主机器上开着 macOS 系统代理(MacPacket,127.0.0.1:1082),Python 会自动继承
    (`urllib.request.getproxies()` 在 macOS 上读的是系统设置,不只是环境变量)。
    **MiniMax / Kimi 都是国内服务,本来就该直连** —— 绕一圈代理只是多一层不稳定,
    2026-09-01 那次卡死就是 LLM 请求走代理挂住(栈停在 _ssl → poll)。
    Claude / Grok 从国内需要代理,那两个保持走代理不动。"""
    import httpx
    return httpx.Client(trust_env=False, timeout=90.0)


_client = None
def get_client():
    """OpenAI 兼容客户端,指向所选供应商(默认 MiniMax-M3)。"""
    global _client
    if _client is None:
        from openai import OpenAI
        key = os.environ.get(_P["key_env"])
        if not key:
            raise RuntimeError(f"未设置 {_P['key_env']},请先 export {_P['key_env']}=sk-...")
        # ⚠️ max_retries 默认 2 → 一次挂起被放大成 3×timeout;外层自己有重试,这里设 1 就够
        _client = OpenAI(api_key=key, base_url=_P["base_url"], max_retries=1,
                         http_client=_direct_http_client())   # MiniMax 国内,直连
    return _client


# ── 深度问答:Kimi k3 + 联网搜索(答正经业务/知识问题)──────────────
_kimi_client = None
def get_kimi_client():
    global _kimi_client
    if _kimi_client is None:
        from openai import OpenAI
        p = PROVIDERS["kimi-code"]
        key = os.environ.get(p["key_env"])
        if not key:
            raise RuntimeError("未设置 KIMI_CODE_API_KEY(深度问答需要 Kimi Code key)")
        _kimi_client = OpenAI(api_key=key, base_url=p["base_url"], max_retries=1,
                              http_client=_direct_http_client())   # Kimi 国内,直连
    return _kimi_client


CLAUDE_CREDS = os.path.expanduser(
    "~/Library/Application Support/droidrun/credentials/auth-profiles.json")

def _claude_token():
    """复用 mobilerun 存的 Claude(Anthropic)OAuth token。没有返回 None。"""
    try:
        return json.load(open(CLAUDE_CREDS, encoding="utf-8"))["claudeAiOauth"]["accessToken"]
    except Exception:
        return None


# ── 飞书告警:走「飞书机器人」自建应用私聊机主(和 飞书通知同一条路)────────
# 不用自定义机器人 webhook——机主要求复用飞书机器人,他手机上本来就在收飞书机器人的 (某) 卡片。
# ⚠️ open_id 是 (用户,应用) 二元映射:这个 ou_ 只在飞书机器人这个 app 下有效,换 app 必须重新拉。
FEISHU_APP_ID     = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_TO_OPEN_ID = os.environ.get("FEISHU_TO_OPEN_ID", "")   # 机主在你飞书应用下的 open_id
_fs_tok = {"v": "", "exp": 0.0}

def _feishu_post(url: str, body: dict, token: str = "") -> dict:
    import urllib.request
    h = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=h)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))

def _feishu_token() -> str:
    """tenant_access_token,带缓存(有效期约2小时,提前60s续)。"""
    if _fs_tok["v"] and time.time() < _fs_tok["exp"] - 60:
        return _fs_tok["v"]
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET):
        return ""
    d = _feishu_post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                     {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET})
    if d.get("code") != 0:
        print(f"    [飞书取token失败] {str(d)[:120]}")
        return ""
    _fs_tok["v"] = d["tenant_access_token"]
    _fs_tok["exp"] = time.time() + d.get("expire", 7200)
    return _fs_tok["v"]

def notify_feishu(title: str, lines) -> bool:
    """让飞书机器人私聊机主一条消息。没配 app 凭据则空操作(返回 False)。
    用途:涉及钱/报价/约见面/承诺的消息被 needs_human 挂起时,机主本人得立刻知道——
    以前这些只写进 decisions.jsonl,等于静默漏掉(攒了39条都没人看过)。"""
    tok = _feishu_token()
    if not tok:
        return False
    text = title + "\n" + "\n".join(lines)
    try:
        d = _feishu_post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
            {"receive_id": FEISHU_TO_OPEN_ID, "msg_type": "text",
             "content": json.dumps({"text": text}, ensure_ascii=False)}, tok)
        if d.get("code") == 0:
            return True
        print(f"    [飞书发送被拒] {str(d)[:150]}")
    except Exception as e:
        print(f"    [飞书发送失败] {str(e)[:100]}")
    return False


def _kimi_deep(question: str, context: str) -> str:
    """Kimi k3 深答(免费兜底)。若必须最新/实时信息才能答准,末尾单起一行输出 [NEED_WEB]。"""
    sys_prompt = (load_persona() +
        "\n\n【正经业务/知识问答】认真、有条理地答,可以比平时长、可分点,用中文、机主本人口吻。不确定/查不到的别编。"
        "\n特别注意:若这个问题**必须依赖最新/实时信息**(如当前价格、近期动态、具体联系方式)才能答准,而你只有训练知识——"
        "照常尽力给方向,但在**最后单独一行**写出 [NEED_WEB];否则不要写。")
    try:
        resp = get_kimi_client().chat.completions.create(
            model=PROVIDERS["kimi-code"]["model"], max_tokens=2500, temperature=1,
            messages=[{"role": "system", "content": sys_prompt},
                      {"role": "user", "content": (f"背景:{context}\n" if context else "") + f"问题:{question}"}])
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"    [k3深答失败] {str(e)[:70]}")
        return ""


def _claude_deep(question: str, context: str) -> str:
    """升级:Claude + 联网搜索,答需要实时信息的问题。失败返回空。
    优先用正经 ANTHROPIC_API_KEY(独立额度、稳);没有则退回复用 mobilerun 的订阅 OAuth(会撞限流)。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    tok = None if api_key else _claude_token()
    if not api_key and not tok:
        print("    [Claude深答跳过:无 ANTHROPIC_API_KEY 也无 OAuth token]")
        return ""
    try:
        from anthropic import Anthropic
        if api_key:
            c = Anthropic(api_key=api_key)                       # 正经API key:独立额度,推荐
        else:
            c = Anthropic(auth_token=tok,
                          default_headers={"anthropic-beta": "oauth-2025-04-20"})   # 订阅OAuth兜底
        r = c.messages.create(
            model="claude-sonnet-5", max_tokens=2000,
            system=load_persona() + "\n\n认真有条理地答这个业务/知识问题,能联网就查证并给要点,用中文、机主本人口吻。",
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            messages=[{"role": "user", "content": (f"背景:{context}\n" if context else "") + question}])
        return "\n".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
    except Exception as e:
        print(f"    [Claude深答/联网失败] {str(e)[:80]}")
        return ""


def deep_answer(question: str, context: str = "") -> str:
    """两级:先 Kimi k3(免费兜底);k3 判定需联网([NEED_WEB])才升级 Claude(联网、贵)。"""
    ans = _kimi_deep(question, context)
    if "[NEED_WEB]" in ans:
        print("    ⬆️ k3 判定需实时信息 → 升级 Claude 联网")
        claude = _claude_deep(question, context)
        if claude:
            return claude
        # Claude 不可用 → 用 k3 的答案(已尽力给了方向),去掉标记
    return ans.replace("[NEED_WEB]", "").strip()


# ── 文生图(MiniMax image-01)→ 推到手机相册 ────────────────────────
def gen_image(prompt: str) -> str:
    """调 MiniMax image-01 生成图片,下载到本地临时文件,返回本地路径;失败返回 ''。"""
    import urllib.request
    key = os.environ.get("MINIMAX_API_KEY")
    if not key:
        print("    [文生图] 无 MINIMAX_API_KEY"); return ""
    try:
        req = urllib.request.Request(
            "https://api.minimaxi.com/v1/image_generation",
            data=json.dumps({"model": "image-01", "prompt": prompt[:1400],
                             "aspect_ratio": "1:1", "n": 1,
                             "response_format": "url"}).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req, timeout=120))
        if (r.get("base_resp") or {}).get("status_code") != 0:
            print(f"    [文生图] 失败:{(r.get('base_resp') or {}).get('status_msg')}"); return ""
        urls = (r.get("data") or {}).get("image_urls") or []
        if not urls:
            print("    [文生图] 没返回图片"); return ""
        local = os.path.join(HERE, "gen_%d.jpg" % int(time.time()))
        urllib.request.urlretrieve(urls[0], local)
        print(f"    [文生图] 生成成功 → {os.path.basename(local)}")
        return local
    except Exception as e:
        print(f"    [文生图] 异常:{str(e)[:80]}"); return ""


def push_to_gallery(local_path: str) -> bool:
    """把图片推到手机相册(DCIM)并通知媒体库刷新,让微信相册能看到它(排在最新)。"""
    if not local_path or not os.path.exists(local_path):
        return False
    remote = "/sdcard/DCIM/Camera/" + os.path.basename(local_path)
    adb("shell", "mkdir", "-p", "/sdcard/DCIM/Camera")
    out = subprocess.run(["adb", "-s", ADB_SERIAL, "push", local_path, remote],
                         capture_output=True, text=True)
    if "error" in (out.stderr or "").lower():
        print(f"    [推图] push失败:{out.stderr[:60]}"); return False
    # 通知媒体库扫描,否则相册里看不到
    adb("shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
        "-d", f"file://{remote}")
    time.sleep(1.5)
    print(f"    [推图] 已推到相册:{remote}")
    return True


def send_image_from_gallery() -> bool:
    """在当前已打开的对话里,把相册最新一张图发出去(配合 push_to_gallery 使用)。
    流程实测(2026-08-28):点+ → 相册 → 勾选左上第一张 → 发送键变绿 → 点发送。"""
    img = screencap(); W, H = img.size
    # 先清空输入框:上一步"发文字"若失败会残留文字,占着输入框会让 + 面板出不来
    set_ime(IME_ADB); time.sleep(0.3)
    tap(int(W * 0.45), int(H * 0.925)); time.sleep(0.4)
    adb("shell", "am", "broadcast", "-a", "ADB_CLEAR_TEXT"); time.sleep(0.4)
    back(); time.sleep(0.6)                 # 收起键盘,否则 + 面板位置会偏
    img = screencap(); W, H = img.size
    tap(int(W * 0.94), int(H * 0.913))      # "+" 号(右下)
    time.sleep(2.5)
    tap(int(W * 0.152), int(H * 0.76))      # "相册"(面板左上第一个)
    ok_gallery = False
    for _ in range(10):                      # 相册加载慢,最多等 ~20s(图多时首屏渲染很慢)
        time.sleep(2)
        if _is_gallery(screencap()):
            ok_gallery = True; break
    if not ok_gallery:
        print("    [发图] 没进相册,放弃"); back(); time.sleep(0.5); return False
    tap(int(W * 0.204), int(H * 0.117))     # 勾选第一张(最新=我们刚推的)
    time.sleep(1.5)
    btn = find_send_button(screencap())     # 选中后发送键变微信绿
    if not btn:
        print("    [发图] 发送键未激活,放弃"); back(); time.sleep(0.5); back(); return False
    tap(*btn)
    time.sleep(3)
    print("    [发图] ✅ 已发送")
    return True


def _is_gallery(img: Image.Image) -> bool:
    """粗判是否在微信相册选择页:整屏偏暗(缩略图网格)且底部有发送栏。"""
    px = img.load(); W, H = img.size
    vals = [sum(px[x, y]) / 3 for y in range(300, H - 400, 60) for x in range(60, W - 60, 60)]
    return (sum(vals) / len(vals)) < 150


def gen_and_send_image(prompt: str) -> bool:
    """一条龙:文生图 → 推相册 → 在当前对话发出。"""
    p = gen_image(prompt)
    if not p:
        return False
    if not push_to_gallery(p):
        return False
    ok = send_image_from_gallery()
    try:
        os.remove(p)          # 本地临时文件清掉
    except Exception:
        pass
    return ok


# ── adb 底层 ────────────────────────────────────────────────────────
def adb(*args, binary=False):
    cmd = ["adb", "-s", ADB_SERIAL, *args]
    out = subprocess.run(cmd, capture_output=True, check=False)
    return out.stdout if binary else out.stdout.decode("utf-8", "ignore")


def screencap(tries: int = 3) -> Image.Image:
    """原生 1080×2400 截图,返回 PIL Image。失败自动重试。
    ⚠️ WiFi 链路差时 PNG 会传一半就断,抛 `image file is truncated` /
    `cannot identify image file`。以前不重试,一次损坏就把**整个会话跳过**——
    2026-09-01 粉丝群聊了半天没人回就是这么来的(它其实点进去了,是截图废了)。"""
    last = None
    for i in range(tries):
        try:
            png = adb("exec-out", "screencap", "-p", binary=True)
            im = Image.open(io.BytesIO(png))
            im.load()                     # 强制解码:截断的 PNG 在这一步才会炸,不要等到后面
            return im.convert("RGB")
        except Exception as e:
            last = e
            if i < tries - 1:
                print(f"    [截图损坏,重试 {i+1}/{tries-1}] {str(e)[:50]}")
                time.sleep(1.2)
    raise RuntimeError(f"截图连续 {tries} 次失败:{last}")


def tap(x, y):
    adb("shell", "input", "tap", str(int(x)), str(int(y)))


def back():
    adb("shell", "input", "keyevent", "4")


def open_wechat_list():
    """回到微信消息列表首页。am start 会恢复上次的对话,所以先按返回退出任何对话,再拉起。"""
    for _ in range(2):
        back()
        time.sleep(0.4)
    adb("shell", "am", "start", "-n", "com.tencent.mm/.ui.LauncherUI")
    time.sleep(1.5)


# ── 中文输入(ADBKeyboard) ──────────────────────────────────────────
def set_ime(ime):
    adb("shell", "ime", "set", ime)
    time.sleep(0.5)


def type_chinese(text: str):
    """通过 ADBKeyboard broadcast 输入中文。调用前须已 set_ime(IME_ADB)。"""
    # 转义单引号
    safe = text.replace("'", "'\\''")
    adb("shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT", "--es", "msg", f"'{safe}'")
    time.sleep(0.6)


# ── 颜色检测:定位微信绿"发送"键 ────────────────────────────────────
def find_send_button(img: Image.Image):
    """扫微信绿 (7,193,96) 求质心。找不到返回 None。"""
    px = img.load()
    W, H = img.size
    xs, ys = [], []
    # ⚠️ 只扫【输入栏】那一条(实测发送键在 0.873H~0.906H)。
    #    以前从 0.78H 开始,会把聊天区最后一条【我方绿气泡】也扫进来 →
    #    质心被拉偏 → 点空。0.865H 以下只剩发送键,干净。
    for y in range(int(H * 0.865), H, 3):
        for x in range(int(W * 0.55), W, 3):      # 只扫右半
            r, g, b = px[x, y]
            if 0 <= r < 70 and 150 < g < 220 and 60 < b < 140:
                xs.append(x); ys.append(y)
    if len(xs) < 80:            # 像素太少 = 没找到按钮
        return None
    return (sum(xs) // len(xs), sum(ys) // len(ys))


# ── 廉价预检:不调 LLM 就判断"最后一条是不是我自己发的" ────────────
def last_bubble_is_mine(img: Image.Image):
    """靠【头像列】判断最后一条消息是谁发的:微信每条消息都带头像,对方在最左、我方在最右
    (比看气泡颜色可靠——图片/视频/表情/语音都没有绿气泡,但都有头像)。
    返回 True(我方最后发言,可直接跳过)/ False(对方在后)/ None(拿不准 → 交给 LLM)。
    纯像素、毫秒级:用来挡掉 60%+ 的"进去才发现没新消息"的空转 LLM 调用。"""
    if img is None:
        return None
    px = img.load(); W, H = img.size
    top, bot = int(H * 0.10), int(H * 0.86)      # 跳过顶部标题栏 + 底部输入栏
    DIFF = 12      # 与背景的色差阈值:对方白气泡(#FFF)在浅色背景(#EDEDED)上只差 18,阈值必须够低
    # 背景色 = 聊天区里出现最多的颜色(自动适配浅色/深色模式,不写死 #EDEDED)
    samp = Counter(px[x, y] for y in range(top, bot, 24)
                            for x in range(int(W * 0.13), int(W * 0.87), 24))
    bg = samp.most_common(1)[0][0]

    def has_avatar(y, x0, x1):
        """该行在头像列上是否被实心内容占满(头像是不透明方块)。"""
        hit = tot = 0
        for x in range(x0, x1, 4):
            p = px[x, y]; tot += 1
            if max(abs(p[i] - bg[i]) for i in range(3)) > DIFF:
                hit += 1
        return tot and hit / tot > 0.55

    LX = (int(W * 0.028), int(W * 0.10))     # 对方头像列(最左)
    RX = (int(W * 0.90), int(W * 0.972))     # 我方头像列(最右)
    ly = ry = None
    for y in range(bot, top, -4):            # 自下而上:第一次命中即最靠下的那条
        if ly is None and has_avatar(y, *LX): ly = y
        if ry is None and has_avatar(y, *RX): ry = y
        if ly is not None and ry is not None: break
    if ry is None:
        return False if ly is not None else None      # 只看到对方头像 → 对方最后

    def other_content_below(y0):
        """我方头像下方还有没有"对方专属区域"的内容:x∈[0.03W,0.19W] 只可能是对方头像/对方气泡左缘
        (微信气泡最宽约屏宽 0.75,我方右对齐的气泡左缘到不了这么左;时间戳/系统提示是居中的)。
        用来兜住"对方头像因为颜色太浅没被识别出来"的漏判——那会让真消息被静默丢掉。"""
        for y in range(min(bot, y0 + 40), bot, 6):
            hit = tot = 0
            for x in range(int(W * 0.03), int(W * 0.19), 6):
                p = px[x, y]; tot += 1
                if max(abs(p[i] - bg[i]) for i in range(3)) > DIFF:
                    hit += 1
            if tot and hit / tot > 0.30:
                return True
        return False

    if ly is None or ry > ly + 30:                    # 我方头像明显更靠下 → 疑似我方最后发言
        if other_content_below(ry):
            return None                                # 下面还有对方的东西 → 拿不准,交给 LLM
        return True
    return False                                       # 对方在后(或贴太近,保守当成要看)


# ── 语音消息:用微信自带的「转文字」读懂它 ──────────────────────────
# 分身只看截图,听不见语音。但微信在语音条旁边直接给了个「转文字」按钮(不用长按菜单,
# 因此**没有误点删除/撤回的风险**),点一下转写就永久留在气泡下面,之后正常读图就能看到。
def find_voice_convert_button(img: Image.Image):
    """找未播放语音条旁边的「转文字」按钮中心。找不到返回 None。
    锚点用**未播放红点**(250,81,81)——聊天区里几乎只有它是这个颜色,而且它的出现条件
    正好等于"这条语音还没处理过"。⚠️ 只能在对话内调用(会话列表的未读角标也是红的)。"""
    px = img.load(); W, H = img.size
    top, bot = int(H * 0.10), int(H * 0.86)
    XL, XR = int(W * 0.15), int(W * 0.60)

    def is_red(p):
        return abs(p[0] - 250) < 25 and abs(p[1] - 81) < 50 and abs(p[2] - 81) < 50

    # 聊天背景色取众数(自动适配深浅色模式)
    bgc = Counter(px[x, y] for y in range(top, bot, 24)
                           for x in range(int(W * 0.13), int(W * 0.87), 24)).most_common(1)[0][0]

    def near_bg(p):
        return max(abs(p[i] - bgc[i]) for i in range(3)) <= 12

    y = bot
    while y > top:
        if sum(1 for x in range(XL, XR, 3) if is_red(px[x, y])) < 4:
            y -= 3; continue
        # 量出这一块红色的 bbox
        ys = [yy for yy in range(max(top, y - 40), min(bot, y + 40))
              if any(is_red(px[x, yy]) for x in range(XL, XR, 3))]
        xs = [x for yy in ys for x in range(XL, XR) if is_red(px[x, yy])]
        if not xs or not ys:
            y -= 3; continue
        y0, y1, x0, x1 = min(ys), max(ys), min(xs), max(xs)
        cy, cx = (y0 + y1) // 2, (x0 + x1) // 2
        w, h = x1 - x0, y1 - y0
        # ⚠️ 必须验证"这是个浮在聊天背景上的小圆点",否则**别人发的截图/卡片里的红色图标**
        #    会被误当成语音红点,点下去就打开了图片查看器(踩过)。
        good = (10 <= w <= 34 and 10 <= h <= 34 and abs(w - h) <= 10
                and near_bg(px[cx, max(top, y0 - 10)])      # 上方是背景
                and near_bg(px[cx, min(bot, y1 + 10)])      # 下方是背景
                and near_bg(px[max(0, x0 - 10), cy]))       # 左侧是背景(红点浮在气泡右边的空隙里)
        if good:
            # 从红点右侧起,沿**中心行**找"非背景"的连续段 = 「转文字」按钮。
            # ⚠️ 别用红点的底边行:那一行正好切过「转文字」三个字,白底会被笔画切碎(踩过)
            sx = None; gap = 0
            for x in range(x1 + 8, int(W * 0.95)):
                if not near_bg(px[x, cy]):
                    sx = x if sx is None else sx
                    gap = 0
                elif sx is not None:
                    gap += 1
                    if gap > 15:
                        if (x - gap - sx) > 50:
                            return ((sx + x - gap) // 2, cy)
                        break                                # 这个红点右边没有按钮
                    continue
        # ⚠️ 这个红点没配按钮(微信不是每条语音都给「转文字」)→ **继续往上找下一条**,
        #    别直接放弃:最下面那条没按钮、上面那条有,是常见情形(踩过,整轮语音都漏转)
        y = y0 - 6
    return None


def convert_voices(img: Image.Image, max_n: int = 3):
    """把当前画面里未播放的语音条挨个转成文字。返回 (是否转过, 最新截图)。"""
    changed = False
    for _ in range(max_n):
        btn = find_voice_convert_button(img)
        if not btn:
            break
        tap(*btn)
        time.sleep(2.5)                            # 转写要一两秒
        img = screencap()
        if _is_image_viewer(img):                  # 万一点歪进了图片全屏 → 退出去,别继续乱点
            print("    ⚠️ 语音转文字点歪了(进了图片查看器),已退出")
            back(); time.sleep(0.8)
            return changed, screencap()
        changed = True
        print(f"    🎤 语音转文字 已点 {btn}")
    return changed, img


# ── 颜色检测:会话列表未读红点 ──────────────────────────────────────
def list_row_centers(img: Image.Image):
    """会话列表里每一行的垂直中心(靠最左的头像方块定位)。
    实测行距是**严格的 194px 网格**(1080×2400),所以检测到几行就能把整张网格补全。"""
    px = img.load(); W, H = img.size
    bg = Counter(px[x, y] for y in range(int(H*0.15), int(H*0.9), 12)
                          for x in range(int(W*0.78), int(W*0.95), 8)).most_common(1)[0][0]
    X0, X1 = int(W * 0.037), int(W * 0.13)
    top, bot = int(H * 0.12), H - int(H * 0.07)
    need = ((X1 - X0) // 6) * 0.6
    hits, s0 = [], None
    for y in range(top, bot):
        on = sum(1 for x in range(X0, X1, 6)
                 if max(abs(px[x, y][i] - bg[i]) for i in range(3)) > 18) >= need
        if on and s0 is None:
            s0 = y
        elif not on and s0 is not None:
            if y - s0 > 60:
                hits.append((s0 + y) // 2)
            s0 = None
    if s0 is not None and bot - s0 > 60:
        hits.append((s0 + bot) // 2)
    if len(hits) < 2:
        return hits
    # 用众数行距把网格补全:头像是纯色块(如公众号的蓝方块)时偶尔检测不到,会漏行
    gaps = [b - a for a, b in zip(hits, hits[1:])]
    pitch = min(gaps)                       # 最小间距 = 一行的高度
    if pitch < 100:
        return hits
    grid, y = [], hits[0]
    while y < bot:
        grid.append(y); y += pitch
    return grid


def row_fingerprint(img: Image.Image, y: int):
    """取某一行【头像区域】的粗指纹,用来判断"这一行还是不是刚才那一行"。
    ⚠️ 这是"点错行"的真正解药:
      流程是 截图 → 送 M3 读列表(**20~40 秒**)→ 按当时坐标点下去。
      这几十秒里只要来一条新消息,那个会话就顶到最上面、**后面所有行整体下移一格**,
      于是点到隔壁。实测 127 次点错,全是系统性地偏一行(常误伤排在上面的 公众号/zzs泽。)。
    做法:扫描时记下目标行的头像指纹,点之前重新截图比对,不一致就说明列表变了。"""
    px = img.load(); W, H = img.size
    x0, x1 = int(W * 0.037), int(W * 0.13)
    y0, y1 = max(0, y - 45), min(H, y + 45)
    vals = []
    for yy in range(y0, y1, 9):
        for xx in range(x0, x1, 9):
            p = px[xx, yy]
            vals.append((p[0] // 32, p[1] // 32, p[2] // 32))   # 量化,容忍轻微渲染差异
    return tuple(vals)


def row_unchanged(img_now: Image.Image, y: int, fp_then, tol: float = 0.85) -> bool:
    """目标行是否还是扫描时那一行(相同位置的头像指纹足够像)。"""
    fp_now = row_fingerprint(img_now, y)
    if not fp_then or len(fp_now) != len(fp_then):
        return False
    same = sum(1 for a, b in zip(fp_now, fp_then) if a == b)
    return same / len(fp_now) >= tol


def snap_row_y(img: Image.Image, y: int):
    """把 M3 给的行 y 吸附到真实行格上。返回 (吸附后的y, 偏移量)。
    ⚠️ 这是"点错行"的正解:M3 的 y 常偏几十到上百像素,微信列表又会按最新消息重排,
    偏一点就点到隔壁行——实测出现过"要进某乙群、结果进了公众号"(2026-08-31)。
    名字过滤挡不住这种,因为点歪了压根不看名字。"""
    rows = list_row_centers(img)
    if not rows:
        return y, None
    best = min(rows, key=lambda r: abs(r - y))
    return best, abs(best - y)


def list_has_unread_signal(img: Image.Image) -> bool:
    """【廉价门】会话列表上到底有没有"值得看一眼"的红色信号(未读角标 / 免打扰小红点 /
    『[有人@我]』红字)。毫秒级纯像素,用来**免掉大部分的 M3 读列表调用**——
    主循环每 10 秒一轮,以前每轮都无条件烧一次 M3 视觉调用,而一天里大部分时间群里是安静的。

    设计取向:**宁可误报,不可漏报**。误报只是多花一次 LLM(照旧正确);漏报会让消息没人回。
    所以扫描带开得宽(覆盖角标区和预览文字区),只用"红像素总量"卡个下限滤掉头像里的零星红色。
    """
    px = img.load(); W, H = img.size
    n = 0
    for y in range(int(H * 0.10), int(H * 0.85), 2):    # 跳开顶部标题栏和底部 tab 栏(都有红点)
        for x in range(100, min(W, 800), 2):
            r, g, b = px[x, y][:3]
            if r > 200 and g < 100 and b < 100:
                n += 1
                if n >= 40:                              # 角标约500+像素,头像杂红一般 <20
                    return True
    return False


def detect_unread_rows(img: Image.Image):
    """扫头像右上角红色未读角标,返回每个未读会话行的 y 中心(去重后)。
    微信角标红 ≈ (250,60,60)。角标在头像右上,头像在最左,故只扫 x[120,220]。"""
    px = img.load()
    W, H = img.size
    hits = []
    for y in range(int(H * 0.10), int(H * 0.85), 2):   # 跳过顶部标题栏和底部 tab 栏(红点误判源)
        for x in range(120, 230, 2):
            r, g, b = px[x, y]
            if r > 200 and g < 100 and b < 100:
                hits.append(y)
    if not hits:
        return []
    # 按 y 聚类(同一行的红点 y 相近),行高约 230px
    hits.sort()
    rows, cur = [], [hits[0]]
    for y in hits[1:]:
        if y - cur[-1] <= 120:
            cur.append(y)
        else:
            rows.append(sum(cur) // len(cur)); cur = [y]
    rows.append(sum(cur) // len(cur))
    return rows


# ── 视觉理解 + 决策 ─────────────────────────────────────────────────
def load_persona() -> str:
    """分身人设 = 内置人格 + persona.md(语气) + about_me.md(我是谁) + people.md(认识的人)。
    这几个文件每次决策都实时读,改了立即生效(无需重启)。"""
    parts = [DEFAULT_PERSONA]
    for path, title in [(PERSONA_PATH, "人设与说话风格"),
                        (ABOUT_PATH, "我是谁(身份/生意/立场/分寸)"),
                        (PEOPLE_PATH, "我认识的人")]:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                parts.append(f"\n\n## {title}\n" + f.read())
    # voice.md 放最后、加最强指令:真实语料是口吻的最高标尺,冲突时以它为准
    if os.path.exists(VOICE_PATH):
        with open(VOICE_PATH, encoding="utf-8") as f:
            parts.append("\n\n## ⭐ 机主本人真实语料(口吻的最高标尺,和上面任何描述冲突时【以这里为准】)\n"
                         + f.read())
    return "".join(parts)


def img_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


def read_and_decide(imgs, memory_text: str = "", pointer_text: str = "",
                    gag_text: str = "", todo_text: str = "",
                    may_offer_image: bool = False) -> dict:
    """把对话截图(可多张:更早历史→最新,最后一张是当前画面)+ 历史记忆交给 M3 决策。"""
    if not isinstance(imgs, list):
        imgs = [imgs]
    mem = (f"\n【你和TA/这个群的历史记忆(可能是更早、已滚出屏幕的事)】\n{memory_text}\n"
           if memory_text.strip() else "")
    ctx = (pointer_text or "") + (todo_text or "") + (gag_text or "") + mem
    if ctx.strip():
        # ⚠️ 这些背景是按【会话列表的行名】取的,而点进来那一下**可能点错行**。
        # 2026-09-02 真实事故:本该进老曹单聊,实际停在某乙的群里,
        # 但喂进去的是老曹的记忆和梗,M3 就信了背景不信屏幕,
        # 把一句「老曹 …你给我看这人啥意思」发进了酒圈群,被两个人当场起哄。
        ctx = ("【⚠️下面的背景资料是按会话列表行名取的,**有可能取错了**。"
               "请**先看屏幕**确认这到底是哪个会话(看顶部标题、群里都有谁);"
               "如果屏幕内容和这些背景对不上,**一律以屏幕为准并完全忽略下面的背景**,"
               "conversation 字段也按屏幕上看到的填。】\n" + ctx)
    mem = ctx
    # ⚠️ 必须告诉它今天几号星期几 —— 否则它会自己编。
    # 2026-09-03 真实翻车:周四早上它说「周末没喝够 周一一大早就开始编排我」,
    # 既编错了星期,也把别人的事安到了硕哥头上。模型没有时间感,不给就瞎猜。
    _wd = "一二三四五六日"[datetime.now().weekday()]
    now_line = (f"【现在是 {datetime.now():%Y年%m月%d日 %H:%M},星期{_wd}】"
                "涉及时间的话(今天/昨天/周末/一大早/上周)**必须按这个来**,不确定就别提时间。\n")
    multi = (now_line + "下面是同一个微信对话的多张截图,按时间【从早到晚】排列,"
             "最后一张是当前最新画面。" if len(imgs) > 1
             else "这是一个微信对话界面截图。")
    prompt = (mem + multi +
        "左侧气泡是对方,右侧绿色气泡是我方(机主本人)。"
        "结合这些历史 + 记忆,针对【对方最近发的内容】决定要不要回、回什么。"
        "**如果对方连着发了好几条还没等到我回复,把这几条一起理解,合成一条自然的回复**(别只盯最后一句,"
        "也别分几条回——只发一条,但内容要照顾到他刚说的这几件事)。"
        "【重要】只要你要回应的那条消息里含**图片或视频**(缩略图看不清内容),就默认 needs_image=true,"
        "并在 image_xy 给出那张图/视频在【最后一张(当前画面)】里的中心像素坐标(尺寸 宽1080 高2400)——"
        "先点开看清楚再回,别只凭模糊小图猜。除非这条根本不用回、或图无关紧要,才 needs_image=false。"
        + ("【这个群可以主动玩】如果当下气氛正合适、一张自制梗图能让大家笑(比如在调侃某人、"
           "有个具体画面很好笑),**你可以主动提出画一张**:offer_image=true 并写好 image_prompt,"
           "reply 里说句自然的话(如'这画面我得整一张')。⚠️ 一天最多一次,凑不上就别硬来,"
           "不合适的场合(严肃话题/生意事/没在开玩笑)一律 false。\n" if may_offer_image else "") +
        "另外判断:对方是不是【让我画图/生成图片/做张表情包】→ wants_image=true,"
        "并在 image_prompt 写好画面描述(要具体:主体+动作+风格,表情包就写'卡通表情包风格');"
        "reply 里写一句自然的话配合(如'来了 给你整一张')。\n"
        "另外判断:这条是不是【正经的业务/专业/知识问题,值得认真研究、给一段有料的长答】"
        "(如'干邑OEM找哪些厂''这款酒行情怎样''帮我分析下XX市场')→ is_deep_q=true;"
        "纯闲聊寒暄调侃 = false。\n"
        "严格只输出如下 JSON:\n"
        '{"conversation":"对话名(顶部标题)",'
        '"is_official":true/false(是否公众号/服务号推送或非真人对话界面),'
        '"last_from_customer":true/false,'
        '"last_customer_msg":"最新一条对方消息原文(若最新是我方发的则填空)",'
        '"need_reply":true/false,'
        '"is_deep_q":true/false,'
        '"wants_image":true/false(对方是否让我"画/生成/做一张图/来张表情包"),'
        '"image_prompt":"若wants_image=true,给出用于文生图的详细中文描述(画面内容/风格,如\'卡通表情包风格\')",'
        '"needs_image":true/false,'
        '"image_xy":[x,y] 或 null,'
        '"reply":"有人情味的中文回复(需看图才能答好则先给能确定的部分)",'
        '"needs_human":true/false,'
        '"offer_image":true/false(仅在允许时:你主动想画一张梗图),'
        '"gag":"若这轮里出现了值得以后回调的梗,一句话记下来(只记【发生了什么好笑的事】,'
        '绝不要记人物身份/关系判断);没有就留空",'
        '"promise":"若你这条回复里【答应了对方什么】(回头给准信/回头约/我让本人跟你说/'
        '帮你查一下…),一句话记下来;没答应什么就留空",'
        '"promise_done":"若上面【你答应过还没兑现的事】里有哪条这轮已经办掉了,把那条复述一遍;否则留空",'
        '"reason":"简短判断依据"}')

    def build_content(scale, gray=False):
        def enc(im):
            if scale < 0.999:
                im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)
            if gray:                       # 去色:审核对灰度图宽松得多(聊天文字照样能读)
                im = im.convert("L").convert("RGB")
            return img_b64(im)
        c = [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{enc(im)}"}} for im in imgs]
        c.append({"type": "text", "text": prompt})
        return c

    last_text = ""; sensitive = False; neterr = None
    # 全尺寸→缩小→再缩小→灰度:判图敏感(1026)时逐级降级重试,灰度是过审率最高的一招
    for scale, gray in ((1.0, False), (0.6, False), (0.4, False), (0.5, True), (0.35, True)):
        try:
            resp = get_client().with_options(timeout=120.0).chat.completions.create(
                model=MODEL, max_tokens=10000,   # 上限非目标,防思考过长截断
                messages=[{"role": "system", "content": load_persona()},
                          {"role": "user", "content": build_content(scale, gray)}],
            )
        except Exception as e:
            if "sensitive" in str(e).lower() or "1026" in str(e):
                sensitive = True
                continue          # 缩小图再试
            # ⚠️ 网络错/超时【不再往外抛】。以前这里 raise,后果是:
            #   一次 LLM 超时就掐死整轮决策(日志里 7 次 `处理出错:Request timed out.`),
            #   而 k3/Claude/Grok 三个兜底就在下面几行,一个都用不上,消息白白丢掉。
            #   现在记下错误继续降级,最终会落到兜底链上。
            neterr = e
            print(f"    [M3 调用失败 scale={scale}] {str(e)[:60]}")
            continue
        text = resp.choices[0].message.content or ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = re.sub(r"```(json)?", "", text).strip()
        last_text = text
        if "{" not in text:
            continue
        text = text[text.find("{"):text.rfind("}") + 1]
        try:
            return json.loads(text)
        except Exception:
            continue
    # 走到这里 = MiniMax 五次尝试全废(判敏 1026 / 返空 / 吐不出 JSON)→ 逐级换模型再读一次。
    # ⚠️ 以前这里写的是 `if sensitive:`,门开太窄:M3 返空的那类(实测 22 次)直接被丢掉、
    #    连兜底都不走,消息就静默没了。链路本来就是通的,缺的只是把条件放宽。
    why = "判敏" if sensitive else ("网络/超时" if neterr else "返空/解析失败")
    # 顺序有讲究:Claude 判定最准(MiniMax 的 1026 大量是误伤普通照片)、且已有额度;
    # Grok 过滤最松,放最后——只有前两个都读不了才轮到它。
    for name, fn in (("k3", _k3_vision_decide), ("claude", _claude_vision_decide),
                     ("grok", _grok_vision_decide)):
        d = fn(imgs[-1], prompt)
        if d:
            d["_vision_fallback"] = name
            print(f"    👁 MiniMax{why} → {name} 视觉兜底成功")
            return d
    # 全失败:标 _skip_seen 让外层记 seen,别每轮死循环刷错
    return {"conversation": "?", "last_from_customer": False, "need_reply": False,
            "reply": "", "needs_human": False, "_skip_seen": True,
            "reason": ("MiniMax判图敏感·跳过" if sensitive
                       else (f"网络/超时(已重试+兜底全废):{str(neterr)[:80]}" if neterr
                             else f"解析失败(已重试):{last_text[:100]}"))}


def _k3_vision_decide(img: Image.Image, prompt: str, system: str = None) -> dict:
    """兜底第一档:Kimi k3(coding 订阅,**不按次额外花钱**,所以排在付费的 Claude/Grok 前面)。
    实测 k3 能读图(2026-08-28 验证:普通图和真实微信列表截图都认得出),就是慢(~40s)。
    ⚠️ temperature 只能 = 1。无 key 返回 {}。"""
    if not os.environ.get(PROVIDERS["kimi-code"]["key_env"]):
        return {}
    try:
        small = img.resize((img.width // 2, img.height // 2), Image.LANCZOS)
        msgs = []
        if system is not False:
            msgs.append({"role": "system", "content": system or load_persona()})
        msgs.append({"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64(small)}"}},
            {"type": "text", "text": prompt}]})
        r = get_kimi_client().with_options(timeout=120.0).chat.completions.create(
            model=PROVIDERS["kimi-code"]["model"], max_tokens=2500, temperature=1, messages=msgs)
        t = r.choices[0].message.content or ""
        t = re.sub(r"<think>.*?</think>", "", t, flags=re.DOTALL)
        t = re.sub(r"```(json)?", "", t)
        if "{" in t:
            return json.loads(t[t.find("{"):t.rfind("}") + 1])
    except Exception as e:
        print(f"    [k3视觉兜底失败] {str(e)[:80]}")
    return {}


def _claude_vision_decide(img: Image.Image, prompt: str, system: str = None) -> dict:
    """MiniMax 判图敏感时的视觉兜底。用已有的 ANTHROPIC_API_KEY,日常只有几次/天,成本可忽略。
    失败/无 key 返回 {}。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {}
    try:
        from anthropic import Anthropic
        small = img.resize((img.width // 2, img.height // 2), Image.LANCZOS)
        r = Anthropic(api_key=api_key).messages.create(
            model="claude-sonnet-5", max_tokens=2000,
            system=(system or load_persona()),
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": img_b64(small)}},
                {"type": "text", "text": prompt}]}])
        t = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        t = re.sub(r"```(json)?", "", t)
        if "{" in t:
            return json.loads(t[t.find("{"):t.rfind("}") + 1])
    except Exception as e:
        print(f"    [Claude视觉兜底失败] {str(e)[:80]}")
    return {}


# xAI Grok:OpenAI 兼容。模型名可能随版本变,用 XAI_MODEL 覆盖(见 tools/test_grok.py 探测)
XAI_BASE  = "https://api.x.ai/v1"
XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4")

def _grok_vision_decide(img: Image.Image, prompt: str, system: str = None) -> dict:
    """最后一档视觉兜底:MiniMax 判敏 + Claude 也读不了时才轮到 Grok(内容过滤最松)。
    机主明确要求:擦边/辣/荤一点的图照常轻松接梗,别装死。只有真人露骨性画面才不点评内容。
    无 XAI_API_KEY 则空操作。"""
    key = os.environ.get("XAI_API_KEY")
    if not key:
        return {}
    try:
        from openai import OpenAI
        small = img.resize((img.width // 2, img.height // 2), Image.LANCZOS)
        guard = ("\n\n【分寸】前面两个模型因内容风控拒读了这张图,但那多半是误伤。"
                 "朋友群里擦边、辣、荤一点的内容(泳装、身材、夜店、荤段子、玩笑向的),"
                 "**照常轻松接一句、玩梗调侃**,别装死不吭声——这是机主明确要求的。"
                 "唯一例外:若是真人的露骨性画面,别描述、别点评画面内容,"
                 "给句不涉及内容的轻反应或 need_reply=false 跳过。")
        c = OpenAI(api_key=key, base_url=XAI_BASE, max_retries=1)
        resp = c.with_options(timeout=90.0).chat.completions.create(
            model=XAI_MODEL, max_tokens=2000,
            messages=[{"role": "system", "content": (system or load_persona())},
                      {"role": "user", "content": [
                          {"type": "image_url",
                           "image_url": {"url": f"data:image/png;base64,{img_b64(small)}"}},
                          {"type": "text", "text": prompt + guard}]}])
        t = resp.choices[0].message.content or ""
        t = re.sub(r"<think>.*?</think>", "", t, flags=re.DOTALL)
        t = re.sub(r"```(json)?", "", t)
        if "{" in t:
            return json.loads(t[t.find("{"):t.rfind("}") + 1])
    except Exception as e:
        print(f"    [Grok视觉兜底失败] {str(e)[:80]}")
    return {}


def capture_with_history(scrolls: int = 1):
    """截当前(最新)+ 向上滚动截更早历史。返回图片列表[早→晚,最新在最后]。
    截完滚回底部,保证 image_xy(相对最新画面)与发送时的界面一致。"""
    latest = screencap()
    hist = []
    for _ in range(scrolls):
        adb("shell", "input", "swipe", "540", "700", "540", "1650", "300")  # 向下滑=看更早
        time.sleep(0.5)
        hist.append(screencap())
    for _ in range(scrolls + 2):  # 多滑几次确保回到底部(最新)
        adb("shell", "input", "swipe", "540", "1650", "540", "600", "220")
        time.sleep(0.25)
    time.sleep(0.4)
    return list(reversed(hist)) + [latest]


# 会话列表里这些一律不进(机主:公众号本来就不回,进去白烧一次 LLM)
OFFICIAL_PREFIXES = ("公众号", "订阅号", "服务通知", "微信团队", "wechat团队",
                     "微信支付", "微信运动", "微信游戏", "腾讯新闻", "折扣与优惠",
                     "购物单", "微信收款助手", "语音记事本")

NO_REPLY_PATH = os.path.join(HERE, "no_reply.txt")

def load_no_reply():
    """不回复名单(`no_reply.txt`,一行一个,支持部分匹配)。**每轮实时读,改完即时生效**。"""
    out = []
    try:
        for line in open(NO_REPLY_PATH, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(re.sub(r"\s+", "", line))
    except FileNotFoundError:
        pass
    return out


FUN_PATH = os.path.join(HERE, "fun_groups.txt")

def is_fun_group(name: str) -> bool:
    """这个会话允许【主动】甩梗图吗?(`fun_groups.txt`,实时读)
    ⚠️ 只有已知情、关系够铁的群才放开——客户群/同事群/长辈群主动发梗图是灾难。"""
    n = re.sub(r"\s+", "", name or "")
    try:
        keys = [re.sub(r"\s+", "", l.strip()) for l in open(FUN_PATH, encoding="utf-8")
                if l.strip() and not l.strip().startswith("#")]
    except FileNotFoundError:
        return False
    return any(k and k in n for k in keys)


IMGQ_PATH = os.path.join(HERE, "img_quota.json")

def img_quota_ok(conv: str, cap: int = 1) -> bool:
    """主动发图的每日限额(每会话每天最多 cap 张)。**用多了就烦人**,这是幽默和骚扰的分界线。"""
    k = _mem_key(conv); today = datetime.now().strftime("%Y-%m-%d")
    try:
        q = json.load(open(IMGQ_PATH, encoding="utf-8"))
    except Exception:
        q = {}
    rec = q.get(k) or {}
    return not (rec.get("d") == today and rec.get("n", 0) >= cap)

def img_quota_use(conv: str):
    k = _mem_key(conv); today = datetime.now().strftime("%Y-%m-%d")
    try:
        q = json.load(open(IMGQ_PATH, encoding="utf-8"))
    except Exception:
        q = {}
    rec = q.get(k) or {}
    q[k] = {"d": today, "n": (rec.get("n", 0) + 1) if rec.get("d") == today else 1}
    json.dump(q, open(IMGQ_PATH, "w", encoding="utf-8"), ensure_ascii=False)


def is_no_reply(name: str) -> bool:
    """机主明确说了不用回的人/群 → 连进都不进(代码级硬过滤,不交给 LLM 判断)。"""
    n = re.sub(r"\s+", "", name or "")
    return any(k and k in n for k in load_no_reply())


def is_official_name(name: str) -> bool:
    """只看会话名就判断是不是公众号/系统号,**不用进去**。
    实测历史上有 98 次是"点进去 → 截图 → 烧一次 M3 → 才发现是公众号"。
    scan_list 的 prompt 里虽然写了排除,M3 还是会时不时列出来,所以在这儿再挡一道。"""
    n = re.sub(r"\s+", "", (name or "")).lower()
    return any(n.startswith(k) for k in OFFICIAL_PREFIXES)


def scan_list_with_llm(img: Image.Image, patrol: bool = False):
    """让 M3 读会话列表,找出有未读/被@的会话。比数红点像素可靠:
    能认免打扰群的'[有人@我]'红字、排除公众号、不被红色头像误判。
    返回 [{'name':..., 'y':int, 'at_me':bool}, ...],y 为该行在截图(高2400)里的中心。"""
    # 列表只需读文字,缩到 0.5 传输更快、也更容易过审核(y 坐标按原图 2400 让模型给)
    small = img.resize((img.width // 2, img.height // 2), Image.LANCZOS)
    b64 = img_b64(small)
    prompt = ("这是微信会话列表首页截图(注意:请按【原始尺寸 宽1080 高2400】给坐标)。找出所有需要我处理的会话:"
              "① 有未读新消息(头像右上角有红色数字角标,**哪怕只有1条也要列出**);"
              "② 有人@我(预览行显示红字『[有人@我]』或类似,免打扰群也会显示);"
              "③ 免打扰会话(头像角标是**小红点无数字**)也算未读,要列出。"
              "排除『公众号/订阅号/服务通知』这类推送。"
              "**务必把所有符合的会话都列全,不要只挑一个**(漏掉会导致那个群一直没人回)。"
              + ("【本轮是巡逻】另外请把列表**最上面的 4 个真人会话**也一并列出(不论有没有未读、有没有红点),"
                 "它们的 at_me 一律填 false。这是为了补上『进过一次标了已读、红点没了』而被漏掉的消息。"
                 if patrol else "")
              + "对每个,给出会话名、该行在截图里的垂直中心y坐标、是否@我。"
              '严格只输出JSON:{"rows":[{"name":"会话名","y":整数,"at_me":true/false}]};没有则 {"rows":[]}。')
    last_err = None
    for attempt in range(2):   # M3 偶尔不吐 JSON,失败重试一次
        try:
            resp = get_client().with_options(timeout=90.0).chat.completions.create(
                model=MODEL, max_tokens=6000,   # 上限非目标,防截断
                messages=[{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": prompt}]}],
            )
            t = resp.choices[0].message.content or ""
            t = re.sub(r"<think>.*?</think>", "", t, flags=re.DOTALL)
            t = re.sub(r"```(json)?", "", t)
            if "{" not in t:      # 没吐 JSON → 当作没未读,重试
                last_err = "无JSON"; continue
            t = t[t.find("{"):t.rfind("}") + 1]
            rows = json.loads(t).get("rows", [])
            return sorted(rows, key=lambda r: (not r.get("at_me"), r.get("y", 0)))
        except Exception as e:
            last_err = e
    # M3 两次都废了 → 逐级降级。⚠️ 这条链是防"单点故障"的:以前列表识别只认 M3,
    # M3 限流/抽风时整个分身就**安静地瞎掉**(日志还在刷,看门狗都发现不了)。
    # 顺序按成本排:k3(coding 订阅不额外花钱)→ Claude → Grok(后两个按次付费)。
    sys_neutral = "你是一个看图提取信息的助手,只输出用户要求的 JSON,不要多说。"
    for name, fn in (("k3", _k3_vision_decide), ("claude", _claude_vision_decide),
                     ("grok", _grok_vision_decide)):
        d = fn(img, prompt, system=sys_neutral)
        if isinstance(d, dict) and "rows" in d:
            print(f"    👁 列表识别 M3失败 → {name} 兜底成功")
            return sorted(d["rows"], key=lambda r: (not r.get("at_me"), r.get("y", 0)))
    print(f"    [列表识别失败·四档全废] {last_err}")
    return []


# ── 看图办事(点开聊天里的图片看高清)────────────────────────────────
def find_image_region(img: Image.Image, y_hint=None):
    """程序检测聊天截图里的图片/卡片区域(不信 M3 的坐标)。
    原理:聊天背景是浅灰、气泡是白/微信绿,而照片/截图色彩杂或偏暗。
    在消息区(避开头像列和顶栏/输入栏)网格采样,聚类出"图片样"色块,
    返回面积最大(或离 y_hint 最近)块的中心;找不到返回 None。"""
    px = img.load(); W, H = img.size
    step = 12
    def img_like(r, g, b):
        mx, mn = max(r, g, b), min(r, g, b)
        if r > 225 and g > 225 and b > 225:            # 白气泡/白底
            return False
        if abs(r - 237) < 16 and abs(g - 237) < 16 and abs(b - 237) < 16:  # 背景灰
            return False
        if g > 200 and r > 110 and r < 200 and b < 170 and g - b > 60:      # 微信绿气泡
            return False
        return (mx - mn) > 45 or mx < 110              # 彩色 或 暗色 → 像照片
    cells = set()
    for y in range(230, H - 620, step):                # 避开顶栏和底部输入区
        for x in range(150, W - 60, step):             # 避开左头像列
            r, g, b = px[x, y]
            if img_like(r, g, b):
                cells.add((x // step, y // step))
    if not cells:
        return None
    # 简易连通聚类(4邻域)
    seen, blocks = set(), []
    for c in cells:
        if c in seen:
            continue
        stack, blk = [c], []
        seen.add(c)
        while stack:
            cx, cy = stack.pop(); blk.append((cx, cy))
            for nx, ny in ((cx+1,cy),(cx-1,cy),(cx,cy+1),(cx,cy-1)):
                if (nx, ny) in cells and (nx, ny) not in seen:
                    seen.add((nx, ny)); stack.append((nx, ny))
        xs = [p[0] for p in blk]; ys = [p[1] for p in blk]
        w = (max(xs) - min(xs) + 1) * step; h = (max(ys) - min(ys) + 1) * step
        if w >= 200 and h >= 150 and len(blk) * step * step >= w * h * 0.45:  # 够大且够"实"
            blocks.append(((min(xs)*step + w//2), (min(ys)*step + h//2), w*h))
    if not blocks:
        return None
    if y_hint is not None:
        blocks.sort(key=lambda t: abs(t[1] - y_hint))   # 离 M3 提示的 y 最近优先
    else:
        blocks.sort(key=lambda t: -t[2])                 # 否则取最大
    return (blocks[0][0], blocks[0][1])


def find_card_region(img: Image.Image, y_hint=None):
    """找**白底的分享卡片**(小红书/公众号/视频号笔记等)。
    `find_image_region()` 明确排除白色,所以这类卡片它天生看不见 —— 群里转发的小红书笔记
    因此一直点不开,只能退回用 M3 的坐标,而那个常常不准(2026-08-31 那条"四位数DRC"就是)。

    判据(实测标定,1080×2400):白色区块内**最长的横向非白连段**——
      · 纯文字气泡 ≈ 42px(一个汉字宽,字与字之间有空隙,连不长)
      · 卡片/图片 ≥ 114px(里面有实心缩略图)
    所以用 ≥90px 当门槛,把卡片和纯文字气泡分开。返回卡片中心,找不到 None。"""
    px = img.load(); W, H = img.size
    step = 8

    def whiteish(p):
        return p[0] > 240 and p[1] > 240 and p[2] > 240

    cells = {(x // step, y // step)
             for y in range(230, H - 500, step)
             for x in range(120, W - 40, step) if whiteish(px[x, y])}
    if not cells:
        return None
    seen, out = set(), []
    for c in cells:
        if c in seen:
            continue
        stack, blk = [c], []
        seen.add(c)
        while stack:
            cx, cy = stack.pop(); blk.append((cx, cy))
            for n in ((cx+1,cy),(cx-1,cy),(cx,cy+1),(cx,cy-1),
                      (cx+1,cy+1),(cx-1,cy-1),(cx+1,cy-1),(cx-1,cy+1)):   # 8邻域,别被圆角切断
                if n in cells and n not in seen:
                    seen.add(n); stack.append(n)
        xs = [q[0] for q in blk]; ys = [q[1] for q in blk]
        x0, x1, y0, y1 = min(xs)*step, max(xs)*step, min(ys)*step, max(ys)*step
        if (x1 - x0) < 250 or (y1 - y0) < 120:      # 太小的白块(短文字气泡/角标)不看
            continue
        # 块内最长横向"非白且非背景"连段 —— 有实心缩略图才算卡片
        best = 0
        for y in range(y0, y1, 6):
            run = 0
            for x in range(x0, x1, 6):
                p = px[x, y]
                bg = abs(p[0]-237) < 10 and abs(p[1]-237) < 10 and abs(p[2]-237) < 10
                if not (p[0] > 235 and p[1] > 235 and p[2] > 235) and not bg:
                    run += 1; best = max(best, run)
                else:
                    run = 0
        if best * 6 >= 90:
            out.append(((x0+x1)//2, (y0+y1)//2, (x1-x0)*(y1-y0)))
    if not out:
        return None
    if y_hint is not None:
        out.sort(key=lambda t: abs(t[1] - y_hint))
    else:
        out.sort(key=lambda t: -t[2])
    return (out[0][0], out[0][1])


def _is_image_viewer(img: Image.Image) -> bool:
    """判断当前是否在全屏图片查看器:聊天底部有浅色输入栏,图片查看器底部是暗的。"""
    px = img.load(); W, H = img.size
    vals = [sum(px[x, y]) / 3
            for y in range(H - 180, H - 60, 8)
            for x in range(100, W - 100, 20)]
    return (sum(vals) / len(vals)) < 120   # 暗 = 图片查看器


def refine_reply_with_image(high_res: Image.Image, request: str) -> str:
    """已点开某张图的高清全屏 → 结合对方请求生成要发的实质回复。
    带超时 + 敏感降级(缩小/灰度),避免卡死在这一步(卡住会导致整轮停摆、群不回)。"""
    txt = (f"群里有人让我看这张图并回应,他的原话/意图是:「{request}」。"
           "看清图片内容,直接给出要发到微信群的中文回复(翻译就给译文、介绍酒就给介绍、"
           "看字就念出来),像真人朋友那样口语简洁、别客服腔;硬事实(价格/评分)不编造。"
           "只输出要发的那句话,不要解释、不要 JSON。")
    for scale, gray in ((1.0, False), (0.6, False), (0.5, True)):
        im = high_res
        if scale < 0.999:
            im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
        if gray:
            im = im.convert("L").convert("RGB")
        try:
            resp = get_client().with_options(timeout=90.0).chat.completions.create(
                model=MODEL, max_tokens=3000,
                messages=[
                    {"role": "system", "content": load_persona()},
                    {"role": "user", "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{img_b64(im)}"}},
                        {"type": "text", "text": txt},
                    ]},
                ],
            )
        except Exception as e:
            print(f"    [看图] scale={scale} gray={gray} 失败:{str(e)[:60]}")
            continue
        t = resp.choices[0].message.content or ""
        t = re.sub(r"<think>.*?</think>", "", t, flags=re.DOTALL).strip()
        if t:
            return t
    return ""


def open_image_and_reply(image_xy, request: str):
    """点开聊天里的图片看高清 → 生成回复 → 退回聊天。坐标没点中/没打开则返回 None 且不乱按。"""
    try:
        x, y = int(image_xy[0]), int(image_xy[1])
    except Exception:
        x = y = None
    # 程序找图优先(M3 的坐标常不准);M3 的 y 只当提示,找不到再退用 M3 原坐标
    shot0 = screencap()
    det = find_image_region(shot0, y_hint=y)
    how = "彩色/暗色图块"
    if not det:                                  # 白底分享卡片(小红书/公众号)走另一条检测
        det = find_card_region(shot0, y_hint=y)
        how = "白底分享卡片"
    if det:
        x, y = det
        print(f"    [看图] 程序检测到{how},点({x},{y})")
    elif x is None:
        print("    [看图] 程序没检测到图片、M3坐标也无效,跳过")
        return None
    else:
        print(f"    [看图] 程序没检测到,退用M3坐标({x},{y})")
    tap(x, y)                      # 点开图片/视频
    time.sleep(1.4)
    shot = screencap()
    if not _is_image_viewer(shot):  # 可能慢加载,再等一下重查
        time.sleep(1.2)
        shot = screencap()
    if not _is_image_viewer(shot):  # 还没开 = 坐标没点中(M3坐标不准)→ 保持在聊天,别按返回
        print(f"    [看图] 点(x={x},y={y})没打开图片(M3坐标偏)→ 退回缩略图,该条别当准信")
        return None
    print("    [看图] 已打开全屏,读高清中…")
    try:
        ans = refine_reply_with_image(shot, request) or None
        print("    [看图] " + ("读图成功" if ans else "读图无内容"))
        return ans
    except Exception as e:
        print(f"    [看图] 读失败·可能被MiniMax判敏感:{str(e)[:50]}")
        return None
    finally:
        back()
        time.sleep(0.6)


# ── 发送 ────────────────────────────────────────────────────────────
_send_times = []          # 最近发送的时间戳(全局限速用)
SEND_MIN_GAP = 6.0        # 任意两条发送之间的最小间隔(秒)
SEND_JITTER = 4.0         # 额外随机抖动上限,别每次都卡整数(像人)
SEND_PER_MIN = 12         # 每分钟发送硬上限,超了就等到窗口松动

def _pace_send():
    """发送节奏闸:防止群里一热闹就同时秒回好几个群(看着像机器)。
    我们平时才 ~7 条/小时,这几个阈值基本不影响正常收发,只削掉突发峰值。
    参考 WeIX 的做法:最小间隔 + 随机抖动 + 每分钟上限。"""
    import random
    now = time.time()
    _send_times[:] = [t for t in _send_times if now - t < 60]   # 只留最近 60s
    # ① 每分钟上限:满了就等最老那条滑出窗口
    if len(_send_times) >= SEND_PER_MIN:
        wait = 60 - (now - _send_times[0]) + 0.5
        if wait > 0:
            print(f"    ⏳ 每分钟发送已达 {SEND_PER_MIN} 条,等 {wait:.0f}s")
            time.sleep(wait)
            now = time.time()
            _send_times[:] = [t for t in _send_times if now - t < 60]
    # ② 最小间隔 + 抖动
    if _send_times:
        gap = now - _send_times[-1]
        need = SEND_MIN_GAP + random.uniform(0, SEND_JITTER) - gap
        if need > 0:
            time.sleep(need)
    _send_times.append(time.time())


def send_reply(text: str) -> bool:
    """在当前打开的对话里发送中文回复。返回是否成功点到发送键。"""
    _pace_send()       # 节奏闸:所有发送路径都经过这里
    set_ime(IME_ADB)   # 每次发送前确保中文输入法(断连重连/系统会把它重置回搜狗→打不进字→发送失败)
    time.sleep(0.3)
    # 点输入框聚焦(输入框在底部中间偏左)
    img = screencap()
    W, H = img.size
    tap(int(W * 0.45), int(H * 0.925))
    time.sleep(0.6)
    # 注:故意不清空输入框——机主想保留"发送失败偶尔累积成长作文"的彩蛋 😄(send 已修好,失败罕见→长作文只偶发)
    type_chinese(text)
    # 发送键要等文字渲染后才出现,多等 + 重试几次(只等 0.4s 会找不到→发送失败)
    btn = None
    for _ in range(4):
        time.sleep(0.6)
        btn = find_send_button(screencap())
        if btn:
            break
    if not btn:
        print("    ⚠️ 没找到发送键,跳过发送(文字已在框里)")
        return False
    # ⚠️ 点完必须**验证**,不能假定点中了。
    # 2026-09-03 实测:日志写着"已发送",可文字还躺在输入框里当草稿——
    # 因为文字换行后输入框变高、发送键上移,而用的是变化前那一瞬的坐标,点空了。
    # 判据:发送键还在 = 框里还有字 = 没发出去(微信输入框空了发送键就消失)。
    for attempt in range(3):
        tap(*btn)
        time.sleep(0.9)
        again = find_send_button(screencap())
        if not again:
            return True                      # 发送键消失 = 真的发出去了
        if attempt == 0:
            print("    ↻ 点了没发出去(发送键还在),重新定位重试")
        btn = again                          # 用最新坐标再点
    print("    ⚠️ 连点 3 次都没发出去,文字留在框里")
    return False


# ── 持久化 ──────────────────────────────────────────────────────────
def _shift_backups(path: str, keep: int):
    for i in range(keep - 1, 0, -1):
        a, b = f"{path}.{i}", f"{path}.{i + 1}"
        if os.path.exists(a):
            os.replace(a, b)


HEALTH_PATH = os.path.join(HERE, "health.json")

def beat(ok: bool, cycle: int = 0, note: str = "", down_secs: int = 0):
    """写心跳。⚠️ 这是给看门狗用的**唯一可信信号**——
    以前看门狗看的是 bot.log 有没有更新,结果 adb 掉线时日志一直在刷报错,
    看门狗一路报"正常",分身瞎了 3 天没人知道(2026-08-28→31 真实事故)。
    日志在动 ≠ 分身活着;只有"完整跑完一轮且 adb 是通的"才算活着。"""
    try:
        json.dump({"ts": datetime.now().isoformat(timespec="seconds"),
                   "ok": ok, "cycle": cycle, "note": note,
                   # 断连已持续多久:看门狗靠它区分"抖一下"和"真掉了"。
                   # ⚠️ 没有这个字段时,一次几秒的 WiFi 抖动就会被误报成"停摆"(2026-09-01 踩过)
                   "down_secs": int(down_secs)},
                  open(HEALTH_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass


def rotate_logs(max_mb: float = 5.0, keep: int = 3):
    """日志轮转,保留最近 keep 份。
    ⚠️ 只轮转【日志】,不动 memory.json / pointer.json / seen.json ——
    那三个是分身的记忆和状态,轮转掉等于让它失忆。"""
    # ① decisions.jsonl:本进程每次 open/close 写,直接改名即可
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) >= max_mb * 1024 * 1024:
            _shift_backups(LOG_PATH, keep)
            os.replace(LOG_PATH, LOG_PATH + ".1")
            print("    🗂 decisions.jsonl 已轮转")
    except Exception as e:
        print(f"    [轮转decisions失败] {str(e)[:60]}")

    # ② bot.log:⚠️ 是 launchd 用 StandardOutPath 打开的,**改名没用**——
    #    进程的 fd 还指着同一个 inode,日志会继续写进改名后的文件,新 bot.log 永远不生成。
    #    正确做法:先把内容复制走,再【原地截断】(launchd 以 O_APPEND 打开,截断后下次写回到 0)。
    blog = os.path.join(HERE, "bot.log")
    try:
        if os.path.exists(blog) and os.path.getsize(blog) >= max_mb * 1024 * 1024:
            _shift_backups(blog, keep)
            with open(blog, "rb") as src, open(blog + ".1", "wb") as dst:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    dst.write(chunk)
            with open(blog, "r+b") as f:
                f.truncate(0)
            print("    🗂 bot.log 已轮转(原地截断)")
    except Exception as e:
        print(f"    [轮转bot.log失败] {str(e)[:60]}")


def load_seen() -> set:
    if os.path.exists(SEEN_PATH):
        try:
            return set(json.load(open(SEEN_PATH, encoding="utf-8")))
        except Exception:
            return set()
    return set()


SEEN_MAX = 3000

def save_seen(seen: set):
    """⚠️ seen 只增不减会一直涨(实测已 452 条)。超过上限就丢一半——
    指纹是 (会话名+消息文本) 的哈希,老指纹对应的消息早就滚出屏幕了,丢掉无害;
    真要重复处理,还有 pointer.json 兜着。"""
    if len(seen) > SEEN_MAX:
        keep = sorted(seen)[-(SEEN_MAX // 2):]
        seen.clear(); seen.update(keep)
        print(f"    🗂 seen 超过 {SEEN_MAX} 条,已裁剪到 {len(seen)}")
    json.dump(sorted(seen), open(SEEN_PATH, "w", encoding="utf-8"), ensure_ascii=False)


def log_decision(rec: dict):
    rec["ts"] = datetime.now().isoformat(timespec="seconds")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


_GENERIC_MSG = re.compile(r"^\s*(\[[^\]]{0,8}\]|[\s\S]{0,6})\s*$")

def fingerprint(conv: str, msg: str, img: Image.Image = None) -> str:
    """去重指纹。
    ⚠️ 老版本只用 (会话名 + 消息文本),表情包/图片的文本一律是 `[图片]`,
       于是**一个群里所有图片的指纹完全相同** → 第一张处理过后,后面每一张都被
       判成"已处理"而不回。(2026-09-03:杨硕发表情包一直没人理,就是这个。)
       语音 `[语音]`、视频 `[视频]` 同理。
    修法:消息文本【太短或只是个占位符】时,补一段**画面指纹**(聊天区下半屏的粗哈希),
       不同的图 → 不同的指纹;同一张图反复看 → 指纹不变,去重照旧有效。"""
    extra = ""
    if img is not None and _GENERIC_MSG.match(msg or ""):
        try:
            W, H = img.size
            crop = img.crop((int(W * 0.05), int(H * 0.45), int(W * 0.95), int(H * 0.86)))
            small = crop.resize((16, 16), Image.LANCZOS).convert("L")
            extra = "|" + "".join(f"{p // 32:x}" for p in small.getdata())
        except Exception:
            pass
    return hashlib.md5(f"{conv}|{msg}{extra}".encode()).hexdigest()[:16]


# ── 每个会话的历史记忆(跨时间记得聊过啥)──────────────────────────
MEMORY_PATH = os.path.join(HERE, "memory.json")

def _load_mem() -> dict:
    if os.path.exists(MEMORY_PATH):
        try:
            return json.load(open(MEMORY_PATH, encoding="utf-8"))
        except Exception:
            return {}
    return {}

# 会话名归一:M3 每次读群名都可能认错一两个字,只去空格治不了。
# 实测 21 个记忆 key 里真实会话只有约 10 个——
#   「某某粉丝群」被认成 某某斯/某某/某某克斯/某某蒂斯/粉丝群004 共 6 种
#   (举例)某个三人群名被 OCR 认成 5~6 种变体,把记忆切碎
# 记忆和处理指针因此被切碎:以为记着 25 条,实际散在 6 个 key 里,每次只捞到一份。
ALIAS_PATH = os.path.join(HERE, "key_alias.json")
_SIM = 0.80          # 相似度阈值:低于它就当成两个不同会话

def _load_alias() -> dict:
    try:
        return json.load(open(ALIAS_PATH, encoding="utf-8"))
    except Exception:
        return {}

def _mem_key(conv: str) -> str:
    """记忆/指针/梗档案的统一键。去空白去装饰符,再用相似度归并 OCR 变体。
    别名表存在 key_alias.json,**可以人工检查和纠正**(归错了直接改这个文件)。"""
    raw = re.sub(r"[\s~～·・\-—.…]+", "", conv or "")   # 含 . 和 … :列表名常被截断成「…XXX...」
    if not raw:
        return ""
    alias = _load_alias()
    if raw in alias:
        return alias[raw]
    from difflib import SequenceMatcher
    canons = set(alias.values())
    best, score = None, 0.0
    for c in canons:
        if abs(len(c) - len(raw)) > 3:        # 长度差太多不可能是同一个
            continue
        r = SequenceMatcher(None, raw, c).ratio()
        if r > score:
            best, score = c, r
    target = best if (best and score >= _SIM) else raw
    if target is raw or target == raw:
        # ⚠️ 新开了一个权威 key —— 但它可能和某个已有权威 key 其实是同一个群。
        # 踩过:首次见到的是个乱码变体(「威廉蒂斯…Q4」)就被立成权威,
        # 后面正常的「(公司)…04」反而另立一个,同一个群裂成两个档案。
        # 所以新权威一旦和老权威相似,就把【老的合并到新的】(取更"干净"的那个:
        # 出现次数多的胜出,这里用别名数量近似)。
        from collections import Counter
        cnt = Counter(alias.values())
        for c in list(canons):
            if c == raw or abs(len(c) - len(raw)) > 3:
                continue
            if SequenceMatcher(None, raw, c).ratio() >= _SIM:
                keep, drop = (c, raw) if cnt[c] >= 1 else (raw, c)
                for k2, v2 in list(alias.items()):
                    if v2 == drop:
                        alias[k2] = keep
                target = keep
                break
    alias[raw] = target
    try:
        json.dump(alias, open(ALIAS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    return target

# 人名:用来做"同一个人跨场子"的记忆串联。从会话名里能认出来的熟人。
# ⚠️ 别放互为前缀的名字(如同时放「某甲」和「曹博」),会重复匹配、记忆喂两遍
CROSS_NAMES = ("某甲", "某乙", "某丁", "杨硕", "某丙", "王乐先生",
               "相宇", "秀一", "鱼想想", "张慧敏")

def get_memory(conv: str) -> str:
    """取某会话最近的历史记忆 + 同一个人在【别的场子】里的少量记忆。

    为什么要跨场子:记忆是按【对话】存的,但人是跨对话的 ——
    老曹在群里跑全马那事,他明天单聊时分身就不知道了,显得很失忆。
    ⚠️ 只串【情节】,人物身份/关系一律走 people.md(人工维护),
      自动写入的身份事实错了会永久污染(杨硕辈分那次的教训)。"""
    mem = _load_mem()
    k = _mem_key(conv)
    own = mem.get(k, [])[-14:]
    out = "\n".join(own)

    # ⚠️ 跨场子只在【单聊】里用,群聊一律不串!
    # 2026-09-01 真实事故:在 211 人的粉丝群里,因为上下文塞满了老曹的记忆和梗,
    # M3 直接把会话认成"和老曹的对话",回了句「被你逮着了😂 老曹这波操作太狠」发进客户群。
    # 群聊本来人就多,再掺别的会话的人名,模型极易张冠李戴。
    is_group = ("、" in (conv or "")) or ("群" in (conv or ""))
    who = [] if is_group else [n for n in CROSS_NAMES if n in re.sub(r"\s+", "", conv or "")]
    if who:
        extra, seen_lines = [], set()
        for name in who:
            for k2, rows in mem.items():
                if k2 == k or name not in k2:
                    continue
                for r in rows[-4:]:
                    if r in seen_lines:        # 同一条别喂两遍
                        continue
                    seen_lines.add(r); extra.append(f"· {r}")
        if extra:
            out += ("\n\n【⚠️下面是 " + "、".join(who) + " 在【别的会话】里聊过的,仅供你了解背景。"
                    "**当前不是那个会话**,回复时别把那边的人名和事直接搬过来说】\n"
                    + "\n".join(extra[-8:]))
    return out

# ── 处理指针(方案A):每个会话"上次处理到哪条",治"标已读后漏回" ──
# 只靠"最后一条是谁发的"会漏:进过一次没回 → 红点消失 → 那几条永远沉底。
# 记下上次看到的最后一条对方消息,下次进来告诉 LLM"这条之后的才是新的",
# 多条新消息就能一起理解、合成一条回复(老曹连发好几条只回一条就是这个病)。
POINTER_PATH = os.path.join(HERE, "pointer.json")

def _load_ptr() -> dict:
    if os.path.exists(POINTER_PATH):
        try:
            return json.load(open(POINTER_PATH, encoding="utf-8"))
        except Exception:
            return {}
    return {}

def get_pointer(conv: str) -> str:
    """给决策 prompt 用的一段话;没有指针返回空串。"""
    p = _load_ptr().get(_mem_key(conv))
    if not p or not p.get("msg"):
        return ""
    return (f"【上次处理进度】我上次处理这个会话是在 {p.get('at','')},当时看到对方最后一条是:「{p['msg']}」。\n"
            "当前屏幕里**在这条之后**的对方消息才是新的、需要你处理的;这条及更早的都已经处理过了,别重复回。\n"
            "如果这条之后有好几条新消息,**把它们合起来理解,回一条照顾到全部的**;"
            "如果这条之后没有对方的新消息,则 need_reply=false。\n")

def set_pointer(conv: str, msg: str):
    """记下本次处理到的最后一条对方消息。"""
    k = _mem_key(conv)
    if not k or k == "?" or not (msg or "").strip():
        return
    p = _load_ptr()
    p[k] = {"msg": msg.strip()[:80], "at": datetime.now().strftime("%m-%d %H:%M")}
    json.dump(p, open(POINTER_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# ── 未了之事(答应了还没兑现的)────────────────────────────────────
# 机主问"牛腩煲那顿定了没"时暴露的短板:分身只记"聊过什么",不记"答应了什么"。
# 这是"会聊天"和"会办事"的分界线。
TODO_PATH = os.path.join(HERE, "todos.json")

def _load_todos() -> dict:
    try:
        return json.load(open(TODO_PATH, encoding="utf-8"))
    except Exception:
        return {}

def _save_todos(d):
    json.dump(d, open(TODO_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

def get_todos(conv: str) -> str:
    """喂给决策的"欠着的事"。⚠️ 超过 14 天的自动作废——别让分身没完没了地催。"""
    lst = [t for t in _load_todos().get(_mem_key(conv), []) if not t.get("done")]
    fresh = []
    today = datetime.now()
    for t in lst:
        try:
            d = datetime.strptime(f"{today.year}-{t.get('at','')}", "%Y-%m-%d")
            if (today - d).days > 14:
                continue
        except Exception:
            pass
        fresh.append(t)
    if not fresh:
        return ""
    return ("【你答应过、但还没兑现的事】\n"
            + "\n".join(f"· [{t.get('at','')}] {t.get('t','')}" for t in fresh[-6:])
            + "\n合适的时候自然地跟进一句就行(比如对方正好提到相关话题),"
              "**别反复催、别每次都提**;已经办完的别再提。\n")

def add_todo(conv: str, text: str):
    k = _mem_key(conv); t = (text or "").strip()[:80]
    if not k or k == "?" or len(t) < 4:
        return
    d = _load_todos(); lst = d.get(k, [])
    if any(t == x.get("t") for x in lst):
        return
    lst.append({"t": t, "at": datetime.now().strftime("%m-%d"), "done": False})
    d[k] = lst[-15:]; _save_todos(d)

def close_todo(conv: str, text: str):
    """把已兑现的那条标记完成(模糊匹配,LLM 复述得不完全一样也能对上)。"""
    from difflib import SequenceMatcher
    k = _mem_key(conv); t = (text or "").strip()
    if not k or len(t) < 4:
        return
    d = _load_todos(); changed = False
    for x in d.get(k, []):
        if x.get("done"):
            continue
        m = SequenceMatcher(None, t, x.get("t", ""))
        lcs = max((b.size for b in m.get_matching_blocks()), default=0)
        # ⚠️ 光看 ratio 不行:LLM 复述会换说法,实测相关的只有 0.52~0.64、
        #    但【最长公共子串】区分得很干净(相关 5~8 字,无关 0 字)。两个判据取或。
        #    宁可多关掉一条(最多是不再跟进)也别漏(漏了会反复提,烦人)。
        if m.ratio() >= 0.40 or lcs >= 4:
            x["done"] = True; changed = True
    if changed:
        _save_todos(d)


# ── 梗档案(让它会"回调"老梗,熟人幽默的核心)────────────────────────
# ⚠️ 刻意**只记情节**(谁干了什么好笑的事),**绝不记人物身份/关系判断** ——
#    自动写入的身份类"事实"一旦错了会永久污染回复(杨硕辈分那次的教训)。
#    情节记错了最多是个不好笑的玩笑,代价低得多。
GAGS_PATH = os.path.join(HERE, "gags.json")

def _load_gags() -> dict:
    if os.path.exists(GAGS_PATH):
        try:
            return json.load(open(GAGS_PATH, encoding="utf-8"))
        except Exception:
            return {}
    return {}

def get_gags(conv: str) -> str:
    """给决策 prompt 用的老梗段落;没有就返回空串。"""
    lst = _load_gags().get(_mem_key(conv), [])[-8:]
    if not lst:
        return ""
    return ("【这个群的老梗(可以自然地拎出来回调,但别硬凑;凑不上就别用)】\n"
            + "\n".join(f"· [{g.get('at','')}] {g.get('g','')}" for g in lst) + "\n")

def add_gag(conv: str, text: str):
    """记一个梗。同一个梗别重复记。"""
    k = _mem_key(conv); t = (text or "").strip()[:80]
    if not k or k == "?" or len(t) < 4:
        return
    g = _load_gags(); lst = g.get(k, [])
    if any(t == x.get("g") for x in lst):
        return
    lst.append({"g": t, "at": datetime.now().strftime("%m-%d")})
    g[k] = lst[-20:]
    json.dump(g, open(GAGS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def append_memory(conv: str, customer_msg: str, reply: str):
    """记一次交互:对方说了啥、我回了啥。每会话滚动保留最近 25 条。"""
    if not conv or conv == "?":
        return
    conv = _mem_key(conv)
    m = _load_mem()
    lst = m.get(conv, [])
    stamp = datetime.now().strftime("%m-%d %H:%M")
    # 截断从 50 放宽到 120:50 字砍得太狠,长一点的问题和回复都被削成半句,
    # 下次读记忆时看不出当时到底聊了啥(记忆总量才 8KB,不差这点)
    who = (customer_msg or "").strip()[:120] or "(图片/其它)"
    lst.append(f"[{stamp}] 对方:{who} | 我:{reply.strip()[:120]}")
    m[conv] = lst[-25:]
    json.dump(m, open(MEMORY_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# ── 主循环 ──────────────────────────────────────────────────────────
# ── 进门先认门牌:用聊天页【标题栏】的像素哈希识别会话 ──────────────
# 为什么要有它:点错行之后,旧流程是"按【行名】加载记忆/梗/待办 → 决策 → 事后校验",
# 校验在决策之后,只能防"错误沉淀进档案",防不了"这一轮说错话"——而且上下文一旦塞满了
# 别的会话的人,M3 连自己报的 conversation 字段都会跟着错(把客服叫成老曹那次)。
# 所以要一个【和上下文无关的硬信号】在加载上下文之前先认门牌:同一个会话的标题栏
# 像素级每次一模一样(同字体同位置同文字),哈希即身份。零 LLM、毫秒级。
# 实测(2026-09-04):同标题不同时刻 = 1.000;「群聊(3)」vs「群聊(5)」= 0.936;
# 不同会话 ≤ 0.33 → 阈值 0.97 很安全。
# ⚠️ 无名群标题「群聊(3)」王乐群和老曹群长得一样 → 会撞。撞了就标 AMBIGUOUS,
#    那两个群退回旧逻辑。宁可少认,不可误认。
TITLE_CACHE_PATH = os.path.join(HERE, "title_hash.json")
TITLE_SIM = 0.97

def title_hash(img: Image.Image):
    """标题栏中段(x 38%~62%, y 6.2%~8.0%)缩到 128×12 灰度、16 级量化。"""
    W, H = img.size
    crop = img.crop((int(W * 0.38), int(H * 0.062), int(W * 0.62), int(H * 0.080)))
    small = crop.resize((128, 12), Image.LANCZOS).convert("L")
    return [p * 16 // 256 for p in small.getdata()]

def _hsim(a, b):
    return sum(x == y for x, y in zip(a, b)) / max(1, len(a))

def _load_title_cache():
    try:
        return json.load(open(TITLE_CACHE_PATH, encoding="utf-8"))
    except Exception:
        return []

def identify_by_title(img: Image.Image):
    """返回 (会话key 或 None, 相似度)。None = 没见过 / 标题不可辨(撞过)。"""
    h = title_hash(img)
    best, bs = None, 0.0
    for e in _load_title_cache():
        sm = _hsim(h, e.get("h", []))
        if sm > bs:
            best, bs = e, sm
    if best and bs >= TITLE_SIM:
        return (None if best.get("key") == "AMBIGUOUS" else best.get("key")), bs
    return None, bs

def learn_title(img: Image.Image, key: str):
    """一次没出错的决策之后,把「标题哈希 → 会话」记下来。同一哈希对应了不同会话 → 标 AMBIGUOUS。"""
    if not key or key == "?":
        return
    h = title_hash(img); cache = _load_title_cache()
    for e in cache:
        if _hsim(h, e.get("h", [])) >= TITLE_SIM:
            if e.get("key") not in (key, "AMBIGUOUS"):
                print(f"    🏷 标题哈希撞车:「{e.get('key','')[:14]}」vs「{key[:14]}」→ 标为不可辨")
                e["key"] = "AMBIGUOUS"
                json.dump(cache, open(TITLE_CACHE_PATH, "w", encoding="utf-8"), ensure_ascii=False)
            return
    cache.append({"h": h, "key": key, "at": datetime.now().strftime("%m-%d %H:%M")})
    json.dump(cache[-80:], open(TITLE_CACHE_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"    🏷 学到标题:「{key[:16]}」")

def _keys_compatible(a: str, b: str) -> bool:
    ka, kb = _mem_key(a), _mem_key(b)
    return bool(ka and kb) and (ka == kb or ka in kb or kb in ka)


# ── 熔断:同一会话连续发送失败到阈值就冷却,别无限重试 ────────────────
# 洞:发送失败不记 seen → 下轮再进去重试。某会话一旦卡住(错屏/IME坏/拟回有问题),
# 会一轮一轮无限怼同一条(历史上「群聊(5)」连续失败过 14 次)。既浪费又可能反复发半成品。
# 参考 WeIX「连续失败N次暂停」。冷却期间该会话跳过发送;冷却到点自动恢复;首次熔断推飞书。
_send_fail = {}            # 会话key -> {"n": 连续失败次数, "until": 冷却截止时间戳}
BREAK_AFTER = 4            # 连续失败这么多次就熔断
BREAK_COOLDOWN = 1800      # 熔断后冷却秒数(30min)

def _breaker_blocked(key: str) -> bool:
    """这个会话现在处于冷却期吗(冷却中就别再试发送)。"""
    r = _send_fail.get(key)
    return bool(r and r.get("until", 0) > time.time())

def _breaker_on_fail(key: str, conv_disp: str):
    r = _send_fail.setdefault(key, {"n": 0, "until": 0})
    r["n"] += 1
    if r["n"] >= BREAK_AFTER and r["until"] <= time.time():
        r["until"] = time.time() + BREAK_COOLDOWN
        print(f"    🔌 熔断:「{conv_disp[:16]}」连续失败 {r['n']} 次 → 冷却 {BREAK_COOLDOWN//60} 分钟")
        notify_feishu("🔌 微信分身:有个会话一直发不出去",
                      [f"会话:{conv_disp}",
                       f"连续发送失败 {r['n']} 次,已暂停 {BREAK_COOLDOWN//60} 分钟。",
                       "常见原因:手机停在别的界面、输入法被重置、或那条回复内容有问题。",
                       f"时间:{datetime.now().strftime('%m-%d %H:%M')}"])

def _breaker_on_ok(key: str):
    _send_fail.pop(key, None)      # 一成功就清零


_precheck_streak = {}      # 每会话连续被"预检跳过"的次数(到阈值强制走一次 LLM 兜底)
# ⚠️ 会话名有 OCR 变体,这个 dict 的 key 会越攒越多;超过 200 就整体清空(重新计数无害)
PRECHECK_MAX_STREAK = 5

def process_one_conversation(seen: set, conv_hint: str = "", at_me: bool = False):
    """当前已在某个对话内:预检 → 调记忆/指针 → 读→决策→(发/记忆)。"""
    # 只看当前一屏(多图历史会让 M3 思考溢出→解析失败→不回复;记忆档已覆盖"记事"需求)
    img0 = screencap()
    # 先把未播放的语音转成文字,再做后面所有判断——转写会永久留在气泡下面,
    # 于是预检/决策/记忆全都能像普通文字一样处理,不用为语音单开一条路
    _voiced, img0 = convert_voices(img0)
    # ② 进门先认门牌:标题哈希说这是别的会话 → **不加载上下文、不决策、不说话**,直接退。
    #    这是"点错会话后遗症"的正解——把身份判断挪到上下文之前,且不依赖 LLM。
    ident, isim = identify_by_title(img0)
    if ident and conv_hint and not _keys_compatible(ident, conv_hint):
        print(f"    🏷 门牌对不上:以为进的是「{conv_hint[:14]}」,标题哈希说是「{ident[:14]}」"
              f"({isim:.2f})→ 本轮不处理")
        return {"_action": "skip_点错行"}
    ctx_key = conv_hint                       # 加载上下文用的键;认出门牌就用门牌
    if ident:
        ctx_key = ident
    pk = _mem_key(conv_hint)
    # ① 廉价预检:最后一条是我自己发的 → 直接跳,不烧 LLM(60%+ 的轮次都是这种空转)。
    #    保险丝:被@的一律走 LLM;同一会话连跳 PRECHECK_MAX_STREAK 次后强制让 LLM 复核一次。
    pre_verdict = last_bubble_is_mine(img0)      # 存下来,和 LLM 的结论比对(见下面的埋点)
    # ⚠️ 以前是"被@的一律走 LLM,不预检"。但实测最活跃的两个群**每一轮都挂着 (@我)**
    #    (微信的[有人@我]标记很粘),于是每次访问必烧一次 LLM —— 这才是 2661 次
    #    "走了LLM才判出我方最后发言"的大头,不是预检判错。
    #    现在 @我 的会话也走预检,只是**复核更勤**(连跳 2 次就强制 LLM),兼顾省钱和安全:
    #    最后一条既然是我自己发的,那个 @ 要么早回过了,要么再接就成了自言自语(persona 已禁)。
    cap = 2 if at_me else PRECHECK_MAX_STREAK
    if _precheck_streak.get(pk, 0) < cap:
        if pre_verdict is True:
            _precheck_streak[pk] = _precheck_streak.get(pk, 0) + 1
            print(f"  [{conv_hint}] 预检·我方最后发言,跳过(省一次LLM)")
            return {"_action": "skip_预检我方最后发言"}
    _precheck_streak[pk] = 0
    if len(_precheck_streak) > 200:
        _precheck_streak.clear(); _precheck_streak[pk] = 0
    imgs = [img0]
    mem = get_memory(ctx_key) if ctx_key else ""   # 调该会话的历史记忆(按认出的门牌取)
    ptr = get_pointer(ctx_key) if ctx_key else ""  # 上次处理到哪条(方案A)
    gag = get_gags(ctx_key) if ctx_key else ""     # 这个群的老梗(用来回调)
    todo = get_todos(ctx_key) if ctx_key else ""   # 答应了还没兑现的事
    may_img = bool(ctx_key) and is_fun_group(ctx_key) and img_quota_ok(ctx_key)
    d = read_and_decide(imgs, memory_text=mem, pointer_text=ptr,
                        gag_text=gag, todo_text=todo, may_offer_image=may_img)
    conv = d.get("conversation") or conv_hint or "?"
    msg = d.get("last_customer_msg", "") or ""

    # 只在"最后一条是顾客发的 + 需要回 + 这条没处理过"时才动作
    fp = fingerprint(conv, msg, img0)
    d["_fingerprint"] = fp
    d["_action"] = "none"
    d["_at_me"] = bool(at_me)
    d["_pre"] = str(pre_verdict)

    # ⚠️ 一致性校验:M3 从屏幕读到的会话名 vs 我们以为点进了哪个会话。
    # 差太远 = 多半点错行了 —— 这时**绝不能把记忆/指针/待办写到 conv_hint 名下**,
    # 否则错误会沉淀进档案,下次继续拿错的背景喂,越错越深(梗污染就是这么来的)。
    from difflib import SequenceMatcher as _SM
    _a = re.sub(r"\s+", "", conv_hint or ""); _b = re.sub(r"\s+", "", conv or "")
    # ⚠️ 微信对没起名的群就显示「群聊(5)」这种通用标题,那是**真实标题**不是读错。
    #    不排除掉的话,那些群会被永远判成"错位"、永远不写记忆(我第一版就踩了)。
    # ⚠️ M3 常把标题写成「群聊(5) - 酒圈朋友群」「群聊(3)(王乐群)」这种"通用标题+自己补的描述",
    #    只认纯 `群聊(5)` 会把它们全判成错位 → 那些群永远不写记忆(我第二版又踩了一次)。
    _generic = bool(re.match(r"^(群聊|微信对话|未知对话|聊天|群)", _b))
    mismatch = bool(_a and _b and _b != "?" and not _generic
                    and (_b not in _a) and (_a not in _b)
                    and _SM(None, _a, _b).ratio() < 0.34)
    if mismatch:
        print(f"    ⚠️ 会话对不上:以为进的是「{conv_hint[:16]}」,屏幕上是「{conv[:16]}」→ 本轮不写档案")


    if d.get("_skip_seen"):                     # 读取失败/图被MiniMax判敏感 → 跳过并记 seen,别死循环刷错
        d["_action"] = "skip_" + (d.get("reason") or "读取失败")[:10]
        # 用 conv_hint 兜底做指纹(读失败时 conv/msg 都空),避免每轮重刷
        seen.add(fingerprint(conv_hint or conv, "", img0))
    elif is_no_reply(conv) or is_no_reply(conv_hint):
        # 第二道防线:列表行名被 M3 认错时(如「小仙儿」→「小仙女」)第一道会漏,
        # 但进来后读到的对话标题通常是准的 —— 进都进来了,至少别回复
        d["_action"] = "skip_不回复名单"
        seen.add(fp)
    elif d.get("is_official") or conv in ("公众号", "订阅号", "服务通知"):
        d["_action"] = "skip_公众号/推送"      # 机主要求:公众号一律不回
    elif not d.get("last_from_customer"):
        d["_action"] = "skip_我方最后发言"
        # 📊 埋点:LLM 说"我方最后发言",但预检没拦住 → 这就是预检漏掉的样本。
        # 命中率只有 27%(1006 拦下 vs 2661 漏掉),但**盲调参数只会越调越糟**,
        # 先把真实失败画面存下来当测试集,拿数据说话。存满 30 张就停。
        if pre_verdict is not True:
            try:
                d["_pre"] = str(pre_verdict)
                sdir = os.path.join(HERE, "samples_precheck")
                os.makedirs(sdir, exist_ok=True)
                if len([f for f in os.listdir(sdir) if f.endswith(".png")]) < 30:
                    img0.save(os.path.join(sdir, f"miss_{pre_verdict}_{int(time.time())}.png"))
            except Exception:
                pass
    elif fp in seen:
        d["_action"] = "skip_已处理"
    elif d.get("needs_human") and not (d.get("need_reply") and d.get("reply")):
        # 纯粹需本人处理、又没想好能说的话 → 不发,留给机主
        d["_action"] = "flag_需人工"
        # 推飞书:同一条消息只推一次(用 "fs:"+指纹 存进 seen 去重,不影响决策逻辑本身)
        nkey = "fs:" + fp
        if nkey not in seen:
            if notify_feishu("🔔 微信分身:有条消息要你本人处理",
                             [f"会话:{conv_hint or conv}",
                              f"对方:{msg[:150]}",
                              f"原因:{(d.get('reason') or '')[:100]}",
                              f"时间:{datetime.now().strftime('%m-%d %H:%M')}"]):
                seen.add(nkey)
                print("    📨 已推飞书")
    elif d.get("need_reply"):
        reply = d.get("reply") or ""
        spontaneous = bool(d.get("offer_image")) and may_img
        if (d.get("wants_image") or spontaneous) and d.get("image_prompt") and not DRY_RUN:
            if spontaneous and not d.get("wants_image"):
                img_quota_use(conv_hint or conv)      # 主动发的才占限额,别人点名要的不占
                print("    🎭 主动甩梗图(白名单群 + 今日额度内)")
            # 对方让画图/做表情包 → 文生图并发出(先发一句话,再发图)
            print(f"    🎨 文生图:{str(d.get('image_prompt'))[:40]}")
            if reply:
                if not send_reply(reply):     # 发送键没出来时重试一次
                    time.sleep(1.5); send_reply(reply)
                time.sleep(1)
            ok_img = gen_and_send_image(str(d["image_prompt"]))
            d["_action"] = "已发图" if ok_img else "发图失败"
            d["_gen_image"] = True
            seen.add(fp)
            if not mismatch:          # 点错行那轮别把指针写到错的会话(和主路径一致)
                set_pointer(conv_hint or conv, msg)
            log_decision(dict(d))
            print(f"  [{conv}] {d['_action']} | 画:{str(d.get('image_prompt'))[:30]}")
            return d
        if d.get("is_deep_q") and msg:
            # 正经业务/知识问题 → Kimi k3 + 联网深答(比 MiniMax 短回复有料得多)
            print(f"    🔎 深度问答(Kimi k3 联网)…问:{msg[:30]}")
            deep = deep_answer(msg, context=conv)
            if deep:
                reply = deep; d["reply"] = deep; d["_deep"] = True
        elif d.get("needs_image") and d.get("image_xy"):
            # 需要看清图才能答好的(介绍酒/翻译图里的字/看图上写啥)→ 点开大图看高清再答
            refined = open_image_and_reply(d["image_xy"], msg or "看看这张图并回应")
            if refined:
                reply = refined
                d["reply"] = refined
                d["_used_image"] = True
        if not reply:
            d["_action"] = "skip_无回复内容"; seen.add(fp)
        elif DRY_RUN:
            d["_action"] = "dry_run_拟回复"; seen.add(fp)
        else:
            bkey = _mem_key(conv_hint or conv)
            if _breaker_blocked(bkey):
                d["_action"] = "skip_熔断冷却中"   # 这个会话最近老发不出去,冷却期先不试
                seen.add(fp)                        # 记 seen,别本轮又当新消息反复评估
            else:
                ok = send_reply(reply)
                if ok:
                    d["_action"] = "已发送"
                    _breaker_on_ok(bkey)
                    if not mismatch:
                        append_memory(conv_hint or conv, msg, reply)   # 键与读取一致(都用列表名)
                    seen.add(fp)
                else:
                    d["_action"] = "发送失败"   # 不记 seen → 下轮重试(输入法修好后就能发出)
                    _breaker_on_fail(bkey, conv_hint or conv)
    else:
        d["_action"] = "skip_无需回复"
        seen.add(fp)

    # ⚠️ 梗的【自动写入已关闭】(2026-09-02)。
    # 原以为"只记情节不记身份"就安全,**错了**:梗里带人名,那就是伪装成情节的身份判断。
    # 实测后果:①同一个梗散落进 7 个群 ②点歪进去的陌生群也被写入
    # ③写进了假事实(把客服小c刷快团团记成"老曹刷了十遍"),然后当真事反复引用,
    #   导致它在粉丝群里管客服叫"老曹"。错误一旦写进档案就会自我强化。
    # 现在只读【人工核对过的】梗,想加梗直接编辑 gags.json。
    # if d.get("gag"):
    #     add_gag(conv_hint or conv, str(d["gag"]))

    # 未了之事:记新答应的、关掉已兑现的(点错行那轮不记)
    if not mismatch:
        if d.get("promise"):
            add_todo(conv_hint or conv, str(d["promise"]))
        if d.get("promise_done"):
            close_todo(conv_hint or conv, str(d["promise_done"]))

    # 学门牌:这轮没有会话错位 → 记下「标题哈希 → 会话」,下次进门零成本认出来
    if not mismatch and conv != "?":
        learn_title(img0, _mem_key(conv_hint or conv))

    # 方案A:不论回没回,都记下"处理到对方的哪条",下次进来才分得清哪些是新的
    if msg and d.get("last_from_customer") and not mismatch:
        set_pointer(conv_hint or conv, msg)
    # 记忆补全:没接话/转本人的也留一笔,免得下次上下文缺一块(机主要求记忆更完整)
    if d["_action"] in ("skip_无需回复", "flag_需人工") and msg:
        append_memory(conv_hint or conv, msg,
                      "(没接话)" if d["_action"] == "skip_无需回复" else "(标记转本人)")

    log_decision(dict(d))
    print(f"  [{conv}] {d['_action']} | 顾客:{msg[:30]} | 拟回:{(d.get('reply') or '')[:40]}")
    return d


def adb_alive() -> bool:
    """adb 是否连着目标设备(USB 掉线/手机休眠时返回 False)。"""
    out = subprocess.run(["adb", "-s", ADB_SERIAL, "get-state"],
                         capture_output=True, text=True)
    return out.stdout.strip() == "device"


def main():
    mode = "影子模式(不发送)" if DRY_RUN else "⚡ 实发模式"
    print(f"启动 · {mode} · 设备 {ADB_SERIAL} · 人设 {'persona.md' if os.path.exists(PERSONA_PATH) else '内置温暖人格'}")
    if ":" in ADB_SERIAL:          # WiFi ADB:启动先连一次(launchd 重启后 adb server 是新的)
        subprocess.run(["adb", "connect", ADB_SERIAL], capture_output=True)
        time.sleep(1.5)
        if not adb_alive():        # 连不上 → 先试自动发现新IP(手机重启会换IP),再回退 USB
            newaddr = autodiscover_wifi()
            if newaddr:
                globals()["ADB_SERIAL"] = newaddr
            else:
                globals()["ADB_SERIAL"] = ADB_USB_FALLBACK
                print(f"⚠️ 无线连不上,回退 USB {ADB_USB_FALLBACK}")

    seen = load_seen()
    # 启动立刻写一次心跳:否则从启动到跑完第一轮之间是空窗,看门狗会误报"停摆"
    beat(adb_alive(), 0, "启动")
    if not DRY_RUN:
        set_ime(IME_ADB)   # 实发才需要切 ADBKeyboard

    fails = 0
    cycle = 0
    alerted_down = False       # 掉线告警只推一次,恢复后复位
    quiet = 0                  # 被廉价门挡掉的轮数(省下的 M3 读列表调用)
    last_alert_s = 0           # 上次掉线告警时已断连的秒数(用于每6小时重播)
    rotate_logs()              # 启动先看一眼日志要不要切
    try:
        while True:
            try:
                if not adb_alive():
                    fails += 1
                    if fails == 1 or fails % 12 == 0:   # 不刷屏,首次和每隔一阵提示一次
                        print(f"⚠️ adb 未连接设备 {ADB_SERIAL},自动尝试重连中…(硬掉线才需插线/检查手机)")
                    beat(False, cycle, f"adb断连 {fails * POLL_INTERVAL // 60}分钟",
                         down_secs=fails * POLL_INTERVAL)
                    # 掉线超 5 分钟 → 推飞书。⚠️ 之后每 6 小时**重播一次**:
                    # 只推一次的话,那一条被漏看/发送失败,就再也没有第二次提醒了(踩过)
                    due = fails * POLL_INTERVAL >= 300 and (
                        not alerted_down or fails * POLL_INTERVAL - last_alert_s >= 21600)
                    if due:
                        alerted_down = True
                        last_alert_s = fails * POLL_INTERVAL
                        notify_feishu("🔴 微信分身:手机连不上了",
                                      [f"设备 {ADB_SERIAL} 已断开约 {fails * POLL_INTERVAL // 60} 分钟,自动重连中。",
                                       "分身现在收不到也回不了微信。",
                                       "常见原因:手机关机/离开WiFi、路由器换了IP、手机重启丢了tcpip。",
                                       f"时间:{datetime.now().strftime('%m-%d %H:%M')}"])
                        print(f"    📨 掉线告警已推飞书(断连 {fails * POLL_INTERVAL // 60} 分钟)")
                    # 主动自愈
                    if ":" in ADB_SERIAL:      # WiFi ADB:直接重连无线地址(WiFi 抖动/手机休眠回来后自愈)
                        subprocess.run(["adb", "connect", ADB_SERIAL], capture_output=True)
                    subprocess.run(["adb", "reconnect", "offline"], capture_output=True)
                    if fails % 6 == 0:                    # 每隔一阵重启 adb server,治更顽固的软掉线
                        subprocess.run(["adb", "kill-server"], capture_output=True)
                        subprocess.run(["adb", "start-server"], capture_output=True)
                        if ":" in ADB_SERIAL:
                            subprocess.run(["adb", "connect", ADB_SERIAL], capture_output=True)
                    if fails % 6 == 3 and ":" in ADB_SERIAL:
                        # 手机重启后 IP 会变、tcpip 模式也会丢 → 借 USB 自动发现新地址重连
                        newaddr = autodiscover_wifi()
                        if newaddr:
                            globals()["ADB_SERIAL"] = newaddr
                            fails = 0
                            continue
                    time.sleep(POLL_INTERVAL)
                    continue
                if fails:
                    print("✅ 设备已重连,继续轮询")
                    if alerted_down:            # 之前报过障 → 报个恢复,别让机主一直悬着
                        notify_feishu("🟢 微信分身:手机已重连",
                                      [f"设备 {ADB_SERIAL} 恢复,分身继续工作。",
                                       f"时间:{datetime.now().strftime('%m-%d %H:%M')}"])
                        alerted_down = False
                        print("    📨 恢复通知已推飞书")
                    fails = 0; last_alert_s = 0

                # 唤醒保活:每 ~3 分钟戳醒一次手机,防 ColorOS 整机深睡切断 USB(配合"不锁定屏幕")
                cycle += 1
                if cycle % 18 == 0:
                    adb("shell", "input", "keyevent", "224")   # KEYCODE_WAKEUP,亮屏但不解锁,无副作用
                if cycle % 360 == 0:                           # 约每小时看一次日志要不要轮转
                    rotate_logs()

                open_wechat_list()
                # 巡逻轮:额外把列表顶部几个会话也过一遍,补"标已读后红点消失→永远沉底"的漏。
                # 进对话现在有廉价预检兜着(没新消息几乎不花钱),所以巡逻很便宜。
                patrol = (cycle % PATROL_EVERY == 0)
                list_img = screencap()
                # 廉价门:非巡逻轮 + 列表上没有任何红色信号 → 直接睡,不烧 M3 读列表。
                # 这是目前最大的一项节省(以前每 10 秒无条件一次 M3 视觉调用)。
                if not patrol and not list_has_unread_signal(list_img):
                    quiet += 1
                    if quiet % 30 == 0:
                        print(f"· 安静中(已省下 {quiet} 次读列表调用)")
                    # ⚠️ 安静轮也必须写心跳!这里是 continue,漏写的话——
                    #    群里一安静超过 25 分钟,心跳就过期,看门狗立刻误报"停摆"(晚上必天天误报)。
                    #    "没消息可处理"和"分身死了"是两回事,心跳要能区分。
                    beat(True, cycle, "安静轮")
                    time.sleep(POLL_INTERVAL)
                    continue
                beat(True, cycle, "读列表中")      # 读列表本身可能很慢,先把心跳顶上
                rows = scan_list_with_llm(list_img, patrol=patrol)  # M3 读列表:认未读/@我,排除公众号
                def _skip_row(nm):
                    return is_official_name(nm) or is_no_reply(nm)
                dropped = [r.get("name", "") for r in rows if _skip_row(r.get("name", ""))]
                if dropped:
                    rows = [r for r in rows if not _skip_row(r.get("name", ""))]
                    print(f"· 跳过(不进去):{', '.join(d[:14] for d in dropped)}")
                if rows:
                    tag = ", ".join(f"{r.get('name','?')}{'(@我)' if r.get('at_me') else ''}" for r in rows)
                    print(f"·{'[巡逻] ' if patrol else ' '}待处理 {len(rows)} 个会话:{tag}")
                for r in rows:
                    # ❌ 2026-09-04 回滚:曾经改成"优先用 M3 给的行序号 row 定位",结果更糟——
                    #    M3 数的行和 list_row_centers() 的网格对不上,实测系统性偏低:
                    #    +1行 798次、+2行 346次、+3行 68次。
                    #    (事后验证:网格本身是对的——多张真实截图都是 [459,653,847,…],
                    #     顶部横幅没被算进去。所以是 **M3 自己数错了行**,不是网格的锅。)
                    #    后果:会话对不上 127→234,当天发送数 → 0。
                    #    教训:**换定位方案前先量偏差分布**,别拿一个没验证的坐标去覆盖旧的。
                    ry, off = snap_row_y(list_img, int(r.get("y", 0)))
                    # ⚠️ 行高 194px,偏移接近半行(>70px)时吸附到哪一行基本是在赌,
                    #    赌错就点进隔壁会话(实测「某甲→公众号」那次就是偏 97px)。
                    #    宁可这轮不点(下轮 M3 会重新给坐标),也别赌。
                    if off is not None and off > 70:
                        print(f"    ⟳ 行坐标偏 {off}px(超过半行),吸附不可靠 → 跳过这行")
                        continue
                    if off is not None and off > 40:
                        print(f"    ↕ 行坐标吸附:{r.get('y')} → {ry}(偏 {off}px)")
                    # ⚠️ 点之前复核:读列表花了几十秒,期间来条新消息整个列表就会下移一行。
                    #    不复核的话就会点到隔壁(实测 127 次)。
                    fp_then = row_fingerprint(list_img, ry)
                    fresh = screencap()
                    if not row_unchanged(fresh, ry, fp_then):
                        # ⚠️ 列表在这几十秒里变了。但它通常只是**整体移了一格**(来了条新消息把某个
                        #    会话顶到最上面),目标行还在,只是换了位置 —— 所以**去附近找回来**,
                        #    别直接放弃。(第一版这里 write `break`,一触发就把整轮剩下的会话全丢掉,
                        #    早上群里一活跃就几乎什么都不处理,是个比原问题更糟的回归。)
                        grid_now = list_row_centers(fresh)
                        cand = [g for g in grid_now if abs(g - ry) <= 194 * 3]
                        found = next((g for g in sorted(cand, key=lambda g: abs(g - ry))
                                      if row_unchanged(fresh, g, fp_then)), None)
                        if found is None:
                            print(f"    ⟳ 列表变了且没找回目标行(y={ry})→ 跳过这行")
                            continue        # 只跳这一行,别把整轮都放弃
                        print(f"    ⟳ 列表移位:目标行 {ry} → {found},已跟上")
                        ry = found
                    tap(320, ry)                   # 进入该会话(x=320 避开头像)
                    time.sleep(1.2)
                    try:
                        process_one_conversation(seen, conv_hint=r.get("name", ""),
                                                 at_me=bool(r.get("at_me")))
                    except Exception as e:
                        print(f"    处理出错:{e}")
                    save_seen(seen)
                    # ⚠️ 每个会话处理完就写心跳:一轮里若有好几个会话、每个都要调 LLM,
                    #    整轮可能几分钟甚至更久;心跳只在轮末写的话,看门狗会把"忙"误判成"死"
                    beat(True, cycle, "处理中")
                    back()                 # 退回列表
                    time.sleep(0.8)
                beat(True, cycle)          # 完整跑完一轮且 adb 通 = 真的活着
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # adb 抽风/截图空/任何意外 → 记下、等一下、继续,绝不让整个 bot 挂掉
                print(f"[轮询出错] {type(e).__name__}: {e},{POLL_INTERVAL}s 后重试")
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\n退出中…")
    finally:
        if not DRY_RUN:
            set_ime(IME_SOGOU)  # 还原输入法
        save_seen(seen)
        print("已保存指纹表,输入法已还原。")


if __name__ == "__main__":
    main()
