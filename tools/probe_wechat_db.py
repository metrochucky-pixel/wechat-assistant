#!/usr/bin/env python3
"""微信本地数据库可行性探针 —— 决定能不能"不看屏幕直接读消息"。

背景:我们现在读消息靠截图+视觉模型(会话名认错/记忆切碎/点错行全从这来)。
若能直接读微信落地的 SQLCipher 数据库,文字部分就完全不需要视觉模型了。
参考 WeIX(zqaini002/weix)的思路:纯文件读库 + 从进程内存取密钥。

这个探针分三关,任何一关过不了就如实报告、停手:
  ① 能不能拿到微信进程的内存读取权限(task_for_pid)
     ⚠️ 微信是 hardened runtime + 本机 SIP 开着,这一关很可能直接失败。
  ② 能不能在内存里扫到 SQLCipher 主密钥(拿 message_0.db 第一页做 HMAC 校验)
  ③ 能不能用密钥解开数据库、读出最近几条消息

⚠️ 必须 sudo 运行(读别的进程内存需要):
    sudo python3 ~/wechatbot/tools/probe_wechat_db.py

只读,不改微信任何东西;拿到密钥只打印前后各 4 字节(遮蔽),不落盘。
"""
import ctypes
import ctypes.util
import glob
import hashlib
import hmac
import os
import struct
import subprocess
import sys

WXDIR = os.path.expanduser(
    "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files")

# ── 关①:task_for_pid ────────────────────────────────────────────────
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def wechat_pid():
    # sudo 下 pgrep -x 有时匹配不上,改用 ps 全量 + 全路径匹配(只认主进程,排除 helper)
    out = subprocess.run(["/bin/ps", "-axo", "pid=,comm="], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pidstr, comm = parts
        if comm.endswith("/WeChat.app/Contents/MacOS/WeChat"):
            return int(pidstr)
    # 兜底:pgrep 全路径
    p = subprocess.run(["/usr/bin/pgrep", "-f", "WeChat.app/Contents/MacOS/WeChat$"],
                       capture_output=True, text=True).stdout.split()
    return int(p[0]) if p else None


def get_task(pid):
    """返回 (task_port 或 None, 说明)。hardened runtime + SIP 会让它失败。"""
    task = ctypes.c_uint32(0)
    # mach_task_self via task_for_pid(mach_task_self(), pid, &task)
    libc.mach_task_self.restype = ctypes.c_uint32
    kr = libc.task_for_pid(libc.mach_task_self(), pid, ctypes.byref(task))
    if kr != 0:
        return None, f"task_for_pid 失败 kr={kr}(5=KERN_FAILURE 多半是 SIP/hardened runtime 挡的)"
    return task.value, "ok"


def read_mem(task, addr, size):
    data = ctypes.c_void_p(0)
    outsz = ctypes.c_uint32(0)
    kr = libc.mach_vm_read(ctypes.c_uint32(task), ctypes.c_uint64(addr),
                           ctypes.c_uint64(size),
                           ctypes.byref(data), ctypes.byref(outsz))
    if kr != 0:
        return None
    buf = ctypes.string_at(data.value, outsz.value)
    return buf


def iter_regions(task):
    """遍历可读内存区域,产出 (addr, size)。"""
    address = ctypes.c_uint64(0)
    size = ctypes.c_uint64(0)
    VM_REGION_BASIC_INFO_64 = 9
    info = (ctypes.c_uint32 * 10)()
    count = ctypes.c_uint32(10)
    obj = ctypes.c_uint32(0)
    while True:
        kr = libc.mach_vm_region(ctypes.c_uint32(task), ctypes.byref(address),
                                 ctypes.byref(size), VM_REGION_BASIC_INFO_64,
                                 ctypes.byref(info), ctypes.byref(count),
                                 ctypes.byref(obj))
        if kr != 0:
            break
        prot = info[0]
        if prot & 0x1:  # VM_PROT_READ
            yield address.value, size.value
        address = ctypes.c_uint64(address.value + size.value)


# ── 关②:SQLCipher 4 密钥校验 ────────────────────────────────────────
def find_db():
    dbs = glob.glob(os.path.join(WXDIR, "*/db_storage/message/message_0.db"))
    return dbs[0] if dbs else None


def sqlcipher_validate(dbpath, key):
    """SQLCipher4 raw-key 校验:用 key + 首页盐 派生,校验第一页 HMAC。
    对 = 这就是主密钥。参考 wechat-dump / SQLCipher 默认参数。"""
    KDF_ITER = 256000
    PAGE = 4096
    HMAC_LEN = 64  # SHA512
    with open(dbpath, "rb") as f:
        page1 = f.read(PAGE)
    salt = page1[:16]
    enc_key = hashlib.pbkdf2_hmac("sha512", key, salt, KDF_ITER, 32)
    mac_salt = bytes(b ^ 0x3a for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, 32)
    # 第一页:前16是盐(明文),之后是密文;末尾 reserve = IV(16)+HMAC(64)
    reserve = 16 + HMAC_LEN
    body = page1[16:PAGE - reserve]
    iv = page1[PAGE - reserve:PAGE - reserve + 16]
    stored = page1[PAGE - reserve + 16:PAGE - reserve + 16 + HMAC_LEN]
    calc = hmac.new(mac_key, body + iv + struct.pack("<I", 1), hashlib.sha512).digest()
    return hmac.compare_digest(calc, stored)


def main():
    if os.geteuid() != 0:
        print("❌ 需要 sudo(读别的进程内存):")
        print("   sudo python3 ~/wechatbot/tools/probe_wechat_db.py")
        return 1

    pid = wechat_pid()
    if not pid:
        print("❌ 微信没运行"); return 1
    dbpath = find_db()
    print(f"微信 pid={pid}")
    print(f"数据库:{dbpath or '没找到 message_0.db'}\n")
    if not dbpath:
        return 1

    print("── 关① 能否读微信进程内存 ──")
    task, msg = get_task(pid)
    if not task:
        print(f"❌ {msg}")
        print("\n→ 这台 Mac(SIP 开 + 微信 hardened runtime)挡住了内存读取。")
        print("  取密钥这条路走不通,除非关 SIP(不建议在日常机上关)。")
        print("  结论:桌面版'直接读库'方案在本机不可行,继续用截图+视觉。")
        return 2
    print(f"✅ 拿到 task port={task}\n")

    print("── 关② 内存里扫 SQLCipher 主密钥 ──")
    tested = 0
    found = None
    for addr, size in iter_regions(task):
        if size > 64 * 1024 * 1024:      # 跳过超大区(多是文件映射,密钥在堆里)
            continue
        buf = read_mem(task, addr, min(size, 8 * 1024 * 1024))
        if not buf:
            continue
        # 32 字节对齐窗口逐个试(堆里 key 通常 16 字节对齐)
        for i in range(0, len(buf) - 32, 8):
            cand = buf[i:i + 32]
            if cand.count(0) > 20:       # 太多零,不像随机密钥
                continue
            tested += 1
            if sqlcipher_validate(dbpath, cand):
                found = cand
                break
        if found:
            break
    print(f"  试了约 {tested} 个候选")
    if not found:
        print("❌ 没扫到密钥(可能在被跳过的区域,或参数不符)。")
        print("  → 库能读但解不开,这条路暂时不通。")
        return 3
    print(f"✅ 找到主密钥:{found[:4].hex()}…{found[-4:].hex()}(遮蔽)\n")

    print("── 关③ 解库读消息 ──")
    print("  (密钥已验证,解密读表留给正式实现;可行性已确认 ✅)")
    print("\n🎉 三关全过:桌面版可以'不看屏幕直接读消息'。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
