# Web LLM Bridge (Browser Bridge)

本地 MVP：**真实浏览器(Playwright) + 网页 LLM 登录态 → 流式输出 → 本地 WebSocket/HTTP**。
内置 Provider 可切换：**DeepSeek Web**(逐字流式) 与 **ChatGPT Web**(DOM 快照流)。
目标：让你的程序像用普通 LLM 客户端一样使用已登录的网页账号，且**实时看到输出**。

> ⚠️ 合规提示：这是对网页版的人工自动化，可能违反各站服务条款，账号存在风控风险。
> 仅建议**本地、低频、个人**使用；需要稳定/商用请用各站官方 API。

```
桌面端(本仓库内置 UI / 未来 Tauri)
        │  ws://127.0.0.1:8765 /ws  (事件推送)
        ▼
    FastAPI Server  (bridge/server.py)
        │  发消息: 键入 + 回车   捕获: fetch 流包装(stream) / DOM 快照(dom)
        ▼
 Playwright 持久化浏览器 (profile/<provider-id>/ 复用登录态)
        ▼
  chat.deepseek.com  /  chatgpt.com   ← 各站首次手动登录一次
```

## 目录结构

```
browser-bridge/
├── main.py              # 入口: python main.py
├── requirements.txt
├── profile/<id>/        # 运行时生成: 每 Provider 独立 Chrome 登录态(勿入库)
├── static/index.html    # 本地聊天 UI(纯前端, 零构建, 可切换 Provider)
└── bridge/
    ├── config.py        # 端口/路径/超时
    ├── providers.py     # Provider 注册表(deepseek / chatgpt)
    ├── events.py        # 协议事件(message_start/delta/message_end/...)
    ├── browser.py       # 浏览器管理: 启动/切换/登录等待/发消息/新建对话
    ├── capture.py       # fetch 流式捕获(stream 模式) + DOM 快照(dom 模式/兜底)
    └── server.py        # FastAPI + WebSocket 广播
```

## 快速开始(Windows)

```powershell
cd H:\browser-bridge
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium     # 首次下载 ~150MB

python main.py
# 浏览器打开: http://127.0.0.1:8765
```

页面左上角**下拉选择站点**(DeepSeek Web / ChatGPT Web) → 点「启动并登录」→
在弹出的浏览器窗口中登录一次；各站点登录态分别保存在
`profile/deepseek/` 与 `profile/chatgpt/`，下次自动复用。
生成中切换站点会被拒绝(409)；空闲时切换会自动关闭旧浏览器并启动新站点。

## 协议事件(WebSocket /ws)

| type | 字段 | 说明 |
|---|---|---|
| `status` | state/busy/error | 浏览器与任务状态 |
| `info` | text | 提示(如"请登录") |
| `message_start` | message_id | 一轮开始 |
| `delta` | kind=`text`\|`reasoning`, text, snapshot | 实时增量; snapshot=true 为整段替换(兜底模式) |
| `message_end` | truncated | 一轮结束 |
| `error` | text | 失败原因 |

## 回复来源: 网页模型 / 只用本地模型

输入框那一行的按钮(和左下角「连接模型」同一套按钮+弹出菜单): 点开有两个选项, 当前生效的会打勾, 菜单里还有「模型设置…」直达设置面板。

| 选项 | 行为 |
|---|---|
| `网页模型`(默认) | 消息走桥接浏览器发给当前站点(ChatGPT/DeepSeek), 和以前完全一样 |
| `只用本地模型` | **不进 ChatGPT**: `POST /api/local_chat` 直接调设置里的 planner(本地模型/API)回答; 事件协议与网页模型一致(message_start → delta* → message_end), 所以照样进历史、World 模式下照样落盘+自测 |

- 按钮上会显示当前站点的缩写(GPT/DS)或「本地」徽标; 设置里若是 API 模式, 文案自动变成「只用 API 模型」;
- 选择是全局偏好(localStorage `wlb.reply.v1`), 刷新/换会话都保持;
- 该模式下**不需要桥接浏览器窗口**(没启动也能聊), 但需要先配好本地模型或 API Key;
- 图片/PDF 附件走站点的文件上传通道, 本地模型模式下会被忽略(会提示); 勾选的工作区文本文件内容照旧一起发过去。

## 文件 / 图片上传

输入框左侧「附件」可挂载**图片与常见文档**，发送时会先经
`POST /api/chat_files`（multipart）把文件字节暂存到 bridge，再投喂给站点的
`<input type=file>` 触发其上传/预览，随后连同文字一起发送。

- 限制：单文件 ≤ 25MB；`/api/chat` 的 `file_ids` 引用暂存文件，至少需文本或文件之一；
- **文本/代码文件**默认「作为文件发送」，点击芯片上的「文件/文本」切换为「解析为文本发送」——
  会把文件内容读出来连同你的文字一起作为消息发出（超长自动截断），不再挂附件；图片只有「文件」一种方式；
- 大段文本会走快速注入写入输入框（避免逐字敲击过慢）；
- 依赖站点是否开放该类型：DeepSeek 支持图片/pdf/txt 等；ChatGPT 以图片为主（文档按页面能力）；
- 属**尽力而为**：若站点无文件输入框或拒绝类型，仅文字照常发送并给出提示。

## 本地工作区(读/写/编辑)

- 默认工作区目录 `workspace/`(见 `bridge/config.py` 的 `WORKSPACE_DIR`，git 忽略)。
- REST：`GET /workspace/tree`、`GET /workspace/file?path=`、`POST /workspace/file {path,content}`、
  `DELETE /workspace/file?path=`。**路径一律校验在工作区内**，越界返回 400。
- UI：左侧「工作区」面板浏览目录树 → 点文件名打开内置编辑器(编辑/保存/发送给 AI)；
  勾选多个文件 → 「发送选中」把内容以文本模式发进对话；
  AI 回答代码块右上角「保存」→ 输入相对路径写入工作区(自动建目录)。
- 用途：把工作区代码发给网页 ChatGPT/DeepSeek 获取修改，再把返回的代码块写回文件——
  即"发代码→收代码→落盘"人工闭环；Phase 2(规划→清单→自动应用)在此之上扩展。
- **每个会话可以各用各的工作区**(点面板标题选目录, 存在浏览器 `localStorage` 的
  `wlb.wsroot.v1`, 键是 `站点|会话id`)：切到某条会话时, **只有它单独设过**才会换目录;
  没设过的一律**沿用当前目录, 一根汗毛都不动**。以前这里是"没设过就恢复内置默认",
  于是新建会话/换会话/站点换了会话 id 都会把用户配的项目目录默默冲掉; 现已修掉,
  `tests/workspace_perconv_check.py` / `connect_fresh_check.py` 都盯着这一点。
- 跑工作区相关测试时会先记住你当前配的目录, 结束时**原样还回去**(不再一律恢复默认)。

## 编码执行器(Phase 2)

输入框下方「🧩 工程任务」：填自然语言任务 →「运行工程任务」，
本地引擎自动执行 **规划 → 分步实现 → 校验落盘 → 摘要**：

- 每个实现步骤要求网页模型输出**文件变更清单**（````json {files:[{op,path,content}]}```），
  本地解析（围栏 JSON / 裸 JSON raw_decode / 表格三种兜底）、路径校验后**自动写盘**，
  并广播 `engineer` 事件（progress/plan/apply/summary/done/error）到 UI 日志面板。
- 清单解析失败自动带错误**重试一次**；仍失败则展示原始回答供人工处理。
- 左侧工作区勾选文件 = 限定模型可见上下文；不勾 = 自动包含全部文本文件(截断保护)。
- 串行互斥：执行器运行期间(与普通聊天共用 busy 锁) 其它发送会 409。
- 限制：网页模型无工具调用，靠提示词协议 → 复杂/模糊任务可能输出不满足协议，
  引擎会明确报错而不是乱改文件；写盘严格限制在 workspace 内。

## World 模式: 落盘与验证的硬规矩(防"假通过")

用户实际踩过的坑：ChatGPT 只回了一段"要改 3 个点"的思路(没有任何代码)，本地模型自己把代码写进了
工作区；随后界面打出 **"本地模型确认项目跑通了(有验收点 + 证据)"** —— 而那次"证据"其实是：
① 它自己写的一句话，服务端只检查了"非空"；② 引用的构建产物时间早于改动；③ 真正验证的对象是
工作区里的**旧副本**(smartclip_verify)，不是用户的项目。代码本身没问题，但这个"通过"不可复核。

现在这几条由代码强制，不靠模型自觉：

**落盘(engineer.py)**
- 整文件替换的毁坏防护：旧文件 ≥2000 字符却被换成 <1/3(疑似截断)，或在根目录新建一个 <200 字符的同名
  残file(疑似路径写错) → **默认跳过**并在预览里标 `warn`(确认框默认不勾选)；条目上带 `"force": true`
  才会真的写。
- `update` 到不存在的文件 = 事实上的新建，同样受上面这条约束。

**验证(server.py `/api/world/verify`)**
- 先定验收点(`checks`)：**没有验收点不接受 done**(没有判定标准)。
- `done` 的证据必须：点名**这次真跑过且退出码为 0** 的命令 → 逐条覆盖验收点 → 引用的输出片段能在
  **真实输出**里找到(凭空编"ALL TESTS PASSED"会被打回)。
- 行为性验收点必须有**行为证据**：跑测试 / 跑刚构建出来的程序；`cmake`/`qmllint` 只算"编译证据"，
  不能当行为证据(与提示词里那句"构建通过 ≠ 需求满足"一致)。
- 复合/包装命令按输出里**最后那个** `XXX_EXIT=N` / `EXITCODE=N` 判成败——`... ; 'EXITCODE=' + $LASTEXITCODE`
  这种写法以前 shell 退出码恒为 0，满屏 ✓ 其实全失败。
- 命令引用工作区根下不存在的路径(如 `cmake -S smartclip_verify`)→ 当场点破"别拿别的副本/旧快照验证"。
- 每轮验证留痕到 `.tmp/verify-audit/<时间戳>/verdict.json`(工作区根、验收点、每条命令/退出码/输出/结论，
  外加 `.verify/` 脚手架拷贝)：`.verify/` 仍会清掉，但事后**可复核**它到底跑过什么。
- 界面把"验收点 / 验证的工作区 / 证据留痕路径"都摊在验证卡片上；ChatGPT 回答里没有代码时，落盘卡片
  会明说"这些改动由本地模型自己编写"。

**测试**
- `tests/` 里任何落盘都必须先 `workspace.use_root(临时目录)`：以前 `world_flow_check.py` 漏了这一步，
  直接把 27 字节的 `EditorArea.qml` 写进了用户配置的项目根目录。

## Planner 模型配置(Phase 3)

顶栏「⚙」设置面板(或直接改 `.bridge_settings.json`, 该文件已 git 忽略)。
**这是全局设置**：所有会话/窗口共用同一份，换会话、换窗口、换站点都不会失效。

| 字段 | 说明 |
|---|---|
| `planner.type` | `web`(默认)= 规划/摘要走当前网页 Provider(不能落盘/自测); `api` = OpenAI 兼容官方 API; `local` = 本机 llama.cpp / LM Studio / vLLM / Ollama |
| `api_base` | `api`: 如 `https://api.deepseek.com/v1`(需以 /v1 结尾); `local`: 如 `http://127.0.0.1:1234/v1` |
| `api_model` | 如 `deepseek-chat` / `qwen2.5-coder-7b-instruct` |
| `api_key` | 官方 API Key(留空=保持已存; 面板可“清除 Key”) |

选 `local` 时地址/模型必须落在真正的本机服务上：面板「检测本地模型」一键扫描常用端口(1234/8080/8081/8000/11434/5000)；
即使没点检测，保存时也会自动补全，扫不到就直接提示(不会存下一份调不通的配置)。
World 模式的**落盘/自测**需要 `api` 或 `local`；`web` 只能做规划/摘要。

分工：**规划(plan)与摘要(summary)走 planner**；**实现(implement)始终走网页大模型**。
planner 调用失败会自动回退到网页 Provider 并提示。UI 提供「测试连接」(读取 /models 验证)。

## Provider 一览(可在 UI 顶部下拉切换)

| Provider | capture_mode | 效果 | 说明 |
|---|---|---|---|
| `deepseek` | `stream` | **逐字 delta** | fetch 流包装解析 + DOM 兜底 |
| `chatgpt` | `dom` | 近实时整段快照刷新 | 当前以 DOM 快照(snapshot 型 delta)为主通道，尚未逆向其流式接口 |

`providers.py` 里每项定义站点 URL、输入框选择器、Cookie 弹窗、新建对话文案与快照选择器。
切换 Provider 会自动关闭旧浏览器、用独立 Profile 启动新站点（生成中拒绝切换）。

## 每次连接都是"新窗口"

点「启动并登录 / 切换到 X」连上之后, 不该看到上次那个界面, 消息列表也不该冒出上次的内容:

- **站点那一侧**(`browser.py: open_fresh_chat()`): 登录成功后, 如果站点还停在上次那个会话
  (URL 里带着会话 id, ChatGPT/DeepSeek 打开首页时会这样), 就主动点一次「新对话」切到新会话页;
  SPA 还没渲染出按钮时再重试一次, 失败也不影响连接本身。
- **本页这一侧**(`app.js: showFreshStart()`): 只有"本页里先见过未连接、之后才连上"才算一次新连接
  (打开页面时本来就连着 = 刷新恢复现状, **不会**清屏); 这时清空消息区、从新会话开始,
  并把标记写进 `sessionStorage`, 所以紧接着刷新页面也还是干净的一屏。
- **旧记录一条都不动**: 侧栏里照样列着历史会话, 主动点一下就能看回去(那时才恢复正常显示)。
- **不碰你的工作区**: 连接/新会话/切会话都不会把工作区目录换掉(没有单独设置过的会话沿用当前目录)。

回归: `tests/fresh_window_check.py`(后端决策) + `tests/connect_fresh_check.py`(界面行为)。

## 设计要点与已知限制(诚实清单)

1. **stream 模式主捕获通道**：注入 `window.fetch` 包装器旁听聊天接口响应流，逐行解析 JSON 里的
   `content / reasoning_content / choices[].delta.content` 等常见字段——签名/鉴权由页面
   自己完成，**不逆向破解协议**。
2. **DOM 快照通道**：stream 模式整轮无产出、或 dom 模式站点(如 ChatGPT)中，轮询页面最后一个
   回答节点整段回传(snapshot=true)，客户端整段替换——保证内容不丢；代价是"块级刷新"而非逐字。
3. **结束判定**：有内容且静默 1.6s / 页面仍在生成则等待 / 单轮 360s 超时。
4. **已知限制**：
   - 站点前端/接口随时改版：DeepSeek 需同步解析字段，ChatGPT 目前只依赖 DOM(相对抗改版)；
   - ChatGPT 的 `dom` 模式在思考期可能长时间无正文——会保持等待到超时，暂无"思考中"提示回传；
   - ChatGPT 回答若一次性整段渲染，只会收到"一次快照"，流式效果取决于其页面渲染节奏；
   - 思考过程(若页面渲染)在兜底/dom 模式下会与正文一起被捕获；
   - 单会话串行：`busy` 时新的发送会返回 409；切换 Provider 期间同样 409；
   - 没有任何加密或鉴权——本服务只应监听 127.0.0.1。

## 下一步建议

- 把事件协议接到你自己的 Scheduler / LLMProvider 抽象；
- 用 Tauri + Vue 替换内置 HTML 页，做成托盘桌面程序；
- 增加多 tab 多会话与对话历史(SQLite)；
- 若需要 ChatGPT 逐字流：登录后在 DevTools 里观察 `backend-api` 请求格式，再把 chatgpt 切到 `stream` 模式。
