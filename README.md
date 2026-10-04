# WeChat Android Bot — 2026.10.04

在 macOS 上通过 ADB 控制 Android 微信的 Python 程序。本版本整合当前手机 bot 的消息调度、后台回答、原图分析、发送核验、飞书协助和来源可追溯的表达偏好学习。

源码仓库：[metrochucky-pixel/wechat-assistant](https://github.com/metrochucky-pixel/wechat-assistant)。

这是现有个人部署的完整代码快照；不是通用的微信官方接口 SDK。群策略、账号昵称和设备回退值已改为配置项，发布源码不含实际个人资料。

## 已整合的能力

- 按发送者和事项合并连续补充，独立处理不同问题；指定群仅被 @ 时回复。
- 两个后台计算进程、调用预算、超时恢复和版本检查，手机操作保持单进程。
- 原图核实与事实缓存；文本/联网查询/生图分别路由。
- 发送前身份和草稿检查，发送后核验；结果不确定时不盲目重发。
- 会话内上下文、分群表达纠正、全局明确偏好、记忆来源撤销。
- 普通未读和 @ 发现持久化、空列表屏外查找、2→4→5 分钟安静复查。

## 安装

验证环境：macOS、Python 3.9、ADB、OnePlus 9R（1080×2400）。其他手机尺寸、微信界面和系统尚未验证。手机需要已登录微信、授权 USB/无线调试、安装并启用 ADBKeyboard；不能同时运行两个控制同一手机的实例。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p ~/.config
# 文件已存在时不要覆盖其中现有配置。
cp -n packaging/wechatbot.env.example ~/.config/wechatbot.env
chmod 600 ~/.config/wechatbot.env
adb devices -l
```

填写实际 ADB 地址和模型密钥。当前列表读取优先 Grok；MiniMax/Kimi/Claude 为各路由的备选，生图需要 MiniMax。飞书为可选集成。模型名称和接口权限需与实际账户匹配；依赖版本固定不代表外部服务永久可用。

在根目录放入自己的 `persona.md`、`voice.md`、`about_me.md`、`people.md`；需要时配置 `no_reply.txt` 和 `fun_groups.txt`。这几个文件及运行数据不随发布包分发。现有部署升级时应保留它们。设置 `WECHAT_SELF_NAME` 为账号实际昵称；`WECHAT_MENTION_ONLY_GROUPS` 为仅被 @ 时回复的群，用 `|` 分隔；`fun_groups.txt` 控制允许自主接话的群。设备属性使用 `ADB_DEVICE_PROP`，USB 回退使用 `ADB_USB_FALLBACK`。可选 `WECHAT_DISCOVERY_ALIASES` 是发现阶段的 JSON 别名表，不能绕过身份核验；`WECHAT_CROSS_NAMES` 配置允许跨单聊关联的名称，默认空。

## 验证与启动

```sh
.venv/bin/python tools/check_upgrade.py
# 默认影子模式：会操作手机读屏和调用模型，但不发送微信回复。
.venv/bin/python tools/run_bot.py
# 准备好实际代回复后，显式启用发送：
.venv/bin/python tools/run_bot.py --send
```

公开版主入口与 `run_bot.py` 都默认影子模式；实际发送请使用 `--send`。影子模式仍可能使用配置好的飞书集成，若只测试读屏，请暂不配置飞书凭据。不要与当前 LaunchAgent 同时启动。

离线升级检查只运行白名单测试和脱敏回放，不操作真实手机、不发消息。不要用自动发现方式运行所有 `tools/test_*.py`：`test_feishu.py`、`test_grok.py` 等是人工集成工具，可能调用外部服务或发送测试通知。

## 封装与升级

```sh
python3 tools/build_release.py
shasum -a 256 -c dist/wechatbot-2026.10.04.zip.sha256
```

封装工具仅读取 Git 当前提交中的受控源码文件，输出 ZIP、包级 SHA-256 和包内逐文件清单。不会读取线上目录、个人资料、SQLite、日志、图片、密钥或 Git 历史。工作区有未提交内容时拒绝封装，避免版本混淆。

升级和回滚步骤见 [发布说明](docs/release-2026.10.04.md)。运行架构和每轮修改见 `docs/`。源码包不包含私人配置和运行数据库，因此可复现代码版本，但不是完整个人数据备份。

## 许可证

沿用本仓库 [MIT License](LICENSE)。原有个人配置模板及 [运维手册](SKILL.md) 保留；历史手册中涉及启动/依赖的步骤以本文为准。
