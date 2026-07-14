# QQ-relay-Chatbot

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-WebUI-009688?logo=fastapi&logoColor=white)
![OneBot v11](https://img.shields.io/badge/Protocol-OneBot%20v11-5865F2)
![License](https://img.shields.io/badge/License-AGPL--3.0-red)

基于 **OneBot v11 WebSocket** 的 QQ 群聊中继机器人。它把群消息分流到两条不同的 AI 路径：需要稳定、可控问答的群走直调 API；需要持续聊天上下文和主动接话的群走管道模式。

支持 SnowLuma 与 NapCatQQ；带纯前端 WebUI、分群插件开关、管道读图、链接摘要、骰子和表情包图床。

> 交流群：QQ 940358918
>
> OneBot 实现： [SnowLuma](https://github.com/SnowLuma/SnowLuma) / [NapCatQQ](https://github.com/NapNeko/NapCatQQ)

---

## 这东西能干嘛

- **双群模式**：`direct` 直调 API，`pipe` 管道会话；每个群独立配置
- **主动发言**：管道群积累 4–8 条普通消息后随机接话，滑动窗口保留最近 30 条上下文
- **管道读图**：按群开启；图片落入临时 inbox，压缩后由多模态 API 生成短描述，再作为文字上下文提供给管道
- **链接摘要**：自动识别 Bilibili、GitHub 和通用网页链接；过滤 CQ 图片链接，带限流与失败兜底
- **插件系统**：消息 / 通知 / 请求三类插件，支持全局与分群开关，配置持久化
- **WebUI**：仪表盘、现代化群设置、管道状态、知识库、人设、插件、手动发消息、日志
- **本地功能**：骰子、表情包归档、OneBot 戳一戳回戳、加群申请提醒、新人欢迎
- **发图保底**：发送前剥离本地图床中不存在或越界的 CQ 图片段，文字不会因一张失效图被整条吞掉

---

## 消息怎么走

```text
QQ 群消息
  │
  └─ OneBot v11（SnowLuma / NapCatQQ WebSocket）
       │
       └─ relay.py
            │
            ├─ .r d20 / .r 3d6  → 本地骰子，立即回复
            │
            ├─ pipe 群
            │    ├─ 可选：图片下载 → 压缩 → 多模态短描述 → inbox / 滑动窗口
            │    ├─ 插件（如链接摘要）
            │    ├─ @ 机器人 → Claude Code CLI 管道会话
            │    └─ 普通聊天累计到阈值 → 主动接话
            │
            └─ direct 群
                 ├─ ! 管理命令（仅主人）
                 ├─ 插件
                 └─ @ 机器人 → Anthropic 兼容 HTTP API
```

管道群与直调群不是“高低配”，而是不同场景的两套动作系统：

| | `direct` 直调 | `pipe` 管道 |
|---|---|---|
| 适合 | 跑团、问答、只想被 @ 时回答的群 | 日常聊天、希望机器人有上下文与存在感的群 |
| AI 调用 | 直接请求 Anthropic 兼容 API | 启动 Claude Code CLI 子进程并续接会话 |
| 上下文 | `memory/conv/<群号>.json`，最多 50 条 | 最近 30 条群消息 + CLI 会话 |
| 主动发言 | 不会 | 4–8 条普通消息随机触发 |
| 图片理解 | 不参与 | 可按群开启，先转成短文字描述再接话 |
| 管理命令 | 主人可用 | 忽略 `!` 命令 |
| 工具权限 | 纯 API 回复 | 默认只读；主人 @ 时才额外开放写工具 |

---

## 快速开始

### 1. 准备环境

| 组件 | 用途 |
|---|---|
| Python 3.10+ | 中继、WebUI、插件 |
| 一个可登录的 QQ 号 | 机器人账号 |
| SnowLuma 或 NapCatQQ | 提供 OneBot v11 WebSocket |
| Node.js + Claude Code CLI | 仅 `pipe` 模式需要 |
| Anthropic 兼容 API | 直调回复，以及开启读图后的图片描述 |

安装 Python 依赖：

```bash
pip install -r requirements.txt
```

`Pillow` 用于图片压缩。没有它时机器人仍会运行，但读图图片无法做尺寸/体积压缩。

如果要使用管道模式，安装 Claude Code CLI：

```bash
npm install -g @anthropic-ai/claude-code
```

### 2. 配置 OneBot

启动 SnowLuma 或 NapCatQQ，并启用 OneBot v11 反向 WebSocket / WebSocket 服务端。默认中继会连接：

```text
ws://127.0.0.1:3001
```

地址和 Token 都能在 `.env` 中修改。

### 3. 写 `.env`

复制示例文件：

```bash
copy .env.example .env
```

至少填写：

```dotenv
DEEPSEEK_API_KEY=你的_API_Key
NAPCAT_WS_URL=ws://127.0.0.1:3001
NAPCAT_TOKEN=你的_OneBot_Token
MASTER_QQ=你的QQ号
BOT_NAME=QQ Bot
```

然后按需要配置群模式，例如：

```dotenv
GROUP_MODE={"123456789":"pipe","987654321":"direct"}
FALLBACK_MODE=direct
```

### 4. 启动

Windows 下直接双击：

```text
启动中繼.bat
```

或命令行启动：

```bash
py -3.13 -u relay.py --webui
```

看到类似日志就说明接通了：

```text
[中繼] 已登录 QQ: 12345678
[中繼] 管道群: [123456789], 其他群走 direct
[中繼] 读图群: （无，默认关）
[中繼] WebUI 启动于 http://127.0.0.1:8800
[中繼] 开始监听...
```

---

## 配置参考

完整注释见 [`.env.example`](.env.example)。下面是最常用的部分。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 无 | API Key；直调和图片描述都需要 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/anthropic` | Anthropic 兼容 API 基地址 |
| `NAPCAT_WS_URL` | `ws://127.0.0.1:3001` | OneBot WebSocket 地址 |
| `NAPCAT_TOKEN` | 空 | OneBot 鉴权 Token |
| `BOT_NAME` | `QQ Bot` | 日志、WebUI、回复中的机器人名称 |
| `MASTER_QQ` | `0` | 主人 QQ；用于管理命令和 pipe 写权限 |
| `GROUP_MODE` | 空对象 | JSON 对象：群号 → `direct` / `pipe` |
| `FALLBACK_MODE` | `direct` | 未单独配置的群使用的模式 |
| `GROUP_VISION` | 空 | JSON 数组：开启管道读图的群号 |
| `CLAUDE_CMD` | `claude` | Claude Code 命令或 Windows 下的 `claude.cmd` 路径 |
| `MEM_DIR` | `./memory` | 记忆、知识库、插件配置、表情包目录 |
| `MEME_SERVER_PORT` | `8801` | 本地表情包 HTTP 图床端口 |
| `WEBUI_HOST` | `127.0.0.1` | WebUI 监听地址 |
| `WEBUI_PORT` | `8800` | WebUI 端口 |

### 群设置与读图

`GROUP_VISION` 只对 `pipe` 群有意义。例如：

```dotenv
GROUP_MODE={"123456789":"pipe"}
GROUP_VISION=[123456789]
```

读图流程不是把原图直接塞给管道 CLI：

1. OneBot 图片优先从 CQ URL 下载，失败时尝试 `get_image`。
2. 图片写到 `memory/inbox/<群号>/`；GIF 取首帧，大图压缩到约 1.5 MB 以内。
3. 多模态 API 生成不超过 80 字的客观短描述。
4. 描述写入群聊滑动窗口，管道只读取文字上下文。
5. 每群最多保留 5 张，超过 30 分钟或消息出窗口后自动清理。

在 WebUI 的 **群设置** 页勾选“读图”也可以写入 `GROUP_VISION`；读图开关会尽量热更新，群模式变更仍建议重启 relay。

> 读图依赖你的 API / 模型本身支持图像输入。不支持时会记录失败日志并按普通文本继续，不会卡住整个群消息流程。

---

## WebUI

启动时带 `--webui`，或设置：

```dotenv
WEBUI_ENABLED=true
```

默认地址：<http://127.0.0.1:8800>

| 页面 | 能做什么 |
|---|---|
| 仪表盘 | 连接状态、运行时长、实时消息流、基础统计 |
| 群设置 | 分群 `direct` / `pipe`、读图开关、`.env` 配置预览与保存 |
| 管道状态 | 查看管道群的计数器、触发阈值和最近上下文 |
| 知识库 | 新建、编辑、删除 Markdown 知识文件 |
| 人设 | 编辑并重载 `persona.md` |
| 插件管理 | 全局开关、每群开关、热重载插件注册表 |
| 发消息 | 手动往指定群发送消息 |
| 日志 | 查看进程内最近日志 |

WebUI 默认只监听 `127.0.0.1`。**不要在没有鉴权和反向代理保护的情况下把它暴露到公网。**

---

## 插件系统

插件配置在 `memory/plugins.json`，支持三种事件：

| 注册函数 | 触发时机 | 内置例子 |
|---|---|---|
| `register()` | 群消息 | `link_summary` |
| `register_notice()` | OneBot notice | 戳一戳回戳、入群欢迎 |
| `register_request()` | OneBot request | 加群申请提醒 |

消息插件签名：

```python
async def my_plugin(bot, gid, uid, nick, text, is_at):
    # 返回 True：拦截，后续 AI 不回复
    # 返回 False / None：放行
    if "关键词" in text:
        await bot.send_group(gid, "收到")
        return True
    return False

register("my_plugin", "关键词回复", my_plugin, default_enabled=True)
```

### 内置插件：链接摘要

`link_summary` 默认开启。群里出现网页链接时会：

- Bilibili：标题、UP 主、封面、播放/点赞/弹幕
- GitHub：仓库简介、star/fork、语言、许可证等
- 其他网页：读取 Open Graph / `<title>` / description
- 忽略 CQ 图片、QQ 多媒体直链和纯图片链接
- 摘要正文最多 100 字，但统计行会完整保留
- 单群限流为每分钟 3 次；同一链接 5 分钟内去重

插件不会因为拉取失败把机器人搞挂：失败只会发出“链接内容拉取失败”的提示。

---

## 群内功能

### 骰子

所有群都能用：

| 输入 | 效果 |
|---|---|
| `.r d20` | 投一个 D20 |
| `.r 3d6` | 投 3 个 D6 并求和 |
| `.r 100` | 投一个 D100 |

### 管理命令

仅 **主人（`MASTER_QQ`）在 direct 群** 可用：

| 命令 | 作用 |
|---|---|
| `!帮助` | 显示命令表 |
| `!清空记忆` | 清空本群直调对话历史 |
| `!重载` | 重读人设、知识库和记忆文件 |
| `!人设 xxx` | 改写人设并重载 |
| `!知识` | 列出知识库文件 |
| `!状态` | 查看本群历史统计 |
| `!打标` | 列出待分类表情包 |
| `!标 <文件名> <标签>` | 归档表情包并添加标签 |

### 表情包

管道回复会以 50% 概率得到表情包提示。机器人会读取：

```text
memory/memes/archive/index.md
```

从索引里选择合适图片并通过本地图床发送。发送前会检查图片是否存在且位于允许目录内；图片失效时只剥掉图片 CQ 码，文字照常发送。

---

## 目录说明

```text
QQ-relay-Chatbot/
├── relay.py             # OneBot 中继、分流、管道、读图、直调 API
├── plugins.py           # 插件注册表和内置插件
├── webui.py             # FastAPI API / WebSocket / 静态文件服务
├── static/              # WebUI SPA
├── memory/
│   ├── persona.md       # 机器人设定（运行时创建）
│   ├── knowledge/       # Markdown 知识库
│   ├── conv/            # direct 群对话历史
│   ├── inbox/           # pipe 读图临时文件（自动清理）
│   ├── memes/           # 表情包索引与文件
│   └── plugins.json     # 插件开关配置
├── .env.example         # 配置样例
└── requirements.txt
```

`memory/`、`.workbuddy/`、`.zcode/` 都是本地运行/工具目录，默认不会进 git。

---

## 安全与排错

### 管道权限

普通群友触发管道时只开放：

```text
WebSearch, Read, Glob
```

只有 `MASTER_QQ` 在管道群 @ 机器人时，才会额外开放 `Edit`、`Write`、`Bash`、`Grep` 等工具。别把 `MASTER_QQ` 随便填成陌生人，也不要把 `PIPE_ADD_DIR` 指到不该让机器人读的目录。

### 常见问题

**`claude 命令未找到`**

确认已安装 Claude Code；Windows 常用配置：

```dotenv
CLAUDE_CMD=claude.cmd
```

如果命令没在 PATH 中，填绝对路径。

**机器人收得到消息但不回复**

1. 看终端是否显示“已登录 QQ”。
2. 确认该群在 `GROUP_MODE` 中的模式，或检查 `FALLBACK_MODE`。
3. `direct` 群只有 @ 机器人后才会调用 AI。
4. `pipe` 群的主动发言要等随机阈值；@ 可以立即触发。
5. 检查 API Key、OneBot WebSocket 地址与 Token。

**读图没有生效**

1. 确认群是 `pipe`。
2. 确认 `GROUP_VISION` 是 JSON 数组且包含该群号。
3. 看日志里的 `[读图]`；直链失败会自动尝试 OneBot `get_image`。
4. 确认 API 模型支持图片输入。

**消息带图时文字没发出去**

新版会在发送前检查本地图床图片。仍有问题时，请检查 `MEME_SERVER_PORT` 是否被占用，以及 `memory/memes/` 文件是否存在。

**重复回复（双响）**

通常是多个 relay 进程同时连接 OneBot。关掉残留进程后只保留一个实例。

---

## 开发与贡献

- 修改插件后，可在 WebUI 的插件管理页重载注册表。
- 修改 `GROUP_MODE` 后建议重启 relay；读图开关会尽量热更新。
- 改动前优先看 `.env.example` 和现有插件接口，别把真实 Key、QQ 号或本地绝对路径提交进仓库。
- 欢迎 Issue / PR；请描述 OneBot 实现、Python 版本、相关日志和复现步骤。

---

## License

[AGPL-3.0](LICENSE) · Copyright (C) 2026 Soenchin

项目地址：<https://github.com/Soenchin/QQ-relay-Chatbot>
