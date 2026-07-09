# QQ-relay-Chatbot

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-%3E%3D0.100-009688?logo=fastapi&logoColor=white)
![License](https://img.shields.io/badge/License-AGPL--3.0-red)

交流群 QQ 940358918

基于 OneBot v11 WebSocket 的 QQ 群聊机器人，支持双模 AI 分流（直调/管道）、骰子、主动发言、表情包、插件系统和 Web 管理面板。兼容 **SnowLuma** 和 **NapCatQQ**。

> SnowLuma: https://github.com/SnowLuma/SnowLuma
> NapCatQQ: https://github.com/NapNeko/NapCatQQ

## 目录

1. [项目概述](#1-项目概述)
2. [架构总览](#2-架构总览)
3. [快速开始](#3-快速开始)
4. [核心功能](#4-核心功能)
5. [插件系统](#5-插件系统)
6. [WebUI 管理面板](#6-webui-管理面板)
7. [管理命令](#7-管理命令)
8. [分流设计](#8-分流设计)
9. [常见问题](#9-常见问题)

---

## 1. 项目概述

### 核心能力

- **双模分流**：不同群走不同 AI 后端（直调 API / 管道子进程）
- **主动发言**：管道群 4~8 条消息随机触发，自然插话
- **骰子**：`.r d20` / `.r 3d6` 等跑团骰子，本地秒回
- **表情包**：管道模式 50% 概率自动带图，支持归档打标
- **插件系统**：可扩展的消息拦截/处理，分群开关，WebUI 管理
- **WebUI**：仪表盘、群设置、知识库、插件管理、手动发消息、实时日志
- **轻量化**：Python + WebSocket，无复杂依赖

### 技术栈

| 层 | 技术 |
|----|------|
| QQ 协议 | SnowLuma / NapCatQQ（OneBot v11 WebSocket） |
| 中继 | Python 3 + asyncio + websockets |
| AI 后端 | Claude Code CLI 代理到 DeepSeek API |
| WebUI | FastAPI + 纯 JS SPA |
| 插件 | Python 注册制，持久化 JSON 配置 |

---

## 2. 架构总览

```
QQ 群消息 --> SnowLuma / NapCatQQ (WebSocket) --> relay.py (过滤 + 分流)
  |
  ├── 骰子 .r --> 本地秒回
  |
  ├── mode=pipe（聊天群）:
  │   ├── 插件钩子（拦截优先）
  │   ├── @触发 --> 带上下文 prompt --> claude 子进程（--resume/--session-id）
  │   ├── 主动发言（4~8 条触发）--> 同上
  │   └── 50% 概率附带表情包指令
  |
  ├── mode=direct（跑团群 / fallback）:
  │   ├── !管理命令 --> 本地处理
  │   ├── 插件钩子（拦截优先）
  │   └── @触发 --> HTTP 直调 DeepSeek API
  |
  └── WebUI（8800 端口）:
      ├── REST API（群管理/知识库/人设/插件）
      ├── WebSocket 实时推送
      └── 图床服务器（8801 端口，供容器内读取表情包）
```

---

## 3. 快速开始

### 前置依赖

| 组件 | 说明 |
|------|------|
| 闲置 QQ 号 | 机器人本体 |
| Python 3.10+ | relay.py + webui.py |
| Node.js（含 npm） | Claude Code CLI |
| SnowLuma / NapCatQQ | OneBot v11 协议实现 |
| Claude Code CLI | `npm install -g @anthropic-ai/claude-code` |

```bash
pip install -r requirements.txt
```

### 配置

复制 `.env.example` 为 `.env`，填写 `DEEPSEEK_API_KEY`、`NAPCAT_WS_URL`、`MASTER_QQ` 等变量。`.env.example` 中有完整注释。

### 启动

1. 确保 SnowLuma / NapCatQQ 已启动，WebSocket 在 `ws://127.0.0.1:3001` 运行
2. 双击 `启动中繼.bat`（或命令行 `py -3.13 -u relay.py --webui`）

正常启动日志：

```
[中繼] 初始化完成（persona: persona.md）
[中繼] 已加载 N 个插件: [...]
[中繼] 管道群: [10000002, 10000003], 其他群走 direct
[中繼] 主动发言阈值: {10000002: 6, 10000003: 5}
[中繼] 已登录 QQ: 12345678
[中繼] WebUI 启动于 http://127.0.0.1:8800
[图床] http://0.0.0.0:8801
[中繼] 开始监听...
```

---

## 4. 核心功能

### 4.1 双模分流

| 模式 | 场景 | 调用方式 | 上下文 | 工具能力 |
|------|------|---------|--------|---------|
| **pipe** | 聊天群 | spawn 子进程（`--resume`/`--session-id`） | 进程内持久 | 只读：WebSearch, Read, Glob（主人 @ 时放开写权限） |
| **direct** | 跑团群 | HTTP POST DeepSeek API | conv/ 文件，最多 50 条 | 纯文本问答 |

### 4.2 骰子（所有群通用）

| 输入 | 效果 | 示例输出 |
|------|------|---------|
| `.r d20` | 投 1 个 20 面骰 | D20 -> **15** |
| `.r 3d6` | 投 3 个 6 面骰求和 | 3D6 -> 2 + 5 + 3 = **10** |
| `.r 100` | 投 1 个 100 面骰 | D100 -> **73** |

### 4.3 主动发言（仅管道群）

- 每收到一条非 @ 消息，计数器 +1
- 阈值范围 **4~8 条**，触发后随机重置
- 滑动窗口保留最近 30 条消息作为上下文
- 触发 prompt 要求自然接话、不要太正式

### 4.4 表情包系统（仅管道群）

- 每次 AI 回复有 **50%** 概率附带表情包指令
- AI 从 `memory/memes/archive/index.md` 索引中选图
- 回复末尾自然嵌入 CQ 码图片
- 表情包管理通过 `!打标` / `!标` 命令（direct 群可用）

### 4.5 管道模式：主人权限提升

- 管道群中，主人（MASTER_QQ）@ 机器人时，工具权限从 `WebSearch,Read,Glob` 提升到 `WebSearch,Read,Edit,Write,Bash,Grep,Glob`
- 其他人的 @ 和主动发言仍保持只读权限

---

## 5. 插件系统

在 `plugins.py` 中注册异步处理函数，支持分群开关，配置持久化到 `memory/plugins.json`。

### 注册插件

```python
import plugins

async def my_plugin(bot, gid, uid, nick, text, is_at):
    """返回 True = 拦截本条消息（AI 不再回复），返回 None/False = 放行"""
    if "你好" in text:
        await bot.send_group(gid, f"{nick} 你好！")
        return True
    return None

plugins.register("my_plugin", "回复'你好'", my_plugin, default_enabled=True)
```

### 钩子时机

- pipe 群：在窗口更新和计数累加之后、AI 回复之前（插件拦截 = 不计数）
- direct 群：在 @ 消息处理之前（包括非 @ 消息也走钩子）

### WebUI 管理

插件管理页面可查看所有插件、全局开关、各群独立开关。无需重启即可生效。

---

## 6. WebUI 管理面板

默认地址：`http://127.0.0.1:8800`

| 页面 | 功能 |
|------|------|
| **仪表盘** | 连接状态、在线时长、实时消息流、快捷统计 |
| **群设置** | 查看/切换群模式（pipe/direct）、调整主动发言阈值、查看对话历史 |
| **管道状态** | 各管道群的计数器、阈值、滑动窗口内容实时查看 |
| **知识库** | 创建/编辑/删除 Markdown 知识文件 |
| **插件管理** | 查看所有插件、全局开关、分群开关 |
| **发消息** | 手动向任意群发送消息 |
| **环境配置** | 在线编辑 GROUP_MODE / FALLBACK_MODE（需重启生效） |

WebUI 通过 `/ws` WebSocket 接收实时事件（消息到达、AI 回复、管道触发）。

**默认绑定 127.0.0.1，仅本机可访问。不要暴露到公网。**

---

## 7. 管理命令

仅 direct 模式群 + 主人（MASTER_QQ）可用：

| 命令 | 效果 |
|------|------|
| `!帮助` | 显示所有命令 |
| `!清空记忆` | 清空本群对话历史 |
| `!重载` | 重读 persona.md + 知识库 + 记忆文件 |
| `!人设 xxx` | 修改人设内容并立即重载 |
| `!知识` | 查看知识库文件列表 |
| `!状态` | 查看本群对话统计（消息数/字符/tokens） |
| `!打标` | 列出待分类表情包 |
| `!标 <文件名> <标签>` | 归档表情包并打标签（如：`!标 001.jpg 草, 无奈, 猫`） |

---

## 8. 分流设计

### 为什么分流？

| 场景 | 需求 | 方案 |
|------|------|------|
| 跑团群 | 短时间高频 @，快速干净回答 | HTTP 直调，每次独立 |
| 聊天群 | 日常闲聊，需要记忆上下文 + 主动插话 | 管道子进程，session 持久 |

### 直调 vs 管道

| 对比项 | direct（直调） | pipe（管道） |
|--------|---------------|-------------|
| 调用方式 | HTTP POST API | spawn claude 子进程 |
| 上下文 | conv/ 文件，最多 50 条 | 进程内自动持久、自动压缩 |
| 工具能力 | 纯文本 | WebSearch / Read / Glob（主人可写） |
| 主动发言 | 不支持 | 4~8 条随机触发 |
| 响应延迟 | 约 1-3 秒 | 约 3-5 秒（含进程启动） |
| 管理命令 | !命令 可用 | 无响应 |
| 表情包 | 不支持 | 50% 概率 |

### 管道工作原理

每条消息启动一个新子进程（`--bare -p`），通过 `--session-id` / `--resume` 保持会话持久。进程退出 = 回复结束，省去解析 ANSI / spinner / 工具调用的麻烦。

### Session 隔离

每个群生成固定 UUID（`uuid.uuid5`），同群每次 resume 到同一个会话，不同群完全隔离。

### 安全设计

| 威胁 | 防护 |
|------|------|
| 群友让机器人删文件 | 管道默认只给 WebSearch, Read, Glob |
| 读到无关文件 | `--bare` 禁用 CLAUDE.md 发现 + `--add-dir` 收窄 |
| 并行冲突 | 同群 asyncio.Lock 串行排队 |
| 多进程串会话 | 固定 session UUID |

### 去重

FIFO 淘汰队列（set + deque），最近 500 条 message_id。WS 重连时避免重推消息导致重复处理。

### 消息拆分

短消息直接发送；超过 30 字按句尾（。！？）拆分，每段尽量 100 字以内，段间间隔 0.4 秒。

---

## 9. 常见问题

### 启动问题

**`claude 命令未找到`**
确认 Claude Code 已安装，Windows 下 `CLAUDE_CMD` 设为 `claude.cmd` 或完整路径。

**`Not logged in · Please run /login`**
管道子进程的环境变量未正确设置。检查 `DEEPSEEK_API_KEY` 和 `DEEPSEEK_BASE_URL`。

### 运行时问题

**机器人不响应 @**
1. 检查启动日志中 `已登录 QQ` 是否正确
2. 检查消息是否被去重（看终端日志）
3. 确认 SnowLuma/NapCatQQ 在线
4. 管道模式下检查进程是否卡死（看日志错误信息）

**双响炮（一条消息回复两次）**
1. 检查是否有多个 relay.py 进程在跑
2. 检查去重是否正常
3. 杀掉残留进程后重启

**管道群 @ 没有上下文记忆**
1. 首次 @ 用 `--session-id` 创建，后续用 `--resume` 续接
2. 如果手动调过 claude 占用了 session ID 会导致冲突
3. 代码有回退逻辑：resume 失败自动用 `--session-id` 重建

**Windows 下换行符丢失**
消息通过 stdin 传入（不是命令行参数），新版已修复。

### 注意事项

- 修改 `persona.md` 后需通过 WebUI 重载或重启生效
- `GROUP_MODE` 修改后需重启 relay.py
- 插件配置即时生效，无需重启

### 已知问题

- 偶现回复附带引号（排查中）
- 表情包触发时偶现 AI 对图本身进行评论说明（prompt 约束持续优化中）

---

## License

AGPL-3.0. Copyright (C) 2026 Soenchin.

GitHub: https://github.com/Soenchin/QQ-relay-Chatbot