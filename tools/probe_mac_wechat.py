#!/usr/bin/env python3
"""电脑版微信可行性探针 —— 决定要不要把分身从手机迁到桌面。

**要验证什么**(通不过就别迁):
  ① 能不能读到微信的【完整】无障碍树(会话名、消息文字,不被屏蔽)
     —— 这是最大的收益:不用截图识别,会话名不再被 OCR 认错,记忆不会被切碎
  ② 能不能【准确点中】某个会话(手机端为此写了三道防线)
  ③ 能不能输入中文并【回车发送】(手机端 83 次发送失败全是这块)
  ④ 能不能找到并【打开图片/视频】看内容(机主明确要求:别人发的图和视频要点开看完)
  ⑤ 语音条能不能找到「转文字」(机主确认桌面版有这个功能)

用法:
    python3 tools/probe_mac_wechat.py            # 只读探测,不点不发,安全
    python3 tools/probe_mac_wechat.py --tree     # 额外打印完整 AX 树(调试用)
    python3 tools/probe_mac_wechat.py --click "文件传输助手"   # 试着点开某个会话
    python3 tools/probe_mac_wechat.py --send "测试"            # 在【当前】会话发一条(慎用)

⚠️ 首次运行会提示没有辅助功能授权,按提示去系统设置里开。
"""
import subprocess
import sys
import time

try:
    from ApplicationServices import (
        AXValueGetValue, kAXValueCGPointType, kAXValueCGSizeType,
        AXIsProcessTrusted, AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue, AXUIElementSetAttributeValue,
        AXUIElementPerformAction, kAXChildrenAttribute, kAXRoleAttribute,
        kAXTitleAttribute, kAXValueAttribute, kAXDescriptionAttribute,
        kAXPositionAttribute, kAXSizeAttribute, kAXPressAction,
        kAXFocusedAttribute, kAXSelectedAttribute,
    )
    from AppKit import NSWorkspace
    import Quartz
except ImportError as e:
    sys.exit(f"❌ 缺依赖:{e}\n   pip3 install pyobjc-framework-ApplicationServices pyobjc-framework-Cocoa")

BUNDLE = "com.tencent.xinWeChat"


def need_permission():
    print("❌ 这个 Python 还没有【辅助功能】授权,读不到微信的界面。\n")
    print("请去开一下(一次性):")
    print("  系统设置 → 隐私与安全性 → 辅助功能 → 点 + 号")
    print("  把下面这个程序加进去并打勾:")
    print(f"     {sys.executable}")
    print("\n  (如果列表里加不进去,可以先把【终端】/【Claude】整个 app 加进去试试——")
    print("   授权是按【发起进程的宿主 app】算的)")
    print("\n开完再跑一次这个脚本。")


def wechat_pid():
    for a in NSWorkspace.sharedWorkspace().runningApplications():
        if a.bundleIdentifier() == BUNDLE:
            return a.processIdentifier()
    return None


def attr(el, name):
    try:
        err, val = AXUIElementCopyAttributeValue(el, name, None)
        return val if err == 0 else None
    except Exception:
        return None


def kids(el):
    return attr(el, kAXChildrenAttribute) or []


def info(el):
    """把一个元素的关键属性抓成 dict —— 注意 title/value/description 三个都要看,
    微信把文字放在哪个属性里并不统一。"""
    d = {"role": attr(el, kAXRoleAttribute)}
    for k, a in (("title", kAXTitleAttribute), ("value", kAXValueAttribute),
                 ("desc", kAXDescriptionAttribute)):
        v = attr(el, a)
        if v is not None:
            d[k] = str(v)[:200]
    # ⚠️ AXPosition/AXSize 是 AXValue 包装的,**必须用 AXValueGetValue 解包**,
    #    直接读 .x/.y 会静默失败(第一版就这么错的,导致所有 rect 都是 None、
    #    会话行一个都认不出来)。
    pos, siz = attr(el, kAXPositionAttribute), attr(el, kAXSizeAttribute)
    if pos is not None and siz is not None:
        try:
            okp, pt = AXValueGetValue(pos, kAXValueCGPointType, None)
            oks, sz = AXValueGetValue(siz, kAXValueCGSizeType, None)
            if okp and oks:
                d["rect"] = (int(pt.x), int(pt.y), int(sz.width), int(sz.height))
        except Exception as e:
            d["rect_err"] = str(e)[:40]
    return d


def walk(el, depth=0, maxdepth=14, out=None):
    if out is None:
        out = []
    if depth > maxdepth:
        return out
    d = info(el)
    d["depth"] = depth
    d["_el"] = el
    out.append(d)
    for c in kids(el):
        walk(c, depth + 1, maxdepth, out)
    return out


def text_of(d):
    return d.get("title") or d.get("value") or d.get("desc") or ""


def main():
    if not AXIsProcessTrusted():
        need_permission()
        return 1

    pid = wechat_pid()
    if not pid:
        print("❌ 微信没在运行。先打开电脑版微信。")
        return 1
    print(f"✅ 找到微信 (pid={pid}),已获辅助功能授权\n")

    app = AXUIElementCreateApplication(pid)
    nodes = walk(app)
    print(f"AX 树节点数:{len(nodes)}")

    if "--tree" in sys.argv:
        for d in nodes:
            t = text_of(d)
            print("  " * d["depth"] + f"{d.get('role')} {d.get('rect','')} {t[:60]}")

    # ── ① 文字可读性:有多少节点带非空文字 ─────────────────────────
    texts = [d for d in nodes if text_of(d).strip()]
    print(f"\n① 文字可读性:{len(texts)}/{len(nodes)} 个节点有文字")
    print("   样例(前 15 条):")
    for d in texts[:15]:
        print(f"     [{d.get('role')}] {text_of(d)[:70]}")

    # ── ② 会话列表 ────────────────────────────────────────────────
    # 微信左侧会话列表通常是 AXList/AXTable,行里带会话名
    lists = [d for d in nodes if d.get("role") in ("AXList", "AXTable", "AXOutline")]
    print(f"\n② 找到 {len(lists)} 个列表容器")
    convs = []
    for L in lists:
        r = L.get("rect")
        if not r or r[2] > 500:          # 会话列表窄(实测约 240pt),右侧消息区宽
            continue
        for c in kids(L["_el"]):
            for d in walk(c, maxdepth=4):
                t = text_of(d).strip()
                if t and d.get("rect") and d["rect"][2] > 100:
                    convs.append((t, d["rect"], d["_el"]))
                    break
    seen = set(); uniq = []
    for t, r, e in convs:
        if t not in seen:
            seen.add(t); uniq.append((t, r, e))
    print(f"   会话行:{len(uniq)} 个")
    for t, r, e in uniq[:12]:
        print(f"     y={r[1]:4d}  {t[:50]}")

    # ── ③ 输入框 ──────────────────────────────────────────────────
    inputs = [d for d in nodes if d.get("role") == "AXTextArea"]
    print(f"\n③ 输入框(AXTextArea):{len(inputs)} 个")
    for d in inputs:
        print(f"     {d.get('rect')} value={repr(text_of(d))[:40]}")

    # ── ④ 图片/视频消息 ───────────────────────────────────────────
    # 机主明确要求:别人发的图和视频必须点开看完。先看这类消息在树里长什么样。
    media = [d for d in nodes
             if d.get("role") in ("AXImage", "AXButton", "AXGroup")
             and any(k in text_of(d) for k in ("图片", "视频", "照片", "Image", "Video", "表情"))]
    print(f"\n④ 疑似图片/视频节点:{len(media)} 个")
    for d in media[:10]:
        print(f"     [{d.get('role')}] {d.get('rect')} {text_of(d)[:50]}")
    imgs = [d for d in nodes if d.get("role") == "AXImage"]
    print(f"   AXImage 节点共 {len(imgs)} 个(前 6 个):")
    for d in imgs[:6]:
        print(f"     {d.get('rect')} {text_of(d)[:50]}")

    # ── ⑤ 语音 / 转文字 ───────────────────────────────────────────
    voice = [d for d in nodes if any(k in text_of(d) for k in ("语音", "转文字", "秒"))]
    print(f"\n⑤ 疑似语音/转文字节点:{len(voice)} 个")
    for d in voice[:8]:
        print(f"     [{d.get('role')}] {d.get('rect')} {text_of(d)[:50]}")

    # ── 可选:试点一个会话 ────────────────────────────────────────
    if "--click" in sys.argv:
        want = sys.argv[sys.argv.index("--click") + 1]
        hit = next((e for t, r, e in uniq if want in t), None)
        if not hit:
            print(f"\n🖱 没找到会话「{want}」")
        else:
            err = AXUIElementPerformAction(hit, kAXPressAction)
            print(f"\n🖱 AXPress「{want}」→ {'成功' if err == 0 else f'失败 err={err}'}")
            time.sleep(1.2)
            nodes2 = walk(AXUIElementCreateApplication(pid))
            t2 = [text_of(d) for d in nodes2 if text_of(d).strip()][:6]
            print("   点击后顶部文字:", t2)

    # ── 可选:试发一条 ────────────────────────────────────────────
    if "--send" in sys.argv:
        msg = sys.argv[sys.argv.index("--send") + 1]
        box = next((d for d in inputs if d.get("rect") and d["rect"][2] > 300), None)
        if not box:
            print("\n✉️ 没找到消息输入框")
        else:
            AXUIElementSetAttributeValue(box["_el"], kAXFocusedAttribute, True)
            ok = AXUIElementSetAttributeValue(box["_el"], kAXValueAttribute, msg)
            print(f"\n✉️ 写入输入框 → err={ok}")
            time.sleep(0.4)
            # 回车发送(CGEvent 键盘事件,keycode 36 = Return)
            for down in (True, False):
                ev = Quartz.CGEventCreateKeyboardEvent(None, 36, down)
                Quartz.CGEventPostToPid(pid, ev)
                time.sleep(0.05)
            time.sleep(1.0)
            after = walk(AXUIElementCreateApplication(pid))
            box2 = next((d for d in after if d.get("role") == "AXTextArea"
                         and d.get("rect") and d["rect"][2] > 300), None)
            left = text_of(box2) if box2 else "?"
            print(f"   发送后输入框内容 = {repr(left)[:60]}")
            print("   " + ("✅ 输入框空了,应该发出去了" if not left.strip()
                           else "❌ 文字还在框里,回车没发出去"))

    print("\n" + "=" * 60)
    print("判断标准:")
    print("  ① 文字能读到、且【不被屏蔽】     → 省掉视觉模型,记忆不再被 OCR 切碎")
    print("  ② 会话行能列出并能 AXPress 点中   → 点错行的问题消失")
    print("  ③ 输入框能写、回车能发            → 83 次发送失败的问题消失")
    print("  ④ 图片/视频节点能定位并点开       → 机主要求的'看图看视频'能做")
    print("  ⑤ 语音条能找到「转文字」          → 语音功能能保住")
    return 0


if __name__ == "__main__":
    sys.exit(main())
