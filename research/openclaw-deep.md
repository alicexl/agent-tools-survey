# OpenClaw 源码深读笔记（成品/产品层代表）

- 深读时点：2026-11-03
- commit：`4285c6ce`（2026-11-03 11:11:34 -0300, "improve(ui): rebuild the channel setup dialog on one dialog layout (#191385)"）
- 仓库：`D:\workspace\agent-tools-survey\repos\openclaw`（浅 clone），pnpm monorepo，包版本 `2026.10.10`（CalVer）
- 定位（`package.json` description）："Multi-channel AI gateway with extensible messaging integrations"；README.md："an open-source AI assistant that runs on your own computer and meets you in the channels you already use"；产品演化史 Warelay → Clawdbot → Moltbot → OpenClaw（`VISION.md`）
- 架构主张（README.md）："The architecture case — trusted gateway, untrusted execution, policy enforced by code"。Gateway 是本地控制平面（sessions/tools/events/channels），Control UI/CLI/TUI 都是它的客户端；模型与 agent harness 全部插件化（"Models and agent harnesses (Claude, Codex, local models) are plugins you can swap without changing anything else"）

---

## 1. 状态（session 持久化 / 重启恢复 / 崩溃恢复）

### 1.1 存储：每 agent 一个 SQLite + 一个共享 state 库，可选 Postgres

- 路径解析（`src/state/openclaw-agent-db.paths.ts`）：
  ```ts
  const resolved = path.resolve(stateDir, "agents", agentId, "agent", "openclaw-agent.sqlite");
  ```
  共享库 `<stateDir>/state/openclaw.sqlite`（`src/state/openclaw-state-db.paths.ts:41`），隔离/检疫库 `openclaw-quarantine.sqlite`（同文件 :54），隐身会话哨兵文件名 `incognito-openclaw-agent.sqlite`（`INCOGNITO_AGENT_SQLITE_BASENAME`）。默认 stateDir `~/.openclaw`，可用 `OPENCLAW_STATE_DIR` 覆盖（`src/config/paths.ts:71`）。
- 引擎用 Node 内建 `node:sqlite` 的 `DatabaseSync`（`src/state/openclaw-agent-db-engine.ts:1` `import type { DatabaseSync } from "node:sqlite"`），同时抽象出 `EngineConnection` 支持 Postgres（`src/infra/database-engine/`）。`package.json` 的 `openclaw` 段声明双引擎与 5 个 Postgres "family"：
  ```json
  "databaseEngines": ["sqlite", "postgres"],
  "postgresFamilies": ["state", "agent", "workboard", "logbook", "team-reports"],
  "schemaVersions": { "state": 39, "agent": 40 }
  ```
- 每 feature 一个 schema version 常量并配对迁移文件（`src/state/openclaw-agent-db-contract.ts:6-21`：`AGENT_COLD_NAVIGATION_SCHEMA_VERSION = 39`、`AGENT_TEXT_SEARCH = 38`、`AGENT_VECTOR_SEARCH = 33`、`AGENT_MEMORY_REVISION = 31`……），启动时 admission + 迁移 + registry 注册（`openclaw-agent-db-registry.ts`、`openclaw-agent-db-lease*.ts` 有跨进程租约/心跳）。
- 会话表结构（`src/state/openclaw-agent-storage-schema.ts:9-40`）：`session_windows`、append-only 转写表
  ```sql
  CREATE TABLE IF NOT EXISTS transcript_events (
    session_id TEXT NOT NULL, seq INTEGER NOT NULL, event_json TEXT NOT NULL, created_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, seq),
    FOREIGN KEY (session_id) REFERENCES "session_windows"(session_id) ON DELETE CASCADE
  ) STRICT;
  ```
  加 `memory_index_chunks` / `memory_embedding_cache`（记忆索引，见 §2）、`session_transcript_fts`（FTS 全文检索，`src/state/openclaw-agent-search-schema.ts:14`）、向量列（`AGENT_VECTOR_SEARCH`）与冷归档 chunks（`openclaw-agent-cold-archive-chunks-schema.ts`）。

### 1.2 启动准入：连 SQLite 版本都要探测

根目录 `node-sqlite.mjs` / `node-sqlite-capabilities.mjs` 在进程启动时探测宿主 Node 的 sqlite 能力并**拒绝启动不合格运行时**（`node-sqlite.mjs`）：
```ts
if (!probe.text && !probe.error) {
  return `${label}: node:sqlite truncates TEXT at embedded NUL (nodejs/node#61954); use 24.16+/26.1+ ...`;
}
if (!probe.version || !isSqliteWalResetSafeVersion(probe.version)) {
  return `${label}: SQLite ${probe.version} is not WAL-reset-safe; use SQLite 3.51.3+, 3.50.7+, or 3.44.6+ ...`;
}
```

### 1.3 Gateway 重启后的会话恢复

- 会话状态本身在 SQLite（§1.1），Gateway 是常驻进程（daemon / systemd-launchd / docker `restart: unless-stopped`），重启后按 agent DB 恢复；启动侧有一整套 admission/preparation 文件（`src/gateway/server-agent-database-startup*.ts`、`src/state/agent-database-startup-integrity.ts`、`openclaw-agent-db-startup.journal-bound.test.ts` 处理 WAL/journal 边界）。
- 消息输入有持久队列：`src/gateway/queued-chat-inputs.ts` + `queued-chat-input-admission.ts`。
- 写给协作 agent 的行为准则（根 `AGENTS.md`）把"重启恢复"上升为产品语义："After a restart, reconcile prior effects and resume already-authorized work under current authority; do not ask for the same approval again solely because the process restarted."（重启后不重问已批过的授权）。

### 1.4 崩溃/断点恢复（turn 级）

- `src/agents/embedded-agent-runner/run/attempt-recovery.ts`：失败按 `"timeout_recovery" | "overflow_recovery" | "prompt_recovery"` 等分类恢复；配套 `incomplete-turn-recovery.ts`（结算未完成工具批）、`context-recovery-state.ts`、`timeout-context-recovery.ts`、`overflow-context-recovery.ts`。
- 其它恢复面：`src/gateway/operator-run-recovery.ts`（操作员 run 恢复）、`exec-approval-recovery.ts`（审批中断恢复）、`in-process-subagent-resume.ts`（子代理续跑）、`src/agents/harness/compaction-recovery.ts`（压缩失败恢复）。
- 根目录 `node-runtime-recovery.mjs` **不是会话恢复**，是 Node 运行时自愈：`resolveRecoveryPath()` 在选用 node 二进制时拒绝 cwd/workspace 内路径与 symlink 中间目录（`// Trust only the private recovery root when launching from HOME; reject other cwd symlinks.`），`isUsableNode()` 探测时 `// Do not pass preload hooks, native-library overrides, or application secrets to probes.` ——恢复路径本身按防投毒设计。

---

## 2. 记忆

### 2.1 双栈：内置文件式记忆（memory-core）+ LanceDB 向量插件（memory-lancedb）

**内置栈（extensions/memory-core，193 个 src 文件）**：
- 工作区根 `MEMORY.md` 是 canonical 根记忆文件（`src/memory/root-memory-files.ts:5-7`：`CANONICAL_ROOT_MEMORY_FILENAME = "MEMORY.md"`，legacy `memory.md`），工作区还有 `memory/` 目录等记忆文件（`extensions/memory-core/src/memory-workspace-files.ts`）。
- 文件被切块索引进 per-agent SQLite：`memory_index_chunks(path,start_line,end_line,hash,model,text,embedding)` + `memory_embedding_cache(provider,model,provider_key,hash,embedding,dims)`（`src/state/openclaw-agent-storage-schema.ts:17-38`）。
- 检索：FTS + 向量 KNN 双路。KNN 直接在 SQLite 向量表上做（`extensions/memory-core/src/memory/manager-search-knn.ts`，kysely `sql` + `admitStoreSearch`/`matchesVectorRow`，带 `fallbackScanRequired` 降级）；embedding 可走本地（`embedding-local-service.ts`）或 HTTP provider。
- "dreaming"：空闲整理/巩固机制（`dreaming-consolidation.ts`、`dreaming-cron.ts`、`dreaming-phases.ts`——按 cron 在低峰把会话语料固化为记忆，含 snippet 去重与 narrative）。
- 会话回填：`session-backfill*.ts` / `session-ingestion.ts` 把历史转写吸入记忆索引；`memory-forget-*.ts` 是一整族"遗忘"实现（索引读、任务、恢复、报告）。

**插件栈（extensions/memory-lancedb）**：LanceDB 向量库（默认 `~/.openclaw/memory/lancedb`，见其 `openclaw.plugin.json` uiHints `dbPath` placeholder），OpenAI 兼容 embedding（`extensions/memory-lancedb/embeddings.ts`），暴露三个工具 `memory_recall` / `memory_store` / `memory_forget`（manifest `contracts.tools`；`index.ts` 用 TypeBox 定义参数 schema）。
- 自动召回挂在 `before_prompt_build`（`index.ts:493`），且**受当轮工具策略约束**：
  ```ts
  const toolAuthority = ctx.toolAuthority;
  toolAuthority.assertActive();
  if (!toolAuthority.allows("memory_recall")) { /* auto-recall skipped by turn tool policy */ }
  ```
- 自动捕获挂在 `agent_end`（`index.ts:588`），捕获判定是启发式 + 防注入（`memory-policy.ts` `shouldCapture()`：触发词正则、长度上限、含 `<relevant-memories>` 拒、`looksLikePromptInjection(text)` 拒、emoji>3 拒）。
- 工程细节：embedding 超时进入 per-agent 60s 熔断（`DEFAULT_RECALL_COOLDOWN_MS = 60_000`），防一个挂起请求拖死主流程；换 embedding 模型/维度不重嵌会破坏向量兼容（`index.ts` 注释："changing provider/model/dimensions without re-embedding corrupts search compatibility"）。

### 2.2 短期 vs 长期边界

- 短期（会话内）= per-agent SQLite 的 `session_windows`/`transcript_events`（append-only、seq 有序、只允许 compaction 改写历史——根 AGENTS.md："Preserve transcript bytes; ... only compaction rewrites history"）；上下文装配有字节/token 双预算硬界（`src/context-engine/bounded-context.ts` `boundContextEngineAssembly`），语义压缩在 `packages/agent-core/src/harness/compaction/`（cut-point 选择、branch summarization、summary cap）。
- 长期（跨会话）= workspace 记忆文件 + 向量/FTS 索引；隐身会话显式排除（`isIncognitoSessionKey(ctx.sessionKey)` 直接 return，`memory-lancedb/index.ts:496`）。

### 2.3 跨渠道身份统一

- 会话键按"渠道:对象:peerId"组织（`src/sessions/session-key-utils.ts:170-206` `parseSessionDeliveryRoute`：`{channel, peerId, peerKind: channel|direct|dm|group, threadId}`，如 `telegram:dm:12345`、`discord:group:<id>`，另有 `agent/<agentId>/` 前缀与 `#thread` 后缀）。**会话天然按渠道 peer 分裂，不存在把同一人不同渠道自动合并的身份层**。
- 跨渠道统一发生在 agent 命名空间：记忆、工作区、凭据都按 `agentId` 归并（memory 检索/嵌入均首参 `agentId`），同一 agent 接多少渠道共享同一份长期记忆与 workspace。这是"按代理统一"而非"按用户统一"的产品取舍。

---

## 3. 工具调用

### 3.1 注册与 schema

- 工具用 `api.registerTool((ctx) => ({name, description, parameters, execute}), {name})` 注册，参数 schema 用 TypeBox（`extensions/memory-lancedb/index.ts:65-78` `Type.Object({...})`）；结果统一 `textResult/jsonResult`（`openclaw/plugin-sdk/tool-results`）。工具可声明元数据：`api.registerToolMetadata({toolName, risk: "high", tags, sideEffecting})`（例：`extensions/codex/index.ts` 中 `codex_threads` 标 `risk: "high"`）。
- 模型坏输出有整包修复逻辑：`packages/tool-call-repair`（纯文本工具调用块解析/剥离，`src/plugin-sdk/tool-payload.ts` re-export）。

### 3.2 工具策略（allow/deny 档位）

- 策略模型 `SandboxToolPolicy = { allow?: string[]; deny?: string[] }`（`src/agents/sandbox/types.ts:23-27`），解析链在 `src/agents/agent-tools.policy.ts`：来源分 agent/global/default，支持 `alsoAllow` 追加合并（`deny wins`），并按 provider docking（`tools.byProvider`）、渠道 group 策略、模板 consent 合并（`src/config/zod-schema.agent-runtime.ts:59-69`，allow 与 alsoAllow 互斥有编译期校验）。
- shell 执行是**五档独立策略**（`src/config/zod-schema.agent-runtime.ts:263-268`）：
  ```ts
  /** Normalized exec policy mode. Prefer this over raw security/ask knobs. */
  mode: z.enum(["deny", "allowlist", "ask", "auto", "full"]).optional(),
  security: z.enum(["deny", "allowlist", "full"]).optional(),   // legacy
  ask: z.enum(["off", "on-miss", "always"]).optional(),          // legacy
  ```
  `auto` 档注释：`Model-backed reviewer used by tools.exec.mode=auto before falling back to human approval.`（AI 预审 → 人工兜底）。allowlist 允许声明 stdin-only 安全二进制；allow-always 授权有 TTL 粒度且自动化审批禁止持久化（同文件 :284 注释）。
- exec 审批持久化在独立 store（`src/infra/exec-approvals.ts`：`ExecApprovalsFile` + socket 通道 + `AllowAlwaysPattern`；`redactExecApprovals` 对展示层脱敏 socket token）。
- 策略钩子 fail-closed：`src/plugins/hook-types.ts` 注释 "Policy hooks fail closed in the global runner. A bounded timeout turns a stalled policy process into a denial instead of freezing the operation."（`before_tool_call` modifyingTimeoutMs 15s）。
- 子代理默认硬禁名单（`src/agents/agent-tools.policy.ts:47-67`）：`SUBAGENT_TOOL_DENY_ALWAYS = ["gateway","agents_list","openclaw","session_status","progress_card","message","sessions_send","conversations_list","conversations_send","conversations_turn", ...]`，叶子代理另禁 `subagents/sessions_spawn` 等——子代理不能越级操作 gateway/直接对用户发消息。

### 3.3 MCP：客户端 + 服务端 + 反向中继

- 客户端：配置 `mcp.servers`（`src/config/mcp-config.ts`），argv 展示层脱敏（`REDACTED_SENTINEL` 回写校验，改形即拒绝）。每个 agent bundle 还有 MCP 物化管理器（`src/agents/agent-bundle-mcp-manager*.ts` 安装/清理/闲置策略/重载，`agent-bundle-mcp-materialize.ts` 物化进 harness）。
- 服务端：OpenClaw 把自己的工具以 stdio MCP server 暴露（`src/mcp/tools-stdio-server.ts`、`openclaw-tools-serve.ts`），可被 Claude Code/Codex 等外部 harness 当 MCP 服务器调用。
- 反向中继插件 `extensions/mcp-relay`（默认禁用）：Gateway **主动发起 outbound WebSocket**，不开放入站端口、不共享 Gateway token（README："The Gateway opens an outbound WebSocket; no inbound Gateway port or configured Gateway token or password is shared with the relay."）——让 ChatGPT 等远端客户端安全接入；授权走 owner/person principal + operator scopes，可选"Sign in with your Gateway"。
- harness 可自带原生 MCP（契约方法 `loadMcpToolCatalog` / `acquireMcpAppRuntime`，见 §4.2）。

### 3.4 结构化输出与类型校验

- 工具参数 = TypeBox/JSON-Schema 运行时校验（`src/plugin-sdk/json-schema-validator.ts`、`json-schema-runtime.ts`）；
- swarm 收集器模式支持对子代理产出强制 output schema（`sessions_spawn` 参数 `outputSchema` 需 `collect=true`，`src/agents/tools/sessions-spawn-tool.ts:395-405` 报错文案即契约）；swarm 有专门 `swarm-output-schema.ts`。

---

## 4. 扩展机制（重点）

### 4.1 插件体系全貌

- 形态：`extensions/<id>/index.ts`（`definePluginEntry({id, name, kind, configSchema, register(api)})`）+ `openclaw.plugin.json` manifest。manifest 声明：`categories`、`activation.onStartup/onCommands`（惰性激活）、`cliCommands/commandAliases`、`configSchema`（JSON-Schema）+ `configGroups`（UI 分组）+ `uiHints`（每字段 label/sensitive/advanced/help）、`contracts.tools`（对外承诺的工具名）、`toolMetadata.sideEffecting`、`doctorContract.stateMigrations`、`catalog.featured`（样例：`extensions/memory-lancedb/openclaw.plugin.json`、`extensions/policy/openclaw.plugin.json`）。
- 运行时注册面 ~50 个（`src/plugins/plugin-api.types.ts:113-441`）：`registerTool / registerHook / registerHttpRoute / registerWidgetPresenter / registerMcpServerConnectionResolver / registerChannel / registerGatewayMethod / registerGatewayAccessPolicy / registerSessionCatalog / registerCli / registerProvider / registerEmbeddingProvider / registerWebSearchProvider / registerImageGenerationProvider / registerMediaUnderstandingProvider / registerDecisionProvider / registerCompactionProvider / registerContextEngine / registerAgentHarness / registerAgentToolResultMiddleware / registerTrustedToolPolicy / registerMemoryCapability / registerSecurityAuditCollector / registerService ...`
- 事件钩子 40+ 个，且**每个钩子在元数据表里声明风险属性与失败语义**（`src/plugins/hook-types.ts:124-188` `PLUGIN_HOOK_METADATA`）：`before_model_resolve / agent_turn_prepare / before_prompt_build / before_agent_reply / model_call_started|ended / llm_input / llm_output / before_agent_finalize / agent_end / before|after_compaction / before_reset / before_channel_ingress / inbound_claim / channel_pairing_requested / message_received / message_sending / reply_payload_sending / message_sent / before|after_tool_call / tool_result_persist / before_message_write / session_start|end / run_stopped / subagent_spawned|progress|ended|delivery_target / gateway_start|stop / heartbeat_prompt_contribution / cron_reconciled|changed / skill_changed / before_dispatch / reply_dispatch / before_install / before_agent_run / resolve_exec_env`。元数据含 `conversationAccess`（能否读会话）、`promptInjection`（是否注入面）、超时（modifying 15s / void 30s / gateway_stop 5s）与 fail-open/fail-closed 注释（如 "Terminal finalization hooks ... timeout fail-opens with the original final answer"）。
- 分发双通道：npm 与 ClawHub（`src/plugins/install-channel-specs.ts:15` `source: "npm" | "clawhub"`；`scripts/lib/official-external-plugin-catalog.json` 为内置回退 feed）。安装前过 `before_install` 钩子 + 外部 policy 命令（§8）。
- 设计纲领（根 `AGENTS.md`）："**Small core, capable plugins.** Model-facing core additions cost ongoing context. Optional capability belongs at edges; core supplies generic contracts."，并有 capability ladder（`src/plugin-sdk/AGENTS.md#choose-the-capability-surface`）约束新能力先落插件边缘。

### 4.2 Harness 插件：agent 运行时可插（本仓最大亮点的之一）

- 契约：`AgentHarness` / `AgentHarnessV2`（`src/agents/harness/types.ts:620-770`）。一个 harness 要实现：`runAttempt`（跑一轮）、可选 `runIsolatedCompletionV2`（零工具纯补全，注释要求 fail-closed："The harness must fail closed when it cannot enforce that native boundary."）、`classify`（结果分类）、`compact`（原生压缩）、`reset`、`withSessionDeletion/withSessionContextReset/withDescendantCancellation`（会话删除/上下文剪除/子代取消的事务边界）、`sessionForkV2`（会话分叉）、`runtimeArtifact.validate`（工件再校验）、`authBinding.fingerprint`（凭据指纹在信任边界重算）、`fetchUsageSnapshot`、`loadMcpToolCatalog/acquireMcpAppRuntime`（原生 MCP 借用）、`loadModelCatalog/filterModelServiceTiers/readModelCatalogReadiness`（原生模型目录投影）。
- 注册：`api.registerAgentHarness(harness, options)` → `src/agents/harness/registry.ts`。两条硬规则（registry.ts:44-56）：
  ```ts
  if (id === "openclaw") { throw new Error('agent harness id "openclaw" is reserved for the built-in runtime'); }
  if (options?.nativeCompaction && (id !== "codex" || pluginId !== "codex")) {
    throw new Error("native compaction requires the registry-owned Codex harness");
  }
  ```
- 现有 harness：
  - 内置 `openclaw`（`src/agents/harness/builtin-openclaw.ts`，嵌入式 runner，模型走 provider 插件：anthropic/openai/google/deepseek/qwen/moonshot/ollama/vllm/lmstudio 等几十个 extensions）；
  - `codex`（`extensions/codex/index.ts:291` `api.registerAgentHarness(createCodexAppServerAgentHarness(...), {nativeCompaction: ...})`；包描述 "OpenClaw Codex app-server harness and native session supervision plugin"，依赖精确 pin `@openai/codex: 0.164.0` 并声明六平台 requiredPlatformPackages）；
  - `copilot`（GitHub Copilot，`extensions/copilot/index.ts:47`）、`agentsapi`（Google Agents API，`extensions/agentsapi/index.ts:15`）、`acpx`（ACP 协议适配层，`extensions/acpx/index.ts:247`，经 `@agentclientprotocol/claude-agent-acp` / `codex-acp` 接外部 CLI agent）、`muse`。
  - Claude 的接入是**provider + CLI 会话目录编目**两条：`extensions/anthropic/`（Claude API provider + Claude Code CLI 集成：`cli-process.ts`、`cli-transport.ts`、`session-catalog-*.ts` 扫描 `~/.claude` 会话进 session catalog）；另有 `migrate-claude` 扩展做迁移。
- harness 选用有 auto-selection 提示（`src/agents/harness/auto-selection.ts`：harness 声明 `autoSelection.providerIds`，不匹配即负向短路）。

### 4.3 skills/ 与 custodian-skills/ 的区别

- `skills/`（50 个）：**用户/助理技能**。`SKILL.md` frontmatter 除 name/description 外有 `metadata.openclaw` 声明式依赖与安装：`requires.anyBins: ["claude","codex","opencode"]`、`requires.config`、`install: [{kind: "node", package: "@anthropic-ai/claude-code", bins: ["claude"]}]`（`skills/coding-agent/SKILL.md`）——技能缺依赖时系统能按 spec 安装。
- `custodian-skills/`（4 个：`add-model-provider` / `cloud-image-bake` / `configure-channel` / `diagnose-gateway`）：**面向 system agent（"ambient owner"）的运维技能**，只有当前 agent 是配置的 system agent 时才挂载（`src/skills/loading/workspace-skill-sources.ts:150-157`）：
  ```ts
  if (resolveCustodianSkillAgentId(opts?.config, opts?.agentId)) {
    roots.push({ dir: path.join(path.dirname(bundledSkillsDir), "custodian-skills"), source: "openclaw-custodian", tier: "bundled" });
  }
  ```
  而 `resolveCustodianSkillAgentId` = agentId === `cfg.agents.defaults.systemAgent.agentId`（`workspace-skill-sources.ts:77-86` + `src/agents/agent-scope-config.ts:224`）。内容是严格的只读诊断/受控变更 playbook（`custodian-skills/diagnose-gateway/SKILL.md`："This playbook is read-only: no config writes, no service restarts, no `doctor --fix` ... Never print secret values"）。即"管家代理专属技能层"，与用户技能隔离。
- 技能发现层级与冲突报告：tier = extra / bundled / custodian / workshop / managed / personal(`~/.agents/skills`) / workspace（`workspace-skill-sources.ts`），同名冲突按 precedence 报告并按内容哈希去噪（`skill-precedence.ts`）。
- 另有 agent **模板**体系（`src/templates/`：manifest `bootstrapFiles: {"AGENTS.md": ...}`、ClawHub 发布 consent、tool-policy candidates——新建 agent 时的脚手架与策略预案）。

### 4.4 子代理 / 多 agent 编排

- 核心工具 `sessions_spawn`（`src/agents/tools/sessions-spawn-tool.ts:344+`）：参数支持 `runtime="subagent"`（内嵌）或 ACP harness spawn、`thread`/`visible`（可见会话）、`runTimeoutSeconds`；swarm 模式参数 `collect=true` + `outputSchema` + `groupId` + `fastMode`（需 `tools.swarm.enabled=true`；collector 派生强制 collect 以保证"approvals stay non-interactive"，:375）。
- 编排组件：`src/agents/subagents/spawn/`（acp-spawn-* 创建/准入/父流存储 SQLite）、`src/agents/subagents/swarm/`（swarm-scheduler claim/close、collector 能力）、announce 链通信（`subagents/announce/`）、capability store 继承（`subagent-capabilities.ts` 的 inherited allow/denylist）。
- 跨会话操作工具族：`sessions_list/history/search/send`、`conversations_*`（受 §3.2 deny 名单管控）。

---

## 5. HITL（人工审批与中断）

- 两阶段审批：工具执行前经 `before_tool_call` 返回 `requireApproval` → 网关注册审批请求并等待决定（`src/agents/bash-tools.exec-approval-request.ts` 文件头注释："Registers two-phase approval requests with the gateway, waits for decisions"；常量 `DEFAULT_APPROVAL_REQUEST_TIMEOUT_MS`）。
- **渠道内呈现**：交互按钮卡（`src/infra/exec-approval-reply.ts:163-174`）：
  ```ts
  const buttons: MessagePresentationButton[] = actions.map((descriptor) => {...});
  return buttons.length > 0 ? { blocks: [{ type: "buttons", buttons }] } : undefined;
  ```
  渠道具备原生审批 UI 时走 capability（`src/channels/plugins/native-approval-prompt.ts` `NATIVE_APPROVAL_PROMPT_RUNTIME_CAPABILITY = "nativeApprovals"`，渠道目录 `approvalFlags`）；无按钮渠道回退 `/approve` 命令（`src/auto-reply/reply/commands-approve.ts:90` `COMMAND_REGEX = /^\/?approve(?:\s|$)/i`，含 Telegram 外部 bot mention 处理、非授权 sender 拒绝、"Only the owner can approve"、gateway client 需 `operator.approvals` scope）。
- 审批等待期间 run 计时暂停（`src/agents/agent-run-approval-wait.ts`：监听 lifecycle 事件 `waiting-approval` / `approval-resolved`，`pausedMs` 不计入执行时间）；审批 custody 有渠道归属与 web push（`src/gateway/approval-channel-custody.ts`、`approval-web-push.ts`）。
- 拒绝的终局语义写给模型（`src/agents/agent-tools.before-tool-call.approval.ts:45-52`）："This denial is final: the approval request is closed. Do not mention /approve ... Do not run the tool call again or ask the user to approve it again."
- 中断/插话：steering 机制把新用户输入注入运行中的 turn（`src/sessions/session-controller.steer.ts`，结果三态 accepted/indeterminate/rejected，indeterminate 显式"never replayable"）；取消有 descendant-cancellation 事务（§4.2）。
- 重启后不重复审批（§1.3 AGENTS.md 约定）。

---

## 6. 可观测性

- **单一诊断事件漏斗，双导出器**：核心产生 `DiagnosticEventPayload`，两个插件各自消费——
  - OTel（`extensions/diagnostics-otel`）：traces+metrics+logs 三信号经 OTLP `http/protobuf` 导出（`src/service.ts` 固定 `OTLP_HTTP_PROTOBUF_PROTOCOL`），采样 `ParentBasedSampler({root: new TraceIdRatioBasedSampler(sampleRate)})`（service.ts:459-462），signal 级 endpoint 覆盖与开关、`diagnostics.otel.captureContent`（span 内容捕获）**默认关**；模型调用带 GenAI semconv 属性（`gen_ai.operation.name`、`openclaw.model_call.time_to_first_byte_ms`）。
  - Prometheus（`extensions/diagnostics-prometheus`）：`api.registerHttpRoute({path: "/api/diagnostics/prometheus", auth: "gateway", ...})`，30+ `openclaw_*` 指标（run/tool/model/tool_execution_blocked/session_stuck/queue_lane_wait/event_loop_delay_p99 等）；文档强调必须走 Gateway 认证（operator.read），不得当公开 `/metrics`。
- 日志：子系统层级 logger（`src/logging/subsystem.ts` `createSubsystemLogger("agents/harness")`，trace..fatal + child），文件日志 tslog JSONL、按日滚动 `/tmp/openclaw/openclaw-YYYY-MM-DD.log`（上限 100MB/24h、5 归档，`src/logging/log-file-path.ts` + `logger.ts:43-44`），全量过 `redactSensitiveText` 脱敏。
- 事件总线：Gateway WebSocket 协议事件目录 `src/gateway/events.ts` `GATEWAY_EVENTS = ["connect.challenge","agent","chat","session.message","sessions.changed","presence","tick","health","heartbeat","cron","node.presence","device.pair.changed", ...]`，广播机械在 `server-broadcast-*.ts`；无 SSE，Control UI 走同一 WS。
- Dashboard/Control UI：`ui/` = Vite + **SolidJS**（`ui/package.json` `solid-js: 2.0.0-rc.18`）SPA，由 Gateway 同端口（默认 18789）服务，经 `packages/gateway-client` WS 协议客户端直连；`openclaw dashboard` 产出短时一次性 owner pairing link（`src/commands/dashboard.ts`）。
- 另有运行轨迹导出（`src/trajectory/`，`commands-export-trajectory.ts`）与 phone-home 策略文档（默认仅每日版本检查，统计 opt-in）。

---

## 7. 部署与许可证

### 7.1 自托管形态

- Docker（根 `Dockerfile`）：多阶段、digest 钉住的 `node:24.22.1-trixie`；最终镜像非 root（`USER node`）、tini 作 init、入口 `docker-entrypoint.mjs`（前台 gateway 先跑 `doctor --fix --non-interactive` 再 `execve`），健康检查 `dist/docker-healthcheck.js`（`/healthz` `/startupz` `/readyz`）。
- `docker-compose.yml`：gateway + cli 两服务同镜像；卷 `~/.openclaw → /home/node/.openclaw`；加固 `cap_drop: [NET_RAW, NET_ADMIN]` + `security_opt: [no-new-privileges:true]`；端口 18789（gateway）/18790（bridge）/3978（MS Teams）；注释掉的 docker.sock 挂载用于沙箱模式。
- `fly.toml`：单进程 gateway，持久卷 `[mounts] source="openclaw_data" destination="/data"`，`auto_stop_machines = false # Keep running for persistent connections`，`/startupz` 健康检查；`render.yaml`：web 服务 + 自动生成 token（`generateValue: true`）+ 1GB disk；`deploy/fly.private.toml`：**无公网 ingress** 的加固模板（"No [http_service] block = no public ingress allocated"，仅 fly proxy/wireguard/ssh 进入，README 注明 hidden from internet scanners）。
- 沙箱（执行隔离）：`src/agents/sandbox/` 完整容器沙箱——Docker 后端必填项 `image/containerPrefix/workdir/readOnlyRoot/tmpfs/network/capDrop`（`types.ts:8-16` `RequiredDockerConfigKeys`）、容器 keyring、SSH 沙箱、workspaceAccess 挂载、浏览器隔离（binds + noVNC）；`mode: "off" | "non-main" | "all"`（`SandboxConfig`）。README 明示默认不沙箱："Tools run on the host for the main session unless you configure sandboxing."
- 密钥：SecretRef 体系 + 独立 secret store + 审计扫描明文凭据（`src/secrets/audit.ts`、`auth-profiles-scan.ts`："Scans auth-profile stores for plaintext credentials, SecretRefs, and OAuth tokens"）、配置写回脱敏（§3.3）；`extensions/onepassword`、`extensions/vault` 提供外部密钥源；compose 把 auth-profile secrets 单独卷挂载。

### 7.2 crates/ 的 Rust：不是桌面壳，是"节点运行时 + 网关客户端"SDK

- `crates/openclaw-gateway-client`：`description = "Reusable Rust transport and policy for OpenClaw Gateway clients"`——tokio+tungstenite(rustls) WS/TLS 客户端传输与连接策略（含 TLS pin mismatch、device token mismatch 处理）；`build.rs` 从 TS 源 `packages/gateway-protocol/src/version.ts` 生成协议版本常量（TS/Rust 协议版本**单源**）。
- `crates/openclaw-node-host`：`description = "Reusable Rust OpenClaw node runtime and headless host"`，`unsafe_code = "forbid"`、clippy pedantic，ed25519/hmac/zeroize；产出一个 `openclaw-node` 无头二进制（`--config <path>`，JSON 单行事件输出）与 sidecar 协议（`SIDECAR_PROTOCOL.md`）——给原生 companion app / 嵌入式 host 实现 "role: node"（apps/ios、apps/android 的官方 App 就以 node 角色连 Gateway：chat、voice、approvals、screen、设备自动化）。
- `apps/`：macOS（Swift/SwiftPM）、iOS（App Store 官方）、Android+wear（Google Play 官方）、linux（Tauri v2 桌面壳）、swabble（macOS Speech.framework 唤醒词 daemon，默认唤醒词 `clawd`、alias `claude`）、shared（Swift 共享组件 + MLX 本地 TTS）。

### 7.3 许可证与治理

- `LICENSE`：MIT，`Copyright (c) 2026 OpenClaw Foundation`；`docs/start/why-openclaw.md`："The whole OpenClaw product is MIT-licensed, with no enterprise edition under a different license."；`docs/help/faq/what-is-openclaw.md`："stewarded by the OpenClaw Foundation, an independent 501(c)(3). OpenAI is one of several donors... Donors do not own, control, or direct the project."（无付费层、无 hosted 服务、无 token；默认仅每日版本检查）。
- `SECURITY.md`：明确的信任模型声明——"OpenClaw is local-first agent infrastructure for trusted operators; it is not designed as a shared multi-tenant boundary between adversarial users on one gateway"；"Anyone who can operate an agent can make it do anything that agent can do. Session ownership ... are usability features, not security boundaries."（共享 agent 非安全边界，要隔离就分 agent/分 host）；prompt-injection-only、scanner-only、dependency-only 报告通常不收；维护者含 NVIDIA/Tencent 的工程师；私密 GHSA 披露流程；无付费 bounty。
- 治理叙事在 `docs/start/why-openclaw.md`（#governance 节）与 `CONTRIBUTING.md`（maintainers 列于 openclaw.org/people；"Being a maintainer is a responsibility, not an honorary title"）。

---

## 8. 供应链安全专题（OpenClaw 差异点）

### 8.1 minimumReleaseAge：7 天依赖冷却，默认从严

`pnpm-workspace.yaml:9-10`：
```yaml
minimumReleaseAge: 10080        # 分钟 = 7 天
minimumReleaseAgeStrict: true
```
任何依赖的新 release 必须"冷"满 7 天才允许进入安装图——把"新版本投毒窗口"直接挡在门外（npm 生态近两年多起 npm 包新版本即发即投毒事件的工程化对策）。对 Agent 产品尤其关键：依赖即工具链，工具链即 Agent 的手。

### 8.2 豁免清单 = 一份带责任人与过期时间的"评审例外台账"

`minimumReleaseAgeExclude` 每条豁免都附注释：**评审依据（PR 号/事件）+ 责任人 + remove-after 时间戳**（`pnpm-workspace.yaml:12-102`）：

```yaml
# Trusted Codex runtimes are outside the dependency cooldown.
minimumReleaseAgeExclude:
  # Approved LanceDB compromised-CA remediation; remove after 2026-11-05T09:36:54.156Z.
  - "@lancedb/lancedb@0.41.0"
  - "@lancedb/lancedb-darwin-arm64@0.41.0"
  ...（8 个平台包）
  - "@openai/codex"
  - "@openai/codex-*"
  # Reviewed libterminal 0.3.11 release; remove after 2026-11-08T02:20:47.166Z.
  - "@openclaw/libterminal@0.3.11"
  # Claude ACP adapter bump for CVE-2026-111497 (brotli fix in SDK 0.3.309 binaries); remove after 2026-10-30T20:00:57.929Z.
  - "@anthropic-ai/claude-agent-sdk@0.3.309"
  ...
```

- **LanceDB compromised-CA 事件**：`@lancedb/lancedb@0.41.0` 被标注为 "compromised-CA remediation"——LanceDB 上游发布链的 CA 出过问题，0.41.0 是修复版；为让修复立即落地而豁免冷却，并给出 2026-11-05 的移除期限。旁证：`CHANGELOG/2026.10.10.md:3055` "Bun's certificate fix is partial, so use patched Node when current rejection of compromised certificate keys is required."（2026 秋证书密钥拒绝事件的一手痕迹）。
- **@openai/codex 豁免逻辑**：codex 扩展的发布元数据要求跟最新（`extensions/codex/package.json` `"requireLatestDependencies": ["@openai/codex"]` 且精确 pin `"@openai/codex": "0.164.0"` + 六平台二进制包）——Codex 运行时迭代快，等 7 天会持续落后，因此把 OpenAI 的 codex 发布链列为"受信运行时"整体豁免（`@openai/codex` + `@openai/codex-*` 通配）。这是一次显式的信任分级决策：OpenAI 发布流程被信任到可豁免冷却，而其余 npm 生态不可。
- 其余豁免同构：pnpm 安全升级（#179992）、acpx #897、tokenjuice #189997、fs-safe/proc-safe 原生包逐版评审、GHSA-68fv-2mgg-jv7q（source-map-js DoS）钉版等。

### 8.3 纵深：不只冷却

- `pnpm-workspace.yaml:110` `blockExoticSubdeps: true`（阻断异构协议子依赖）；`allowBuilds` 逐包白名单原生构建脚本（`onnxruntime-node: false`、`koffi: false` 显式拒绝；`tree-sitter-bash: false`）；17 个 `patches/*` 本地补丁（baileys、matrix-js-sdk、playwright-core、tar、chrome-devtools-mcp 等）；`overrides` 大量安全钉版（tar 7.5.22、qs 6.16.1、tough-cookie 6.0.2…）。
- pre-commit 即审计：`git-hooks/pre-commit` → `scripts/pre-commit/guard-staged-content.mjs`，含 `pnpm-audit-prod.mjs`（零安装直查 npm bulk advisories，`MIN_SEVERITY = "high"`，用 pnpm lock 解析生产依赖）。
- `security/opengrep/`：仓库自带 OpenGrep（Semgrep 系）精确规则包——**把历史 GHSA 转成防回归的静态规则**：`rules/ghsa-82g8-464f-2mv7/`（skill env 注入宿主）、`ghsa-fv94-qvg8-xqpw`（ssh 沙箱上传）、`ghsa-j425-whc4-4jgc`（host env 覆盖）、`ghsa-qxgf-hmcj-3xw3`（镜像下载）、`openclaw-policy`；每条规则带 `.ts/.js` fixture（`// ruleid:` / `// ok:` 标注）与 provenance 元数据校验（`check-rule-metadata.mjs`）。README："Maintainers investigate advisories and generate candidate rules outside the public repo ... durable artifacts needed to block regressions in PRs."
- 安装期策略：`src/security/install-policy.ts`——技能/插件安装可要求外部 policy 命令裁决，解释器白名单（`POLICY_INTERPRETER_NAMES`：bash/bun/deno/env/fish/node/perl/powershell/pwsh/python/ruby/sh/zsh）、policy 脚本路径的**祖先目录逐级权限/属主校验**（group/world-writable 且非 sticky 拒绝；属主须 root 或当前 uid；Windows ACL 不可验证即拒绝）；安装来源带 `authority: "openclaw" | "official" | "third-party" | "unknown" | "user"` 与 `mutable/network` 标记。插件目录另有信任审计（`src/security/audit-plugins-trust.ts`）。
- `scripts/check-install-dependency-ownership.mjs`：拒绝借非物理目录（symlink 借用安装）reconcile 依赖（"Refusing to reconcile dependencies through ... it is not a physical directory."）。
- 运行时侧延续同一思路：`node-runtime-recovery.mjs` 恢复路径拒 cwd 内 node 二进制与 symlink 中间层（§1.4）；MCP argv 展示脱敏 + 回写校验（§3.3）。

**小结**：OpenClaw 把供应链防御做成了"默认冷却 + 显式信任分级 + 例外台账化 + 历史漏洞规则化"，且证据（评审注释、PR 号、remove-after 期限）直接沉淀在版本库里——在同类 Agent 工具中是目前看到的最完整的一套。

---

## 9. 代码规模量级（附件）

| 区域 | 数量 |
|---|---|
| `packages/` | 24 个核心包（agent-core、ai、gateway-client、gateway-protocol、llm-core、memory-host-sdk、plugin-sdk、plugin-package-contract、sdk、worker-runtime、tool-call-repair、model-catalog-core、net-policy、retry、terminal-core、markdown-core、media-* 等） |
| `extensions/` | 182 个插件（消息渠道 16+：telegram/whatsapp/slack/discord/signal/imessage/sms/x(Twitter)/matrix/msteams/googlechat/line/irc/nostr/feishu/zalo…；模型 provider 数十个：anthropic/openai/google/deepseek/qwen/moonshot/kimi-coding/groq/together/openrouter/ollama/vllm/lmstudio/azure-bedrock/vertex…；还有 policy、mcp-relay、mcp-events、memory-*、diagnostics-*、a2a、browser、canvas、session-share、visitor-access 等） |
| `skills/` + `custodian-skills/` | 50 + 4 |
| TS 文件（不含 node_modules，含测试） | src ≈28.7k、extensions ≈11.1k、ui ≈5.2k、packages ≈1.4k，合计 ≈46.5k |
| 根目录 `.mjs` shim | 20 个（node-sqlite、node-runtime-*、gateway-*、cli-*、openclaw.mjs 等启动/自举层，发布包内随 dist 分发） |
| crates/ | 2 个 Rust crate（gateway-client、node-host，含 1 个二进制） |
| 测试文化 | 几乎每个源文件配同名 `.test.ts`（vitest）；`src/state` 单目录 799 文件、`src/agents` 2226 文件 |

其他可引用的定位事实：默认端口 18789；`openclaw onboard --install-daemon` 一键起 daemon；`taxonomy.yaml` + `docs/maturity/` 是插件成熟度分级体系。
