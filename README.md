# QQ Group Chat Relay Bot
交流群🐧🤖940358918

一个基于 **SnowLuma + WebSocket** 的 QQ 群聊机器人，支持双模 AI 分流（直调/管道）、骰子、主动发言，同样兼容 **NapCatQQ**。

> SnowLuma: https://github.com/SnowLuma/SnowLuma
> 
> NapCatQQ: https://github.com/NapNeko/NapCatQQ
>
推荐使用Agent一键安装：“给我按照这个项目部署一个QQ机器人”
## 目录

1. [项目概述](#1-项目概述)
2. [前置依赖](#2-前置依赖)
3. [目录结构](#3-目录结构)
4. [SnowLuma 配置](#4-snowluma-配置)
5. [relay.py 详解](#5-relaypy-详解)
6. [启动方式](#6-启动方式)
7. [使用说明](#7-使用说明)
8. [分流架构详解](#8-分流架构详解)
9. [常见问题排查](#9-常见问题排查)

---

## 1. 项目概述

### 核心能力

- **双模分流**：不同群走不同的 AI 后端
- **主动发言**：群消息量达到随机阈值时，机器人主动插话。也可以@触发
- **骰子**：`.r d20` / `.r 3d6` 等跑团骰子，本地秒回
- **表情包**：管道模式下 50% 概率自动带图，支持归档打标
- **轻量化部署**：关键代码仅两个py脚本。
- **后端功能强大**：接入Claudecode cli，通过分配工具权限进行电脑管理。

### 架构总览

```
QQ 群消息 → SnowLuma / NapCatQQ (WebSocket) → relay.py (过滤 + 分流)
  ├─ 跑团群 @ → HTTP 直调 API（无状态，每次独立）
  ├─ 聊天群 @ → 子进程管道（session 持久，带记忆）
  ├─ 聊天群 主动触发(7~15条随机) → 同上管道
  ├─ 骰子 .r → 本地秒回
  ├─ !管理命令 → 仅跑团群响应
  └─ 图床 HTTP 服务器 → SnowLuma 读取表情包
```

---

## 2. 前置依赖

### 软件要求

| 组件 | 说明 |
|------|------|
| 一个闲置QQ号 | 机器人本体 |
| Python 3.13+ | relay.py 运行环境 |
| Node.js (npm) | 管道模式 AI 后端依赖 |
| SnowLuma（或 NapCatQQ） | QQ 协议实现，提供 OneBot v11 WebSocket 接口 |
| Claude Code CLI | 管道模式 AI 后端（`npm install -g @anthropic-ai/claude-code`） |

### Python 依赖

安装依赖：

```
pip install -r requirements.txt
```

- `httpx` — HTTP 请求
- `websockets` — WebSocket 连接 SnowLuma / NapCat

### 环境变量

复制 `.env.example` 为 `.env`，填写你的配置：

| 变量 | 说明 |
|------|------|
| `DEEPSEEK_API_KEY` | API 密钥（直调模式必填） |
| `NAPCAT_TOKEN` | WebSocket 认证 Token |
| `MASTER_QQ` | 你的 QQ 号（拥有管理权限） |
| `GROUP_MODE` | JSON 格式群分流配置（可选，默认用代码内占位值） |
| `CLAUDE_CMD` | Claude CLI 路径（默认 `claude`） |
| `MEM_DIR` | 记忆/人设存储目录（默认为相对路径） |

管道子进程会自动继承环境变量。

---

## 3. 目录结构

```
项目根目录/
├── relay.py                   # 主程序
├── webui.py                   # Web 管理面板后端
├── 启动中繼.bat                # 启动脚本（一键启动 relay + WebUI）
├── static/                    # WebUI 前端静态文件
│   ├── index.html
│   ├── app.js
│   └── app.css
├── memes/                     # 表情包资源
├── .env.example               # 环境变量模板
└── .gitignore

记忆目录（由 MEM_DIR 环境变量指定）：
{记忆目录}/
├── persona.md                 # 人设文件（管道模式的 system prompt）
├── knowledge/                 # 知识库（.md 文件，自动合并进人设）
├── conv/                      # 对话历史（跑团群直调模式，JSON 格式，最多 50 条）
└── memory/                    # 管道模式自动记忆目录
```

---

## 4. SnowLuma 配置

### WebSocket 设置

SnowLuma 需要开启反向 WebSocket 服务：

- **地址**: `ws://127.0.0.1:3001`
- **Token**: 从 `.env` 文件读取

SnowLuma 会在 `127.0.0.1:3001` 上监听 WebSocket 连接，relay.py 作为客户端连接上去。

### 消息格式

SnowLuma 推送给 relay.py 的消息格式（JSON）：

```json
{
  "post_type": "message",
  "message_type": "group",
  "group_id": 123456789,
  "user_id": 123456789,
  "raw_message": "[CQ:at,qq=12345678] 你好",
  "message_id": 12345,
  "sender": {
    "card": "群名片",
    "nickname": "QQ昵称"
  }
}
```

注意：
- @ 机器人的消息 `raw_message` 中包含 `[CQ:at,qq=<bot_qq>]`
- 图片消息显示为 `[CQ:image,file=xxx]`
- 合并转发显示为 `[CQ:forward,id=xxx]`（只算 1 条消息）
- 回复他人时 `raw_message` 是实际文字内容，不包含被回复的消息

---

## 5. relay.py 详解

### 配置方式

所有配置通过环境变量读取（详见 `.env.example`），relay.py 顶部使用 `os.getenv()` 加载，不硬编码敏感信息。

### 核心类：`RelayBot`

#### `__init__()` — 初始化

- 加载人设（从 `persona.md` 读取，启动时一次性加载到内存）
- 初始化去重缓存（`set` + `deque`，FIFO 淘汰，最多 500 条）
- 初始化管道群计数器、随机阈值、滑动窗口

#### 去重机制 `_dedup(mid)`

使用 `set` + `deque` 实现 FIFO 淘汰：

```python
def _dedup(self, mid) -> bool:
    """True = 已见过（跳过），False = 新消息"""
    if mid in self._seen_ids:
        return True
    self._seen_ids.add(mid)
    self._seen_queue.append(mid)
    if len(self._seen_queue) > self._seen_max:
        old = self._seen_queue.popleft()
        self._seen_ids.remove(old)
    return False
```

> **注意**：旧版本使用 `clear()` 清空去重，重连时 SnowLuma 会重推旧消息导致重复处理。新版使用 FIFO 逐条淘汰，安全可靠。

#### 分流逻辑 `on_group_msg()`

```
消息进入 on_group_msg
  ├─ 以 .r 或 。r 开头 → 骰子处理，返回
  ├─ 查 GROUP_MODE 获取模式
  │
  ├─ mode == "pipe":
  │   ├─ 以 ! 开头 → 忽略（聊天群不响应管理命令）
  │   ├─ @触发 → 拼 prompt → _call_pipe()
  │   ├─ 未 @ → 加入滑动窗口 + 计数器++
  │   │   └─ 达到随机阈值(7~15) → 拼带上下文的 prompt → _call_pipe()
  │   └─ 回复发回群
  │
  └─ mode == "direct"（含 fallback）:
      ├─ 以 ! 开头 → on_admin() 处理管理指令
      ├─ 未 @ → 忽略（只响应 @）
      └─ @触发 → 存对话历史 → _call_api() → 存回复 → 发群
```

#### 主动发言机制

- **计数器**：每个管道群独立，每收到一条非 @ 消息 +1
- **随机阈值**：7~15 条之间随机，触发后重置
- **滑动窗口**：`deque(maxlen=10)`，保留最近 10 条消息
- **触发 prompt**：
  ```
  以下是最近群聊记录：
  
  [昵称] 消息1
  [昵称] 消息2
  ...
  
  你是群成员，自然接一句，不要太正式。
  
  （回复控制在 50 字以内）
  ```
- **空窗口保护**：窗口为空时跳过本次触发

#### 管道模式 `_call_pipe(gid, message)`

聊天群的 AI 调用方式。流程：

1. **生成 session UUID**：用群号通过 `uuid.uuid5()` 生成固定 UUID
2. **先试 `--resume`**：续接已有会话
   - 成功 → 返回回复
   - 失败（会话不存在） → 继续下一步
   - 其他错误 → 返回错误提示
3. **回退 `--session-id`**：创建新会话
4. **消息通过 stdin 传递**（不是命令行参数，防 Windows 换行符截断）

```python
# 实际进程命令（简化）：
claude --bare -p
  --add-dir <记忆目录>
  --allowedTools "WebSearch,Read"
  --system-prompt "<persona.md全文>"
  --resume <group-uuid>
# 消息体通过 stdin 传入
```

关键参数说明：

| 参数 | 作用 |
|------|------|
| `--bare` | 禁止自动发现 CLAUDE.md |
| `-p` | Print 模式，输出纯文本，进程退出 = 回复结束 |
| `--add-dir` | 限定可读目录 |
| `--allowedTools` | 工具白名单，只给 `WebSearch,Read` 防止写操作 |
| `--resume` / `--session-id` | 会话持久化，各群 UUID 独立 |
| `stdin` | 传消息正文，绕开 cmd 换行符截断 |

#### 直调模式 `_call_api(messages)`

跑团群的 AI 调用方式。调用兼容 API 端点：

```python
POST https://api.deepseek.com/anthropic/v1/messages
Headers:
  x-api-key: ...
  anthropic-version: 2023-06-01
  content-type: application/json

Body:
{
  "model": "deepseek-v4-flash",
  "system": "<persona全文 + 知识库>",
  "messages": [...],
  "max_tokens": 500
}
```

对话历史存到 `{MEM_DIR}/conv/{群号}.json`，最多保留最近 50 轮（MEM_DIR 通过环境变量配置）。

#### 管理命令 `on_admin()`

仅跑团群（direct 模式）可用：

| 命令 | 效果 |
|------|------|
| `!清空记忆` | 清空本群对话历史 |
| `!重载` | 重读 persona.md + 知识库 |
| `!人设 xxx` | 修改人设内容并立即重载 |
| `!知识` | 查看知识库文件列表 |
| `!状态` | 查看本群对话统计 |
| `!打标` | 列出待分类表情包 |
| `!标 <文件名> <标签1, 标签2...>` | 归档表情包并打标签 |

#### 并发控制

管道群使用 `asyncio.Lock` 串行排队，同群同一时间只处理一条消息，后续消息等待前面的完成。

#### 字数限制

relay.py 层强制追加 `（回复控制在 50 字以内）` 到每次管道调用的 prompt 末尾，不依赖人设文件。

#### 表情包系统

管道模式下，每次 AI 回复有 50% 概率附带表情包指令。AI 会从 `memes/archive/index.md` 索引中选图，以 CQ 码形式自然嵌入回复末尾。

**目录结构**：

```
memes/
├── unsorted/        # 待分类（丢进去就行）
├── archive/         # 已归档
│   └── index.md     # 索引文件（!标 命令自动维护）
└── ...
```

**图床 HTTP 服务器**：

启动时自动在 `0.0.0.0:8801` 开启极简 HTTP 文件服务器，供 Docker 内的 SnowLuma 通过 `host.docker.internal:8801` 读取表情包文件。端口可通过 `MEME_SERVER_PORT` 环境变量配置。

**管理命令**（仅 direct 模式群 + 主人可用）：

| 命令 | 效果 |
|------|------|
| `!打标` | 列出 unsorted 目录下所有待分类表情包 |
| `!标 <文件名> <标签1, 标签2...>` | 将文件移入 archive/，写入 index.md |

示例：
```
!标 001.jpg 草, 无奈, 猫
```

---

## 6. 启动方式

### 首次启动

1. 确保 SnowLuma（或 NapCatQQ）已启动且 WebSocket 服务在 `ws://127.0.0.1:3001` 运行
2. 双击 `启动中繼.bat`

### 启动脚本

```batch
@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3.13 -u relay.py --webui
pause
```

| 参数 | 作用 |
|------|------|
| `chcp 65001` | 切 UTF-8 编码，防中文乱码 |
| `cd /d "%~dp0"` | 自动定位到脚本所在目录（无需手动改路径） |
| `py -3.13` | 使用 Python 3.13 启动器 |
| `-u` | unbuffered 模式，实时刷新日志 |
| `--webui` | 同时启动 Web 管理面板（默认端口 8800） |

### 启动日志

正常启动会看到：

```
[中繼] 初始化完成（persona: persona.md）
[中繼] 管道群: [10000002, 10000003], 其他群走 direct
[中繼] 主动发言阈值: {10000002: 9, 10000003: 12}
[中繼] 已登录 QQ: 12345678
[中繼] 开始监听...
```

### ⚠️ WebUI 安全警告

**WebUI（`--webui`）默认绑定 `127.0.0.1:8800`，仅本机可访问。**

WebUI 暴露了完整的机器人控制面：
- 查看所有群的聊天历史
- 发送消息、修改人设、管理知识库
- 实时消息流 WebSocket

**绝对不要** 将 WebUI 绑定到 `0.0.0.0` 或暴露到公网。如果确实需要远程访问，请在前面加一层反向代理认证（如 nginx basic auth）。

### 重启方式

1. 在终端窗口中按 `Ctrl+C` 停止
2. 确保没有旧进程残留
3. 重新双击启动脚本

> **重要**：改 persona.md 后必须重启 relay 才生效（persona 启动时加载到内存）。

---

## 7. 使用说明

### 骰子（所有群通用）

| 输入 | 效果 | 示例 |
|------|------|------|
| `.r d20` | 投 1 个 20 面骰 | D20 → **15** |
| `.r 3d6` | 投 3 个 6 面骰并求和 | 3D6 → 2 + 5 + 3 = **10** |
| `.r 100` | 投 1 个 100 面骰 | D100 → **73** |

### 跑团群

- **@机器人** → AI 聊天（HTTP 直调 API）
- **!命令** → 管理功能
- 不响应主动发言

### 聊天群

- **@机器人** → AI 聊天（子进程管道，带会话记忆）
- **机器人自动插话** → 7~15 条消息随机触发一次主动发言
- **!命令** → 无响应

### 未知群

fallback 到 direct 模式，跟跑团群行为一致。

---

## 8. 分流架构详解

### 为什么需要分流？

| 场景 | 需求 | 方案 |
|------|------|------|
| 跑团群 | 短时间高频 @，需要快速干净的回答 | HTTP 直调，每次独立 |
| 聊天群 | 日常闲聊，需要记忆上下文，需要主动插话 | 管道进程，session 持久 |

### 直调模式 vs 管道模式对比

| 对比项 | direct（直调） | pipe（管道） |
|--------|---------------|-------------|
| 调用方式 | HTTP POST API | spawn 子进程 |
| 上下文 | 每次独立，靠 conv/ 文件维持最多 50 条 | 进程内自动持久，自动压缩 |
| 工具能力 | 纯文本问答 | WebSearch / Read |
| 主动发言 | 不支持 | 7~15 条随机触发 |
| 响应延迟 | 约 1-3 秒 | 约 3-5 秒（含进程启动） |
| 管理命令 | !重载 !清空记忆 等 | 无响应 |

### 为什么管道模式不是真正的"长驻"？

每条消息都启动一个新子进程（`--bare -p`），但：
- 通过 `--session-id` / `--resume` 保持会话持久（Claude Code 自动管理会话存储）
- 进程退出 = 回复结束，省去解析 ANSI / spinner / 工具调用的麻烦
- 聊天群每天 10-20 条互动，2-3 秒启动延迟可以忽略

真正的 stdin/stdout 长驻管道（单个进程不退出）需要复杂的状态解析和边界检测，收益不大。

### Session 隔离方案

```python
def pipe_session_id(gid: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"qq-group-{gid}"))
```

- 每个群生成固定 UUID
- 同群：每次 resume 到同一个会话
- 不同群：完全隔离，不串味
- 不能用 `--continue`（`-c`）：它会按工作目录找最近会话，多群会串

### 安全性设计

| 威胁 | 防护措施 |
|------|---------|
| 群友让机器人删文件 | `--allowedTools "WebSearch,Read"`，禁止 Bash/Write/Edit |
| 读到无关记忆 | `--bare` 禁用 CLAUDE.md 自动发现 |
| 读到历史记录 | `--add-dir` 收窄范围 + persona 约束 |
| 跑团群 @ 并发 | 同群 `asyncio.Lock` 串行排队 |

---

## 9. 常见问题排查

### 启动报错

**问题**：`claude 命令未找到`
**解决**：确认 Claude Code 已安装且 `CLAUDE_CMD` 环境变量路径正确。

**问题**：`Not logged in · Please run /login`
**解决**：环境变量未正确设置。管道进程需要 `ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL`。

### 运行时问题

**问题**：机器人不响应 @
**可能原因**：
1. 机器人 QQ 号在启动日志中是否正确显示（`已登录 QQ: xxxx`）
2. 消息是否被去重（检查日志有无该消息）
3. SnowLuma 是否在线
4. 管道模式下进程是否卡死（检查日志中错误信息）

**问题**：双响炮（一条消息机器人回复两次）
**排查**：
1. 检查是否有多个 relay.py 进程在跑
2. 检查去重日志确认 message_id 是否被缓存
3. 旧进程残留是最常见原因，杀掉残留进程后重启

**问题**：主动发言时说无关内容
**原因**：滑动窗口内容为空或管道收到的消息格式不对
**排查**：在 relay.py 中 `if should_speak:` 前加 `print(repr(speak_prompt))` 查看实际传给管道的 prompt

**问题**：管道群 @ 没有上下文记忆
**原因**：
1. 首次 @ 时使用 `--session-id` 创建，后续用 `--resume` 续接
2. 如果测试时手动调过 claude 命令占用了 session ID，会导致冲突
3. relay 有回退逻辑：resume 失败时自动用 `--session-id` 重建

**问题**：Windows 下消息中的换行符丢失
**解决**：消息通过 `stdin` 而非命令行参数传入。如果是旧版本（命令行参数传消息），必须改成 stdin 方式。

### 踩坑记忆

- **改 persona.md 后需重启** relay 才生效，因为人设是启动时一次性加载的。
- **别让进程查 `Get-Process` 误判** — `py` 启动器在 `Get-Process` 中看不到。用 `Get-CimInstance Win32_Process -Filter "name like '%python%'"` 替代。
- **去重缓存在内存中**，重启后丢失。SnowLuma 重连后可能重推旧消息，但 FIFO 淘汰机制不会让缓存无限制增长。

---

## License

MIT
