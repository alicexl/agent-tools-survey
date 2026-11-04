# OpenClaw 调研卡

> 调研日期：2026-11-03 ｜ 版本基线：v2026.10.3（文档更新至 v2026.10.9）｜ 仓库：github.com/openclaw/openclaw ｜ 392,894 stars ｜ MIT ｜ © OpenClaw Foundation 501(c)(3)
> 本卡为文档层深读（docs.openclaw.ai + GitHub README + 外部公告/新闻），源码深读另行进行。

---

## 0 身份与分层归属

**结论：成品/产品层 + 运行时基础设施的复合体——「个人 AI 助理产品」的外壳 + 「本地 Agent 控制平面（Gateway）」的内核，且模型/渠道/工具/记忆全部插件化。**

- 产品层证据：面向最终用户的个人助理形态——20+ 消息渠道（Discord/iMessage/Slack/Teams/Telegram/WhatsApp/飞书/微信…）、桌面/iOS/Android 客户端、`openclaw dashboard` 浏览器控制台、语音（Talk）。README 定位 "Your own personal AI assistant. Any OS. Any Platform."
- 运行时基础设施证据：Gateway 是 "the local control plane for sessions, tools, events, and channel connections"——常驻守护进程（默认 127.0.0.1:18789，WebSocket + HTTP 复用），承载会话路由、工具策略、审批、定时任务、节点管理、可观测性导出；对外暴露 OpenAI 兼容 HTTP API（/v1/chat/completions、/v1/responses、/v1/models、/v1/embeddings，默认关闭）与 MCP server（`openclaw mcp serve`），可被其他程序当作 Agent 后端调用。
- 插件化内核："State, memory, and credentials live on your hardware. Models and agent harnesses (Claude, Codex, local models) are plugins you can swap." 渠道、模型 provider、记忆引擎、语音、沙箱后端、可观测性导出器全部是插件（核心 npm 包内置约 70 个 + 官方外部包 22 个），公共插件 SDK 约 150 个入口、CI import guard 强制边界。
- 治理：独立 501(c)(3) 基金会，"no paid tier, hosted service, or token"（无付费层/无托管服务/无代币），签名发布 + npm provenance。作者 Peter Steinberger 2026-02 加入 OpenAI，项目独立（"OpenAI is a donor, not an owner"）。沿革：Clawdbot（2025-11）→ Moltbot（2026-01-27）→ OpenClaw（2026-01-30）。
- 工程形态：TypeScript + pnpm monorepo；npm 包名 `openclaw`，CLI `openclaw`；Node 24.22.1+/26.12.1+（推荐 26）；开发需 pnpm workspace（plain npm 不支持）。

来源：
- https://github.com/openclaw/openclaw
- https://docs.openclaw.ai/start/why-openclaw
- https://docs.openclaw.ai/plugins/plugin-inventory
- https://docs.openclaw.ai/agent-runtime-architecture

---

## 1 七维度逐项

### 1.1 状态（会话持久化 / 恢复 / checkpoint）

**逐项结论：**

- **会话模型与持久化**：会话按 `sessionKey` 路由（形如 `agent:main:...`）。默认所有 DM 跨渠道汇入共享 main 会话（个人助理默认）；`session.dmScope` 可切换 `main` / `per-peer` / `per-channel-peer`（安全 DM 模式）/ `per-account-channel-peer`。群聊/房间天然隔离，cron 每次新会话，webhook 隔离。会话与转录持久化在每 agent 的 SQLite：`~/.openclaw/agents/<id>/agent/openclaw-agent.sqlite`（旧版为 `sessions/<sessionId>.jsonl` 转录文件）。`session.identityLinks` 可把同一人在多渠道的 DM 会话合并为一个规范身份。
- **会话重置**：每日 04:00 滚动重置（默认）+ `session.reset.idleMinutes` 空闲重置 + `/new` `/reset` 手动；`session.maintenance`（mode=enforce, pruneAfter 30 天, maxEntries 500）做维护；CLI `openclaw sessions --json`、会话内 `/status`、`/context list`。
- **Gateway 重启恢复（强项，文档极详）**：SQLite 承载全部可恢复状态（历史、被打断的 turn、子代理注册表、后台任务、投递队列、cron 存储、restart sentinel）。三套检测机制：turn admission 在模型调用前落一条 SQLite 事务、shutdown markers、启动孤儿扫描。优雅退出有 5 分钟 drain 预算；睡眠/冻结检测约 30 秒。恢复用合成系统消息自动续跑；每会话 3 次计费式恢复预算，超限写 tombstone 不再自动恢复；crash-loop 熔断（5 分钟内 3 次非正常启动则抑制渠道自启，`openclaw gateway call channels.start` 手动覆盖）。**不恢复**：Gateway 终端 PTY；cron（重新跑而非恢复）；ACP 会话（IDE 所有）。子代理注册表可恢复，>2 小时的过期运行被 finalize，反复卡死者 tombstone。暴露 Prometheus 指标 `openclaw_session_recovery_total` / `openclaw_session_recovery_age_seconds`。
- **checkpoint / time-travel**：**未找到**专门功能（尝试路径：docs.openclaw.ai/llms.txt 全索引无对应页面；关键词 checkpoint/time-travel 无用户文档）。最接近的替代物：JSONL/SQLite 全量转录 + compaction 摘要（`/compact`、`compact_boundary` 标记）+ 子代理 `context: "fork"` 会话派生（把当前转录分叉成子会话，相当于手动快照分叉）。

来源：
- https://docs.openclaw.ai/concepts/session
- https://docs.openclaw.ai/gateway/restart-recovery
- https://docs.openclaw.ai/gateway/security（dmScope/identityLinks 亦见此页）

### 1.2 记忆（体系全貌 / 短长期 / 检索）

**结论：文档层见过的最完整的记忆安全设计——分层文件记忆 + SQLite 溯源 + 混合检索（BM25+向量）+ 做梦巩固 + 写路径即安全边界。**

- **体系全貌（memory-core 插件，五层）**：
  1. Instructions：`AGENTS.md`（人写，始终注入）；
  2. Curated core：`MEMORY.md` + `USER.md`（做梦巩固产物，预算注入）；
  3. Episodic：`memory/YYYY-MM-DD.md` 日记 + 原始转录（**只按需召回，永不自动注入**——这是明确的安全属性）；
  4. Prospective（长期意图）：standing intents 存 SQLite（时间型编译成 cron、事件型走 `intent` 工具[关键词/向量/渠道/过期/预算/冷却，默认冷却 24h、预算 3 次、90 天过期]、愿望型转 Markdown 待复查）；
  5. Review：`DREAMS.md`（永不注入）。
  设计原则：无隐藏状态；写入是难点；**写路径即安全边界**；确定性闸门 + 闸门内模型判断；每步失败不阻塞回复。
- **短期 vs 长期**：短期=会话转录 + compaction（`agents.defaults.compaction.memoryFlush` 可把压缩内容刷入记忆）；长期=curated core + episodic + standing intents；跨会话召回开关 `agents.entries.<id>.memory.search.rememberAcrossConversations`。
- **溯源与污染防护**：每条记忆带 origin 类（owner/agent/untrusted/system）+ 会话类型 + 时间戳/取代键，保守分类（绝不默认 owner）；cron/心跳/子代理会话的内容不具备晋升资格；网络来源工具结果做 taint 传播；防召回循环；对照 OWASP ASI06、MINJA（arXiv:2503.03704）记忆投毒文献建模。`openclaw memory forget` 可按溯源清除。
- **检索（有 RAG，混合检索）**：BUILTIN 引擎 = SQLite FTS5 BM25 关键词 + 向量嵌入 + hybrid 融合；**CJK 三元组分词**（中文检索友好）；可选 sqlite-vec 加速（缺失则进程内余弦）。嵌入 provider：OpenAI（默认 text-embedding-3-small）/Gemini/Voyage/Mistral/DeepInfra 自动探测，Ollama 与本地 node-llama-cpp（GGUF）显式配置（本地模型存在时 local 优先）。索引位于 `~/.openclaw/memory/<agent>.sqlite`，分块约 400 token/80 重叠，1.5s 防抖重索引。
- **召回双通道**：Lane 1 = bootstrap 注入（仅合格溯源）+ 混合排序搜索（重要性 × 30 天半衰期时间衰减）+ trigger 注入（词法+向量预筛 ≥0.65，每 turn 最多 3 条，仅 curated 层）；Lane 2 = Active Memory 阻塞式子代理升级（recall 意图且 Lane 1 未命中时，模式 always/off）。记忆槽位可整体换引擎（`plugins.slots.memory`；备选 Honcho/LanceDB/memory-wiki 插件，memory-wiki 支持 per-agent vault）。
- **跨渠道身份统一**：有——`session.identityLinks` 把同一人多渠道 DM 折叠为一个会话；但**跨 agent 记忆搜索已移除**（QMD 路径删除），每个 agent 只检索自己的语料，共享材料需显式 `memory.search.extraPaths`。
- CLI：`openclaw memory status | index --force | search | forget | promote`。

来源：
- https://docs.openclaw.ai/concepts/memory-architecture
- https://docs.openclaw.ai/concepts/memory-builtin
- https://docs.openclaw.ai/concepts/multi-agent（跨 agent 记忆边界）

### 1.3 工具调用（内置集 / 结构化输出 / 权限 / 类型校验）

- **内置工具集（按 group 组织，`tools.profile` 选择档位）**：`minimal` / `coding` / `messaging` / `full`（full=本地 onboarding 默认）。full 档分组：group:runtime（exec/process/code_execution[bash 别名]）、group:fs（read/write/edit/apply_patch）、group:sessions（13 个会话工具含 sessions_spawn/yield/subagents）、group:memory（memory_search/memory_get）、group:web（web_search/x_search/web_fetch）、group:ui（browser/screen/theme/dashboard/terminal/portal/canvas/show_widget）、group:automation（automations/gateway/plugins/openclaw）、group:messaging、group:nodes、group:agents（ask_user/progress_card/goal）、group:media（view_image/image_generate/music_generate/video_generate/tts/pdf）、group:plugins（含 `bundle-mcp`——MCP 工具的聚合暴露面）。注意文档明确：profile 只决定工具清单，不授予执行权限（执行权限在 Permissions 菜单/exec 策略层）。
- **工具权限控制（多层，deny 优先）**：`tools.allow`/`tools.deny`（大小写不敏感通配，deny wins）；`tools.byProvider`（base→provider→allow/deny 顺序）；`tools.toolsBySender`（按发送者：`channel:` / `id:` / `e164:` / `username:` / `name:` / `*` 前缀，纵深防御）；`tools.gatewayPolicy`（部署方具名 allowlist+action，拒绝通配/组名，需重启，面向企业代管部署）；`tools.elevated`（enabled+allowFrom 渠道门，会话内 `/elevated on|off|ask|full`，可绕沙箱）；`tools.sandbox.tools` 对 MCP 工具额外设门。
- **MCP 双向**：客户端（`mcp add/doctor/probe/login/status/reload`，Streamable HTTP/SSE/stdio + OAuth）+ 服务端（`openclaw mcp serve`）；MCP server 工具以插件工具形式挂在 `bundle-mcp` id 下，且再过一道 `tools.sandbox.tools` 门。
- **结构化输出**：专门的 `llm-task` 内置插件（"Generic JSON-only LLM tool for structured tasks callable from workflows"，供工作流从 Agent 内部调结构化 JSON 子任务）；`lobster` 外部插件（typed workflow tool with resumable approvals）；Gateway 对外 OpenAI 兼容端点带 "documented function-tool subset"。未见 provider 级 response_format/json_schema 文档页（尝试路径：llms.txt 索引无独立 structured-output 页——此能力主要经由 llm-task 工具与 OpenAI 兼容 API 体现）。
- **类型校验**：TypeBox 是全项目的 schema 真相源——Gateway WebSocket 协议全部帧（req/res/event）用 TypeBox 定义，AJV 运行时校验每条入站帧 + 导出 JSON Schema（dist/protocol.schema.json）+ Swift codegen（macOS 客户端模型）；副作用方法强制 `idempotencyKey`。Agent 工具的参数 schema 定义在 `src/agents/agent-tools*.ts`；Code Mode（`tools.codeMode`）给 JS 代码执行两种执行器（node:vm 受信 vs quickjs 加固）。

来源：
- https://docs.openclaw.ai/gateway/config-tools/tool-policy
- https://docs.openclaw.ai/concepts/typebox
- https://docs.openclaw.ai/plugins/plugin-inventory（llm-task/lobster）
- https://docs.openclaw.ai/gateway/openai-http-api（OpenAI 兼容面，见 why-openclaw 引用）

### 1.4 扩展机制（写深：插件体系 / harness 插件 / 子代理 / 多 agent 编排）

**A. 插件体系**
- 形态：npm 包 + `openclaw.plugin.json` manifest（加载器先验 manifest 再导入运行时代码）；分发三档：核心 npm 包内置（约 70 个，`@openclaw/*`，装完即有）/ 官方外部包（22 个，npm + ClawHub 按需装，如 discord/feishu/whatsapp/codex/acpx/diagnostics-otel）/ 仅源码检出（qa-*）。清单由 `pnpm plugins:inventory:gen` 从 `extensions/*/package.json` 生成。
- 安装与信任：`openclaw plugins install <spec>`（裸 spec 先 ClawHub 后 npm，`clawhub:` / `npm:` 可强制源）；`plugins.allow` 白名单；`security.installPolicy` 本地命令返回 allow/warn/block（连 ClawHub 技能也过这道）；ClawHub 注册表有发布审核/安全审计（VirusTotal、ClawScan、静态分析、per-release 信任裁决，`openclaw skills verify` 取验证信封）——但 pending 扫描可带警告安装。**插件与 Gateway 同进程运行=信任代码**，缓解=白名单+installPolicy 钩子+锁版本+依赖锁定（pnpm-lock.yaml 为受审依赖图）+CI 强制 SDK 边界。
- 挂载面（capability registration points，SDK 约 150 入口、shrink-only 预算）：model providers、channels、tools、skills、speech、memoryEmbedding、webSearch/webFetch、mediaUnderstanding、videoGeneration、documentExtractors、agentToolResultMiddleware、agent harness、CLI backend、沙箱后端等。

**B. Harness 插件（agent 后端可插拔——本项目最独特的分层）**
- 概念：agent harness = "一次已准备 agent turn 的底层执行器"，不是 provider、不是渠道、不是工具注册表。SDK 入口 `openclaw/plugin-sdk/agent-harness`（AgentHarnessV2 契约：`supports(ctx)` 声明 + `runAttempt(params)` 执行）。
- 可用后端清单（文档实证）：
  1. **内置 `openclaw` 运行时（旧名 `pi`）**：默认嵌入式 agent 循环，位于 `src/agents/embedded-agent-runner/`；
  2. **`codex` harness**（官方外部 `@openclaw/codex`）：驱动 Codex app-server 原生循环（原生线程 resume/compaction/审批/中途转向/桥接 OpenClaw 工具/computer use）；要求 app-server ≥0.125.0；`openai/*` 模型引用默认即走 codex harness（除非显式 pin pi）；
  3. **`claude-cli` CLI backend**（bundled Anthropic 插件）：常驻 Claude Code stdio 进程、stream-json 续turn、bundleMcp 把 Gateway 工具经回环 MCP 桥给 Claude、`/think`→`--effort` 映射、YOLO exec 策略→`--permission-mode bypassPermissions` 映射；文档记录 Anthropic 员工已表态允许此用法；
  4. **`google-gemini-cli` CLI backend**（bundled Google 插件）；
  5. **Copilot SDK**（`github-copilot` provider 插件，跑 GitHub Copilot SDK session loop，why-openclaw 明示）；
  6. **ACP/acpx**（官方外部 `@openclaw/acpx`）：外部编码 harness 运行时（Claude Code/Gemini CLI/OpenCode/Cursor/Droid/显式 Codex ACP），非交互式，独立 permissionMode；
  7. 其他 provider 型后端：opencode / opencode-go / kilocode / copilot-proxy / litellm 等。
- 选择策略（四层）：model 级 `agentRuntime.id` > provider 级 > `auto`（注册 harness 的 supports() 认领）> 内置 openclaw 兜底。显式 runtime id **fail-closed**（选不到就报错，绝不静默回落 PI）；整 agent/整会话 runtime pin 已废弃（`openclaw doctor --fix` 清理）。核心/harness 职责切分：core 保留 provider/model 解析、auth、转录、workspace/沙箱/工具策略、渠道回调、模型回退；harness 拿 prepared attempt（含 runtimePlan 策略包），不得中途换 provider/model。第三方 harness 安装标记为 experimental。

**C. 子代理（subagents）**
- `sessions_spawn`（isolated 默认 / fork 分叉请求者转录）+ `sessions_yield`（等待原语，禁止轮询）+ `subagents` 工具（list/kill/log/info/send/steer）+ `/subagents` 斜杠命令。专用 `subagent` 队列 lane，默认并发 8。嵌套深度 `maxSpawnDepth` 1–5（默认 1；=2 即 orchestrator 模式：主→编排子代理→工作者孙代理；depth-2 永远禁 spawn）；`maxChildrenPerAgent` 默认 5（1–20）；`runTimeoutSeconds` 超时；announce 回传机制（带 NO_REPLY/ANNOUNCE_SKIP 静默令牌、runtime/token/cost 统计行、状态由运行时判定而非模型文本推断）。按深度的工具策略：叶子子代理默认**无任何 session 工具**；沙箱请求者 spawn 非沙箱目标会被拒（`sandbox: "require"`）。auth 按 agent id 解析、主 agent auth 作回退合并。线程绑定子代理会话（thread bindings）当前仅 Discord 支持。

**D. 多 agent 编排（Gateway 内多 agent + 路由）**
- 每个 agent = 完整个人格边界：独立 workspace（AGENTS.md/SOUL.md/USER.md）、独立 `agentDir`（auth profiles/model registry/配置）、独立 SQLite 会话库、独立 skills 白名单。`openclaw agents add <id>` 创建。
- 绑定路由：`bindings[]` 把 (channel, accountId, peer[, guildId]) 映射到 agentId，最具体者胜（层级：精确 peer > 父 peer > peer 通配 > guild+roles > guild > team > account > channel > 默认 agent）。多账号渠道（含 feishu/whatsapp/telegram 等 17 个）每 accountId 可路由到独立 agent。
- agent 间通信默认关闭：`tools.agentToAgent.enabled` + allow 白名单。agent 溯源（operator/agent/claw 创建来源，`openclaw agents list --tree`）；系统代理可请求创建新 agent 但需操作员批准。飞书渠道另有 dynamic agents 模式（每个 DM 发送者动态分配独立 agent + workspace）。llms.txt 还有 parallel-specialist-lanes / delegate-architecture 专页（本次未逐页展开，属编排模式文档）。

来源：
- https://docs.openclaw.ai/plugins/plugin-inventory
- https://docs.openclaw.ai/plugins/sdk-agent-harness
- https://docs.openclaw.ai/concepts/agent-runtimes
- https://docs.openclaw.ai/gateway/cli-backends
- https://docs.openclaw.ai/tools/subagents
- https://docs.openclaw.ai/concepts/multi-agent
- https://docs.openclaw.ai/start/why-openclaw（ClawHub/SDK 预算/信任模型）

### 1.5 HITL（审批机制）

**结论：审批体系分层完整且文档化极细——工具策略（能不能用）× exec 审批（主机命令放不放）× elevated（显式提权）× 渠道配对（谁能说话），四层独立。**

- **exec 审批（主机命令守门）**：本地文件 `~/.openclaw/exec-approvals.json`；三旋钮 `security`（deny/allowlist/full）× `ask`（off/on-miss/always）× `askFallback`（无 UI 时 deny/allowlist/full）。统一入口 `tools.exec.mode`：`deny`/`allowlist`/`ask`/`auto`/`full`——**auto = 原生 auto-reviewer 先自动审、不能安全批准才转人工**（推荐默认）。allowlist 按 agent、按二进制路径 glob（basename 条目被忽略）；safe bins（jq/grep/cut/sort/uniq/head/tail/tr/wc，stdin-only、拒绝路径参数与变量展开）；heredoc 在 allowlist 模式下恒需审批；解释器 `-c/-e` 内联求值需 `tools.exec.strictInlineEval`。审批对话框含命令+参数+cwd+agent+解析后可执行路径+主机/策略元数据，操作：Allow once / Always allow（入 allowlist）/ Deny；超时视为拒绝；生命周期以 `Exec running/finished/denied` 系统消息回投会话。
- **聊天渠道内审批（UX 亮点）**：审批提示可转发到任意聊天渠道（含插件渠道），用 `/approve allow-once | allow-always | deny` 回复裁决；`approvals.exec`（mode: session/targets/both, agentFilter, sessionFilter, targets[channel+to]）。macOS 侧为 Node Service→Mac App 的 UDS+token+HMAC+TTL IPC。
- **elevated 提权**：`tools.elevated.enabled` + `allowFrom`（全局门）+ per-agent 门（只能更严，双门都要放行）；会话态 `/elevated on|off|ask|full`；提权 exec 绕过沙箱走 gateway/node 逃逸路径（文档明示这是设计）。
- **入站身份门**：DM 默认 `dmPolicy: "pairing"`（陌生发送者收到配对码，1 小时过期、每渠道 pending 上限 3、`openclaw pairing approve <channel> <code>`）；群聊 allowlist + `requireMention`；`contextVisibility`（all/allowlist/allowlist_quote）控制补充上下文可见性。斜杠命令仅授权发送者可用（`commands.allowFrom`）。
- **其他 harness 的审批映射**：Codex Guardian（auto→approvalPolicy on-request + auto_review + sandbox workspace-write，且强制覆盖不安全的 legacy 组合）；ACPX（permissionMode: approve-reads/approve-all/deny-all + nonInteractivePermissions: fail/deny）。主机 exec 与 ACPX harness 权限互不放松（取更严者）。

来源：
- https://docs.openclaw.ai/tools/exec-approvals
- https://docs.openclaw.ai/tools/permission-modes
- https://docs.openclaw.ai/gateway/security

### 1.6 可观测性（日志 / dashboard / OTel / evals）

- **日志**：默认 `/tmp/openclaw/openclaw-YYYY-MM-DD.log`（`logging.file` 可改；命名 profile 另有路径）；**redaction 恒开不可配置关闭**（`logging.redactPatterns` 可加自定义模式）；`openclaw logs` tail；JSONL 日志带 traceId/spanId/parentSpanId 可与 span 关联；subsystem 前缀（如 `main-session-restart-recovery`、`subagent-interrupted-resume`）；`OPENCLAW_DIAGNOSTICS=telegram.http` 类诊断 flag 定向调测。
- **Dashboard**：`openclaw dashboard` 打开浏览器 Control UI（Gateway HTTP 面上的 SPA，默认 `http://127.0.0.1:18789/`，`gateway.controlUi.basePath` 改基路径）。定位为**管理面**：聊天、配置、exec 审批（Nodes→Exec approvals 卡片）、日志 tab、会话/在线状态（presence：谁在看谁在打字，草稿不出本机不进模型）。认证走 gateway.auth（token/password/Tailscale Serve 身份头/trusted-proxy）；设备身份用纯 JS Ed25519 配对。文档反复强调勿公开暴露。
- **OTel（一等公民）**：`diagnostics-otel` 插件，OTLP/HTTP protobuf（grpc 被忽略）三信号（metrics/traces/logs 独立开关）；另有 `diagnostics-prometheus` 插件。指标族覆盖 model（openclaw.tokens / openclaw.cost.usd / openclaw.run.duration_ms / openclaw.context.tokens / openclaw.model_call.duration_ms|time_to_first_byte_ms 等，含 GenAI 语义约定 gen_ai.client.token.usage / gen_ai.client.operation.duration）、消息流（webhook/message.delivery）、队列与会话（queue.depth / session.state / session.recovery.requested|completed / session.stuck）、harness 生命周期（openclaw.harness.duration_ms，按 harness.id/plugin/outcome）、exec、内存压力、工具循环。span 族覆盖 model.call / harness.run / tool.execution / exec / webhook / context.assembled / tool.loop。**隐私默认**：原始 prompt/回复/工具输入输出一律不导出（hash-only 请求 id；captureContent.* 四类内容需逐项显式 opt-in 且导出时仍做 redaction）；对 provider 调用注入 W3C traceparent（防伪造跨服务血缘）。采样 sampleRate（root-span）+ flushIntervalMs。session liveness 三分类（long_running/stalled/stuck，stuckSessionWarnMs 阈值，stalled 先观察后 abort-drain）。
- **evals**：**产品内未找到**面向用户的 eval 框架/评测页（尝试路径：llms.txt 索引、plugins 清单——只有 qa-lab 为源码内 QA 工具非用户 eval）。仅 Foundation 层面有 "councils on agent identity, agent profiles, evals, and enterprise deployment"。ClawHub security audits 属技能安全扫描非模型评测。

来源：
- https://docs.openclaw.ai/gateway/opentelemetry
- https://docs.openclaw.ai/web/dashboard
- https://docs.openclaw.ai/gateway/security（日志/转录/redaction）

### 1.7 部署与安全（沙箱 / 密钥 / 数据本地性 / 自托管边界）

- **沙箱**：`agents.defaults.sandbox`（默认**关闭**——"Tools run on the host for the main session unless you configure sandboxing"）。开启后：`mode`（off/non-main/all）、`scope`（agent/session/shared 容器粒度）、`backend`（docker 默认 / ssh 远程 / openshell 托管远程；security 页另提 Podman）。Docker 后端：默认镜像 `openclaw-sandbox:bookworm-slim`（`scripts/sandbox-setup.sh` 构建；**默认无网络** `network:"none"`，`host` 与 `container:` 被阻断）、`workspaceAccess`（none/ro/rw 挂载 agent 工作区）、自定义 binds 有符号链接解析 fail-closed 的危险源阻断（docker.sock、/etc、/proc、/sys、/dev、~/.ssh、~/.aws、~/.gnupg 等）、沙箱浏览器为独立容器（专用网络+noVNC 密码+CDR CIDR 白名单）、GPU 透传。SSH 后端=remote-canonical（一次 seed 不回同步）；OpenShell 有 mirror/remote 两工作区模式。另两条隔离路线：整 Gateway 容器化（/install/docker，注意 DooD 主机路径约束）与 cloud workers（一次性云机执行+转录留在共享 Gateway）。`tools.elevated` 是全局明示逃逸口。Gateway 进程本身不进沙箱。
- **密钥管理**：SecretRef 体系（`source: env|file|exec` 三类 provider；官方示例 1Password `op`、HashiCorp Vault、SOPS——均为 exec provider）。**未找到** macOS Keychain 专用 provider（尝试路径：gateway/secrets.md 全文只有 env/file/exec 三源；密钥链集成只能靠 exec 包装）。运行时模型：激活期 eager 全量解析→内存快照；解析失败 fail-fast 阻止启动；reload 原子交换（失败保 last-known-good）；active-surface 过滤（未启用面的悬空引用不阻塞）；`openclaw secrets audit --check`（明文静态值/未解析引用/优先级遮蔽扫描）与 `secrets configure` 交互迁移；MCP server env 支持 SecretInput；`.env` 工作区文件被禁止覆盖 provider 凭据与 `OPENCLAW_*` 命名空间（防克隆仓库注入攻击者账号）。凭据落盘位置文档列表化（credentials/**、per-agent SQLite auth_profile_store、state/openclaw.sqlite 的 MCP OAuth）。
- **数据本地性**：默认全本地（`~/.openclaw/`，`OPENCLAW_STATE_DIR` 可移）；对 Foundation 的唯一默认请求是版本检查（`update.checkOnStart: false` 关闭）；模型/渠道流量直连对应服务商，**无中继云**；onboarding 生成 token 后本地回环连接也要认证（fail-closed）。日志/转录 redaction 恒开。
- **自托管边界与安全工具箱**：完全自托管（无付费层/无托管服务）；一个 Gateway = 一个信任域（明确不支持互不信任多租户共享，需按租户拆 Gateway cell，见 multi-tenant-hosting 页）；容器镜像默认暴露 bind 属例外（有 exposure runbook）；`openclaw security audit [--deep|--fix|--json]` 漂移审计（结构化 checkId 目录：fs.*/gateway.*/tools.exec.*/plugins.*/security.exposure.*，--fix 只做窄幅修复）；MITRE ATLAS 威胁模型专页；hardened baseline 配置样例（loopback+token+pairing+messaging profile+exec deny）；trust boundary matrix 列出"按设计非漏洞"清单（含公开评审姿态）。

来源：
- https://docs.openclaw.ai/gateway/sandboxing
- https://docs.openclaw.ai/gateway/security
- https://docs.openclaw.ai/gateway/secrets
- https://docs.openclaw.ai/start/why-openclaw（含 what-we-do-not-claim）

---

## 2 国内可用性

- **飞书：一等公民**。官方外部插件 `@openclaw/feishu`（OpenClaw 官方维护，npm+ClawHub），状态 "production-ready for bot DMs + group chats"。WebSocket 默认事件传输（**无需公网 URL**，企业内网可部署），webhook 模式可选（复用 Gateway 18789 端口 /feishu/events）。支持流式卡片回复、飞书文档/wiki/云盘/多维表格工具、多账号、消息限额与配额优化、群会话作用域与话题线程、ACP 会话（从聊天拉起外部编码 harness）、多 agent 路由，以及 **dynamic agents（每个 DM 用户动态创建独立 agent + 独立 workspace，多用户部署模式）**。生态侧：飞书官方插件发布文章（feishu.cn/content/article/7613711414611463386）与飞书 openclaw 解决方案中心（feishu.cn/openclaw）。
- **微信：腾讯官方外部插件**。`@tencent-weixin/openclaw-weixin`（npm 包，渠道 id `openclaw-weixin`；**代码不在 OpenClaw 主仓**，走通用渠道插件契约；QR 扫码登录；腾讯 iLink API；凭据存 OpenClaw 状态目录）。支持私聊+媒体；**当前插件能力元数据未宣传群聊**。版本兼容：2.x 线要求 OpenClaw ≥2026.3.22（1.x 线 legacy）。快速安装 `npx -y @tencent-weixin/openclaw-weixin-cli install`。企业微信另有独立社区外部插件（openclaw-wecom）；QQ 机器人有官方外部插件 `@openclaw/qqbot`。
- **国内模型路径：内置 provider 插件覆盖完整**。核心包内置：qianfan（百度）/ qwen+dashscope+modelstudio（阿里）/ volcengine（火山豆包）/ tencent-tokenhub（腾讯）/ stepfun / xiaomi / zai（智谱）/ moonshot+kimi / deepseek / minimax（含媒体生成）/ byteplus。另支持任意 OpenAI 兼容 baseUrl（model baseUrl > provider URL > OPENAI_BASE_URL 环境变量）与本地推理（Ollama / LM Studio / vLLM / SGLang / llama.cpp）。即：**模型层可 100% 国产/本地化**。
- **治理背书**：腾讯既是 Foundation 捐赠者/合作伙伴（Amazon、Atlassian、GitHub、Microsoft、NVIDIA、OpenAI、Red Hat、Tencent 等 30+ 组织），也是官方点名的安全贡献者（"security contributors from NVIDIA and Tencent"），且微信插件由腾讯发布——国内大厂参与度高。
- **断外网可行性评估**：安装链 = npm registry（或 openclaw.ai 安装脚本）；npm 在国内可直连或走 npmmirror 镜像（注：官方文档无中国镜像记载，npmmirror 为社区通行方案）；运行期若模型走国内 provider + 渠道只用飞书/QQ/微信，理论上可不依赖外网（版本检查可关 `update.checkOnStart:false`）。**仍需外网的部分**：ClawHub 插件安装（可强制 `npm:` 源规避）、docs.openclaw.ai 文档站、桌面/移动端配对相关云端资源（若有）。
- **中文资料**：官方文档有 zh-CN 语言版本；社区教程活跃——知乎专栏（微信/钉钉/飞书/QQ 接入教程，zhuanlan.zhihu.com/p/2013706717012186762）、博客园/CSDN 实操文、Coze 文档站的 OpenClaw 教程（docs.coze.cn/tutorial_openclaw）、GitHub 社区封装（AlexAnys/openclaw-feishu）。

来源：
- https://docs.openclaw.ai/channels/feishu（及子页 setup/access-control/advanced-configuration/dynamic-agents/configuration-reference）
- https://docs.openclaw.ai/channels/wechat
- https://docs.openclaw.ai/plugins/plugin-inventory（provider/渠道清单）
- https://docs.openclaw.ai/start/why-openclaw（Tencent 捐赠/安全贡献）
- https://www.npmjs.com/package/@tencent-weixin/openclaw-weixin
- https://feishu.cn/content/article/7613711414611463386 ｜ https://feishu.cn/openclaw
- https://zhuanlan.zhihu.com/p/2013706717012186762 ｜ https://docs.coze.cn/tutorial_openclaw

---

## 3 企业落地注记

- **基金会治理（供应链/可持续性：正面）**：独立 501(c)(3)，MIT、无企业版分裂授权；全职团队、Foundation 身份签名发布 + npm provenance + 可验证 artifact attestations；公开安全公告（2026-08-27 时点 647 条公告——文档刻意说明这是披露记录数而非可利用缺陷数/安全评分，反过来说明披露文化成熟）；四个 Foundation 议会（agent identity / agent profiles / evals / enterprise deployment）含企业部署议题；第三方背书：NVIDIA NemoClaw 发行版（OpenShell 内核级沙箱加固）、Nebius 生产加固指南、学术安全案例研究（arXiv:2603.12644）。作者入职 OpenAI 不影响中立性（"the Switzerland of AI"）。
- **个人助理定位 vs 企业张力（需正视）**：默认形态是"可信单操作员个人助理"——DM 全并 main 会话、沙箱与 exec 审批**默认关**、宿主 exec 默认 `security:"full"` 免审批。企业化全靠显式配置（文档以 why-openclaw 页 + security 页 hardened baseline 给出完整路径：sandbox all + openshell/docker、guarded/workspace 权限档、Tailscale/身份代理、gateway.roles deny-all 默认角色、SecretRefs 全覆盖、消息审计 + OTel 入 SIEM、定期 `security audit --deep` 告警）。**BYOD 风险显著**：员工在个人设备跑 Gateway 即把公司凭据/浏览器 profile/邮件暴露给个人 agent 运行时，文档自己列出 company-shared agent 的约束条件（专用机/专用 OS 用户/业务专用账号、不得混入个人身份）。互不信任多租户明确不支持共享 Gateway。
- **安全事件史（红旗区，2025 末—2026 病毒式传播期集中爆发）**：
  - CCB Belgium（比利时网络安全中心）公告 "1-click RCE"：CVSS 8.8，影响 OpenClaw < 2026.1.29，CWE-669，恶意 URL 窃取 Gateway token（https://ccb.belgium.be/advisories/warning-critical-vulnerability-openclaw-allows-1-click-remote-code-execution-when）；
  - DepthFirst 披露 prompt injection 数据外泄链（2026-01 下旬，agent 泄漏 API key）；
  - Immersive Labs 分析 ClawHub 上 900+ 恶意 skills，一度建议直接卸载（⚠ 口径注记 2026-11-05：数字随研究机构与时间窗而异——IMDA 报告引 Koi Security：2026-02-01 发现 341 个、02-16 报告 824 个；Unit 42 对 02—05 月分析仍发现 5 个绕过初期治理的样本。"检测命中/确认恶意/已下架/仍可下载"是不同指标，报告正文已改用机构+时间窗口径）；
  - Cisco 博客（2026-01-28）"Personal AI Agents like OpenClaw Are a Security Nightmare"；
  - 多伦多大学校内安全通告（2026-02-04）要求停用；
  - SentinelOne "Claw Chain" CVE 链分析（2026-10）；
  - 学术研究 arXiv:2606.11007（三周内多向量企业威胁案例）。
  项目侧回应可考证：why-openclaw 的 trust-model/"not vulnerabilities by design" 论述、pairing 默认、token fail-closed、untrusted-content 包装与 chat-template 特殊 token 清洗（Qwen/ChatML/Llama 等 `<|im_start|>` 伪造角色边界防护——对国产自托管后端尤其实用）。
- **团队协作面**：multi-user 会话（immutable creator / assignable owner / 实际 prompt 者）、presence、Co-authored-by Git 署名（验证 GitHub 身份者默认开）、team gateway 部署模式、operator scopes 具名角色。作为"团队共享 agent"可用，但边界仍是"互信团队"而非"互斥租户"。

来源：
- https://docs.openclaw.ai/start/why-openclaw（governance / what-we-do-not-claim / openclaw-and-hermes-agent）
- https://docs.openclaw.ai/gateway/security（threat model / incident response / not-vulnerabilities-by-design）
- https://ccb.belgium.be/advisories/warning-critical-vulnerability-openclaw-allows-1-click-remote-code-execution-when
- SentinelOne / Cisco / Immersive Labs / DepthFirst / 多伦多大学通告（见上；经 WebSearch 检索）
- https://arxiv.org/html/2603.12644v1 ｜ https://arxiv.org/abs/2606.11007 ｜ https://nebius.com/blog/posts/openclaw-security ｜ https://nvidianews.nvidia.com/news/nvidia-announces-nemoclaw

---

## 4 关键源码文件清单（供后续源码深读）

> Monorepo：TypeScript + pnpm workspace。顶层关键区（来自官方 agent-runtime-architecture / typebox / plugin-inventory 三页的代码路径披露）：

| 路径 | 职责（深读优先级） |
| --- | --- |
| `src/gateway/protocol/schema.ts` | TypeBox 协议真相源：全部 WS 帧（req/res/event）schema ★ |
| `src/gateway/protocol/index.ts` | AJV 运行时校验器 ★ |
| `src/gateway/server.impl.ts` | 握手 + 方法分发 ★ |
| `src/gateway/server-methods-list.ts` / `server-methods/*.ts` / `method-scopes.ts` | RPC 方法清单/实现/作用域分类（发现面） |
| `src/gateway/client.ts` | 节点客户端 |
| `src/agents/embedded-agent-runner/`（`run.ts`、`run/`、`model*.ts`、`extra-params.*`） | 内置 agent 尝试循环、模型选择/规范化、compaction、转录接线 ★★★（PI 运行时主体） |
| `src/agents/sessions/`（`session-manager.ts`、`package-manager.ts`、`resource-loader.ts`、`extensions/`、`tools/`） | 会话持久化、资源发现、in-session 扩展加载、技能/主题/TUI 工具渲染器 ★★ |
| `src/agents/runtime/` | 把 `@openclaw/agent-core` 接到插件 SDK LLM 运行时的 facade |
| `src/agents/agent-tools*.ts` | OpenClaw 自有工具定义、参数 schema、工具策略、before/after 适配器 ★★ |
| `src/agents/agent-hooks/` | 内置运行时钩子（compaction safeguard / instructions / context pruning） |
| `src/agents/harness/` | **harness 注册表、选择策略、生命周期**（多 harness 核心）★★★ |
| `src/llm/` + `src/llm/providers/` | 模型/provider 注册表、transport、各 provider 流实现（含 DeepSeek/Qwen/Z.AI 等) ★★ |
| `packages/agent-core/`（`@openclaw/agent-core`） | 可复用 agent 核心：循环、harness 类型、消息、compaction、prompt 模板、技能、会话存储契约 ★★ |
| `extensions/*/`（每个插件一目录 + `openclaw.plugin.json`） | 全部插件：feishu、deepseek、qwen、qianfan、volcengine、tencent、memory-core、memory-wiki、browser、openshell-sandbox…（清单生成命令 `pnpm plugins:inventory:gen`）★★★ |
| `apps/macos/Sources/OpenClawProtocol/GatewayModels.swift` | Swift codegen 客户端模型 |
| `scripts/sandbox-setup.sh` / `sandbox-common-setup.sh` / `sandbox-browser-setup.sh` / `docker/setup.sh` | 沙箱镜像与容器化部署脚本 ★ |
| `dist/protocol.schema.json` | 生成的协议 JSON Schema（draft-07） |
| 运行时状态目录（非源码但深读必看）：`~/.openclaw/openclaw.json`、`agents/<id>/agent/openclaw-agent.sqlite`、`agents/<id>/sessions/*.jsonl`（legacy）、`memory/<agent>.sqlite`、`credentials/**`、`state/openclaw.sqlite`、`exec-approvals.json`、`sandboxes/**` | 配置/会话/记忆/凭据/MCP OAuth/审批/沙箱工作区落盘 |
- 注：GitHub packages 目录树页面为 JS 渲染，无法直接抓取文件列表（尝试路径：https://github.com/openclaw/openclaw/tree/main/packages ，仅得仓库元信息）。`packages/` 下已确认存在 `agent-core`；深读时建议 `git clone` 后以 `pnpm plugins:inventory:gen` 与目录遍历核对完整包清单。子代理/多 agent 源码预计落在 `src/agents/`（sessions_spawn 等工具）与 Gateway 会话管理区，具体文件名待源码层确认。

---

## 5 信息源 URL 列表

官方文档（docs.openclaw.ai，均可用页 URL + `.md` 后缀取 markdown）：
- https://docs.openclaw.ai/llms.txt（全站索引）
- https://docs.openclaw.ai/concepts/session
- https://docs.openclaw.ai/gateway/restart-recovery
- https://docs.openclaw.ai/concepts/memory-architecture
- https://docs.openclaw.ai/concepts/memory-builtin
- https://docs.openclaw.ai/gateway/config-tools/tool-policy
- https://docs.openclaw.ai/concepts/typebox
- https://docs.openclaw.ai/plugins/sdk-agent-harness
- https://docs.openclaw.ai/concepts/agent-runtimes
- https://docs.openclaw.ai/agent-runtime-architecture
- https://docs.openclaw.ai/gateway/cli-backends
- https://docs.openclaw.ai/tools/subagents
- https://docs.openclaw.ai/concepts/multi-agent
- https://docs.openclaw.ai/tools/exec-approvals
- https://docs.openclaw.ai/tools/permission-modes
- https://docs.openclaw.ai/gateway/opentelemetry
- https://docs.openclaw.ai/web/dashboard
- https://docs.openclaw.ai/gateway/sandboxing
- https://docs.openclaw.ai/gateway/security
- https://docs.openclaw.ai/gateway/secrets
- https://docs.openclaw.ai/channels/feishu（及 /setup /access-control /advanced-configuration /dynamic-agents /configuration-reference /messaging 子页）
- https://docs.openclaw.ai/channels/wechat
- https://docs.openclaw.ai/plugins/plugin-inventory
- https://docs.openclaw.ai/start/why-openclaw（含 /the-trust-boundary /policy-as-code /secrets /provenance /versioned-state-guarded-upgrades /identity-and-roles /openclaw-and-hermes-agent 子页）

仓库与包：
- https://github.com/openclaw/openclaw
- https://www.npmjs.com/package/openclaw
- https://www.npmjs.com/package/@tencent-weixin/openclaw-weixin
- https://www.npmjs.com/package/@openclaw/feishu

生态与中文资料：
- https://openclaw.org（基金会）
- https://feishu.cn/content/article/7613711414611463386 ｜ https://feishu.cn/openclaw
- https://zhuanlan.zhihu.com/p/2013706717012186762 ｜ https://docs.coze.cn/tutorial_openclaw ｜ https://github.com/AlexAnys/openclaw-feishu

安全事件与外部研究：
- https://ccb.belgium.be/advisories/warning-critical-vulnerability-openclaw-allows-1-click-remote-code-execution-when
- https://nebius.com/blog/posts/openclaw-security
- https://nvidianews.nvidia.com/news/nvidia-announces-nemoclaw
- https://arxiv.org/html/2603.12644v1 ｜ https://arxiv.org/abs/2606.11007
- SentinelOne Claw Chain 分析、Cisco 博客（2026-01-28）、Immersive Labs 恶意 skills 研究、DepthFirst prompt-injection 披露、多伦多大学通告（2026-02-04）——经 WebSearch 检索获得
