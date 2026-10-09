# QQ Relay Chatbot

基于 **OneBot v11 WebSocket** 的 QQ 群聊中继机器人，支持 [SnowLuma](https://github.com/SnowLuma/SnowLuma) 和 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)。

按群选择直调 API 或 Claude Code CLI 管道会话，通过本地 WebUI 配置机器人、管理知识库、查看日志，以及启动、停止和重启中继。

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/WebUI-FastAPI-009688?logo=fastapi&logoColor=white)
![OneBot](https://img.shields.io/badge/OneBot-v11-5865F2)
![License](https://img.shields.io/badge/License-AGPL--3.0-red)

> 交流群：**940358918**
>
> 本仓库提供 Python 源码版。以下安装、运行与开发说明均以源码版为准。

## 功能

- **分群模式**：`direct` 直调 Anthropic 兼容 API；`pipe` 通过 Claude Code CLI 保留会话。
- **主动接话**：管道群普通消息累积到随机阈值后触发回复，也可通过 @ 立即触发。
- **管道读图**：按群开启，将图片转换成短文字描述，再加入聊天上下文。
- **本地控制台**：仪表盘、群设置、管道状态、知识库、插件、连接配置和运行日志。
- **独立启停**：停止中继后，控制台仍可用于改配置和排错。
- **群聊功能**：骰子、表情包图床、链接摘要、戳一戳回复、新人欢迎和入群申请通知。
- **日志留存**：终端、网页和磁盘共用凭据脱敏链路；文件自动轮换。

## 两种群模式

| | `direct` 直调模式 | `pipe` 管道模式 |
|---|---|---|
| 适用场景 | 问答、跑团、只想被 @ 时回答 | 持续聊天、上下文续接、主动接话 |
| 回复方式 | Anthropic 兼容 HTTP API | Claude Code CLI 子进程 |
| 触发方式 | @ 机器人 | @ 机器人，或普通消息累积到阈值 |
| 上下文 | 本地对话历史，最多 50 条 | 最近 30 条群消息与 CLI 会话 |
| 图片理解 | 不参与 | 可按群开启，先转换为文字描述 |
| 群内管理命令 | 主人可用 | 忽略 `!` 管理命令 |

默认主动接话阈值为 **4–8 条**，可在管道状态页调整。图片描述仍依赖支持图像输入的 API / 模型，不是把原图直接传给 CLI。

## 快速开始

### 1. 准备依赖

| 依赖 | 用途 |
|---|---|
| Python 3.10+ | 运行中继、WebUI 和插件 |
| SnowLuma 或 NapCatQQ | QQ 登录与 OneBot v11 WebSocket 服务 |
| Anthropic 兼容 API | 直调回复，以及开启读图后的图片描述 |
| Node.js 和 Claude Code CLI | 仅管道模式需要 |

下载源码并安装 Python 依赖：

```bash
git clone https://github.com/Soenchin/QQ-relay-Chatbot.git
cd QQ-relay-Chatbot
python -m pip install -r requirements.txt
```

建议使用独立 Python 虚拟环境。需要管道模式时，另行安装并配置 Claude Code CLI：

```bash
npm install -g @anthropic-ai/claude-code
```

### 2. 开启 OneBot WebSocket

在 SnowLuma / NapCatQQ 中登录机器人 QQ，并开启 **WebSocket 服务端（正向 WebSocket）**。中继作为客户端连接它，默认地址是：

```text
ws://127.0.0.1:3001
```

记录实际地址和访问 Token，稍后填入配置页。**仅在普通 QQ 客户端登录，不会提供中继需要的 OneBot 接口。**

### 3. 打开配置页

```bash
python webui.py
```

访问 **<http://127.0.0.1:8800/#/settings>**，在“连接与配置”中填写：

- 机器人名称、主人 QQ 号；主人 QQ 为 `0` 时不指定主人。
- OneBot WebSocket 地址和 Token。
- 直调 / 读图使用的 API 地址、Key 和模型。
- 使用管道模式时的 Claude CLI 命令。Windows 常用 `claude.cmd`，也可填写完整路径。

默认只打开控制台，**不会自动连接 QQ**。保存后会创建项目旁的 `.env`；群模式和读图开关在“群设置”中配置。

也可复制 [`.env.example`](.env.example) 为 `.env` 后手动编辑。示例 Key / Token 只是占位符，必须换成自己的配置。

### 4. 启动中继

点击页面顶部的 **启动中继**。显示“运行中”且日志出现“已登录 QQ”“开始监听”，表示 OneBot 已连通；这不等于 API 或 CLI 已验证可用。

后续希望打开控制台时自动启动中继，可运行：

```bash
python webui.py --start
```

也可使用原入口：

```bash
python relay.py --webui
```

不要同时运行两份控制台使用同一数据目录。

## 启动、停止与配置生效

| 操作 | 行为 |
|---|---|
| 启动中继 | 重新读取已保存的配置，初始化资源并连接 OneBot |
| 停止中继 | 关闭连接、消息任务、HTTP 客户端、图床和自有 Claude 子进程树；控制台保留 |
| 重启中继 | 先停止，再读取新配置启动 |
| 关闭浏览器标签页 | 不会停止控制台或中继 |
| 在终端按 Ctrl+C | 退出控制台，并清理本进程拥有的中继资源 |

- 启动配置校验失败时，控制台仍可用来修复配置、查看日志。
- OneBot 断开后会自动重连；手动停止会取消重连。
- 停止会中断未完成的回复，但**不会结束独立运行的 SnowLuma / NapCat**。
- 清理失败时保留资源归属并显示错误，不能直接重复启动；请先重试停止并检查日志。
- 机器人设置保存后可通过“重启中继”应用。**WebUI 地址 / 端口、当前日志目录的变更需要退出并重开整个控制台**。
- 读图开关会尽量热更新；群模式变更建议重启中继。
- 系统或启动脚本设置的环境变量优先于 `.env`，配置页会提示覆盖项。

若只运行无界面中继，设置 `WEBUI_ENABLED=false`，然后运行 `python relay.py`，不要加 `--webui`。

## 常用配置

完整示例与注释见 [`.env.example`](.env.example)。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BOT_NAME` | `QQ Bot` | 机器人名称 |
| `MASTER_QQ` | `0` | 主人 QQ；影响管理命令和管道写权限 |
| `NAPCAT_WS_URL` | `ws://127.0.0.1:3001` | OneBot WebSocket 服务端地址 |
| `NAPCAT_TOKEN` | 空 | OneBot 访问 Token |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/anthropic` | Anthropic 兼容 API 基地址 |
| `DEEPSEEK_API_KEY` | 空 | 直调 / 读图 API Key |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 直调 / 读图模型 ID，需与服务商支持的模型一致 |
| `CLAUDE_CMD` | `claude` | CLI 命令；示例文件使用 `claude.cmd` |
| `GROUP_MODE` | 空对象 | 群号到 `direct` / `pipe` 的映射 |
| `FALLBACK_MODE` | `direct` | 未单独配置的群使用的模式 |
| `GROUP_VISION` | 空列表 | 开启管道读图的群号 |
| `MEM_DIR` | `./memory` | 知识、对话、插件配置和表情包目录 |
| `PIPE_ADD_DIR` | `./memory` | CLI 工作目录与可读取目录；读图 inbox 也在此目录下 |
| `MEME_SERVER_PORT` | `8801` | 表情包图床端口 |
| `WEBUI_ENABLED` | `false` | 是否随 `relay.py` 启动 WebUI；示例文件设为 `true` |
| `WEBUI_HOST` | `127.0.0.1` | 控制台监听地址 |
| `WEBUI_PORT` | `8800` | 控制台端口 |

分群示例：

```dotenv
GROUP_MODE={"123456789":"pipe","987654321":"direct"}
FALLBACK_MODE=direct
GROUP_VISION=[123456789]
```

API 配置页不会改写 Claude CLI 自身的配置，也不会自动发起付费 API 测试。

## 数据目录与备份

默认使用**项目目录旁**的 `.env` 和 `memory/`，不依赖启动命令所在的工作目录。

需要单独存放数据时：

```bash
python webui.py --data-dir "./QQBotData"
# 自动启动中继：
python webui.py --data-dir "./QQBotData" --start
```

也可以通过环境变量 `QQBOT_DATA_DIR` 指定数据根；`--data-dir` 优先。数据根需在启动时指定，不是在 `.env` 中切换。

- `.env` 位于数据根目录。
- 相对 `MEM_DIR`、`PIPE_ADD_DIR` 均相对于数据根解析；绝对路径保持原位置。
- 同一数据根只允许一个控制台 / 中继实例。锁由操作系统释放，正常退出或崩溃后不需要手动删除 `.relay.lock`。
- 数据根不可写时明确报错，不会静默换到其他目录。
- **更改路径不会迁移旧文件**。迁移前先停止程序、备份配置和数据，再复制到新位置并核对路径。

默认数据内容：

```text
.env                       # 连接与机器人配置（含明文凭据）
memory/
├── persona.md             # 机器人设定
├── knowledge/             # Markdown 知识库
├── conv/                  # direct 群对话历史
├── memory/                # 记忆文件
├── inbox/                 # 管道读图临时文件
├── memes/                 # 表情包索引与图片
├── plugins.json           # 插件开关
└── logs/                  # 默认布局下的日志
```

自定义 `PIPE_ADD_DIR` 时，`inbox/` 跟随该路径；指定独立数据根时，日志改为数据根下的 `logs/`。Claude CLI 自身的用户配置与会话由 CLI 管理，不保证全部位于上述目录内。

备份时保留 `.env`、`MEM_DIR`、自定义的 `PIPE_ADD_DIR` 及所需 CLI 配置。不要把包含凭据和聊天内容的备份公开分享。

## WebUI

默认地址：**<http://127.0.0.1:8800>**。

| 页面 | 功能 |
|---|---|
| 仪表盘 | 连接状态、运行时长、实时消息与统计 |
| 群设置 | 分群模式、读图开关、配置预览与保存 |
| 管道状态 | 会话计数、消息积累、触发阈值与最近上下文 |
| 知识库 | 新建、编辑和删除 Markdown 文件 |
| 插件管理 | 全局 / 分群开关、重载插件注册表 |
| 连接与配置 | 编辑机器人身份、QQ / API 连接、CLI 路径和端口 |
| 运行日志 | 增量刷新、筛选、暂停滚动、复制与存储位置 |

配置页只写变更字段，保留其他配置及注释，整批校验通过后原子保存。Key / Token 不回显：留空保留原值，明确勾选清除才删除。原“发消息”页面已移除，旧 `#/send` 书签会跳转配置页；发送 API 保留兼容。

界面采用雾灰 / 鼠尾草绿浅色主题，支持窄屏和键盘操作。数字滚动动画遵循系统“减少动态效果”设置；知识库删除需二次确认，失败时保留编辑内容。

中文字体使用随项目提供的 Adobe **思源黑体简体中文 2.005 可变版**，无需安装或访问外部 CDN。完整字体约 13.61 MiB；首次加载时先使用系统字体。来源与 SIL OFL 1.1 许可见 [`static/fonts/README.md`](static/fonts/README.md)。

## 日志与排错

启动输出、stdout / stderr 和未捕获的 Python 异常会写入运行日志，同时保留终端输出。

- **网页**：本次进程最近 1000 条，每 1.5 秒增量刷新；离开日志页停止轮询。
- **文件**：默认 `<MEM_DIR>/logs/relay.log`；独立数据根下为 `<数据根>/logs/relay.log`。
- **轮换**：当前文件加 **1 份备份** `relay.log.1`，每份约 2 MiB，总保留约 4 MiB；重启不清空文件。
- **暂停滚动**：只停止自动滚到底部，不停止接收。
- **清空显示**：仅清除当前页面内容，不删服务器内存或磁盘日志。
- **复制**：复制当前筛选结果；剪贴板不可用时提供手动复制。
- **跨重启记录**：网页只显示当前进程，旧记录查看本地文件。

已配置的 Key / Token 和常见凭据格式在进入终端、网页与文件前脱敏。**脱敏不等于匿名化**：消息、昵称、QQ 号和路径仍可能出现，未知凭据格式也不能保证全部识别，分享前务必检查。

文件写入失败时会继续保留内存日志并提示错误。突然断电或强杀可能丢失尚未完成的一行。普通 `print` 的日志级别按内容推断，不等同于结构化业务事件。

### 常见问题

**一直显示“重连中”**

确认 SnowLuma / NapCat 已登录、启用的是 WebSocket 服务端，并核对地址、端口与 Token。查看日志中的连接错误；普通 QQ 登录本身不提供 OneBot 服务。

**找不到 `claude` 命令**

确认已安装 Claude Code CLI，且当前进程能访问其 PATH。Windows 可设置 `CLAUDE_CMD=claude.cmd`，或填写完整路径。更改系统 PATH 后需重开控制台。

**收得到消息但不回复**

检查该群模式；direct 群需要 @，pipe 普通聊天需要累计到阈值。再检查 API / CLI 是否可用、模型 ID 和 Key 是否正确。连接 OneBot 成功不代表 AI 服务已就绪。

**读图没有生效**

确认该群为 pipe 且开启读图；模型必须支持图片输入。查看 `[读图]` 日志。下载或识别失败时会按普通文本继续。

**启动失败，提示图床端口占用**

检查 `MEME_SERVER_PORT`，停止占用它的多余实例或换端口后重启中继。不要随意结束不认识的进程。

**重复回复 / 双响**

可能有多个中继连接同一个 QQ。实例锁只限制相同数据根；不同目录或旧版本仍可能重复运行。核对进程归属后停止多余实例。

**保存配置后没有变化**

检查页面是否提示环境变量覆盖，并按配置类型重启中继或整个控制台。更改数据目录不会自动搬迁旧内容。

## 群聊功能与插件

### 内置命令

所有群可使用骰子：`.r d20`、`.r 3d6`、`.r 100`。

以下管理命令仅对 **direct 群中的主人** 开放：

| 命令 | 作用 |
|---|---|
| `!帮助` | 显示命令表 |
| `!清空记忆` | 清空当前群的直调历史 |
| `!重载` | 重读人设、知识库和记忆 |
| `!人设 内容` | 更新人设并重载 |
| `!知识` | 列出知识库文件 |
| `!状态` | 查看当前群历史统计 |
| `!打标` | 列出待分类表情包 |
| `!标 文件名 标签` | 归档表情包并添加标签 |

表情包索引默认位于 `memory/memes/archive/index.md`。发送前会剥离不存在或越界的本地图床图片引用，避免失效图片导致整条文字回复发送失败。

### 插件接口

内置插件包括 Bilibili / GitHub / 通用网页链接摘要、戳一戳回复、新人欢迎和入群申请通知。可在 WebUI 中全局或按群启停；配置保存在 `MEM_DIR/plugins.json`。

三类注册入口：`register()` 消息、`register_notice()` 通知、`register_request()` 请求。消息插件示例：

```python
async def my_plugin(bot, gid, uid, nick, text, is_at):
    if "关键词" in text:
        await bot.send_group(gid, "收到")
        return True  # 已处理，不再继续 AI 回复
    return False

register("my_plugin", "关键词回复", my_plugin, default_enabled=True)
```

现有接口与实现见 [`plugins.py`](plugins.py)。

## 安全注意事项

- **不要直接把 WebUI 暴露到公网。** 默认仅监听 `127.0.0.1`；配置、日志和启停接口限制本机同源访问，但这不等于整个控制台具备完整的用户鉴权。
- `.env` 中的凭据是明文。不要提交真实配置、知识库、聊天记录、日志或备份。
- 管道模式会启动具有工具权限的 CLI。普通群友默认开放 `WebSearch`、`Read`、`Glob`；主人 @ 时会额外开放写入和命令工具。
- 谨慎填写 `MASTER_QQ`，不要把 `PIPE_ADD_DIR` 指向整个磁盘、私人文档或含凭据的目录。
- 网页、群消息和知识内容可能包含不可信指令；工具权限限制不应被当作完整沙箱。

## 开发与验证

### 模拟 WebUI

需要 Node.js 22+，无需安装前端依赖：

```bash
node tools/preview.mjs
```

打开 <http://127.0.0.1:8812>。预览只使用内存模拟数据，不启动 QQ 中继、不读写真实 `.env` 或知识库；重启预览会重置内容。

### 测试

```bash
# 后端单元 / 集成测试
python -m unittest discover -s tests -v

# 浏览器回归测试，默认使用本机 Windows Edge
node tools/test-webui.mjs
# 也可指定 Chromium / Edge 可执行文件
node tools/test-webui.mjs "/path/to/chromium"
```

后端测试使用临时文件、模拟 OneBot、临时回环图床和测试专用子进程，覆盖配置权限、日志脱敏 / 轮换、路径、实例锁、启停竞态和资源清理。不连接真实 QQ，不调用付费 API。

浏览器测试使用随机端口模拟服务和独立浏览器配置，检查响应式布局、数字动画、删除确认、配置表单、日志及启停交互。截图和报告默认写入系统临时目录 `relay-ui-artifacts`，可用 `UI_ARTIFACTS` 指定位置。

### 主要文件

```text
relay.py                  # OneBot、群消息分流、CLI、读图和直调 API
relay_control.py          # 启动、停止、重启与重连状态
owned_process.py          # 自有 CLI 子进程树管理
app_paths.py              # 资源 / 数据路径与实例锁
env_config.py             # 配置解析、校验与原子写入
runtime_logs.py           # 日志采集、脱敏和轮换
webui.py                  # FastAPI API、WebSocket 和静态文件服务
plugins.py                # 插件注册表与内置插件
static/                   # 原生 HTML / CSS / JavaScript 界面
tests/                    # 后端测试
tools/preview.mjs         # 模拟服务
tools/test-webui.mjs      # 浏览器回归测试
.env.example              # 配置示例
```

提交改动前运行相关测试，避免把真实凭据、用户数据或本机运行产物加入 Git。反馈问题时请提供 Python 版本、OneBot 实现、脱敏日志和复现步骤。

## 许可证

[AGPL-3.0](LICENSE) · Copyright (C) 2026 Soenchin

思源黑体单独遵循 [SIL OFL 1.1](static/fonts/OFL.txt)。
