# DeepSeek Harness (dsh) 调研卡

调研日期：2026-11-03（基于当日 master 分支源码 tarball 全文精读：docs/ 目录 + 关键包 README）

- **仓库**：github.com/deepseek-ai/deepseek-harness（官方第一方，2026-08-13 建仓，270,674 stars，MIT，TypeScript）
- **版本现状**：36 个 release 全部为 prerelease，最新 `dsh-v0.2.2-alpha.3`（2026-11-03）；CLI/npm 版本号 `0.2.2-alpha.3`
- **分发**：npm `@deepseek-ai/dsh`（`npx @deepseek-ai/dsh web`）· Python wheel `deepseek-harness-sdk` + 平台运行时 `deepseek-harness-runtime-bin`（SDK 自带 Node 运行时，无需系统 Node）· Desktop（Electron）安装包
- **⚠ 冒名包**：PyPI `deepseek-harness` / `deepseek-harness-cli`、npm 老包 `dsh` 为冒名/无关包，勿装

---

## 0 身份与分层归属

**归属：Runtime / Harness 层（Agent 运行时框架），非 Agent 产品、非模型层。**

理由：
1. dsh 不自带"开箱即用的业务 Agent"，它提供会话（事件溯源日志）、agent-loop（默认驱动）、工具注册表、沙箱、审批、持久化、多 profile 启动等**运行时底座**，模型完全中立（内置 DeepSeek / anthropic / openai / moonshotai(Kimi) / zai(GLM) 适配器 + Custom model API 接任意 base URL × openai-completions / openai-responses / anthropropic-messages 三协议）。
2. 架构自述 "Everything is a Plugin"，基于 Cordis（vendor 进仓的插件框架，设计论文 arXiv:2608.25512《A Programming Paradigm for Spatiotemporal Composability》）：插件向共享 ctx 注册服务/类型化事件/可逆 effect，"没有可打补丁的特权核心"——模型适配器、工具注册表、会话日志、agent loop 本身全是插件，均可被配置替换。

**"成品属性"边界（重要）**：
- README 明示 **developer preview**："It is in developer preview and iterating rapidly. **THERE WILL BE COMPATIBILITY-BREAKING CHANGES.**"
- `SAFETY.md` 明示：**未做过安全审计，不得视为安全或生产就绪**；沙箱/审批/权限控制"不能保证隔离或防止损害"，不得作为不可信工作负载的唯一安全控制。
- 成熟度定级建议：**架构设计成熟度极高（事件溯源、能力缝、格式版本化纪律远超同类 preview 项目），产品/企业就绪度低（单用户认证、全 prerelease、无审计）**。选型时应视为"可深度定制的框架底座 + 高速演进的开源实验"，而非可托管给最终用户的成品。

---

## 1 七维度逐项

### 1.1 状态：会话持久化 / 断点恢复 / checkpoint —— **有，且是架构核心**

- **会话 = append-only 事件日志**（`SessionEventMap`，事件溯源）。LLM 消息历史永远由日志派生（`deriveMessages()`），从不单独存储；重放即对同一事件重推导。"Model-visible means logged"：任何到达模型的输入必须可从日志重建（含每次请求的 `request/header` 快照）。
- **持久化 seam**：`ctx.sessionPersistence`（`create/open/stat/list` + 每会话 `SessionHandle: read/append/flush/close`）。默认 JSONL 后端：`session.vN.jsonl[.zstd]`，一事件一行，zstd 压缩。**跨进程单写者**：第二个 `open(id,'write')` 抛 `SessionAlreadyOwnedError`。
- **checkpoint 概念存在**（两层）：① `session/flush` = 显式 durability barrier（awaited parallel 事件，`ctx.sessions.flush()`），后台写入为 write-behind 有界批量窗口；② `dsh-session-checkpoint-policy` 包拥有**每请求 durability checkpoint**，turn 边界不自动 flush。
- **崩溃恢复**：持久层不截断中断的 turn（长任务事件已落盘，截断即损失）。恢复时 agent-loop 通过写 handle 读取日志、计算 `interruptedTurnClosers`（补缺失 tool 错误、`step/end`、合成 `turn/end {reason:'interrupted'}`）后作为普通批次追加再发布会话。撕裂的物理尾部（torn tail）由 JSONL 后端部分解码并重写。只读观察者（session-query）在内存中平衡同样补偿，不回写。
- **resume 入口齐全**：Web 会话列表（SessionController `resolveAgent` "explicitly resuming"；冷会话自动 resume + 并发去重）；headless `--session-id <id>`（明确支持 resume，且 `--permission-mode` "including on resume"）；ACP `session/resume`（校验 workspace 后恢复，不重放旧更新）；TypeScript/Python SDK。**会话格式带版本迁移链**：当前 V4（`latestFinalizedVersion: 4`，`latestReleasedVersion: 4`，evidence `dsh-v0.2.0-rc.2`），vN→vN+1 相邻迁移，**已提交的 generation 路径永不重命名/覆盖/删除**，未来版本拒绝读。
- **fork**：任意 turn 边界前缀复制为新会话（`ctx.sessions.fork(boundary)` / Remote `fork`），开放尾部自动补合成闭合器（`forked` 原因）。

佐证：docs/subsystems/session.md、docs/subsystems/persistence.md、docs/session-format-status.md、docs/cli-help.md、packages/acp/acp/README.md

### 1.2 记忆：短期 / 长期 / 项目记忆

- **短期（会话上下文）**：事件日志 + **compaction 能力缝**（可选能力，非 loop 脊柱）：`compaction/start|summary|end` 锁 + `user/message` 的 `surfaceOp: replace` 覆盖旧表面区间；`dsh-compaction-basic` 在 `agent/pre-step` 检测压力、`agent/request-error` 处理上下文溢出恢复，含工具结果修剪与 image-offload（图片下沉）。`token-meter` 提供回放快照式请求压力/表面 token 计量（usage 锚定 vs 估算两态）。
- **项目记忆（AGENTS.md 类）——有，默认启用**：`dsh-agent-instructions`（dsh-base 自带，65,536 字节/链预算）。细节：候选文件 `AGENTS.md` / `CLAUDE.md` + 本地叠加 `AGENTS.local.md` / `CLAUDE.local.md`；`.git` 标记项目根；**用户全局链**（`$DSH_HOME/AGENTS.md` → `~/.agents/AGENTS.md`）+ **项目链**（根→cwd 由宽到具体）分链预算、跨链去重（内容相同只渲染一次）；成功的 fs 操作后增量发现更深层嵌套指令文件，会话 resume 时重建 baseline。
- **长期跨会话记忆——无内置**。官方立场：提供三个**默认关闭**的第三方 MCP 记忆参考配置（Memorix / MCP Reference Memory / Engram，经 `@deepseek-ai/dsh-mcp-client` 挂载为 `mcp__<server>__<tool>` 工具），并明确声明"仅为互操作示例，不代表背书"。DSH 自己不下载/初始化/托管记忆服务。
- **技能体系**：`dsh-skill` 注册表（多源合并、按需加载、`modelInvocable`/`userInvocable` 双面策略）+ `dsh-skill-filesystem`（本地发现）+ `dsh-tool-skill`（模型侧入口），另有 office 格式技能包。

佐证：docs/subsystems/compaction.md、docs/subsystems/token-meter.md、packages/context/agent-instructions/README.md、docs/user/guide/mcp-memory.md、packages/skill/skill/README.md

### 1.3 工具调用：注册表 / 结构化输出 / 权限体系

- **注册表** `ctx.tools`：`register()` 全局或 agent-scope（scoped 遮蔽 global，保留字 `run_code`）；`restrict({allow/deny})` 每 scope 过滤继承工具（可叠加取交）；`resolve()` **每步条件激活**（注册可带 `when(agent, modelSelection)`）；`schemas()` 白名单投影——execute/render/timeout/present 等宿主字段绝不泄入模型请求。
- **结构化输出 = 一等公民**：每个工具强制声明 `ToolOutputDefinition`（输出 JSON Schema + 纯函数 `render` 投影到模型内容 + 可选 `presentationMeta`）。统一 JSON Schema DSL（string/number/…/oneOf，enum/const 标量约束；外部 wire schema 走受支持子集强校验，不支持的关键字直接拒绝而非假装接受）。**PTC（Program-Tool-Calling）模式**：`tool-executor-ptc` 让模型以 `run_code` 程序化子调用工具（嵌套调用有独立调度与 `tools/ptc-dispatch` 日志事件）。
- **执行管线**：`tools/pre-execute`（决策 allow/deny/cancel/**ask**）→ 审批服务 → `ToolGuard`（**单调只收紧**的最终守卫，无 allow 返回值，后序监听无法翻案）→ `tools/execute`（around waterfall：超时/重试/指标）→ `tools/post-execute`（accept/替换内容或值/block 转纠错反馈）→ `tools/result`。并发调度 parallel/exclusive（`isConcurrencySafe` 分类 + 有界滚动池）。工具卡片 UI 中立词汇（generic/terminal/diff/search/read/web 六种 card）。
- **权限体系 = 两独立旋钮 × 预设**：
  - `sandbox/mode`：`read-only` | `workspace-write`（含 `--add-dir` 额外可写根）| `danger-full-access`；
  - `approval/policy`：`ask`（默认，走应答链，无应答者= fail-closed `unavailable`）| `never`（确定性拒绝，CI/无人值守姿态）；
  - **Permission presets**（`ctx.permissionPresets`）把两旋钮打包：默认表 `workspace-write`(+ask) 与 `danger-full-access`(+never)，`custom` 派生态 / `auto`（实验 Auto review 集成，danger-full-access+ask）为保留字。切换产生 durable 的 `permission/preset` 日志事件（log-only，不进模型 transcript），可重放。
- **MCP 为 opt-in**：`@deepseek-ai/dsh-mcp-client`（stdio / Streamable HTTP），工具命名 `mcp__<serverName>__<tool>`；stdio 桥启动子进程前**剥离凭据类环境变量与全部 `DSH_*` 变量**；崩溃自动重连（backoff + 工具重同步，预算耗尽则注销）。

佐证：docs/subsystems/tools.md、docs/subsystems/permission-presets.md、docs/subsystems/approval.md、docs/capability-seams.md

### 1.4 扩展机制（核心卖点，写深）

**微内核与插件的关系**：Cordis 以 vendor 形式钉在仓内（`vendor/`，rescoped 为 `@deepseek-ai/cordis`、private、pin 上游 SHA）。五个核心观念：插件=Service 对象（函数或 Service 子类）；context=服务仓库（`ctx.tools`/`ctx.llm`/`ctx.sessions`…按 key 寻址，不 import 具体实现）；`inject` 声明依赖→加载顺序隐式表达；类型化事件五种分发模式（emit/waterfall/parallel/serial/bail）；**注册即 effect，卸载按序回滚**。"Everything is a Plugin"的字面含义：换一个 fs/subprocess provider 指向远程沙箱，Bash/PTY/LSP 工具整体随迁，无需 provider 分叉。

**插件形态（三种）**：
1. **npm 包 / Git 源静态插件**：包 `package.json` 的 `dsh` 字段声明（`dsh.bundle` 指向 patch 文件）；`dsh plugin add <pkg>` 把参数原样转发给 pnpm 装进 profile 目录（可用 `remove`/`why`）。安装时做 `engines.dsh` / `@deepseek-ai/dsh*` peer 依赖兼容检查。Git 源支持 `git+<url>.git#<commit>` + 子目录。
2. **配置组合（不算写代码也算扩展）**：profile = `$DSH_HOME/profiles/<name>` 下有序 bundle 层（`dsh.profile` 列出）+ 用户 `cordis.patch.yml` + `--patch` overlay。任意行可按 id 整体替换或插入；`dsh --dump-config` 打印当前组合树。`dsh-base` 为 web/headless/sdk/acp 共享底层（模型适配器、工具、持久化、沙箱+审批策略、设置、凭据、遥测），web-app / headless / sdk-app / acp-app 各加一层，`sdk-minimal` 为刻意的全显式例外。
3. **动态插件（runtime self-modification，最激进）**：`packages/extensions`（`ctx.dynamicCordisRunner` + `cordis-host-runner`）——**让 agent 自己定义版本化 Cordis 包**（define/undefine/run/stop，host 半 + browser 半双端运行，Client 半需审批或面板手势授权，可"批准未来版本"），另配 `ctx.cordisInspect` 只读检查注册表。这是"agent 给自己写插件"的官方实现，带审批门。

**可 hook 的面（文档化扩展点，capability seam = Service Definition / Provider / Consumer 三角色完备）**：
- LLM：`ctx.llm` 注册适配器（cookbook: adding-an-llm-adapter）；
- 工具/能力：`ctx.tools`、`ctx.shell`、`ctx.terminals`、`ctx.fs`（provider 或 `fs/*` 事件）、`ctx.subprocess`、`ctx.sandbox`（进程约束后端）、SSH 全家桶（ssh fs/subprocess/sandbox 同策略远程执行）；
- Loop 拦截：`agent/pre-step`（改写/拒绝进入消息）、`agent/request`、`llm/stream`、`tools/*`、`agent/turn-stopping`；
- 上下文注入：`agent.inject()`（落地下一请求）、SessionEventMap 声明合并新增事件类型（如 compaction、hook/invoked）；
- UI：`ConversationNodeDefinition` + keyed renderer、slots、设置卡（cookbook: adding-a-settings-card）、命令 `ctx.commands`（无模型 turn 的人类命令）、webhook（`ctx.webhookRuntime` 外部触发会话）；
- Subagent providers：`fork-in-process` / `spawn-in-process` / `claude-code` / `codex` / `dsh-sdk` / `acp`（把 Claude Code、Codex 等其它产品当被委托 turn 的 provider）；Agent Teams（team 包：任务策略 + 共享任务板）。
- `docs/capability-seams.md`（71KB）是全量 seam 图谱 + `docs/module-graph.md`（137KB）为全模块依赖图。

**插件市场——没有官方中心市场**："DSH ships no index source"。机制 = **自托管 plugin index**：一个静态 JSON（format 1，含标题/图标/作者/版本/来源 npm 或 git/`engines.dsh`），任何静态主机可发布；Web 端"Discover"页签为**实验性 bundle（默认关）**；仓内 `apps/plugin-market-generator` 参考生成器（只下载读文件，不执行包代码）。社区发现靠 GitHub topic `dsh-plugin`。

**ACP profile 的意义**：`dsh --profile acp` 起 **Agent Client Protocol（agentclientprotocol.com，ACP v1，Zed 编辑器等推动的编辑器↔agent 开放标准协议）** 的 stdio 服务，定位 automation-only：程序化客户端可 create/resume/list/close 持久会话、挂 MCP、选模型与 reasoning effort、收语义更新；刻意省略 DSH 专属展示（卡片/计划/todos/终端视图/elicitation）。意义：①任意 ACP 兼容编辑器/IDE（Zed 等）可把 dsh 作为 agent 后端；②`dsh-subagent-acp` 反向把 dsh 自己当外进程 subagent 用。另注意实验互操作包：`hooks-claude-code`（Claude Code hook 桥，hook/invoked 日志对）、`subagent-claude-code` / `subagent-codex`。

佐证：docs/architecture.md、docs/cordis-primer.md、docs/subsystems/extensions.md、docs/user/guide/plugin-market.md、docs/user/develop/（basic=config/tool/publish，framework=events/service，practice=dynamic-cordis/llm-adapter）、packages/acp/acp/README.md、docs/capability-seams.md

### 1.5 HITL：审批 / 中断 / 恢复

- **审批**：`ctx.approval.request()` 要求 turn 开启（审计对必须落在 durable log 的 commit/replay 边界内），追加 `approval/asked` + `approval/decided` 审计对（log-only，不进模型 transcript）。结果闭集且 **fail-closed**：`allowed-once`（唯一授权，仅限本次动作）/ `rejected` / `cancelled` / `unavailable`（缺应答者、应答者抛错、返回非法值一律归一为 unavailable→拒绝）。UI 应答链按 agent 路由（Web 审批卡片挂在已流出的 tool call 上，不重复渲染参数）；ACP 桥提供一次性机器决定。
- **审批与沙箱的关系**：两旋钮正交组合（见 1.3）。`read-only`/`workspace-write` 是可下发给沙箱 provider 的模式；`danger-full-access` 消费者直接 spawn 原始 argv、不调用 `ctx.sandbox`。**审批后的升级重试 = 显式 mode override 的一次新 confine 调用**（per-call policy，不改 provider 状态）。approval policy 状态变化通过 runtime-context 快照对模型可见。
- **中断**：`cancel` 取消活跃 turn 而**不丢 pending inbox**；取消原因类型化（`AgentCancelCause`），turn/end 记 `aborted`；流中途取消会把已送达前缀定稿为 `interrupted: true` 的 assistant/message。
- **恢复**：中断/崩溃统一由 1.1 的 resume + `interrupted` 合成闭合器兜底；`interrupted` 是 loop 永不主动发出的唯一 TurnEndReason（只有恢复路径会写）。
- Web UI：`ui-approval` 审批卡 + `ui-permission-presets` 选择器；headless 默认面向无人值守（`--approval-policy never` 可显式设）。

佐证：docs/subsystems/approval.md、docs/subsystems/permission-presets.md、docs/subsystems/sandbox.md、docs/subsystems/session.md

### 1.6 可观测性：trace / 日志 / OTel / 评测

- **trace**：**未找到独立的分布式链路 trace 视图**（全 docs grep，"trace"命中的是 Cordis traceable proxy 机制与 session-query）。dsh 语境的 "trace" = **可回放会话 trace**：`session/event` → JSONL，重放 = `sessions.create(id, {seed})`（extension-cookbook 明文 "SessionTelemetryBackend / replayable trace"）；以及 `ctx.sessionQuery`（"Session reads, traces, filters, and search"——SQLite 后端全文检索/排序/摘要/游标分页；实验包 `tool-session-query` 让**模型自己查询历史会话**）。每次模型请求理论上可从日志+代码完整重建（含 usage、精确时间流）。
- **OTel**：`packages/telemetry/otel`（`ctx.otel` 工厂，独立 event/Session-log 通道，OTLP/HTTP，按消费者配置 transport/resource/scope）。产品遥测（`product-telemetry-otel`）**只上报显式选定的事件**，不自动采集 Session 数据或标识符；桌面端分析跟随启动期采集开关，普通 Web 客户端不采集；`DSH_TELEMETRY_MODE=DISABLED` 彻底关闭。注意：OTLP exporter 用 Node 自带 HTTP 客户端，**不走代理配置**（直连，出口被堵时上报直接失败但不影响功能）。反馈（feedback 包）经授权的 canonical-prefix 捕获 + 脱敏后上报。
- **token 计量**：`token-meter` 按路由定价的请求压力/表面快照（usage 锚定优先，无锚则保守估算）。
- **评测**：仓库级测试体系极强但**不是产品质量评测集**——real-API e2e（有 key 才跑，"只有带 key 的运行才能证明 agent 对真模型可用"）、keyless recorded-session snapshot 回放（走 shipped profiles）、Web 浏览器快照（Chromium/WebKit + ARIA 证据）、per-file 100% 覆盖门；`benchmarks/` 为性能门（long-session-browser、session-corpus、session-open 等）+ BENCHMARK.md 指向 Python SDK jsonrpc-agent 基准。**未找到**面向任务成功率/回归评分的产品级 eval 数据集与打分框架（类似 evals/ 的东西不存在）——如需 Agent 质量评测须自建。

佐证：docs/subsystems/otel.md、docs/subsystems/product-telemetry.md、docs/subsystems/token-meter.md、docs/subsystems/session-query.md、docs/cookbook/extension-cookbook.md、docs/testing.md、BENCHMARK.md

### 1.7 部署细节：自托管边界 / 数据流向 / 企业网关

**三种凭据/数据流模式**：
1. **DeepSeek 账号 token 模式**（`llm-deepseek-account`）：Desktop/账号登录，`x-dsh-auth-token` 头；模型目录来自账号服务 `GET /models`（端点不可配置）；无回退到 API key。
2. **纯 API key 模式**（默认 Web 引导路径）：Settings→Models 填 key（write-only，存 `$DSH_HOME/.credentials.yaml`，页面只见脱敏描述符）；第三方卡同构；**Custom model API**：任意 base URL + 三协议任选 + 环境变量引用 key——**企业内网网关/中继的标准接法**（一网关双协议需建两个 provider）。
3. **完全离线自托管模型端**：Custom model API 指内部 vLLM/网关即可；harness 本身除模型调用外无强制外联（遥测可关、市场默认无源、MCP 默认无）。

**⚠ 重大数据流发现（金融企业红线级）**：**shipped profiles 默认挂载并启用 `dsh-session-log-deepseek`（enabled: true）**——对官方 DeepSeek API 的每个请求会附带 `dsh_session_log` 字段（增量上传规范会话日志，单请求上限 8 MiB 序列化字节），并有 Web 开关"Settings → General → Upload Session Log when using the official model API"。即**用官方 API 时会话内容默认出域到 DeepSeek**；`ctx.sessionLogDeepSeek.registerExclusion()` 可编程排除，`enabled: false` 或不配官方路由可关闭。选型必须显式处置此开关。

**自托管边界**：`dsh web` 默认 127.0.0.1:3080 loopback；对外发布走反向代理：`--public-url`（对外地址广告）+ `--trusted-host`（/api 浏览器信任围栏接受的外部 authority，必须与浏览器所见 Host 一致）+ 代理义务（保 Host、剥挂载前缀、转发 WebSocket 升级、改写 cookie Path/Secure、重定向裸挂载路径）。原生 TLS：`--tls-cert/--tls-key`（PEM，一次读入，无证书管理）。**认证模型 = 打印 URL 里的 launch token 换签名 session cookie——单用户/小团队语义**；无 SSO、无用户管理、无多租户隔离（api-remote-access 的 role-based 策略面向 Peer 客户端调用 Remote 方法，不是人类用户 RBAC）。SSH 远程模式：SSH 启动只打印 host URL（编辑器/SSH client 拥有转发地址），`sandbox-ssh` 与 ssh fs/subprocess 同策略远程执行。

**企业代理接入**：标准 `HTTP_PROXY/HTTPS_PROXY/NO_PROXY/ALL_PROXY` 环境变量（启动时读；`$DSH_HOME/.env` 可放，项目 .env 被拒绝以防仓库决定流量去向）；不支持 SOCKS（启动时报告并跳过）；CIDR 写法无效（用主机/域后缀）；TLS 拦截型公司代理需 `NODE_EXTRA_CA_CERTS=corp-ca.pem`；模型调用的 bash 工具/git/MCP 子进程继承代理变量；**代理 URL 里的密码会传给模型写的命令**（文档自己警告）；loopback 永远直连；模型写的 workflow/PTC 代码**不接收**代理设置（防泄代理密码，需要联网须自行配置且受沙箱约束）。

佐证：docs/user/guide/providers.md、docs/user/guide/public-deployments.md、docs/user/guide/network-proxy.md、docs/api-gateway.md、packages/session/session-log-deepseek/README.md、packages/llm/llm-deepseek-account/README.md

---

## 2 国内可用性

**总评：国产第一方出品、双语文档覆盖近乎全量，是同层工具中国内可用性最好的；剩余摩擦在网络与 Node 版本，不在合规。**

- **天然国产**：DeepSeek AI 官方 org，默认模型路线即 DeepSeek API（platform.deepseek.com 取 key）。
- **中文文档：有，且全面**。根 README.zh.md + docs 下几乎每页都有 `.zh.md` 对应版（子系统页、用户指南、cookbook、升级指南全部双语成对维护，且有 doc-sync 门禁校验双语一致性）。国内团队几乎零文档障碍。UI 文案走 locale 字典（verify-client-ui-i18n 门禁拒绝硬编码）。
- **无境外依赖运行：可行**。anthropic/openai/moonshotai/zai 均为"添加第三方 provider"的可选卡，不配置即不连接；内置目录只在选中该 provider 时使用。Custom model API 可指向内网网关或 DeepSeek 官方端点。遥测 `DSH_TELEMETRY_MODE=DISABLED` 全关（OTLP 直连失败本来也不影响功能）；身份标识为匿名（identity 包）。⚠ 注意 1.7 的 session-log 默认上传问题——数据出域方向是 DeepSeek（境内），非境外，但金融合规仍需显式决策。
- **npm 安装可行性**：`npx @deepseek-ai/dsh` 走 npm registry——国内直连 npmjs 一般可用但不稳，可配 npmmirror 镜像或企业私服（`.npmrc` registry 全局生效；plugin index 还支持每插件 `registry` 覆盖，可钉内网源）。`dsh plugin` 底层是 pnpm，同样吃 `.npmrc`。**硬要求：Node ≥ 24.9**（apps/cli engines；企业镜像/基础镜像需预装新 Node；Python SDK 用户例外——runtime wheel 自带 dsh CLI，无需系统 Node）。Desktop 提供安装包（Windows 签名流程在 apps/desktop/windows-signing/）。
- **网络注意点**：文档站 deepseek-harness.github.io 与 Discord 社区在国内可达性差（GitHub Discussions 是替代反馈渠道）；克隆/下载 GitHub 仓库可能需代理（本调研中 raw.githubusercontent.com 曾 TLS 失败、api.github.com 正常）；源码运行需 pnpm + GitHub 访问。
- **冒名包风险**（再强调）：PyPI `deepseek-harness`/`deepseek-harness-cli`、npm `dsh` 不是本项目——国内私服若代理了全量 PyPI/npm，须防开发者误装。

---

## 3 企业落地注记

1. **developer preview / 无稳定版 = 版本兼容风险（官方明示）**。README："THERE WILL BE COMPATIBILITY-BREAKING CHANGES"；SAFETY.md：未做安全审计、不得当安全/生产就绪、不得作为不可信工作负载唯一安全控制。**但破坏性变更纪律显著好于一般 preview**：Session 格式有版本状态机（V4 finalized+released；prerelease 产品发布也承担已发布数据兼容义务）；规则要求每个外部可感知破坏性变更立即写 upgrade-guide（docs/upgrade-guide/ 按 v0.1.7-rc.2→v0.2.2-alpha.2 每版逐项，含迁移步骤）；相邻迁移只增不改，已提交 generation 不可变。落地建议：**钉死具体版本 + 每次 upgrade 前读对应 upgrade-guide + 会话数据留存迁移前备份**。
2. **Windows 沙箱（ACL restricted-token）细节**：`read-only` 下不授予任何显式可写根、对环境 ACL 缺口**自报 partial enforcement**；当前已知 partial 面 = **硬链接、非受限读（unconfined-read）、AppContainer-ACL 边界**；每 session/workspace 对发随机私有临时目录 + SID，workspace SID 与长期授权 per-workspace。结论：**Windows 上 enforcement = partial 是常态**，要求绝对边界的消费方必须拒绝或显式提示（框架已把 full/partial 作为事实传给消费者）。Linux 旧 Landlock ABI 同为 partial。整体 fail-closed：无可用后端 → `SANDBOX_UNAVAILABLE`，**静默未受限直通被架构禁止**。
3. **供应链（插件即 npm 包的投毒面）**：
   - 插件安装 = `pnpm add` 进 profile 目录 → 继承 npm 全部风险（typosquat、install 脚本、依赖混淆）；企业应锁定私服 + 禁 http index + 审计 install scripts。
   - plugin index 文档自警："Indexes are third-party content… DSH checks the file format but not those claims"，http index 链路上任何人可改（官方建议 https）；无官方中心市场 = 无集中审核。
   - **动态插件是独有额外攻击面**：agent 可自己写并加载 host+browser 半边代码（有审批门与"批准未来版本"机制，但 preview 期未审计）。金融场景建议不启用 extensions 相关 bundle。
   - 加分项：vendored Cordis pin 上游 SHA（THIRD_PARTY_NOTICES 全量披露）、CI per-file 100% 覆盖门、公开 postmortem（4 篇）与决策记录（.agents/notes）文化；e2e 明确"带 key 才算证明"。
4. **治理面缺口**：单用户 launch-token 认证，无 SSO/RBAC/多租户/审计导出；审批与权限变更审计只存在会话日志内（无集中审计管道）；数据出域默认项（session-log 上传）需显式关闭；无 SLA/商业支持（社区支持 = GitHub Discussions + Discord）。**金融机构当前定位建议：研发侧试点/二次开发底座（其架构与中文文档是稀缺优势），生产面向最终用户提供前须等稳定版 + 自建认证审计层。**

---

## 4 关键源码文件清单（深读入口）

（路径相对仓库根；每包均有 README.md + README.zh.md，docs/subsystems/ 有对应子系统页）

**启动与组合**
- `apps/cli/` — dsh 启动器（bin、profile 引导、`--dump-config`；config 示例含 mcp-memory 三配置）
- `packages/boot/app-boot/` — profile/bundle 组合与分层加载（README#profiles）
- `packages/boot/plugin-manager/` — 插件管理（Web + agent 用，转发 pnpm）
- `packages/boot/plugin-market/` — plugin index 服务（拉取/缓存/plugin_market 工具）
- `packages/bundle/base|web-app|headless|sdk-app|acp-app|sdk-minimal/` — 六个 profile bundle
- `apps/web/`、`apps/desktop/`、`apps/desktop-host/` — Web UI / Electron 壳 / 私有 Host
- `apps/plugin-market-generator/` — index 参考生成器

**核心循环与工具**
- `packages/core/session/`（types.ts = SessionEventMap/SessionHeader/SESSION_FORMAT_VERSION）、`packages/core/agent/`、`packages/core/agent-loop/`（README 极详尽：装配/取消/恢复）
- `packages/core/tools/`（index.ts 注册表/schema.ts DSL/presentation.ts 卡片词汇）
- `packages/core/system-prompt/`、`packages/core/scope/`、`packages/core/tool-executor-ptc/`（PTC run_code）

**持久化与查询**
- `packages/session/session-persistence/` + `session-persistence-jsonl/`（durability seam + JSONL 后端）
- `packages/session/session-checkpoint-policy/`（每请求 checkpoint）
- `packages/session-query/session-query/` + `session-query-sqlite/`（traces/检索/导出）
- `packages/session/session-log-deepseek/`（⚠ 默认会话日志上传）、`session-telemetry-otel/`
- `docs/persistence-schema.json`、`docs/persistence-catalog.md`（事件目录，364KB 生成物）

**权限/审批/沙箱**
- `packages/interaction/user-approval/`、`packages/interaction/permission-presets/`
- `packages/sandbox/sandbox/`（seam）、`packages/sandbox/sandbox-local/`（bwrap/Landlock/Seatbelt/Windows ACL，README 有各平台配置）
- `packages/ssh/sandbox-ssh/`（远程沙箱）

**LLM 适配**
- `packages/llm/llm/`（词汇+适配 seam）、`llm-deepseek/`、`llm-deepseek-api-key/`、`llm-deepseek-account/`、`llm-pi-ai/`（第三方/自定义 provider 聚合适配）、`token-meter/`

**扩展与生态**
- `packages/extensions/cordis-host-runner/`（动态插件 host 半边；extensions.md 为总览）
- `packages/acp/acp/`（ACP v1 服务）；`packages/subagent/subagent-{fork-in-process,spawn-in-process,acp,claude-code,codex,dsh-sdk}/`、`packages/team/`
- `packages/mcp/mcp-client/`；`packages/context/agent-instructions/`；`packages/skill/`
- `packages/compaction/compaction{,-basic}/`

**网关与遥测**
- `packages/api/gateway/`、`packages/api/session-controller/`、`packages/api/remote-access/`、`packages/client/connection/`（RPC/信任边界/launch token）；`docs/api-gateway.md`
- `packages/telemetry/otel/`、`packages/host/product-telemetry-otel/`、`packages/identity/`

**Python 与文档**
- `python/sdk/`、`python/sdk-runtime/`（wheel 打包 dsh CLI 的运行时）
- 关键文档：`docs/architecture.md`、`docs/capability-seams.md`、`docs/cordis-primer.md`、`docs/cordis-tutorial/`、`docs/session-format-status.md`、`docs/cli-help.md`、`docs/subsystems/`（约 60 页）、`docs/user/develop/`（插件开发三部曲）、`docs/upgrade-guide/`、`SAFETY.md`

---

## 5 信息源 URL 列表

主源（2026-11-03 master tarball 全文精读，路径可在 GitHub 对应浏览）：
- https://github.com/deepseek-ai/deepseek-harness （README.md / README.zh.md / SAFETY.md / BENCHMARK.md / 根 package.json）
- https://github.com/deepseek-ai/deepseek-harness/tree/master/docs （architecture / cli-help / session-format-status / api-gateway / cordis-primer / capability-seams / testing / cookbook/* / upgrade-guide/*）
- https://github.com/deepseek-ai/deepseek-harness/tree/master/docs/subsystems （session / persistence / approval / permission-presets / sandbox / tools / compaction / otel / product-telemetry / token-meter / extensions / session-query）
- https://github.com/deepseek-ai/deepseek-harness/tree/master/docs/user/guide （index / providers / network-proxy / public-deployments / mcp-memory / plugin-market / python-sdk）
- https://github.com/deepseek-ai/deepseek-harness/tree/master/docs/user/develop （basic / framework / practice）
- 包级：packages/context/agent-instructions、packages/acp/acp、packages/session/session-log-deepseek、packages/llm/llm-deepseek-account、packages/skill/skill（以上各 README.md）
- 文档站（同内容投影）：https://deepseek-harness.github.io/deepseek-harness/

协议与论文参考：
- https://agentclientprotocol.com （ACP v1）
- https://arxiv.org/abs/2608.25512 （Cordis 设计论文）

已核实事实（GitHub API，2026-11-03）沿用任务书：270,674 stars / MIT / TypeScript / 36 prereleases（最新 dsh-v0.2.2-alpha.3）/ npm `@deepseek-ai/dsh` / PyPI `deepseek-harness-sdk`(+runtime-bin) / 维护者 tianyicui 等。
