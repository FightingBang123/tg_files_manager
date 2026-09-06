# Telegram 文件助手（File Assistant Bot）

功能类似微信的「文件传输助手」：
把文件发给 Bot → 自动分类保存 → 随时按分类 / 关键词取回。

## 核心设计：服务器零文件存储

- **文件本体不上传服务器**：Bot 收到文件后只把 `file_id` / `file_unique_id` 等
  **元数据**写入本地 SQLite（`files.db`），文件内容始终留在 Telegram 服务器上。
- 取回时直接调用 `sendPhoto / sendDocument / sendVideo ...` 并传入已存的 `file_id`，
  Telegram 会直接从自己的 CDN 把文件发给用户，本机磁盘几乎零占用。
- 同一个文件重复发送会自动去重（按 `file_unique_id`），不会产生重复记录。

## 与「收藏消息」的结合

- Bot 发出的每条消息都自带 Telegram 原生的 ⭐ 收藏按钮（点开消息菜单即可看到），
  一键即可把内容存入 Telegram「收藏消息」，之后可在任意设备离线取用。
- 建议给重要文件先 `/get` 重新取回后再收藏，避免原始消息被清理导致 `file_id` 失效。

## 功能清单

| 命令 | 说明 |
| --- | --- |
| `/recent [n]` | 最近 n 个文件（默认 10，最多 30） |
| `/list` | 按分类浏览（图片 / 文档 / 文本 / 视频 / 音频 / 语音 / 贴纸 / 动画 / 视频笔记） |
| `/search <关键词>` | 搜索文件名、说明文字、文本正文、`#标签` |
| `/note <内容>` | 保存一段文本；回复某条消息后发 `/note` 可保存被回复的内容 |
| `/get <id>` | 取回文件或文本（文件用 `file_id` 重发，本体仍在 Telegram 服务器） |
| `/tag <id> #标签...` | 追加自定义标签 |
| `/rename <id> 新名称` | 重命名（便于搜索） |
| `/del <id>` | 删除本地记录（不影响聊天中的原消息） |
| `/stats` | 统计各分类数量 |
| 直接发文件 / 文本 | 自动保存、分类、解析 `#标签`；转发文本也会自动保存 |

**Inline Mode**：在任意聊天输入 `@你的Bot <关键词>` 即可直接检索并发送文件
（需在 BotFather 中执行 `/setinline` 开启）。

## 快速开始

```bash
cd tg_files_manager
python -m venv .venv && source .venv/bin/activate   # 可选，本机已装依赖可跳过
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env：填入 BOT_TOKEN 与 ALLOWED_USER_IDS（用 @userinfobot 查自己的 ID）
python bot.py
```

启动日志：

```text
Connected to bot @xxx_bot (id=...)
Bot is running with long polling. Metadata DB: files.db
```

## 环境变量

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `BOT_TOKEN` | ✅ | BotFather 创建的 Token |
| `ALLOWED_USER_IDS` | 建议 | 允许使用的用户 ID，逗号分隔；留空 = 所有人可用 |
| `DB_PATH` | 否 | 元数据库路径，默认 `files.db` |
| `TIMEZONE` | 否 | 展示时间时区，默认 `Asia/Shanghai` |
| `PROXY_URL` | 否 | 代理地址，如 `http://127.0.0.1:7890`、`socks5://127.0.0.1:1080`；留空则用系统环境代理 |
| `CONNECT_TIMEOUT` / `READ_TIMEOUT` / `WRITE_TIMEOUT` / `POOL_TIMEOUT` | 否 | httpx 超时秒数，网络差时调大（默认 15/30/30/15） |

## 目录结构

```text
tg_files_manager/
  bot.py           # 主程序：命令 / 分类 / 搜索 / 取回 / inline mode
  storage.py       # SQLite 元数据层（只存 file_id，不含文件本体）
  requirements.txt
  .env.example
  .env             # 本地配置，勿提交 Git
  files.db         # 运行时生成
```

## 网络故障排查（ConnectError）

若出现 `httpx.ConnectError` / `httpcore.ConnectError`（连不上 api.telegram.org）：

1. **网络是间歇性抖动**：PTB 对 get_updates 是无限重试（max_retries=-1），
   本 Bot 已注册错误处理器，网络恢复后会自动继续，无需重启。日志只会出现一行
   `网络错误（PTB 会自动重试，无需处理）`，不再刷屏堆栈。
2. **需要代理**：在 `.env` 配置 `PROXY_URL`（如 `http://127.0.0.1:7890`），
   同时作用于普通 API 请求和 get_updates 长轮询；或直接设置系统环境变量
   `HTTPS_PROXY`（httpx 会自动读取）。
3. **超时太短**：调大 `CONNECT_TIMEOUT` / `READ_TIMEOUT`。
4. 若 Bot 启动时 getMe 能通但轮询断连，多为防火墙对长连接重置，走代理即可。

## 注意事项

- `file_id` 与 Bot 绑定，换 Token 后旧 `file_id` 全部失效。
- 若删除与 Bot 聊天中的原始文件消息，Telegram 服务器可能清理文件，`/get` 会报错。
- 本工程为个人使用设计，`ALLOWED_USER_IDS` 建议务必填写，避免他人占用。
- 群聊 / 频道中的消息不会被保存（仅处理与 Bot 的私聊）。
