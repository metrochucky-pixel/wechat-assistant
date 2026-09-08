#!/usr/bin/env python3
"""会话名 key 的定期体检 —— 把 OCR 糊出来的重复"权威 key"找出来。

背景:M3 每次读群名都可能认错字,`_mem_key()` 用相似度归并,但**糊得太狠时归不到一起**
(实测「某某粉丝群」被读成「威廉蒂斯酒友粉丝群Q4」,10 个字错 3 个,
相似度只有 0.70,够不到 0.80 的阈值 → 同一个群裂成两份档案)。

阈值不能盲目调低(会把两个不同的三人群并成一个),所以做成**人工确认**的维护脚本:
    python3 tools/merge_keys.py            # 只报告,不改
    python3 tools/merge_keys.py --apply    # 确认无误后执行合并
建议每周或发现"分身记忆断片/张冠李戴"时跑一次。
"""
import json, os, sys
from difflib import SequenceMatcher
from collections import Counter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOOSE = 0.65        # 报告用的宽松阈值;要人眼确认,所以可以放宽

def main():
    apply = "--apply" in sys.argv
    mem = json.load(open(os.path.join(HERE, "memory.json"), encoding="utf-8"))
    keys = sorted(mem, key=lambda k: -len(mem[k]))     # 条数多的当权威
    pairs, taken = [], set()
    for i, a in enumerate(keys):
        if a in taken: continue
        for b in keys[i+1:]:
            if b in taken: continue
            r = SequenceMatcher(None, a, b).ratio()
            if r >= LOOSE:
                pairs.append((b, a, r, len(mem[b]))); taken.add(b)
    if not pairs:
        print("✅ 没发现疑似重复的 key"); return
    print("疑似同一个会话(左 → 并入右):")
    for b, a, r, n in pairs:
        print(f"  【{b}】({n}条)  →  【{a}】   相似度 {r:.2f}")
    if not apply:
        print("\n(只是报告。**人眼确认无误后**再加 --apply 执行)"); return

    alias_p = os.path.join(HERE, "key_alias.json")
    alias = json.load(open(alias_p, encoding="utf-8")) if os.path.exists(alias_p) else {}
    FIX = {b: a for b, a, _, _ in pairs}
    for k, v in list(alias.items()):
        if v in FIX: alias[k] = FIX[v]
    json.dump(alias, open(alias_p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for f in ("memory.json", "pointer.json", "gags.json", "todos.json"):
        p = os.path.join(HERE, f)
        if not os.path.exists(p): continue
        d = json.load(open(p, encoding="utf-8")); nd = {}
        for k, v in d.items():
            ck = FIX.get(k, k)
            if isinstance(v, list):
                nd.setdefault(ck, [])
                for x in v:
                    if x not in nd[ck]: nd[ck].append(x)
            else:
                nd[ck] = v
        json.dump(nd, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"  {f}: {len(d)} → {len(nd)}")
    print("✅ 合并完成")

if __name__ == "__main__":
    main()
