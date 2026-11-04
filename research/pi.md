# Pi 调研卡

> 调研日期 2026-11-03。对象：**Pi**（GitHub `earendil-works/pi`，CLI 命令 `pi`，npm `@earendil-works/pi-coding-agent`）。前身为 `badlogic/pi-mono`（现 301 重定向至 earendil-works/pi，同源铁证）。主导者 Mario Zechner（badlogic，4,160 commits；mitsuhiko/Armin Ronacher 937 次之），主体 Earendil Works（奥地利 public benefit corporation）。约 119,776 stars，MIT，当前 v1.5.0（2026-11-02 发布），npm 周下载约 586 万。
> 自我定位："minimal, extensible agent harness you can make your own"（极简、可扩展、由你做主的 agent harness）。
> 信息源：pi.dev/docs/latest/* 与 GitHub 仓库原文（本卡所有引用均出自官方文档或仓库源码；本次调研对仓库做了 shallow clone 逐文档核对）。

## 0 身份与分层归属

**结论：Runtime / Harness 层，带强"可嵌入 SDK"属性。不是产品化平台，是"组件库 + 一个默认发行版（CLI）"。**

理由：

1. **monorepo 本身就是分层结构**（README "Packages" 表 + 实际 14 个包目录）：
   - `pi-ai`：统一多 provider LLM API（纯库，可独立使用）；
   - `pi-agent-core`：带工具调用与状态管理的 agent runtime（纯库）；
   - `pi-coding-agent`：交互式编码 agent CLI（harness，npm 包名即产品名）；
   - `pi-tui`（差分渲染终端 UI 库）、`pi-durable`（durable 执行 harness）、`pi-telemetry`（厂商中立遥测契约）、`pi-mcp`（独立 MCP 客户端）、`pi-schema`（JSON Schema 构建器）、`pi-codemode`（QuickJS 沙箱）、`pi-env`（进程/SSH/远程环境）、`chord`（跨进程对象 runtime）、`evals`。
   下层三个包（pi-ai / pi-agent-core / pi-tui）可完全不装 CLI 单独引用。
2. **四模式中两种就是"被嵌入"形态**：SDK（`createAgentSession()` 进程内嵌入，可替换全部边界组件）与 RPC（stdin/stdout JSONL 子进程，语言无关）。官方 README 直接把 **OpenClaw** 引为 "a real-world integration"——Pi 的生态位是**被别的产品当 runtime 用**，而非自己当终端产品。
3. **无平台层**：无云服务、无账号体系、无内置商业网关（Radius 是 Earendil 的独立周边产品，Pi 不依赖它运行）。与 Claude Code / Codex CLI 这类"产品 + 可扩展点"相对，Pi 是"原语 + 默认拼装"。
4. **"Primitives, not features" 哲学**：刻意不内置子 agent、plan mode、权限系统（README 原文 "skips features like sub-agents and plan mode. Ask Pi to build what you want, or install a package that does it your way"）——进一步佐证其定位是底座而非全家桶。

分层归属一句话：**Agent Runtime（库）→ Coding Harness（CLI 默认发行版）→ 可被 SDK/RPC 嵌入为更大产品的内核**。

## 1 七维度逐项

### 1.1 状态（会话持久化 / pi-durable / 断点恢复）

**CLI 会话层（pi-coding-agent）**

- 自动保存会话，默认存 `~/.pi/agent/sessions/`，按工作目录分组；可用 `--session-dir` / `PI_CODING_AGENT_SESSION_DIR` / `sessionDir` 设置重定向；`--no-session` 跑临时会话（不可恢复）。来源：pi.dev/docs/latest/sessions。
- **会话是树不是列表**：JSONL 文件，每条 entry 有 `id` + `parentId`，当前 entry 决定活跃分支。恢复入口：`pi --continue`（当前目录最近会话）、`pi --resume` / `/resume`（选择器，可搜索/改名/删除）；分支操作 `/tree`（树内跳转到任意历史点，不删旧分支）、`/fork`（从历史 user 消息分叉成新会话文件）、`/clone`（复制当前分支）。会话记录创建时的工作目录且永不更改。
- Entry 类型（session-format.md）：session 头（version 3）、message（user/assistant/toolResult/system）、`model_change`、`thinking_level_change`、`usage`、`compaction`、`context_edit`、`branch_summary`、`custom` / `custom_message`（扩展自定义持久条目）、`label`、`session_info`。
- 导出/分享：`/export`（HTML/JSONL）、`/share`（默认 Radius artifact，否则私有 GitHub gist）、`/bug`（私有诊断报告）。

**pi-durable 包（durable execution harness，实验性——"The API changes without notice between releases"）**

- 语义是**真正的 durable execution / 检查点状态机**，不是简单的会话存盘："Conversations, model turns, tool calls, and your own state are **committed to storage before anything is shown**. If the process dies mid-turn, reopening the storage picks the work up where it stopped"。即：一切变更经单一原子提交线，先落存储再展示。
- 核心概念：Harness（一个打开的存储+调度机器）/ Conversation（不可变 entry 转录）/ Commit（原子写：entry+文档+task 要么全存要么全不存）/ **Task（每一步存 checkpoint 的持久状态机**，内置 `pi.generation`/`pi.tool` 任务跑 agent loop）/ Submission（带 `requestId` 幂等——重启后同 ID 提交返回同一 submission，不重复执行）/ Document（与转录同存的类型化 JSON 状态：`pi.agent`/`pi.live`/`pi.inbox`/`pi.usage` 等）。
- **断点恢复的具体行为**：进程死亡时正在跑的模型请求记为失败尝试，按 `settings.retry` 退避重试；确定性崩溃受 `restartLimit`（默认 5）熔断成 `restart_limit` 错误结果，不会无限崩溃循环；工具调用 intent 先提交，crash 后仅 `replay: "safe"` 声明的工具重放，否则给模型返回 `interrupted` 错误结果；codemode 脚本有 journal，重启后确定性重放已完成调用继续往下跑。
- 存储后端：Memory / SQLite（Node，WAL）/ JSONL / IndexedDB（浏览器）/ **Cloudflare Durable Object**（含 `onWake` 闹钟适配）。sleep 跨重启持久（`sleepUntil` 存储后进程可退出，到点由宿主唤醒）。
- 随 Pi 1.0 一起发布（pi.dev 404 页标语 "We've shipped Pi 1.0 with Pi Durable"）；定位是给**构建长期运行/多会话/子 agent 产品**的宿主用，CLI 默认并不用它（CLI 用自己的 SessionManager）。

**断点恢复分层总结**：CLI 层=会话树 + resume（会话级）；pi-durable=任务级 mid-turn 续跑 + 幂等提交 + 崩溃熔断。

### 1.2 记忆（上下文管理 / AGENTS.md / 长期记忆）

**会话内上下文管理：compaction（压缩摘要）——机制文档化极细（compaction.md 专页）**

- 自动触发：`contextTokens > contextWindow - reserveTokens`（默认 reserveTokens 16384）；压缩时保留最近 `keepRecentTokens`（默认 20000）不动，其余生成结构化摘要。手动 `/compact [instructions]` 可带焦点指令。两者均可配、可按模型 override（`compaction.modelOverrides`，如 1M 窗口模型单独调 reserveTokens=400000）。
- 摘要是**结构化格式**：Goal / Constraints & Preferences / Progress（Done/In Progress/Blocked）/ Key Decisions / Next Steps / Critical Context + `<read-files>`/`<modified-files>` 文件清单（跨多次压缩**累计**追踪）。原始条目**永不删除**，只是不再发给模型。
- 溢出恢复：provider 报 context-overflow 或提前 `stopReason:"length"` 时，一次 compact-and-retry；失败则保留 omission 编辑不重试。
- **Branch summarization**：`/tree` 切换分支时可对被放弃分支生成摘要注入新分支（保留 abandoned 路径的成果）。thinking 块不入摘要；工具结果序列化时截断到 2000 字符。
- 扩展钩子：`session_before_compact`（可 cancel 或完全接管摘要，如换一个小模型来总结）、`session_compact_failed`、`session_before_tree`。

**AGENTS.md 支持：一等公民**

- 上下文文件发现链：`AGENTS.override.md` > `AGENTS.md` / `AGENTS.MD` > `CLAUDE.md` / `CLAUDE.MD`（同目录内 override 替换非合并）；从 agent 目录（`~/.pi/agent/`）、工作目录及其父目录向上发现。用户级全局指令放 `~/.pi/agent/AGENTS.md`；`SYSTEM.md` 整体替换默认系统提示、`APPEND_SYSTEM.md` 追加（项目级 `.pi/SYSTEM.md` 优先于全局）。来源：configuration.md。
- 注意安全面：**上下文文件的发现不需要 project trust**——不信任目录里的 AGENTS.md 也会被读入系统提示（security.md 明示 "Treat instructions in a folder as untrusted input even when you decline project trust"）。

**长期记忆：未找到内建机制。** 全部官方文档中没有任何跨会话自动记忆/用户偏好学习特性；跨会话持续性 = 会话树本身（可随时 resume）+ AGENTS.md 手写指令 + Skills（打包的指令/脚本/参考资料）+ 扩展用 `pi.appendEntry()` 存自定义持久状态。这是"Primitives, not features"哲学的直接体现——记忆要么自己写（文件/扩展），要么装包。

### 1.3 工具调用（内置工具 / 结构化输出 / 类型校验 / MCP）

**内置工具集（刻意极小）**：`read`、`bash`、`powershell`（Windows）、`edit`、`write`、`grep`、`find`、`ls` 共 8 个；**默认只启用 4 个**（`defaultTools` = read/bash/edit/write，其余 `--tools` / settings / `pi.setActiveTools()` 开启）。另有 4 个**内置扩展**（`builtin:mcp`、`builtin:llama.cpp`、`builtin:codemode`、`builtin:tool-search`）提供的工具：
- `codemode`：模型写 JavaScript 脚本（QuickJS 沙箱，无 Node API/文件/网络/定时器），脚本内 `tools.<name>()` 并行编排调用任意工具、`models.classify()/generateImages()` 调非 LLM 模型，**只有脚本输出回给模型**——大批量工具调用的 token 经济性设计；`searchTools()`（BM25）/`describeTool()`/`ALL_TOOLS` 工具发现。
- `tool_search`：按需加载 deferred 工具（大 MCP 服务器场景）。

**结构化输出**：
- 工具层：声明 `outputSchema` + 返回 `structuredContent`——模型仍看 `content` 文本，但程序化调用方（codemode 脚本、SDK）拿结构化值；`terminate: true` 可让 run 在该工具批后提前结束（官方 structured-output.ts 示例即"最终结构化答案工具收尾"）。pi-durable 工具同构（`structuredOutputSchema`/`structuredOutput`，Harness 校验）。
- 模型层没有独立的 "structured output API"（未见文档）——统一以"工具调用 + JSON Schema"实现。

**类型校验（pi-ai unified API 的 tool schema 体系）**：
- 工具参数就是 **JSON Schema**。官方推荐 `@earendil-works/pi-schema`（无 eval、8.5KB、支持 2020-12/Draft-7、编译成闭包、错误为 JSON Pointer 路径），TypeBox（`Type.Object` 等即 JSON Schema）与裸 JSON Schema 对象同样接受；`StringEnum`（pi-ai）生成所有 provider 都认的枚举。
- 校验前做**宽松矫正**：数字字符串→数字、`"true"`→true、单值→单元素数组、可选属性中的 null 丢弃；TypeBox 超出 JSON Schema 的特性（Refine/BigInt/Codec 等）明确不生效；校验错误消息（如 `- path: missing`）直接回给模型自纠。
- pi-ai 统一性：一个 `Tool` 类型/事件流跨全部 provider；usage/cost 统一记账；跨 provider mid-session handoff；chat 目录只收支持 tool calling 的模型。

**MCP（内置客户端，细节完整）**：
- 自研客户端 `@earendil-works/pi-mcp`（**不依赖官方 MCP SDK**）：stdio + Streamable HTTP（legacy SSE 明确拒绝），独立可复用。
- 配置：`~/.pi/agent/mcp.json`（用户级）+ `.pi/mcp.json`（项目级，需 project trust，同名覆盖用户级）；`pi mcp add/list/remove/login/logout`（shell 级）+ `/mcp`（会话内管理、改 exposure、重连）。工具命名 `mcp__<server>__<tool>`。
- OAuth 全套：PKCE/动态注册（DCR）/Client ID Metadata Document（CIMD，pi.dev 托管 client.json）、token 存 `~/.pi/agent/mcp-auth.json` 自动刷新、RFC 8252/9207/9728/8414 相关行为。
- **Exposure 分层**（大工具集治理）：`codemode`（默认，脚本可达不声明）/ `deferred`（tool_search 发现后声明）/ `direct`（像内置工具直接声明）/ `hidden`；`toolExposure` 支持按工具名/通配模式覆盖（如 `delete_*: hidden`）。
- 资源：`list_mcp_resources` / `list_mcp_resource_templates` / `read_mcp_resource`（兼容 Codex/OpenCode 的资源工具用法）；>20KB 文本截中间、全文落临时文件。
- **权限集成**：每个 MCP 调用走 Pi 同一条 tool pipeline——扩展的 `tool_call`/`tool_result` 钩子（权限门）对 MCP 工具同样生效；server 的 `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint` annotations 暴露给扩展做分级审批。
- 可替换性：装 `pi-mcp-adapter` 等注册 `/mcp` 的扩展即**整体替换内置 MCP**；SDK 会话不自动装 MCP 扩展（`createMcpExtension()` 手动加）。官方文档附 Claude Code/Cursor/VS Code/Codex/OpenCode 配置迁移对照表。

### 1.4 扩展机制（Pi 的灵魂，写深）

**Extensions = 加载进 Pi 进程的 TypeScript 模块**（jiti 加载、免编译、`/reload` 热重载）。位置：`~/.pi/agent/extensions/`（全局）或 `.pi/extensions/`（项目级，需 project trust）或 `-e` 临时挂载。文档第一行就是："**Pi can create extensions. Ask it to build one for your use case.**"

**API 面（ExtensionAPI，能改什么）**——按 docs/extensions.md（3,349 行专页）：

| 能力 | API | 说明 |
|---|---|---|
| 事件拦截 | `pi.on(...)` | 全生命周期事件：startup（`project_trust` 可接管信任决策）、session（start/shutdown/before_compact/before_tree/before_switch/before_fork）、agent（before_agent_start/agent_start/turn_*/agent_end/agent_settled）、model（`context`/`context_with_system` 改上下文、`before_provider_headers`/`before_provider_request` 改请求载荷、`after_provider_response`、`provider_stream_event`）、tool（`tool_call` **可 block/mutate**、`tool_result` 可改结果、`tool_execution_*` 观察一切定案调用）、input、user_bash |
| 自定义/覆盖工具 | `pi.registerTool()` | 可**同名静默覆盖全部内置工具**（read/bash/powershell/edit/write/grep/find/ls）；内置工具支持 pluggable operations 把执行委托到 SSH/容器（remote execution）；`prepareArguments` 兼容旧会话参数形状；`withFileMutationQueue` 参与文件写队列 |
| 命令/快捷键/旗标 | `registerCommand` / `registerShortcut` / `registerFlag` | `/mycommand`、ctrl+x、自定义 CLI flag |
| Provider/模型 | `registerProvider`（含异步模型发现 `refreshModels`）/ `registerVirtualModel` | 动态接任意网关/本地服务；virtual model 做路由（官方 jev-router.ts 示例：分类器模型路由请求） |
| MCP | `registerMcpServer` / `unregisterMcpServer` | 会话级注册 MCP server |
| 渲染/TUI | `registerToolRenderer` / `registerMessageRenderer` / `registerEntryRenderer` / `registerMarkdownTransformer`；`ctx.ui.custom()` | 自定义工具调用/结果的终端渲染、全键盘交互 TUI 组件（官方示例连 snake 游戏都有） |
| 持久状态 | `pi.appendEntry(customType, data)` | 自定义会话条目类型，跨重启存活 |
| 进程内执行 | `pi.exec()`、`pi.sendMessage` / `sendUserMessage`、`setModel`、`setActiveTools` 等 | 直接驱动 agent |
| HITL 原语 | `ctx.ui.confirm/select/input/notify/setStatus/setWidget` | 扩展弹出确认/选择/输入框 |

官方示例目录 40+ 个（examples/extensions/）：`confirm-destructive.ts`（rm -rf/sudo 前确认）、`git-checkpoint.ts`（每 turn 自动 git stash 检查点）、`custom-compaction.ts`（换模型接管压缩）、`custom-provider-anthropic`、`gondolin`（micro-VM 工具隔离）、`dirty-repo-guard.ts`、`handoff.ts`、`interactive-shell.ts` 等。

**Skills 机制**：实现 [Agent Skills 规范](https://agentskills.io/specification)（跨工具可移植）。`SKILL.md` frontmatter（name/description/license/compatibility/metadata/`allowed-tools`（实验性预授权列表）/`disable-model-invocation`）+ scripts/references/assets 目录；启动**只把 name+description 注入系统提示**，任务命中才读全文（渐进披露）；`/skill:name args` 强制加载；目录递归发现；支持共享位置 `~/.agents/skills/` 与 `.agents/skills/`（与 Claude Code 等其它 agent 的技能目录互操作）；官方文档直接引用 Anthropic skills 集合与 `badlogic/pi-skills` 为例。

**其它资源类型**：prompt templates（`/name` 展开的可复用消息模板）、themes（终端配色）。

**打包与分发（Pi Packages）**：包=普通 npm/git 包（约定目录 extensions/skills/prompts/themes 或 `package.json` 的 `pi` manifest；npm 包打 `pi-package` keyword）。`pi install npm:@example/pi-tools@1.0.0` / `git:github.com/user/repo@v1` / 本地路径；`pi list` / `pi remove` / `pi update --extensions`；npm 精确版本与 git tag/commit 会 pin；项目级包声明需 project trust；宿主供应的包（pi-ai/pi-agent-core/pi-coding-agent/pi-tui/typebox）必须声明为 peerDependencies。

**"官方发行版 vs 自组装"哲学的关系**：这是理解 Pi 的关键。官方发行版（CLI）只是默认拼装——SDK 可替换 `ModelRuntime`/`SettingsManager`/`SessionManager`/`ResourceLoader`/工具集**全部边界组件**；`--no-builtin-tools` 可完全去掉内置工具只跑扩展工具；`-builtin:mcp` 可逐个关掉内置扩展；pi-durable 甚至提供**另一整套** harness 组件（自带 read/write/edit/bash 工具、hooks、plan mode 示例 26-plan-mode.ts、subagent 示例）。"无子 agent / 无 plan mode"不是能力缺失，而是把这类特性降级为扩展生态的内容。

**扩展市场/收录**：有——**pi.dev/packages 官方 Package Catalog**（按 npm keyword `pi-package` 自动发现收录，`pi.image`/`pi.video` 字段提供画廊预览）。性质是目录不是治理市场（无审核签名机制）；社区集合如 `badlogic/pi-skills`、`pokutuna/pi-extensions`、`pi-extension-toolkit`（扩展脚手架）。

### 1.5 HITL（人工确认 / 中断恢复）

**前提：无内置权限系统**（security.md 原文 "Pi can read, change, and execute files with the permissions of the account that started it, and it **does not ask for approval before every tool call**"）。人工确认由三层机制承担：

1. **扩展实现 approval（官方正路）**：`tool_call` 事件返回 `{block: true, reason}` + `ctx.ui.confirm()` 弹窗确认——官方示例 `confirm-destructive.ts` 就是"bash 命令含 rm -rf/sudo 时弹确认"；MCP annotations（readOnlyHint/destructiveHint 等）供扩展做分级审批；SDK/RPC 宿主可走同一事件流自建审批 UI（RPC 模式有专门的 Extension UI record 家族在进程间转发 confirm/select/input 交互）。
2. **Project trust（启动期 HITL 门）**：项目目录含 `.pi/settings.json`、`.pi/mcp.json`、`.pi/extensions|skills|prompts|themes`、`.pi/SYSTEM.md` 等受保护资源时询问是否信任（默认 `ask`，存 `~/.pi/agent/trust.json`；`--approve`/`--no-approve` 单次覆盖；扩展可接管 `project_trust` 事件实现企业策略）。**注意它只管"要不要加载项目级可执行资源"，不约束工具调用能力**。
3. **交互中断/恢复（TUI 原语）**：Enter=steering（当前 assistant turn 后插入引导）、Alt+Enter=follow-up（run 结束后排队）、Alt+Up 收回排队消息、**Escape 中止当前 run**（排队消息退回编辑器，不丢）；`/tree` 回任意历史点重开分支；crash/退出后 `pi --continue` 恢复；`agent_settled` 事件=真正空闲信号（`agent_end` 后可能还有重试/压缩/排队）。SDK 同构：`steer()`/`followUp()`/`abort()`/`waitForIdle()`。
- pi-durable 层为产品级 HITL 多备了一档：`beforeTool` hook 直接 block、`watch()` 提交后状态全量可观察、busy conversation 的 steer/follow-up/reject 投递语义、`maxTurns` 轮数上限。

### 1.6 可观测性（pi-telemetry / 日志 / trace）

**pi-telemetry = 厂商中立遥测契约库，不是 OpenTelemetry，也不绑定任何后端**：
- 提供：回调式 `TelemetryContext`/`TelemetrySpan` 契约（显式传参传播，**无** `AsyncLocalStorage`/全局 current-span，Node/Bun/浏览器/Workers 通用）、span/attribute/event/status、counters/up-down counters/histograms、跨进程 propagation carrier + span links、`NOOP` 与 `InMemory` 参考实现、`TelemetryStore` 查询（findTraces/getTrace/renderTrace）、适配器一致性测试套件。README 明示："**no exporter, global current-span state, or dependency on a telemetry backend**...provide an adapter for **OpenTelemetry**, Sentry, logs, or another backend"——OTel 是适配目标之一，不是依赖。
- 类型化 schema 体系：`defineTelemetrySchema`/`createTypedSpanStarter` 编译期约束 span 名与属性；各域包各自出 schema——pi-ai 的 `AI_TELEMETRY_SCHEMA`（`pi.ai.*`）、pi-durable 的 `DURABLE_TELEMETRY_SCHEMA`（`pi.durable.submit/phase/entry/submission/checkpoint/replay` 事件 + `phase.duration`/`tasks.active` 指标；属性只含 ID 与控制元数据、**不含**消息/参数/结果）、pi-agent-core 保留 agent-loop/session 埋点。
- CLI 侧可观测面：footer 实时上下文用量与成本；`/session` 会话统计（tokens/cost/文件）；usage 条目写入会话文件；`~/.pi/agent/mcp.log`（MCP server 日志，5MB 轮转）；crash-log/diagnostics；`/bug` 诊断报告（含环境与 provider 配置但**不含凭据值**）。设置项：`enableInstallTelemetry`（匿名安装/更新上报，默认 **true**）、`enableAnalytics`（默认 false）。
- **局限**：CLI 发行版无内建 OTel 导出（未找到相关文档/设置）；企业要 trace/审计落地需自己写适配器扩展（`tool_execution_end` 可观察一切已定案工具调用，是审计日志的正确挂点）。

### 1.7 部署与许可证

- **许可证**：MIT（全仓库统一，含全部子包）。无 CLA/商业条款纠缠；Earendil Works 商业产品是 Radius（AI 网关，early alpha），Pi 本体无付费墙。
- **纯本地**：无云端依赖——模型目录（bundled catalog）缓存离线可用，`pi update --models` 才刷新 pi.dev overlay；Nix 构建离线并 pin `nix/model-catalog.json`；npm 安装官方建议 `--ignore-scripts`（正常运行**不需要** dependency lifecycle scripts）。
- 运行时要求 Node.js ≥22.19（macOS/Linux/Windows 安装器可代装）；Bun 独立二进制同样发布（release 流程对 node/bun 双通道 smoke test）；Nix 从源码构建（Linux/macOS ARM64+x86-64）；Windows 一等支持（PowerShell 安装器、windows.md 专页、独立 `powershell` 内置工具）。
- **无沙箱的安全含义（对比）**：与 Codex CLI（OS 级 sandbox：macOS Seatbelt / Linux landlock）和 Claude Code（内置权限门禁系统）不同，Pi 官方明文无内置权限系统，工具以启动用户全权限运行；官方对策是**文档化的容器化三模式**（containerization.md）：
  1. **Gondolin 扩展**（earendil-works/gondolin，本地 Linux micro-VM/QEMU）：pi 进程与文件型凭据留在宿主，read/write/edit/bash/grep/find/ls 与 `!` 命令路由进 VM——工具级隔离但注意 VM 内继承宿主环境变量（env 传入的 key 会泄露进 VM，官方有警告）；
  2. **Plain Docker**：整进程进容器（named volume 存容器内 `~/.pi/agent`）；
  3. **OpenShell**（NVIDIA）：文件/进程/网络/凭据/推理路由策略沙箱；另有 **Docker Sandboxes**（sbx 代理在宿主替换真实 provider 凭据）。
  security.md 还划清边界：本地 agent 预期行为、prompt injection、无沙箱默认行为**不在安全响应范围**（除非证明越过本地用户已有权限）。
- **嵌入部署形态**：SDK（Node/Bun 进程内，14 个官方示例从 minimal 到 full-control）；RPC（`pi --mode rpc`，stdin/stdout JSONL 四类 record：command/response/session event/extension UI，Python 最小客户端示例在文档里；`RpcClient` TS 封装）；print（`pi -p`）/JSON（`pi --mode json`）单发自动化；姊妹仓 `earendil-works/pi-chat` 做 Slack 等聊天自动化。**没有内置 HTTP 服务面**——企业要包 API 网关得自带（或用 chord 的 HTTP 能力自建）。
- **供应链自律（同类项目中最完整之一，README "Supply-chain hardening" 节）**：直接外部依赖精确 pin（save-exact=true）；`.npmrc` 设 `min-release-age=2` 拒绝当日新发布的依赖；package-lock 为 ground truth、pre-commit 拦截意外 lockfile 提交；pi.dev 安装器从根 lockfile 生成 `install-lock/` 钉死传递依赖；lifecycle scripts 显式 allowlist；CI `npm audit --omit=dev` + signatures 定时跑；npm 发布走 GitHub Actions OIDC trusted publishing；发布产物带 SHA256SUMS 与可离线复现的源码包。

## 2 国内可用性

**接国产/国内模型：原生支持度罕见地好**（providers.md + pi-ai README 的当前 provider 目录，v1.5 时代）：

- **内置 provider（/login 或 env key 即用，无需自配端点）**：
  - DeepSeek：`DEEPSEEK_API_KEY`
  - 智谱 **ZAI Coding Plan**：Global（`ZAI_API_KEY`）与 **China**（`ZAI_CODING_CN_API_KEY`）两套
  - MiniMax：Global + **China**（`MINIMAX_CN_API_KEY`）
  - Moonshot：Global + China（`MOONSHOT_API_KEY`）
  - **Qwen Token Plan**：Individual/Token Plan + **China**（`QWEN_TOKEN_PLAN_CN_API_KEY`）
  - Xiaomi MiMo：API 计费端点 + Token Plan cn/ams/sgp 三区域（`XIAOMI_TOKEN_PLAN_CN_API_KEY` 等）
- **任意 OpenAI 兼容端点经 `~/.pi/agent/models.json`**（models.md "Configure a compatible endpoint"，官方示例就是 Ollama）：
  ```json
  { "providers": { "ollama": { "baseUrl": "http://localhost:11434/v1", "api": "openai-completions", "apiKey": "ollama", "models": [{"id": "qwen2.5-coder:7b"}] } } }
  ```
  `apiKey`/header 支持 `$ENV`/`${ENV}` 插值与 `!command`（接企业密钥管理器）；可声明 contextWindow/maxTokens/promptCache/samplingParamsByThinkingLevel 等字段。DeepSeek/通义/智谱的 OpenAI 兼容端点均可走此路（或直接用上面内置 provider 更省事）。内置 provider 还可只改 `baseUrl` 走企业代理网关。
- **本地模型**：Ollama / LM Studio / vLLM / SGLang 走 models.json；llama.cpp 路由是**一等公民**（`/llama` 命令管理路由、GGUF 按需加载、NVIDIA RTX 专页 nvidia-rtx-llama-cpp.md）；自有协议经 provider 扩展（`registerProvider`，官方示例有 custom-provider-anthropic/gitlab-duo）。
- **无境外依赖运行**：可行——运行时唯一外联是所选模型端点（可全指国内/本地）；模型目录缓存离线可用；`--ignore-scripts` 安装。但三处基础设施在境外：pi.dev（安装脚本/模型目录 overlay/共享服务）、GitHub（git 包分发、issue）、npm registry（CLI 与扩展包）。企业内网落地需要：npm 镜像（`npmCommand` 设置可指定）、提前缓存模型目录（bundled catalog 本来就随包分发，够用）、git 包走内网 Git 托管。`httpProxy` 设置仅为 AI 请求配代理。
- **中文资料**：无官方中文文档（CLI 输出与文档全英文）。社区中文内容已形成规模：[runoob Pi 入门教程](https://www.runoob.com/ai-agent/pi-coding-agent.html)、[知乎：Pi Agent Core 设计逻辑深度解析](https://zhuanlan.zhihu.com/p/2004665077618458930)、[博客园系列教程](https://www.cnblogs.com/znlgis/p/20959153)、[CSDN 入门指南](https://blog.csdn.net/u013970991/article/details/157992930)、[graysonchen 使用指南 gist](https://gist.github.com/graysonchen/23b03af24fa98e69b7d3e532d6378b8e)、[B 站 Mario Zechner 演讲《我讨厌所有编程助手，于是自己造了一个》](https://www.bilibili.com/video/BV1RgdqBiEeg)。

## 3 企业落地注记

- **极简哲学的利弊（金融/大型企业视角）**：
  - 利：行为**完全可审计可改写**（无黑盒平台层，一切经扩展/SDK 可替换）；MIT 无许可风险；供应链自律强（pin+min-release-age+lifecycle allowlist+OIDC 发布，比多数同类开源 CLI 严谨）；纯本地、模型端点完全自主（合规面友好）；typescript 全栈，平台团队可深度定制。
  - 弊：**企业必备件全部自建**——权限审批、审计日志、长期记忆、密钥治理、usage 集中报表都要写成扩展或宿主功能；无商业支持/SLA（Earendil Works 是 PBC，商业产品线在 Radius，Pi 本体社区维护；且"新贡献者的 issue/PR 默认自动关闭、维护者每日复核"的门槛模式对紧急求助不友好）；无内置沙箱意味着**默认形态不满足金融行业终端管控要求**，必须搭配 Gondolin/Docker/OpenShell 之一落地。一句话：**把它当"agent 底座库"评估，不要当"开箱即用企业工具"评估**；没有 TS 工程能力的团队总拥有成本会很高。
- **生态位：被集成 > 集成别人**。官方引 OpenClaw 为 real-world integration（用 Pi 做持久化 agent 的产品底座）；pi-chat 面向 Slack 自动化；pi-durable 直接瞄准 Cloudflare Durable Objects 等持久执行场景。企业典型用法二选一：(a) SDK 模式做自研编码助手/内部 agent 平台的内核（替换 ResourceLoader 接内部扩展仓库、SessionManager 接审计存储）；(b) RPC 模式包一层网关做审批/代理/计量。
- **供应链管控（pi install 的包来源）**：`pi install npm:`/`git:` 即 npm registry 与 Git repo——企业需要 registry 代理 + git 白名单 + **强制精确 pin**（注意：非精确版本的 `-e` 包每次启动检查 registry，有新匹配版本就重装；不 pin 的声明会追新）。扩展在 Pi 进程内以全权限执行任意 TS，**装前 code review 是唯一防线**；建议企业配置 `defaultProjectTrust: "never"` + 扩展只从内部 registry 装。会话与凭据集中存 `~/.pi/agent/`（auth.json/sessions/），注意 `sessionDir` 重定向到受管存储、禁用/审计 `/share`（可外传 Radius/GitHub gist）与 `/bug` 上传。
- **版本节奏风险**：lockstep 版本、minor=breaking（无 major）、pi-durable 标实验性随时变 API——嵌入方应 pin 精确版本并锁 lockfile（该仓库自身供应链纪律反过来也要求下游如此）。

## 4 关键源码文件清单

monorepo 根 `packages/` 下 14 个包 + `apps/p2`（私有应用）。深读入口：

| 包 | 关键路径（`packages/<pkg>/` 下） |
|---|---|
| **ai**（pi-ai 统一 LLM API） | `src/types.ts`（Message/Tool/Usage 核心类型）、`src/models.ts`、`src/model-catalog.ts`、`src/models.generated.ts`（生成的模型目录，勿手改——经 `scripts/generate-models.ts`）、`src/providers/`（全部内置 provider 工厂）、`src/api/`（各 API 实现：anthropic/openai-completions/responses 等）、`src/auth/` + `src/oauth.ts`、`src/compat/`、`src/telemetry.ts` |
| **agent**（pi-agent-core） | `src/agent.ts`、`src/agent-loop.ts`、`src/types.ts`（AgentMessage/事件流）、`src/stream-fn.ts`、`src/proxy.ts` |
| **coding-agent**（CLI） | `src/main.ts` / `src/cli.ts` / `src/config.ts`；`src/core/agent-session.ts` + `agent-session-runtime.ts`（SDK 入口 `src/index.ts` / `src/core/sdk.ts`）；`src/core/session-manager.ts`（会话树/entry 类型）；`src/core/compaction/`（`compaction.ts`、`branch-summarization.ts`、`utils.ts`）；`src/core/tools/`（`read/bash/powershell/edit/write/grep/find/ls.ts` 八个内置工具）；`src/core/extensions/types.ts`（ExtensionAPI/全部事件类型）；`src/core/mcp-servers.ts`、`src/core/system-prompt.ts`、`src/core/skills.ts`、`src/core/settings-manager.ts`、`src/core/resource-loader.ts`、`src/core/model-registry.ts`、`src/core/project-trust.ts` / `trust-manager.ts`、`src/core/package-manager.ts`、`src/core/telemetry.ts`、`src/core/crash-log.ts`；`src/modes/`（interactive/print/json/rpc 四模式，RPC 协议定义在 `src/modes/rpc/rpc-types.ts`）；`src/extensions/`（内置扩展 mcp/codemode/tool-search/llama.cpp）；`examples/`（sdk 14 例 + extensions 40+ 例）；`docs/`（44 页文档，`docs.json` 定义站点导航） |
| **durable**（durable harness） | `src/harness/`、`src/session/`、`src/tasks.ts`（defineTask 状态机）、`src/entries.ts`、`src/documents.ts`、`src/storage/`（`sqlite/`、`jsonl/`、`indexeddb`、Cloudflare 适配）、`src/env/`、`src/codemode/`、`src/tool-search/`、`src/tools/`、`docs/spec.md`（规范文档） |
| **tui** | `src/`（差分渲染、组件、autocomplete、keybind 处理等） |
| **telemetry** | `src/index.ts`（契约）、`src/memory.ts`（InMemory）、`src/metrics.ts`、`src/noop.ts`、`src/render.ts`、`src/testing/`（conformance） |
| **mcp**（独立 MCP 客户端） | `src/client.ts`、`src/transports/`（stdio/streamable-http/pipe）、`src/protocol/`、`src/oauth/` |
| **schema** | `src/`（JSON Schema 构建+无 eval 校验） |
| **codemode** | `src/`（QuickJS runtime、wasm.ts、portable.ts） |
| **env** | `src/process.ts`、`src/ssh.ts`、`src/remote-env.ts`、`src/watch.ts` |
| **chord** | `src/`（contracts/documents/store/watch/react/sqlite/telemetry-sqlite/web/tui 等） |
| **evals** | `src/harness.ts`、`src/plan.ts`、`src/report.ts`、`src/docker.ts` |

（本清单来自对 `github.com/earendil-works/pi` main 分支 2026-11-03 的 shallow clone 实测枚举。）

## 5 信息源 URL 列表

官方（pi.dev 文档 URL 规律为 `https://pi.dev/docs/latest/<页名>`，与仓库 `packages/coding-agent/docs/<页名>.md` 一一对应；GitHub blob 链接为永久锚）：

- 仓库与 README：https://github.com/earendil-works/pi
- 文档首页：https://pi.dev/docs ；导航定义：https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/docs.json
- 会话与上下文：https://pi.dev/docs/latest/sessions ；会话文件格式：https://pi.dev/docs/latest/session-format
- 压缩参考：https://pi.dev/docs/latest/compaction
- 工作原理：https://pi.dev/docs/latest/how-pi-works
- 安全（含 project trust）：https://pi.dev/docs/latest/security ；容器化：https://pi.dev/docs/latest/containerization
- 扩展（核心）：https://pi.dev/docs/latest/extensions ；Skills：https://pi.dev/docs/latest/skills ；包：https://pi.dev/docs/latest/packages ；提示模板：https://pi.dev/docs/latest/prompt-templates
- MCP：https://pi.dev/docs/latest/mcp ；Codemode：https://pi.dev/docs/latest/codemode
- 模型与 provider：https://pi.dev/docs/latest/models ；https://pi.dev/docs/latest/providers ；models.json 字段：https://pi.dev/docs/latest/models-configuration ；自定义 provider：https://pi.dev/docs/latest/custom-provider ；本地模型：https://pi.dev/docs/latest/llama-cpp
- 四模式：交互 https://pi.dev/docs/latest/usage ；JSON https://pi.dev/docs/latest/json ；RPC https://pi.dev/docs/latest/rpc ；SDK https://pi.dev/docs/latest/sdk ；CLI https://pi.dev/docs/latest/cli
- 配置与设置：https://pi.dev/docs/latest/configuration ；https://pi.dev/docs/latest/settings
- 各库包 README：pi-ai https://github.com/earendil-works/pi/blob/main/packages/ai/README.md ；pi-agent-core .../packages/agent/README.md ；pi-durable .../packages/durable/README.md ；pi-telemetry .../packages/telemetry/README.md ；pi-mcp .../packages/mcp/README.md ；pi-schema .../packages/schema/README.md
- 包目录（扩展生态收录）：https://pi.dev/packages
- 周边：Gondolin https://github.com/earendil-works/gondolin ；pi-chat https://github.com/earendil-works/pi-chat ；pi-skills https://github.com/badlogic/pi-skills ；Agent Skills 规范 https://agentskills.io/specification ；changelog RSS https://pi.dev/changelog.xml
- 中文社区资料：runoob 教程 https://www.runoob.com/ai-agent/pi-coding-agent.html ；知乎解析 https://zhuanlan.zhihu.com/p/2004665077618458930 ；博客园系列 https://www.cnblogs.com/znlgis/p/20959153 ；CSDN https://blog.csdn.net/u013970991/article/details/157992930 ；使用指南 gist https://gist.github.com/graysonchen/23b03af24fa98e69b7d3e532d6378b8e ；B 站演讲 https://www.bilibili.com/video/BV1RgdqBiEeg
