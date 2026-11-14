# yelin-send-mcp

[中文](#中文) | [English](#english)

---

## 中文

一个 stdio MCP 服务：让 AI 助手**主动**把话、图、歌、表情和文件发到你指定的地方。
出口可以同时配好几个：你自己搓的前端（webhook）、Telegram、Discord、ntfy。每个出口单独回报成功还是失败，一个坏了不耽误别的。

走标准输入输出，不开任何网络服务；只有在助手调用发送工具时才往外连你配置的出口。只依赖官方 `mcp` 库。

### 先看你需要哪个

- **只想让 AI 聊天时发表情包，而且你的聊天窗会显示工具结果里的图片**（有的 MCP 客户端会，比如 Kelivo；以你用的版本为准）：用不着这个插头，装同一个作者写的 `yelin-sticker-mcp` 就够了。
- **要把东西送到另一个地方**（你自己做的网页或 App 聊天页、Telegram、Discord、ntfy），或者要发 AI 自己画的图、写的歌、文件：装这个。发到自己的页面要有收件端，`examples/` 里有 Python 和 Node 例子，收到以后还要自己接进页面显示。配上 `yelin-sticker-mcp` 时，AI 把挑好的表情路径交给它发，这个插头要能读到那个路径。
- **Telegram、Discord、ntfy**：这三个是现成出口，按下面"出口怎么配"填好地址、目标和需要的凭据就能用（Telegram 要 bot 令牌和 chat_id，Discord 要 webhook 地址，ntfy 要服务器和主题）。

### 工具

| 工具 | 做什么 |
|---|---|
| `send_text(text, to?)` | 发一段话。 |
| `send_image(source, caption?, to?)` | 发一张图：PNG / JPG / GIF / WEBP。**动图发出去还是动图**（见[动图怎么保住](#动图怎么保住)）。 |
| `send_audio(source, title?, caption?, to?)` | 发一首歌或一段语音：MP3 / M4A / WAV / OGG。 |
| `send_file(source, caption?, to?)` | 兜底：任何文件。 |
| `list_targets()` | 列出配了哪些出口、能不能用。**不显示任何令牌**，webhook 路径和 ntfy 主题名也会隐藏。 |

- `source` 可以是本机文件路径，也可以是 `https://` 链接（服务会先下载，同样受大小上限约束）。
- `to` 可选，写出口名字，逗号分隔（如 `"phone,my-chat"`）；不写就发到所有出口。
- 文件格式按文件头判断，不看扩展名；`send_image` 收到不是图的文件会直接拒绝，`send_audio` 同理。
- 返回结果每个出口一行：`ok` 或 `FAILED` 加原因。全部失败时作为工具错误返回。
- 结果最后固定一行 `event: {...}`（一行 JSON）：这次发送的 `id`、`kind`、`ts`，发文件时还有 `file` 的 `name`、`mime`、`size`、`sha256`、`animated`。`id` 和 `sha256` 跟 webhook 收到的那份一样，接收端拿它把工具结果和收到的文件对上号。这一行不含文件内容，也不含令牌。

### 安装

需要 Python 3.10 或更新。

```bash
git clone <本仓库地址> yelin-send-mcp
cd yelin-send-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

配好出口（见下文）后，可以先在终端试一下，不用经过助手：

```bash
.venv/bin/python send_mcp.py targets            # 看看配了哪些出口
.venv/bin/python send_mcp.py send-text "测试一下"   # 往所有出口发一条
```

### 配到 Claude Code

```bash
claude mcp add --transport stdio \
  --env SEND_TELEGRAM_BOT_TOKEN=<你的 bot 令牌> \
  --env SEND_TELEGRAM_CHAT_ID=<你的 chat_id> \
  send -- /path/to/yelin-send-mcp/.venv/bin/python /path/to/yelin-send-mcp/send_mcp.py
```

`--env` 要放在服务名（这里是 `send`）前面，可以写多个。加完在 Claude Code 里用 `/mcp` 看连上没有；删掉用 `claude mcp remove send`。

注意：`--env` 的值会以明文写进 Claude Code 的配置（默认是你本机的 `~/.claude.json`；用 `--scope project` 会写进项目里的 `.mcp.json`）。**带令牌的配置不要用 project 范围，也不要把 `.mcp.json` 提交进仓库。**

其他支持 MCP 的客户端，一般是这样的 JSON：

```json
{
  "mcpServers": {
    "send": {
      "command": "/path/to/yelin-send-mcp/.venv/bin/python",
      "args": ["/path/to/yelin-send-mcp/send_mcp.py"],
      "env": { "SEND_WEBHOOK_URL": "https://chat.example.com/inbox", "SEND_WEBHOOK_TOKEN": "<令牌>" }
    }
  }
}
```

### 出口怎么配

两种方式，二选一：

- **只用环境变量**：每种出口最多一个，名字固定为 `webhook` / `telegram` / `discord` / `ntfy`。
- **配置文件**：想要多个同类出口、起自己的名字、单独设大小上限时用。配置文件里一旦写了 `outlets`，就**只用**文件里列的出口（下表里那些快捷环境变量不再生效，但令牌照样从环境变量读）。

配置文件位置：`SEND_CONFIG` 指定，默认 `~/.config/yelin-send-mcp/config.json`（遵循 `XDG_CONFIG_HOME`）。格式见 `config.example.json`。

**令牌只从环境变量读。** 配置文件里只写"令牌放在哪个环境变量里"（`token_env`，Discord 是 `url_env`）；如果在配置文件里直接写了 `token` 之类的键，会被忽略，并在 `list_targets` 里提示。

#### 通用

| 环境变量 | 配置文件键 | 默认 | 说明 |
|---|---|---|---|
| `SEND_CONFIG` | — | `~/.config/yelin-send-mcp/config.json` | 配置文件位置 |
| `SEND_MAX_BYTES` | `max_bytes` | `10485760`（10 MB） | 单个文件上限，超过就不读、不发 |
| — | 出口里的 `max_bytes` | 无 | 某个出口自己的更小上限；超过只有这个出口失败 |
| `SEND_FETCH_ALLOW_HTTP` | `fetch_allow_http` | 关 | `source` 是链接时默认只下载 `https://`；设 `1` 才允许 `http://` |
| — | 出口里的 `allow_http` | 关 | 允许这个出口走 `http://`（只建议本机调试用） |

#### webhook（给自己搓前端的人）

| 环境变量 | 配置文件键 | 默认 | 说明 |
|---|---|---|---|
| `SEND_WEBHOOK_URL` | `url` | — | 收件地址，必须 `https://` |
| `SEND_WEBHOOK_TOKEN` | `token_env`（写变量名） | 空 | 令牌 |
| `SEND_WEBHOOK_HEADER` | `header` | `Authorization` | 令牌放哪个请求头；是 `Authorization` 时自动加 `Bearer ` 前缀，其他头放原值 |
| `SEND_WEBHOOK_MODE` | `mode` | `json` | `json`：文件 base64 放进 JSON；`multipart`：JSON 和文件分开传 |
| `SEND_WEBHOOK_ALLOW_HTTP` | `allow_http` | 关 | 设 `1` 才允许 `http://`（本机调试） |

收件端怎么写，见下面的 [webhook 收到什么](#webhook-收到什么) 和 `examples/`。

#### Telegram

1. 在 Telegram 里找 **@BotFather**，发 `/newbot`，按提示起名字，拿到 bot 令牌（形如 `数字:一串字母数字`）。
2. 先给你的 bot 发一句话（bot 不能主动找没跟它说过话的人）。
3. 在浏览器打开 `https://api.telegram.org/bot<你的令牌>/getUpdates`，在返回里找 `"chat":{"id":...}`，那个数字就是 `chat_id`。
   发到群里：把 bot 拉进群，在群里发一条 `/start@你的bot名`，再看 getUpdates，群的 id 是负数。
4. 配置：

| 环境变量 | 配置文件键 | 说明 |
|---|---|---|
| `SEND_TELEGRAM_BOT_TOKEN` | `token_env`（默认就是这个变量名） | bot 令牌 |
| `SEND_TELEGRAM_CHAT_ID` | `chat_id` | 发给谁 |
| — | `api_base` | 默认 `https://api.telegram.org`；自建 Bot API 服务时改 |

Telegram 的限制：图片 10 MB、其他文件 50 MB；一段话最长 4096 字、图片说明最长 1024 字。超长的话会自动拆成几条，超长的说明会在文件后面另发一条。

#### Discord

1. 在你的服务器：**服务器设置 → 整合（Integrations）→ Webhooks → 新建 Webhook**，选好频道，点 **复制 Webhook URL**（需要"管理 Webhook"权限）。
2. **这个 URL 本身就是密钥**，谁拿到谁就能往频道里发，所以它只从环境变量读：

| 环境变量 | 配置文件键 | 说明 |
|---|---|---|
| `SEND_DISCORD_WEBHOOK_URL` | `url_env`（默认就是这个变量名） | Webhook URL |

文件用 multipart 上传，原样送达，GIF / WEBP 动图在 Discord 里会动。普通服务器单个文件上限约 10 MB。一段话最长 2000 字，超长自动拆分。发送时关掉了 @ 提及，助手的话不会 @所有人。

#### ntfy

1. 装 ntfy 手机 App（或用网页版），订阅一个主题。在公共服务器 `ntfy.sh` 上，**主题名就相当于密码**，谁知道名字谁就能看、能发，所以起一个又长又随机的名字。
2. 主题 URL 形如 `https://ntfy.sh/你的主题名`，自建服务器换成自己的地址。
3. 受保护的主题需要访问令牌（ntfy 网页版：账户 → 访问令牌，以 `tk_` 开头）。

| 环境变量 | 配置文件键 | 说明 |
|---|---|---|
| `SEND_NTFY_URL` | `url` | 主题 URL |
| `SEND_NTFY_TOKEN` | `token_env`（写变量名，可选） | 访问令牌；配置文件里写了 `token_env` 就必须设上这个变量 |
| `SEND_NTFY_ALLOW_HTTP` | `allow_http` | 局域网自建、只有 http 时设 `1` |

文字直接作为通知内容；图、歌、文件作为附件发出（`ntfy.sh` 附件上限 15 MB，几个小时后过期；自建服务器要先开启附件功能）。

### 动图怎么保住

文件始终**原样**发出去，不转码。容易把动图变成静态图的只有 Telegram 的 `sendPhoto`（它会压成 JPEG），所以动图从不走它：

| 发什么 | webhook | Telegram | Discord | ntfy |
|---|---|---|---|---|
| 文字 | `text` | sendMessage | 消息 | 通知 |
| 静态图 PNG / JPG / WEBP | 文件 | sendPhoto | 附件 | 附件 |
| GIF | 文件，`animated` 标出是否会动 | sendAnimation | 附件（会动） | 附件 |
| 动态 WEBP / APNG | 文件，`animated: true` | sendDocument（原文件，不转码） | 附件（会动） | 附件 |
| MP3 / M4A | 文件 | sendAudio（带标题） | 附件 | 附件 |
| WAV / OGG | 文件 | sendDocument（标题并进说明） | 附件 | 附件 |
| 其他文件 | 文件 | sendDocument | 附件 | 附件 |

是不是动图按文件内容判断：GIF 数帧数，WEBP 看动画标记，PNG 看有没有 APNG 的动画块。

### webhook 收到什么

每次发送一个 `POST`。请求头：

- `Content-Type: application/json`（multipart 模式是 `multipart/form-data`）
- `Authorization: Bearer <令牌>`（或你配的头，放原值）
- `User-Agent: yelin-send-mcp/0.1.0`

发文字：

```json
{
  "v": 1,
  "event": "text",
  "id": "5d1c0f2b8e9a4c3f9b7a6e5d4c3b2a10",
  "ts": "2026-01-01T12:00:00+00:00",
  "source": "yelin-send-mcp",
  "text": "在吗？给你看个东西"
}
```

发图、歌、文件：

```json
{
  "v": 1,
  "event": "image",
  "id": "8a7b6c5d4e3f40a1b2c3d4e5f6a7b8c9",
  "ts": "2026-01-01T12:00:00+00:00",
  "source": "yelin-send-mcp",
  "caption": "看这个",
  "file": {
    "name": "dance.gif",
    "mime": "image/gif",
    "size": 48213,
    "sha256": "<文件的 sha256，十六进制>",
    "animated": true,
    "data": "<文件内容，base64>"
  }
}
```

| 字段 | 说明 |
|---|---|
| `v` | 格式版本，现在是 `1` |
| `event` | `text` / `image` / `audio` / `file` |
| `id` | 这次发送的编号（32 位十六进制）。同一次发送到多个出口时编号相同，可以用来去重 |
| `ts` | 发送时间，ISO 8601，UTC |
| `text` | 只有 `text` 事件有 |
| `caption` | 随附的一句话，可能是空字符串（`text` 事件没有） |
| `title` | 只有 `audio` 事件有 |
| `file.name` | 清理过的文件名（去掉了路径和特殊字符，扩展名和真实格式一致）。**收件端也不要直接拿它当路径用** |
| `file.mime` | 按文件内容判断的类型，如 `image/webp`、`audio/mpeg` |
| `file.size` / `file.sha256` | 原文件大小（字节）和哈希，可用来校验 |
| `file.animated` | 只有图片有：`true` / `false` |
| `file.data` | 原文件的 base64；**multipart 模式没有这个字段** |

**multipart 模式**（`mode: "multipart"`）：表单里有两个字段，`payload` 是上面那段 JSON（不带 `file.data`），`file` 是文件本身（带文件名和 `Content-Type`）。文字消息只有 `payload`。适合大文件，省掉 base64 多出来的三分之一。

收件端返回任意 2xx 算成功。**3xx 一律算失败、不跟随**；其他状态码或连不上也算失败。

### 自己的前端怎么接（examples/）

`examples/` 里有两个最小的收件口子，各 60 行左右，没有任何依赖：

- `python_receiver.py`（Python 标准库 `http.server`）
- `node_receiver.js`（Node 18+ 原生 `http`）

它们做的事一样：

1. `POST /inbox`：用固定时间比较校验令牌，限制请求大小，把文件用随机名字存进 `examples/inbox/`（不信任发来的文件名），消息记进列表。
2. `GET /messages?after=<编号>`：给你的聊天页拉新消息，返回 `[{id, kind, ts, text, title, url, mime, animated}]`。
3. `GET /files/<名字>`：取图、歌、文件。

```bash
INBOX_TOKEN=换成你的令牌 python3 examples/python_receiver.py
# 或者
INBOX_TOKEN=换成你的令牌 node examples/node_receiver.js
```

聊天页每隔几秒拉一次 `/messages?after=<上次显示到的 id>`，按 `kind` 画：`text` 画成气泡，`image` 用 `<img src=url>`（GIF / WEBP 会动），`audio` 用 `<audio controls src=url>`，`file` 给个下载链接。

本机试的时候，发送端设 `SEND_WEBHOOK_URL=http://127.0.0.1:8787/inbox` 加 `SEND_WEBHOOK_ALLOW_HTTP=1`。正式用时在前面放一个 HTTPS 反向代理（Caddy、nginx 或隧道），发送端填 `https://` 地址。例子里的 `/messages` 和 `/files` 没有自己的登录，要放在你聊天页的登录后面。

### 和 yelin-sticker-mcp 配合

yelin-sticker-mcp（同系列的另一个 MCP 服务）负责"按意思挑表情"，这个仓库负责"发出去"，两个一起装：

```bash
claude mcp add --transport stdio sticker -- /path/to/yelin-sticker-mcp/.venv/bin/python /path/to/yelin-sticker-mcp/sticker_mcp.py
claude mcp add --transport stdio --env SEND_WEBHOOK_URL=https://chat.example.com/inbox --env SEND_WEBHOOK_TOKEN=<令牌> \
  send -- /path/to/yelin-send-mcp/.venv/bin/python /path/to/yelin-send-mcp/send_mcp.py
```

流程：`sticker_find("抱抱")` 挑出候选，结果里带每张图的本机文件路径 → `send_image(<那个路径>, caption="抱抱你")` 发出去。
GIF 和 WEBP 动图原样送达，在 webhook、Discord、ntfy 里都还会动，Telegram 也不会被压成静态图。
可以在给助手的说明里写一句："想发表情时，先用 sticker_find 挑一张，再把它的路径交给 send_image。"

### 安全说明

- **令牌只从环境变量读**，不从配置文件读；配置文件里写了也会被忽略。
- **令牌永远不进返回结果、不进日志**。`list_targets` 只显示出口名、类型、主机名、Telegram 的 chat_id 和"令牌已设/未设"；错误信息里如果出现令牌，会被替换成 `***`。日志写到标准错误，只记"发了什么类型 → 哪个出口 → 成功/失败"，不记消息内容。
- **只走 https**。webhook、ntfy、Telegram API、Discord 都必须 `https://`；只有显式打开 `allow_http`（或对应的 `SEND_*_ALLOW_HTTP=1`）才放行 `http://`，建议只在本机调试时用。
- **发送时从不跟随重定向**。收件地址返回 3xx 一律当失败，请求不会被转到别的地址，令牌也就不会跟着出去。
- 从链接下载 `source` 时最多跟 5 次重定向，但不会从 https 降到 http，也不带任何令牌；边下边数，超过上限立刻停。
- **大小上限**：全局默认 10 MB，在读文件之前就检查；每个出口还可以单独设更小的上限。
- 文件类型按文件内容判断；发出去的文件名会去掉路径和特殊字符。
- Discord 消息关掉了 @ 提及。
- 这个服务能读到运行它的账号能读的任何本机文件，并把它发到你配置的出口——这正是它的用途。只配你愿意让助手发往的出口；在 Claude Code 里，每次调用工具默认都会先问你要不要允许。
- 收件端要自己校验令牌、限制请求大小、不要信任发来的文件名（`examples/` 都做了）。

### 测试

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

测试只用临时目录和一个跑在 `127.0.0.1` 上的假服务器（端口由系统随机分配），不连外网，也不读你真实的配置。覆盖：https 校验、拒绝重定向且令牌不跟出去、多个出口里一个失败、动图发出去还是动图、超过大小上限、令牌不进结果和日志等。装了 Pillow 的话会多跑一项"真实动图识别"的测试，没装就跳过。

### 许可

MIT，见 `LICENSE`。

---

## English

A stdio MCP server that lets an AI assistant **reach out on its own**: send text, images, songs, stickers and files
to the places you choose. Several outlets can be active at once: your own front end (webhook), Telegram, Discord and
ntfy. Each outlet reports success or failure on its own, and one failing never stops the others.

It talks over stdin/stdout and opens no network service; it only connects to your configured outlets when a send
tool is called. The only dependency is the official `mcp` package.

### Which one do you need

- **You only want the AI to send stickers in chat, and your chat window shows images from tool results** (some MCP clients do, Kelivo for example; check the version you use): you don't need this plug; `yelin-sticker-mcp` (same author) is enough.
- **Things have to reach another place** (your own web page or app, Telegram, Discord, ntfy), or the AI should send pictures it made, songs or files: install this one. Your own page needs a receiver (`examples/` has Python and Node ones), and you still wire what arrives into the page. With `yelin-sticker-mcp`, the AI hands this plug the path of the sticker it picked, so this plug must be able to read that path.
- **Telegram, Discord, ntfy**: built-in outlets; set the address, the target and the credentials each one needs as described under "Configuring outlets" (Telegram: bot token and chat_id; Discord: webhook URL; ntfy: server and topic).

### Tools

| Tool | What it does |
|---|---|
| `send_text(text, to?)` | Send a message. |
| `send_image(source, caption?, to?)` | Send an image: PNG / JPG / GIF / WEBP. **Animated images stay animated** (see [Keeping animations animated](#keeping-animations-animated)). |
| `send_audio(source, title?, caption?, to?)` | Send a song or voice note: MP3 / M4A / WAV / OGG. |
| `send_file(source, caption?, to?)` | Fallback: any file. |
| `list_targets()` | List the configured outlets and whether they are ready. **No token is ever shown**; webhook paths and ntfy topics are hidden too. |

- `source` is a local file path or an `https://` URL (downloaded first, under the same size limit).
- `to` is optional: outlet names, comma separated (e.g. `"phone,my-chat"`); empty means every outlet.
- File types are detected from the file header, not the extension; `send_image` refuses non-images, `send_audio` non-audio.
- The result has one line per outlet: `ok` or `FAILED` with the reason. If every outlet fails, it is returned as a tool error.
- The last line is always `event: {...}`, one line of JSON: this send's `id`, `kind` and `ts`, plus for files `file` with `name`, `mime`, `size`, `sha256` and `animated`. The `id` and `sha256` are the ones a webhook receives, so a receiving page can match the tool result to the file it got. It never carries the file content or a token.

### Install

Python 3.10+.

```bash
git clone <this repository> yelin-send-mcp
cd yelin-send-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Once outlets are configured (below) you can try them from a terminal, without the assistant:

```bash
.venv/bin/python send_mcp.py targets          # list configured outlets
.venv/bin/python send_mcp.py send-text "test"  # send one message to every outlet
```

### Use with Claude Code

```bash
claude mcp add --transport stdio \
  --env SEND_TELEGRAM_BOT_TOKEN=<your bot token> \
  --env SEND_TELEGRAM_CHAT_ID=<your chat id> \
  send -- /path/to/yelin-send-mcp/.venv/bin/python /path/to/yelin-send-mcp/send_mcp.py
```

`--env` goes before the server name (`send` here) and can be repeated. Check it with `/mcp` inside Claude Code;
remove it with `claude mcp remove send`.

Note: `--env` values are stored in plain text in Claude Code's configuration (by default `~/.claude.json` on your
machine; `--scope project` writes the project's `.mcp.json`). **Don't use project scope for tokens, and never commit
a `.mcp.json` that contains them.** Other MCP clients take the same `command` / `args` / `env` JSON shown in the
Chinese section.

### Configuring outlets

Pick one of two ways:

- **Environment variables only**: at most one outlet per type, named `webhook` / `telegram` / `discord` / `ntfy`.
- **Config file**: for several outlets of the same type, your own names, or per-outlet size limits. As soon as the
  file has an `outlets` list, **only** those outlets are used (the quick variables below are ignored, but tokens
  are still read from environment variables).

Config file location: `SEND_CONFIG`, default `~/.config/yelin-send-mcp/config.json` (honours `XDG_CONFIG_HOME`).
See `config.example.json`.

**Tokens are read from environment variables only.** The config file only names the variable that holds a token
(`token_env`; for Discord `url_env`). Keys such as `token` written directly in the config file are ignored, and
`list_targets` says so.

#### General

| Variable | Config key | Default | Meaning |
|---|---|---|---|
| `SEND_CONFIG` | — | `~/.config/yelin-send-mcp/config.json` | config file location |
| `SEND_MAX_BYTES` | `max_bytes` | `10485760` (10 MB) | per-file limit; larger files are neither read nor sent |
| — | outlet `max_bytes` | none | a smaller limit for one outlet; only that outlet fails |
| `SEND_FETCH_ALLOW_HTTP` | `fetch_allow_http` | off | URL sources must be `https://` unless this is `1` |
| — | outlet `allow_http` | off | allow `http://` for that outlet (local testing only) |

#### Webhook (for your own front end)

| Variable | Config key | Default | Meaning |
|---|---|---|---|
| `SEND_WEBHOOK_URL` | `url` | — | receiver URL, must be `https://` |
| `SEND_WEBHOOK_TOKEN` | `token_env` (variable name) | empty | token |
| `SEND_WEBHOOK_HEADER` | `header` | `Authorization` | header carrying the token; `Authorization` gets a `Bearer ` prefix, other headers get the raw value |
| `SEND_WEBHOOK_MODE` | `mode` | `json` | `json`: file as base64 inside the JSON; `multipart`: JSON and file as separate form parts |
| `SEND_WEBHOOK_ALLOW_HTTP` | `allow_http` | off | `1` allows `http://` (local testing) |

#### Telegram

1. Talk to **@BotFather** in Telegram, send `/newbot`, follow the prompts and copy the bot token.
2. Send your bot a message first (bots cannot start a chat).
3. Open `https://api.telegram.org/bot<your token>/getUpdates` in a browser and find `"chat":{"id":...}`: that number
   is the `chat_id`. For a group, add the bot, post `/start@yourbot` in the group, and check getUpdates again
   (group ids are negative).

| Variable | Config key | Meaning |
|---|---|---|
| `SEND_TELEGRAM_BOT_TOKEN` | `token_env` (this name by default) | bot token |
| `SEND_TELEGRAM_CHAT_ID` | `chat_id` | where to send |
| — | `api_base` | default `https://api.telegram.org`; change for a self-hosted Bot API server |

Telegram limits: photos 10 MB, other files 50 MB, messages 4096 characters, captions 1024. Long text is split into
several messages; an over-long caption is sent as a separate message after the file.

#### Discord

1. In your server: **Server Settings → Integrations → Webhooks → New Webhook**, choose a channel, **Copy Webhook URL**
   (needs the Manage Webhooks permission).
2. **The URL itself is the secret** (anyone holding it can post to the channel), so it is read from the environment only:

| Variable | Config key | Meaning |
|---|---|---|
| `SEND_DISCORD_WEBHOOK_URL` | `url_env` (this name by default) | webhook URL |

Files are uploaded as multipart, byte for byte; animated GIF / WEBP play in Discord. The usual per-file limit is about
10 MB. Messages are capped at 2000 characters and split automatically. Mentions are disabled, so the assistant
cannot ping @everyone.

#### ntfy

1. Install the ntfy app (or use the web app) and subscribe to a topic. On the public `ntfy.sh` server **the topic
   name works like a password**: anyone who knows it can read and post. Use a long random name.
2. The topic URL looks like `https://ntfy.sh/your-topic`; use your own host if you self-host.
3. Protected topics need an access token (ntfy web app: Account → Access tokens, starts with `tk_`).

| Variable | Config key | Meaning |
|---|---|---|
| `SEND_NTFY_URL` | `url` | topic URL |
| `SEND_NTFY_TOKEN` | `token_env` (variable name, optional) | access token; if `token_env` is set in the config, the variable must be set |
| `SEND_NTFY_ALLOW_HTTP` | `allow_http` | `1` for a self-hosted server on your LAN that only speaks http |

Text becomes the notification body; images, audio and files are sent as attachments (`ntfy.sh` allows up to 15 MB
and expires them after a few hours; self-hosted servers must enable attachments first).

### Keeping animations animated

Files are always sent **as they are**, never re-encoded. The one thing that would flatten an animation is Telegram's
`sendPhoto` (it re-encodes to JPEG), so animated images never go that way:

| What | Webhook | Telegram | Discord | ntfy |
|---|---|---|---|---|
| text | `text` | sendMessage | message | notification |
| still PNG / JPG / WEBP | file | sendPhoto | attachment | attachment |
| GIF | file, `animated` tells if it moves | sendAnimation | attachment (animated) | attachment |
| animated WEBP / APNG | file, `animated: true` | sendDocument (original file) | attachment (animated) | attachment |
| MP3 / M4A | file | sendAudio (with title) | attachment | attachment |
| WAV / OGG | file | sendDocument (title goes into the caption) | attachment | attachment |
| anything else | file | sendDocument | attachment | attachment |

Animation is detected from the content: GIF frame count, the WEBP animation flag, the APNG animation chunk.

### What the webhook receives

One `POST` per send, with `Content-Type: application/json` (or `multipart/form-data`), `Authorization: Bearer <token>`
(or your header with the raw value) and `User-Agent: yelin-send-mcp/0.1.0`. The bodies are the two JSON examples in
the Chinese section above:

| Field | Meaning |
|---|---|
| `v` | format version, currently `1` |
| `event` | `text` / `image` / `audio` / `file` |
| `id` | id of this send (32 hex characters), the same on every outlet of one send, handy for de-duplication |
| `ts` | send time, ISO 8601, UTC |
| `text` | `text` events only |
| `caption` | the line sent with a file, may be an empty string (not on `text` events) |
| `title` | `audio` events only |
| `file.name` | cleaned file name (no path, no special characters, extension matches the real format). **Don't use it as a path on the receiving side either** |
| `file.mime` | type detected from the content, e.g. `image/webp`, `audio/mpeg` |
| `file.size` / `file.sha256` | size in bytes and hash of the original file |
| `file.animated` | images only: `true` / `false` |
| `file.data` | base64 of the original file; **absent in multipart mode** |

**Multipart mode** (`mode: "multipart"`): the form has a `payload` field holding the same JSON (without `file.data`)
and a `file` field with the file itself (file name and `Content-Type` set). Text messages carry only `payload`. Good
for large files: no base64 overhead.

Any 2xx response is success. **Every 3xx is a failure and is never followed**; other statuses or connection errors
are failures too.

### Wiring up your own front end (examples/)

`examples/` holds two minimal receivers of about 60 lines each, with no dependencies:

- `python_receiver.py` (Python standard library `http.server`)
- `node_receiver.js` (Node 18+ built-in `http`)

Both do the same:

1. `POST /inbox`: check the token with a constant-time compare, cap the request size, store the file under a random
   name in `examples/inbox/` (the sender's file name is never trusted), record the message.
2. `GET /messages?after=<id>`: for your chat page to pull new items, `[{id, kind, ts, text, title, url, mime, animated}]`.
3. `GET /files/<name>`: the stored images, audio and files.

```bash
INBOX_TOKEN=change-me python3 examples/python_receiver.py
# or
INBOX_TOKEN=change-me node examples/node_receiver.js
```

Your chat page polls `/messages?after=<last id shown>` every few seconds and draws each item by `kind`: `text` as a
bubble, `image` as `<img src=url>` (GIF / WEBP keep moving), `audio` as `<audio controls src=url>`, `file` as a
download link.

For a local try, set `SEND_WEBHOOK_URL=http://127.0.0.1:8787/inbox` and `SEND_WEBHOOK_ALLOW_HTTP=1` on the sender.
For real use put an HTTPS reverse proxy in front (Caddy, nginx or a tunnel) and give the sender the `https://` URL.
The examples' `/messages` and `/files` have no login of their own: keep them behind your chat page's login.

### Using it with yelin-sticker-mcp

yelin-sticker-mcp picks a sticker by meaning; this server sends it. Install both (see the commands in the Chinese
section), then: `sticker_find("hug")` returns candidates including each image's local file path →
`send_image(<that path>, caption="hugs")` sends it. Animated GIF / WEBP arrive as they are and keep moving in a
webhook front end, Discord and ntfy, and are not flattened on Telegram. A line for the assistant's instructions:
"To send a sticker, pick one with sticker_find, then pass its path to send_image."

### Security

- **Tokens come from environment variables only**, never from the config file (written there, they are ignored).
- **Tokens never appear in tool results or logs.** `list_targets` shows only names, types, host names, the Telegram
  chat_id and whether a token is set; any token that shows up in an error message is replaced with `***`. Logs go to stderr and record only
  "kind → outlet → ok/failed", never message content.
- **https only.** Webhook, ntfy, the Telegram API and Discord must be `https://`; `http://` is allowed only when
  `allow_http` (or the matching `SEND_*_ALLOW_HTTP=1`) is switched on, meant for local testing.
- **Redirects are never followed when sending.** A 3xx from a receiver is a failure; the request is not replayed
  anywhere else, so the token cannot travel with it.
- Downloading a URL `source` follows at most 5 redirects, never from https down to http, and sends no token; the
  download stops as soon as it passes the size limit.
- **Size limit**: 10 MB by default, checked before a file is read; each outlet can set a smaller one.
- File types come from the content; outgoing file names are stripped of paths and special characters.
- Discord mentions are disabled.
- This server can read any local file the account running it can read and send it to your outlets; that is its job.
  Only configure outlets you want the assistant to reach. Claude Code asks before each tool call unless you allow it.
- Receivers must check the token, cap request sizes and not trust incoming file names (the examples do).

### Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

Tests use temporary directories and a fake server on `127.0.0.1` (OS-assigned port) only: no internet, no real
config. They cover https enforcement, refused redirects with no token leaking, one failing outlet among several,
animations staying animated, the size limit, and tokens staying out of results and logs. With Pillow installed one
extra test checks animation detection on real images; without it, that test is skipped.

### License

MIT, see `LICENSE`.
