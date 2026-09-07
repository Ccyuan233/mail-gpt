# Mail GPT：用邮件与 Codex 连续对话

Python 3.11+ / Windows 优先 / SQLite / IMAP + SMTP / 官方 Codex CLI。
模型后端使用 **ChatGPT 登录的 Codex CLI**，不需要 OpenAI API key。正常请求使用该账号的 Codex 额度；实际可用额度、模型和限流由账号决定，程序不承诺无限使用。

第一版已实现核对已读和未读来信、发件人白名单、`[GPT]` / `[GPT:NEW]`、纯文本及 HTML 转文本、去引用、Gmail thread / RFC 回退、持久会话、去重、自动回复防循环、TLS 收发、错误脱敏及中断后的人工核对。没有 Web UI。

## 你需要两个邮箱

```text
user@example.com（你在手机上发问题）
    ↓ To: bot@gmail.com；Subject: [GPT] Physics
bot@gmail.com（本机程序收信 → Codex → 回信）
    ↓
手机直接 Reply，继续同一个 Codex session
```

`.env.example` 的 `ALLOWED_SENDERS` 使用示例地址 `user@example.com`。`bot@gmail.com` 和 `user@example.com` 都是文档示例；请在本机 `.env` 填入实际机器人邮箱和允许的发件人。不能把同一个地址同时用作机器人和允许的发件人，否则自回复防护会拒绝。

Gmail 插件连接用于本次核实账号，不是可导出给后台的 IMAP/SMTP 凭证。后台独立运行，不依赖 Codex 桌面任务保持打开；需要电脑开机、网络畅通、该程序持续运行。

## 1. 准备配置

在这个项目目录打开 PowerShell：

```powershell
# 首次配置；已有 .env 时不要覆盖
Copy-Item .env.example .env
notepad .env
```

填入：

```dotenv
EMAIL_ADDRESS=bot@gmail.com
EMAIL_PASSWORD=该专用邮箱的Gmail应用专用密码
ALLOWED_SENDERS=user@example.com
```

专用 Gmail 账号先开启两步验证，再在 Google 账号安全设置中创建应用专用密码。只在本机 `.env` 输入，不要把密码发到聊天里，也不要用 Gmail 主密码。某些账号/组织策略不提供应用专用密码；这种情况需要之后增加 OAuth，当前版本尚未实现 OAuth。

其余默认值适用于 Gmail：IMAP 993，SMTP SSL 465。也支持 `SMTP_SECURITY=starttls` 搭配 587。不会降级到明文连接。非 Gmail 可以改主机名，但必须按收件服务实际情况配置 `AUTH_SERV_ID`、验证认证结果头与 IMAP 支持情况。

`.env`、SQLite、邮件回答、Codex 登录状态和会话文件均被 `.gitignore` 排除。项目不读取你原有的 Codex 登录文件。

## 2. 登录 Codex 并检查

本机已发现原生 `codex.exe`，版本 **0.153.4**。机器人已通过官方 ChatGPT 登录，并完成真实两轮会话记忆测试。为防止共享个人会话、插件和项目配置，机器人使用 `data/codex-home` 独立存放官方登录状态和会话；其他电脑需要重新登录。

本机没有普通 `python` 命令在 PATH，但 Codex 附带 Python 3.12.14。辅助脚本会依次使用项目虚拟环境、Codex 附带 Python、系统 Python：

```powershell
.\run.ps1 -Command doctor -Offline
.\run.ps1 -Command login
.\run.ps1 -Command doctor
.\run.ps1 -Command smoke
```

`login` 运行官方 `codex login`，按终端提示在浏览器登录你的 ChatGPT 账号。没有自动化 ChatGPT 网页，也不会抓取 cookie。`smoke` 会实际调用模型两次，测试新建会话和记忆续接，会消耗 Codex 额度，但不发送邮件。

如果 PowerShell 因脚本执行策略阻止运行，可在本次进程使用 `powershell -ExecutionPolicy Bypass -File .\run.ps1 -Command doctor -Offline`，不需要永久修改系统策略。

其他电脑安装 Python 3.11+ 和官方 Codex CLI 后：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m mail_gpt doctor --offline
.\.venv\Scripts\python -m mail_gpt login
.\.venv\Scripts\python -m mail_gpt doctor
.\.venv\Scripts\python -m mail_gpt smoke
```

运行时只有 Python 标准库，不需要额外 pip 包。若 `codex` 在 PATH 上是 `.cmd`、`.bat` 或 `.ps1` 包装器，请将 `CODEX_PATH` 改为安装中的原生 `codex.exe` / 原生二进制的完整路径；本程序不通过 shell 执行包装器。

## 3. 开始收发

```powershell
# 首次建议只轮询一轮；这会处理扫描起始日期后符合条件且尚未回复的邮件
.\run.ps1 -Command run -Once

# 持续运行，默认每 30 秒轮询一次；Ctrl+C 停止
.\run.ps1 -Command run
```

通用命令为 `python -m mail_gpt run --once` / `python -m mail_gpt run`。

2026-09-06 已在本机启动一个隐藏的后台进程，默认每 30 秒轮询。不要再同时启动第二个实例。进程信息保存在 `data/worker-process.json`，日志在其中指定的 `stderr` 文件；正常连接会出现 `poll_complete`。可运行 `powershell -File .\stop-bot.ps1` 停止这个后台实例。没有配置开机自启，电脑重启后需要重新运行。处理中强制停止后，未完成请求可能需要按下方恢复说明核对。

从你的邮箱给专用邮箱发：

```text
Subject: [GPT] A-Level Physics

解释一下 circular motion 中 centripetal force 为什么不做功。
```

收到回答后直接 Reply：`如果速度大小改变呢？`。`Re: [GPT]` 和常见中文回复前缀都能识别。邮件解析后只传新正文，历史保存在 Codex session 中。

`[GPT:NEW] 新话题` 强制新建独立会话，即使旧话题有待人工核对的请求也可以开始。机器人的回复主题会归一为 `[GPT]`，所以之后直接 Reply 会续接新会话。旧会话和待核对记录保留：回复旧答案仍续接旧会话，回复新答案续接新会话；有明确回复引用时优先按引用选择会话，没有引用时才使用 Gmail 分组。Gmail 可能因为主题变化重新分组，程序仍通过 RFC 引用头找到同一内部会话。`[CODEX]`、`[GPT:SEARCH]` 等尚未支持的模式会被忽略。

模型响应耗时之外最多加一轮轮询延迟；不能保证每封都在几十秒内收到。同一程序串行处理邮件以保持上下文顺序，默认每小时最多接受 30 封，超出的等待后续轮询。打开邮件、Gmail 自动标为已读或其他客户端修改已读状态，不会再导致漏处理；是否已经回信以数据库中的 Message-ID 记录为准。

程序首次运行会把扫描起始日期写入 SQLite，默认从首次运行前一天开始；从旧版升级时按最早处理记录的前一天初始化。该日期不会随着重启或时间推移自动向后移动，因此离线期间的邮件仍可补处理。需要扫描更早的来信时，在 `.env` 设置 `IMAP_START_DATE=YYYY-MM-DD` 后重启。只扫描配置的收件文件夹，仍受正文大小、发件人认证和主题规则约束。`[GPT]测试` 与 `[GPT] 测试` 都有效。

检查消息状况时使用以下命令，它会同时核对实际邮箱与只读数据库，并单独报告 `pending-not-recorded`（收到但还没进入处理记录）：

```powershell
.\run.ps1 -Command status
# 或 python -m mail_gpt status
```

状态命令不发信、不修改已读状态、不恢复或修改工作中的请求。读取邮箱后会再取一次数据库快照，减少扫描期间已处理邮件被误报为待处理的情况；它仍是查询时的快照，新邮件可能在查询后到达。不要仅凭数据库中没有 `pending` 就判断不存在漏信。

## 默认安全策略及边界

- 严格匹配发件人地址和收件人；不向来信的 `Reply-To`、Cc 或其他地址发送。多发件人/缺少 Message-ID 的来信拒绝。Gmail 的 `+tag`、点号别名不会自动合并，需明确加入白名单。
- 默认要求收件服务写入的**最上方** `Authentication-Results`：认证服务器 `mx.google.com`、DMARC pass，且 `header.from` 与发件域名一致。不会扫描后续可伪造的同名头。迁移服务前需验证该服务会覆盖/正确排序认证头；不能把 From 白名单当作身份认证。邮件转发或组织邮件策略可能使认证失败而被忽略。
- 检查自己发送的邮件、Auto-Submitted、Precedence、X-Auto-Response-Suppress、List-Id、空 Return-Path 等，输出邮件带 `auto-replied` 标记。
- 默认 `read-only`、不允许申请提权、禁用 shell/执行器/浏览器/插件/Apps/MCP 插件发现/子代理/记忆/图像工具，禁用搜索，忽略用户配置与规则，不加载项目指令。使用独立 Codex home 和空工作目录。子进程环境不包含邮箱密码、API key 或父任务连接信息。
- `read-only` 本身不是“不能读本机文件”的保证，因此还关闭上述工具能力；固定提示只是补充。CLI 固定在已核实的 0.153.4，升级必须重新审查能力和测试，不要仅修改版本号绕过检查。此处是 CLI 功能与沙箱策略，不是独立虚拟机；强隔离部署应使用单独 OS 用户/容器，仅挂载机器人必需目录。真实登录后的攻击性输入验证尚未完成。
- 附件仅列出文件名和 MIME 类型，忽略内容，不保存、执行或送给模型。单封邮件默认上限 1 MiB，正文上限 24,000 字符，回答上限 512,000 字节。
- 中文/英文引用清理是启发式规则，不保证所有客户端格式及行内回复都无损。被删除的引用有时可能是问题的一部分；复杂问题建议独立写在引用上方。
- SQLite 保存最终待发送邮件正文；Codex 保存对话。它们不是加密文件。日志不记录正文、密码、认证 token 或原始异常，但会记录主题、发件人、消息和会话 ID。保护本机账号及 `data/`，备份时注意隐私。

## 去重、异常及恢复

Gmail `X-GM-THRID` 保留为字符串，避免 64 位 ID 精度丢失。会话按机器人账号、发件人隔离；Gmail ID 和收发双方的 RFC Message-ID 都登记别名。SQLite 用 `(account, message_id)` 唯一约束去重，并保存会话映射、待发送回答和稳定回复 Message-ID。

旧版曾因 IMAP capability 的字符串/字节类型判断错误而未使用 Gmail ID。升级后会依据实际收件箱补齐缺失的 Gmail 别名；如果一个 Gmail thread 曾意外产生多个旧会话，选择其中最近回答过的会话作为后续会话，保留旧会话文件和 RFC 别名，不拼接或重写历史。

处理流程：`pending → generating → ready → sending → sent`。

1. 读取使用 `BODY.PEEK[]`，不提前标为已读；确认发送成功才标记。标记失败只记日志，不改变发送结果，也不阻断本轮已读取的后续请求。
2. 回答先落盘为 `ready`。重启后可以直接发送保存的回答，不重复调用 Codex，即使原信后来已读也会处理待发箱。
3. SMTP 发送与 SQLite 无法形成跨系统原子事务。断线可能发生在服务器已经接受邮件之后，所以 `sending` 中断或发送异常进入 `review`，**不自动重发**。不声称实现严格 exactly-once。
4. Codex 失败时回复短错误和 Error ID，日志只保存异常类别。部分会话被阻断，之后用 `[GPT:NEW]` 重新开始；同一失败邮件不反复调用模型。
5. 整个进程在生成中崩溃时，重启会将它标记 `review`。此时无法安全判断模型是否已经处理过，不会自动再生成或发送；需要人工跳过原信，再发 `[GPT:NEW]`。
6. 单实例文件锁防止两个进程共用同一个数据库。请勿为同一个机器人账号启动不同数据库的多个实例。

查看待核对请求：

```powershell
python -m mail_gpt review
```

先查专用邮箱的“已发送”，按输出的 `reply_id` 检索（Gmail 可用 `rfc822msgid:...`）。明确处理后：

```powershell
# 已找到发出的回答
python -m mail_gpt resolve '<原Message-ID>' sent
# 放弃这封请求；生成中断后建议如此，再发送新的 [GPT:NEW]
python -m mail_gpt resolve '<原Message-ID>' skip
# 核对后决定重新发送本地已保存回答；下一次 run 才会发送
python -m mail_gpt resolve '<原Message-ID>' retry-send
```

如果服务器已经接受却尚未显示在已发送中，人工重试仍有重复风险。检查清楚再选择。`review` 和未解决的待发邮件会挡住同一会话后续请求，其他会话可以继续。

## 项目结构

```text
mail_gpt/
  __main__.py  启动、官方登录、自检、运行、恢复操作
  config.py    配置和校验
  mail.py      邮件层 Protocol、IMAP/SMTP、MIME 与引用处理
  security.py  白名单、收件地址、DMARC、自动回复防护
  codex.py     官方 CLI 适配器、stdin、JSONL、会话恢复
  storage.py   SQLite、会话别名、收发状态、实例锁
  status.py    只读核对邮箱与数据库，识别尚未登记的来信
  service.py   串联流程，不依赖具体邮件服务
tests/         离线单元及 subprocess 集成测试
data/          运行数据，忽略于 Git
```

没有加入尚未验证的 Docker 部署、OAuth、附件阅读或自动开机启动。这些可以在真实 Gmail 与 Codex 联调稳定后补充。

## 测试与本次验证

```powershell
python -m unittest discover -v
# 本机也可使用：
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m unittest discover -v
```

47 项离线测试使用临时数据库、模拟 IMAP/SMTP、模拟 CLI，以及独立 Python 子进程 fixture。覆盖 thread/session 持久化、RFC 引用回退、NEW、去重、白名单、DMARC、防循环、引用与附件、参数及环境隔离、错误脱敏、限流、SMTP 不确定投递和崩溃恢复；另外覆盖已读 QQ 回复补处理、无空格主题、真实 IMAP capability 类型、QQ 引用标记、扫描日期持久化、旧线程别名修复及状态对账。本次新增测试验证独立新话题避开旧请求阻塞、旧话题可继续回复、重启后不重复生成、已读标记失败不阻断处理，以及扫描期间完成的请求被正确报告。离线测试不登录、不联网、不发信。

2026-09-06 的本机验证：Python 3.12.14；Codex CLI 0.153.4；已直接读取 `exec --help`、`exec resume --help` 与 feature list。已完成官方 ChatGPT 登录；真实新会话记住随机测试词，恢复相同会话后准确回答，`smoke` 通过。CLI 当前默认选用 `gpt-6-astra`，本次首轮约 116 秒，不保证几十秒内回信。Gmail IMAP 和 SMTP 的真实 TLS 登录均通过；后台监听已连接并完成首轮读取。随后已处理实际授权来信并获得 Gmail SMTP 发送成功确认；手机端 Reply 的完整闭环尚需进一步验证。

参考代码已通过 GitHub 连接器读取：[getThreadId.js](https://github.com/tgeant/gmail-chatgpt-assistant/blob/main/emailProcessingUtils/getThreadId.js)、[index.js](https://github.com/tgeant/gmail-chatgpt-assistant/blob/main/index.js)。只借鉴 Gmail thread 思路，本项目为独立实现，没有照搬其旧 OpenAI API 调用或代码。当前官方公开配置 schema 也已读取用于检查配置字段；实际命令以本机版本为准。官方 [Codex 非交互文档](https://developers.openai.com/codex/noninteractive) 此次访问返回 Forbidden，未把页面内容视为已经核验。
