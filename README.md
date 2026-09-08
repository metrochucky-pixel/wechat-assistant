<h1 align="center">🤖 wechat-assistant</h1>

<p align="center">
  <b>在一台真安卓机上,以你本人的口吻自动回复微信。</b><br>
  不走协议 / 网页版(封号风险低)· 靠 <b>ADB 截图 + 多模态大模型</b>看屏做决策 · 人设和记忆都是可改的文件。
</p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-Android%20%2B%20Mac%2FLinux-blue">
  <img src="https://img.shields.io/badge/python-3.9%2B-green">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey">
  <img src="https://img.shields.io/badge/status-personal%20project-orange">
</p>

> ⚠️ **仅供学习研究,自负风险。** 用 AI 以本人身份聊天涉及微信 ToS 与 AI 生成内容标识等合规问题,请自行评估。**默认对涉及钱 / 承诺的消息一律转本人,绝不替你拍板。强烈建议用小号。**

---

## 为什么又造一个微信 bot?

市面上的「AI 自动回复」大多做到"能回消息"就停了。真正难的不是**回**,是**长期、可靠地扮演一个真人而不露馅**——而这恰恰是这个项目死磕的地方。

它踩过的坑,都变成了机制:

| 你会遇到的问题 | 它的解法 |
|---|---|
| 截图认字总把群名认错,记忆被切成好几份 | 相似度归并 key + 定期体检合并 |
| 点进会话点歪了行,在 A 群说了 B 群的话 | **进门先认门牌**——标题栏像素哈希识别身份,对不上就闭嘴 |
| "日志一直在滚"但其实手机早断了,几天没人发现 | **心跳 + 独立看门狗**,进程死了/手机掉线,飞书告诉你 |
| 发送失败就无限重试,把半成品反复怼出去 | **熔断**——连续失败就冷却 + 告警 |
| AI 记错事实,越聊越离谱 | **不自动写事实**;拿不准的每两天通过飞书**问你**,你答完写进档案 |
| 回复千篇一律一股 AI 味 | 真实语料当口吻标尺(样本比规则管用),自检口头禅 |

## ✨ 能做什么

- 🧠 **分层记忆** — 每会话滚动记忆 · 跨会话的「人物」记忆 · 处理指针(治"标已读后漏回")· 梗档案(会回调老梗)· 未了之事(答应了会记着)
- 🎭 **人设即文件** — 说话风格 / 身份 / 人脉 / 真实语料都是 Markdown,改完实时生效不重启
- 🗣️ **看图 · 看视频 · 听语音** — 点开图看高清,语音走微信自带「转文字」
- 🎨 **文生图** — 让它画张图,自动生成并发进群
- 🚦 **发送节奏 + 三档 LLM 兜底** — 主模型挂了自动降级,发送限速防刷屏
- 🤔 **人在环里** — 分身不懂就问你,你是它的老师

## 🏗️ 一图看懂

```
 launchd 常驻
     │  每 10s 轮询会话列表(红点预检,安静时不烧 LLM)
     ▼
 有新消息的会话 ──► 认门牌 ──► 截图 ──► 多模态模型决策
     │                                   （回不回 / 回什么 / 看图 / 画图 / 转本人）
     ▼
 ADBKeyboard 发送(限速 + 熔断) ──► 写记忆
                                                                    
 外挂:🐕 看门狗(飞书告警) · 🤔 定期请教(飞书问你) · 🎨 文生图链路
```

## 🚀 快速开始

**你需要**:一台安卓机(开 USB 调试)· 一台常开的电脑(Mac/Linux)· `adb` · Python 3.9+

```bash
git clone https://github.com/metrochucky-pixel/wechat-assistant
cd wechat-assistant
pip3 install openai pillow anthropic pyobjc-framework-ApplicationServices

# 1) 手机装 ADBKeyboard 并设为输入法(中文靠它发)
# 2) 配密钥(至少填 MINIMAX_API_KEY)
cp .env.example ~/.config/wechatbot.env && chmod 600 ~/.config/wechatbot.env

# 3) 从模板写你自己的人设(复制后去掉 .example)
cp persona.example.md persona.md      # 你的语气
cp about_me.example.md about_me.md    # 你是谁
cp people.example.md people.md        # 你认识的人

# 4) 先影子跑:主程序顶部 DRY_RUN=True,看它"会怎么回但不真发"
python3 wechat_autoreply.py
```

确认它回得像你、点得准,再把 `DRY_RUN` 改成 `False` 真发。

## ⚠️ 这不是开箱即用 —— 一定先读

它靠"截图找像素"工作,这些像素和**你的**设备强绑定,直接 clone 几乎肯定不完全对:

- **分辨率写死 1080×2400** — 你的手机不一样,发送键、会话行高(194px 网格)、相册/图片坐标全要重标
- **微信版本敏感** — 微信一更新,标题栏、「转文字」按钮、发送键颜色可能变
- **中文靠 ADBKeyboard** — 手机必须装它并设为当前输入法
- **首次要配 WiFi ADB** — `adb tcpip 5555` + 开发者选项「无线调试」,细节见 [`SKILL.md`](SKILL.md)

**先 `DRY_RUN=True` 跑,对着日志逐项校准坐标,全对了再真发。**

## 📂 目录

| 文件 | 作用 |
|---|---|
| `wechat_autoreply.py` | 主程序:轮询 / 决策 / 发送 / 记忆 / 文生图 |
| `tools/watchdog.py` | 外部看门狗,进程死了发飞书 |
| `tools/ask_owner.py` | 每两天把分身拿不准的事问机主 |
| `tools/merge_keys.py` | 会话名 OCR 变体合并维护 |
| `tools/test_*.py` | 各通道连通性自测 |
| [`SKILL.md`](SKILL.md) | **踩坑运维手册 —— 最值得看的部分** |
| `*.example.*` | 人设 / 名单模板,复制去掉 `.example` |

## 🔒 安全

- 密钥只放 `~/.config/wechatbot.env`(chmod 600),**永不进仓库**
- `.gitignore` 已焊死所有私有文件:记忆 / 日志 / 你的人设 / 语料
- 你的 `people.md`、`memory.json` 含真名和真实聊天,**默认不上传**——别手动加进去
- bot 运行时别手动碰那台手机(会抢控制)

## 📜 License

[MIT](LICENSE) · 这是个人项目,随便玩,别拿去干坏事。
