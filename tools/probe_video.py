#!/usr/bin/env python3
"""视频抽帧可行性探针 —— 决定"看完整段视频再回复"怎么做。

**要验证什么**:
  ① 点开聊天里的视频,播放器是不是【自动播放】(是 → 只需连续截帧,不用碰进度条)
  ② 连续截的帧【画面会不会变】(变 = 真在播;不变 = 静态图或没播)
     —— 这同时也是【图 / 视频 自动判别器】:不用认播放器 UI,看画面动不动就行
  ③ 退出播放器要按几次返回才回到聊天(别把整个会话退出去)

用法(bot 必须先停,否则抢手机):
    python3 tools/probe_video.py <x> <y>        # 点这个坐标,抽帧
    python3 tools/probe_video.py <x> <y> 6 1.2  # 抽6帧、间隔1.2秒

帧存到 scratchpad/vid/f*.png,只读不发,不改微信任何东西。
"""
import io
import os
import subprocess
import sys
import time

from PIL import Image

OUT = "/tmp/wechatbot-video-probe"


def adb(*args, binary=False):
    r = subprocess.run(["adb"] + list(args), capture_output=True)
    return r.stdout if binary else r.stdout.decode("utf-8", "ignore")


def shot():
    for _ in range(3):
        try:
            im = Image.open(io.BytesIO(adb("exec-out", "screencap", "-p", binary=True)))
            im.load()
            return im.convert("RGB")
        except Exception:
            time.sleep(1.0)
    raise RuntimeError("截图失败")


def sig(img):
    """16×16 灰度粗签名,用于比较两帧差异。"""
    return list(img.resize((16, 16), Image.LANCZOS).convert("L").getdata())


def diff(a, b):
    """两帧平均逐点灰度差(0~255)。越大 = 画面变化越大。"""
    sa, sb = sig(a), sig(b)
    return sum(abs(x - y) for x, y in zip(sa, sb)) / len(sa)


def is_dark_bottom(img):
    """底部暗 = 全屏查看器(聊天底部是浅色输入栏)。"""
    px = img.load()
    W, H = img.size
    vals = [sum(px[x, y]) / 3 for y in range(H - 180, H - 60, 8) for x in range(100, W - 100, 20)]
    return (sum(vals) / len(vals)) < 120


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    x, y = int(sys.argv[1]), int(sys.argv[2])
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    gap = float(sys.argv[4]) if len(sys.argv) > 4 else 1.5
    os.makedirs(OUT, exist_ok=True)

    before = shot()
    before.save(f"{OUT}/f_before.png")
    print(f"点 ({x},{y}) …")
    adb("shell", "input", "tap", str(x), str(y))
    time.sleep(1.6)

    first = shot()
    opened = is_dark_bottom(first)
    print(f"① 全屏查看器打开了吗:{'✅ 是' if opened else '❌ 否'}"
          f"(相对点击前画面变化 {diff(before, first):.1f})")
    if not opened:
        print("   → 没点开。可能点空了,或这条不是图/视频。不再乱按,退出。")
        return 2

    print(f"② 连续抽 {n} 帧(间隔 {gap}s),看画面动不动:")
    frames = [first]
    first.save(f"{OUT}/f0.png")
    for i in range(1, n):
        time.sleep(gap)
        f = shot()
        d = diff(frames[-1], f)
        f.save(f"{OUT}/f{i}.png")
        frames.append(f)
        print(f"   帧{i}: 与上一帧差异 {d:6.2f}  {'← 画面在动(视频在播)' if d > 8 else '← 几乎没动'}")

    dmax = max(diff(frames[0], f) for f in frames[1:])
    print(f"\n   相对第0帧的最大变化:{dmax:.2f}")
    print(f"   判定:{'🎬 视频(在播放)' if dmax > 8 else '🖼 静态图'}")

    print("\n③ 退出:按一次返回,看是否回到聊天")
    adb("shell", "input", "keyevent", "4")
    time.sleep(1.2)
    after = shot()
    after.save(f"{OUT}/f_after.png")
    back_in_chat = not is_dark_bottom(after)
    print(f"   回到聊天了吗:{'✅ 是' if back_in_chat else '❌ 否(还在全屏,可能要再按一次)'}")
    print(f"   和点击前画面差异:{diff(before, after):.1f}(≈0 = 完好回到原位)")
    print(f"\n帧已存到 {OUT}/f*.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
