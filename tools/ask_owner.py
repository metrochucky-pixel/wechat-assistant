#!/usr/bin/env python3
"""分身的「定期请教」——把它拿不准的事攒起来,每两天早上 10 点通过飞书机器人问机主一次。

为什么是这个设计(重要):
    分身的长期记忆有个死结——滚动窗口会让老事实消失,但**自动蒸馏事实风险很大**:
    机主已多次纠正人物事实(杨硕辈分、托尼不在群、老曹可放开聊),自动写入的错事实
    会永久污染回复。所以不自动写事实,而是**把不确定的地方问出来**,由机主回答后
    人工写进 people.md —— 人在环里,风险低、质量高。

问什么:
    · 群里出现过、但 people.md 里没有的人(该怎么称呼、什么身份、用什么口气)
    · 拿不准的别名/关系(避免把一个人当成两个人)
    · 悬而未决的承诺(说了"回头给准信"却一直没给的)
    · 反复卡壳、每次都不知道怎么接的话题

不问什么:
    · 已经在 people.md / about_me.md 里写清楚的
    · 上次问过的同一个人/同一件事(asked.json 去重)

调度:launchd 每天 10:00 拉起,脚本自己节流——距上次成功发送不足 40 小时就跳过,
      于是实际约等于"每两天一次"。没有问题要问时**不发**,不打扰。

手动跑:  python3 ~/wechatbot/tools/ask_owner.py --now    # 忽略节流,立刻跑一次
        python3 ~/wechatbot/tools/ask_owner.py --dry    # 只打印,不发飞书
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wechat_autoreply as wa  # noqa: E402

STATE = os.path.join(wa.HERE, "asked.json")
MIN_HOURS = 40          # 距上次发送不足这么久就跳过 → 约等于每两天一次


def _state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {"last": "", "subjects": []}


def _save(s):
    json.dump(s, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _recent_context(max_chars=6000):
    """把最近的聊天记忆汇成一段文本喂给模型。"""
    mem = {}
    if os.path.exists(wa.MEMORY_PATH):
        try:
            mem = json.load(open(wa.MEMORY_PATH, encoding="utf-8"))
        except Exception:
            pass
    blocks = []
    for conv, lines in mem.items():
        blocks.append(f"【会话:{conv}】\n" + "\n".join(lines[-15:]))
    text = "\n\n".join(blocks)
    return text[-max_chars:]


def main():
    now = datetime.now()
    dry = "--dry" in sys.argv
    force = "--now" in sys.argv or dry

    st = _state()
    if not force and st.get("last"):
        try:
            if now - datetime.fromisoformat(st["last"]) < timedelta(hours=MIN_HOURS):
                print(f"[{now:%m-%d %H:%M}] 距上次 {st['last']} 不足 {MIN_HOURS}h,跳过")
                return
        except Exception:
            pass

    ctx = _recent_context()
    if not ctx.strip():
        print("没有聊天记忆,跳过")
        return

    known = ", ".join(st.get("subjects", [])) or "(还没问过)"
    people = ""
    for p in (wa.PEOPLE_PATH, wa.ABOUT_PATH):
        if os.path.exists(p):
            people += open(p, encoding="utf-8").read() + "\n"

    prompt = f"""你是【机主本人】的微信分身。下面是你【已经掌握的人物/身份档案】和【最近的聊天记忆】。

请找出你**拿不准、需要请教机主本人**的地方,整理成问题。重点找:
1. 聊天里出现过、但档案里没有的人 —— 该怎么称呼他、什么身份、用什么口气聊;
2. 可能是同一个人的不同叫法(避免把一个人误当成两个人),或拿不准的关系;
3. 你答应过却一直没兑现的事(比如说了"回头给你准信"但后来没有下文);
4. 反复出现、你每次都不知道该怎么接的话题。

规则:
- **只提问,不要编造事实**。不确定就问,别自己下结论。
- 档案里已经写清楚的**不要再问**。
- 这些主题上次已经问过,别重复:{known}
- 最多 6 个问题,挑最影响回复质量的。真的没什么可问就返回空列表。
- 问题要具体、好回答(机主看一眼就能用一句话答上来),别问空泛的大问题。

严格只输出 JSON:
{{"questions":[{{"subject":"这个问题围绕的人名或事(用作去重键,尽量简短)","question":"要问机主的话"}}]}}

【已掌握的档案】
{people[:4000]}

【最近的聊天记忆】
{ctx}
"""
    try:
        resp = wa.get_kimi_client().chat.completions.create(
            model=wa.PROVIDERS["kimi-code"]["model"], max_tokens=2500, temperature=1,
            messages=[{"role": "user", "content": prompt}])
        t = resp.choices[0].message.content or ""
    except Exception as e:
        print(f"❌ 生成问题失败:{str(e)[:120]}")
        return

    t = re.sub(r"<think>.*?</think>", "", t, flags=re.DOTALL)
    t = re.sub(r"```(json)?", "", t)
    try:
        qs = json.loads(t[t.find("{"):t.rfind("}") + 1]).get("questions", [])
    except Exception:
        print(f"❌ 解析失败:{t[:200]}")
        return

    # 去重:同一个主题问过就不再问
    seen = set(st.get("subjects", []))
    fresh = [q for q in qs if q.get("question") and q.get("subject") not in seen]
    if not fresh:
        print(f"[{now:%m-%d %H:%M}] 没有新问题要问,不打扰")
        return

    # 顺带把"答应了还没兑现的事"一并报上去 —— 机主问过牛腩煲,说明他其实在意这个
    pend = []
    try:
        todos = json.load(open(os.path.join(wa.HERE, "todos.json"), encoding="utf-8"))
        for conv, items in todos.items():
            for it in items:              # ⚠️ 别用 t:上面 t 是 LLM 返回文本,会被遮蔽
                if not it.get("done"):
                    pend.append(f"· [{it.get('at','')}] {conv[:12]}:{it.get('t','')}")
    except Exception:
        pass

    lines = ["这几天聊下来有些地方我拿不准,想跟你确认一下:", ""]
    lines += [f"{i}. {q['question']}" for i, q in enumerate(fresh, 1)]
    if pend:
        lines += ["", "另外这些是我答应了还没兑现的事:"] + pend[:8]
    lines += ["", "(直接跟 Claude 说答案就行,它会帮我写进 people.md)"]
    print("\n".join(lines))
    if dry:
        print("\n(--dry 模式,没有发送)")
        return

    if wa.notify_feishu("🤔 微信分身:有几个问题想请教你", lines):
        st["last"] = now.isoformat(timespec="seconds")
        st["subjects"] = list(seen | {q["subject"] for q in fresh})[-60:]
        _save(st)
        print(f"\n✅ 已推飞书({len(fresh)} 个问题)")
    else:
        print("\n❌ 飞书发送失败,不记状态(下次会重试)")


if __name__ == "__main__":
    main()
