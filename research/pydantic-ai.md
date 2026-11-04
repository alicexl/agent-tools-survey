# Pydantic-AI 调研卡

> 对象：pydantic/pydantic-ai（MIT，v2.65.0，GitHub stars 21,007，2026-11-03 当日活跃——版本/star 数为委托方已核实数据）
> 调研日：2026-11-03。文档基线：ai.pydantic.dev 当前线上文档（部分页面 canonical 已迁至 pydantic.dev/docs/ai/，两域同源）。所有论断附来源；查证不到的明确标注。

## 0 身份与分层归属

**一句话定位**：Pydantic 团队出品的"类型优先"Python Agent 开发框架——"把 FastAPI 的开发体验带进 GenAI 应用与 Agent 开发"，工具参数与结构化输出全部经 Pydantic 校验，把整类错误从运行期前移到编码期。

**归层：开发库/SDK 层**。pip/uv 安装的纯 Python 库（Python 3.10+），进程内嵌、无强制服务端、无强制云依赖；不是平台、不是 SaaS、不托管运行时。佐证：官方首页自我定义为 "Python agent framework … to build production grade applications with Generative AI"，安装即 `pip install pydantic-ai`（https://ai.pydantic.dev/ ；https://ai.pydantic.dev/install/）。

生态分三层（选型时须区分）：
1. **核心库 `pydantic-ai` / `pydantic-ai-slim`**（MIT，本卡主体）——Agent 循环、工具、结构化输出、MCP、OTel、UI 适配器；
2. **`pydantic-ai-harness`**（独立包，0.x 版本策略）——官方"能力（capability）与整装 harness 库"：Memory、StepPersistence、SubAgents、FileSystem/Shell、Coder/Researcher 等 30+ 能力，与核心库分包以便快速迭代（https://pydantic.dev/docs/ai/harness/）；
3. **Pydantic Logfire**（商业观测平台，有免费层、企业版可自托管）——完全可选集成。

## 1 七维度逐项

### 1.1 状态（run 状态 / persistence / 进程内 vs 服务端）

**结论**：Agent 对象设计为全局无状态；每次 `run()` 产生独立 run（`run_id`），跨 run 状态由 `Conversation` 对象承载（消息 + 会话累计 usage + `conversation_id` + 挂起的 deferred tool 请求）。持久化四条路径按需选级，框架刻意对数据库"无主见"（schema 留给用户）。

- **自存（核心库）**：每次 run 交还 `result.conversation`，用户用 `ConversationTypeAdapter.dump_json()` 存进自己 DB 的 jsonb 列（按 `conversation_id` 键控），下次 `agent.run(prompt, conversation=...)` 续跑；会话级 `UsageLimits` 预算跨 run 累计。官方原话："Pydantic AI is deliberately unopinionated about your database."（https://ai.pydantic.dev/persistence/）
- **ConversationStore（核心库）**：消息级存储抽象——每条消息只存一次、增量追加（`new_message_index`）、`revision` 乐观并发（冲突抛 `ConversationConflict`）、`branch`/`fork` 分叉、`scope=` 多租户隔离（他人 scope 视为不存在）。内置 `InMemoryConversationStore`（测试用），生产后端只需实现 5 个存储原语即可接自库（同上）。
- **StepPersistence（Harness 包）**：以 capability 形式包装上述模式，在每次已结算的工具调用边界打快照，另记 append-only 事件日志与 tool-effect ledger（记录每个工具调用是 started/returned/raised，崩溃后可判断"未完成的调用外部效果未知"）；后端含 in-memory / file / SQLite / PostgreSQL / MongoDB，存储层是可自实现的 protocol；支持 `continue_run`/`fork_run`。注意其快照只含消息，不含 usage 与挂起 deferred 请求，需要时应同时存 `Conversation`（https://ai.pydantic.dev/persistence/ ；https://pydantic.dev/docs/ai/harness/）
- **Run 级容错 = Durable execution（核心能力）**：run 在工具调用中途进程死亡可重放已完成步骤、重试在飞步骤——接 Temporal / DBOS / Prefect 引擎（Restate/Kitaru/Airflow 为集成项）（https://ai.pydantic.dev/persistence/ ；https://pydantic.dev/docs/ai/harness/）
- **注意（当前文档明确）**：没有"回退到半成品 run 的第 4 步重放"这种步内快照——步内容错属 durable execution（persistence 页 "What isn't here" 节）。
- **进程内 vs 服务端**：库本体进程内运行；对外的"服务端形态"是自带适配层——Vercel AI / AG-UI 前端适配器、web chat UI、CLI（`clai`）、ACP（编辑器接入），均由用户自托管，无官方托管 daemon（https://pydantic.dev/docs/ai/harness/ 末节 "the agent plugs into any interface"；https://ai.pydantic.dev/persistence/ ）。
- 运行控制：`RunUsage`（含 cost/tool_calls 计数）与 `UsageLimits(request_limit / total_tokens_limit / cost_limit / tool_calls_limit)` 防失控（https://ai.pydantic.dev/multi-agent-applications/ ）。

### 1.2 记忆（短期 / 长期 / 检索）

**结论**：短期记忆=消息历史本体，框架明说"没有独立记忆系统"；跨会话长期记忆是 Harness 包的 `Memory` capability；检索记忆 = `ConversationSearch`（BM25），无内置向量 RAG。

- **短期（message history）**：`message_history=` 参数回传历史续跑；`all_messages()/new_messages()` 取消息，`ModelMessagesTypeAdapter` 做 JSON 序列化存取；消息与模型无关（可跨 agent/跨模型复用）。`history_processors=[...]` 在每次模型请求前改写历史（隐私过滤、只留近 N 条、用便宜模型摘要旧消息；可带 `RunContext` 按 usage 动态裁剪；注意 tool call/return 必须成对）（https://ai.pydantic.dev/message-history/ ）。官方定位："an agent's memory of the conversation it is having *is* its message history. There is no separate memory system to add for that."（https://ai.pydantic.dev/persistence/ ）
- **长期（Memory capability，Harness 包）**：给 agent 一本 Markdown 笔记本，agent 用自带工具写/读/搜；按 **namespace**（通常从 deps 解析出的用户/租户 ID）而非 conversation_id 键控——这正是它能跨会话存活的原因；每次请求注入有界摘录而非全本；store 支持 文件目录 / SQLite / PostgreSQL（persistence 页），Harness 文档另列 in-memory/file/Postgres。用法示例 `Memory(FileStore('.agent-memory'))`。另有 provider 侧 `MemoryTool`（Anthropic 的记忆工具契约，存储仍由你供）（https://ai.pydantic.dev/persistence/ ；https://pydantic.dev/docs/ai/harness/ ）
- **检索**：`ConversationSearch`（Harness）——对 StepPersistence 已写下的历史做排名检索，给模型一个按需拉回早前轮次（含被 compaction 丢掉的轮次）的工具，与 StepPersistence 共用同一 store、零额外写路径（https://ai.pydantic.dev/persistence/ ；https://pydantic.dev/docs/ai/harness/ ）。
- **无内置向量检索/RAG**：官方明确 "Pydantic AI tools are more general-purpose"，向量检索仅是展望（issue #58）；embeddings 另有独立 API 模块 `pydantic_ai.embeddings`（https://ai.pydantic.dev/tools/ ；https://ai.pydantic.dev/llms.txt ）。

### 1.3 工具调用（注册 / 结构化输出 / 校验重试 / MCP）

**注册族**：`@agent.tool`（带 `RunContext` 依赖注入，默认推荐）/ `@agent.tool_plain`（无 ctx）/ `Agent(tools=[函数 | Tool(...)])`（`takes_ctx`、改名、自定义 `prepare`）/ `FunctionToolset.tool` / `Tool.from_schema`（无类型信息的函数手喂 JSON schema，此时不校验参数）。工具 schema 从函数签名生成，docstring 经 griffe 提取参数描述（google/numpy/sphinx 风格，可 `require_parameter_descriptions=True` 强制）。动态工具：per-tool `prepare` 与 agent 级 `prepare_tools`（按 deps/model 过滤或改写工具表）；并行工具调用默认 `asyncio.create_task` 并发（可 `sequential`）；`tool_timeout`（agent 级/per-tool，超时计入重试预算）；`ToolReturn` 把 return_value / 给模型的多模态 content / 不给模型的 metadata 三分离（https://ai.pydantic.dev/tools/ ；https://ai.pydantic.dev/tools-advanced/ ）。

**结构化输出**：`Agent(output_type=...)`（**当前 v2 API 名为 `output_type`；模板所写 `result_type` 为历史旧名，更名沿革见 Upgrade Guide**）。支持标量/list/dict/TypedDict/dataclass/Pydantic 模型/union/多类型列表（union 每个成员注册为独立 output tool）。**三种输出模式**由标记类切换：`ToolOutput`（默认，走 tool calling，兼容性最好）、`NativeOutput`（provider 原生 JSON Schema/Structured Outputs，注意 Gemini 不能同时用工具）、`PromptedOutput`（提示注入 schema，兜底弱模型）。另有 `TextOutput`（函数收纯文本）、output functions（模型被迫调用、调用即结束 run、结果不回传模型，天然用于校验加工与 agent 交接，可 `ModelRetry`）、`StructuredDict`（外部/动态 JSON schema，框架不校验）、`BinaryImage` 图像输出、`@agent.output_validator` 异步输出校验器（校验失败 raise `ModelRetry` 令模型重试）、`validation_context` 传 Pydantic 校验上下文（https://ai.pydantic.dev/output/ ）。

**类型校验与重试（核心卖点）**：工具参数与结构化输出均由 Pydantic（本尊）校验。校验失败自动生成 `RetryPromptPart`（含错误细节）回传 LLM 令其改参重试；工具内可 `raise ModelRetry('...')` 主动要求重试；两者共享 Tool/Agent 级 `retries` 预算，超限抛 `UnexpectedModelBehavior`（https://ai.pydantic.dev/tools-advanced/ ；首页注 6："errors are passed back to the LLM so it can retry"）。

**MCP**：
- 客户端（消费 MCP server 工具）：三种传输 `MCPServerStreamableHTTP` / `MCPServerSSE`（官方标注已弃用）/ `MCPServerStdio`（子进程），每个 server 实例即一个 toolset，`Agent(toolsets=[...])` 挂载；`load_mcp_servers('mcp.json')` 从 JSON 配置批量加载，支持 `${VAR}` / `${VAR:-default}` 环境变量展开（密钥不入配置文件）；`tool_prefix` 防多 server 重名；`process_tool_call` 钩子注入请求元数据；可发现/读取 MCP resources；支持 MCP sampling（`set_mcp_sampling_model()`，server 经 client 回调调 LLM）与 elicitation（server 向用户结构化追问）；HTTP 类 server 可传 `http_client=httpx.AsyncClient` 定制 TLS/mTLS/企业 CA/代理。安装：`pydantic-ai-slim[mcp]`（https://ai.pydantic.dev/mcp/client/ ；https://ai.pydantic.dev/mcp/overview/ ）
- FastMCP 客户端：`FastMCPToolset`（`pydantic-ai-slim[fastmcp]`）基于 FastMCP Client 连任意 MCP server（无论对方是否 FastMCP 所写），支持从 FastMCP Server 实例/Client/Transport/HTTP URL/脚本/JSON 配置构造；同代码库内 server 可免网络往返直接挂载；暂不支持 elicitation 与 sampling（https://ai.pydantic.dev/mcp/fastmcp-client/ ）
- 服务端（把 agent 暴露为 MCP server）：官方示例即用 MCP Python SDK 的 `from mcp.server.fastmcp import FastMCP` 把 Pydantic AI agent 包成 MCP 工具；`MCPSamplingModel` 让 server 内 agent 经 MCP sampling 回调客户端模型（https://ai.pydantic.dev/mcp/server/ ）

**FastMCP 与 Pydantic 生态关系（模板要求写清，经查证修正）**：**"FastMCP 已捐给 Pydantic 生态"的说法不准确**。查证结果：
1. FastMCP（gofastmcp.com，仓库 github.com/PrefectHQ/fastmcp，作者 Jeremiah Lowin）**仍是独立项目**（2025-10 的 issue 仍挂在 PrefectHQ org 下）；
2. 实际发生的是 **FastMCPToolset 集成被贡献进 pydantic-ai**：PrefectHQ/fastmcp#1370（2025-08，"Contribute FastMCP Toolset to Pydantic AI"）跟踪，PR #2784 合并，#2246（2025-10）跟进文档；pydantic-ai 以可选 extra `[fastmcp]` 引入 fastmcp 包；
3. pydantic-ai 2.29.0 起兼容 FastMCP 3/4 双主版本（社区迁移文佐证）；
4. 另注意**同名异物**：文档 server 示例里的 `mcp.server.fastmcp.FastMCP` 是 MCP 官方 Python SDK 内置的轻量实现，不是 gofastmcp.com 的 FastMCP。
（来源：https://github.com/PrefectHQ/fastmcp/issues/1370 ；https://github.com/PrefectHQ/fastmcp/issues/2246 ；https://ai.pydantic.dev/mcp/fastmcp-client/ ；https://gofastmcp.com/integrations/pydantic-ai ）

### 1.4 扩展机制（multi-agent / graphs / 无插件系统的哲学）

官方把多 agent 复杂度分**五级**（https://ai.pydantic.dev/multi-agent-applications/ ）：
1. 单 agent；
2. **Agent delegation（agent as tool）**：在父 agent 工具内 `await child_agent.run(..., deps=ctx.deps, usage=ctx.usage)`（usage 透传父 run 计数；deps 应为父 deps 子集）；或用 Harness 的 `SubAgents` capability——交一列命名 agent，自动暴露单个 `delegate_task(agent_name, task)` 工具、转发 deps、透传 usage limits、把可用子 agent 列表放进可缓存前缀的静态指令；
3. **Programmatic hand-off**：应用代码顺序编排多个 agent，`message_history` 在 agent 间传递；
4. **pydantic-graph**：类型注解定义节点/边的异步图与状态机库（`BaseNode.run()` 返回类型即出边，运行时校验；`GraphRunContext` 状态+DI；状态持久化 `SimpleStatePersistence`/`FullStatePersistence`/`FileStatePersistence`，生产自继承 `BaseStatePersistence` 接 Postgres 等；`iter_from_persistence` 支持跨进程逐节点推进与 HITL 中断恢复；mermaid 图生成）。**独立无 `pydantic-ai` 依赖**，官方态度克制："Don't use a nail gun unless you need a nail gun"（https://ai.pydantic.dev/graph/ ）；
5. **Deep Agents**（Harness 组合件）：Planning / FileSystem / Shell / SubAgents / 沙箱代码执行（DockerSandbox、CodeMode）/ Compaction / `Coder`、`Researcher` 整装 harness。

**官方扩展点 = capability 体系，而非插件系统**：一切扩展是 `capabilities=[...]` 列表里的 Python 对象（核心自带 Thinking/WebSearch/MCP/Instrumentation/Hooks 等 + Harness 30 余个 + 第三方 `pydantic-ai-<name>` 命名规范的社区包），Hooks 提供生命周期钩子，`FunctionModel`/`TestModel`/`WrapperModel`/`FallbackModel` 扩展模型层。core 与 Harness 的分包边界官方自述为 "mechanical, not a maturity tier"——需要模型/框架支持的能力进 core，其余进 Harness 快速迭代（0.x 版本策略）（https://pydantic.dev/docs/ai/harness/ ）。**设计哲学**：不做中央插件注册/市场，扩展靠 Python 原生组合（工具、toolset、capability、deps 依赖注入）完成。

### 1.5 HITL（人审）

**结论**：HITL 由 Deferred Tools 机制一等公民化——工具审批（approval）与外部执行（external execution）两类，支持进程内 inline 解析与跨进程 stop-the-world 两种流程，挂起状态随 Conversation 持久化。

- **声明审批**：`@agent.tool_plain(requires_approval=True)` / `Tool(requires_approval=...)` / `FunctionToolset.tool`（静态）；按参数/上下文条件审批则在工具体内 `raise ApprovalRequired(metadata=...)`（`ctx.tool_call_approved` 判断已批）；`args_validator` 阶段可先拒非法参数再送审；对 toolset（含 MCP server 工具）整体加审用 `ApprovalRequiredToolset`（https://ai.pydantic.dev/deferred-tools/ ）
- **外部执行**：工具体 `raise CallDeferred`（或 `ExternalToolset` 纯外挂工具），run 以待决调用结束，结果稍后回填——适配前端工具/后台慢任务。
- **解析流程 A（inline）**：注册 `HandleDeferredToolCalls` capability，handler 收 `DeferredToolRequests` 返回 `DeferredToolResults`，run 单次调用内继续（`build_results(approve_all=...)` 便捷构造）；实时会话（realtime）场景必须走 inline。
- **解析流程 B（stop-the-world）**：`DeferredToolRequests` 进入 `output_type`，run 结束返回 `approvals` 列表（工具名+已校验参数+call_id）；人工收集决定后构造 `DeferredToolResults`（每 call_id 映射 `bool` / `ToolApproved(override_args=...)` / `ToolDenied(message=...)`；外部调用结果可映射值/`ModelRetry`/`ToolFailed`），连同原 history/`conversation` 开**新 run**（勿复用旧 run_id，经 `conversation_id` 关联）。
- **暂停即持久化**：挂起请求存于 `Conversation.deferred_tool_requests`（messages 无法重建这些信息），存会话即存暂停态（https://ai.pydantic.dev/persistence/ ）。
- **流式可见性**：`DeferredToolRequestsEvent` / `DeferredToolResultsEvent` 事件让前端在 handler 等待期间同步提示。
- **官方安全警告（金融场景必读）**："Approval is not an authorization boundary against an untrusted client"——UI 适配器收到的审批决定由客户端随请求提交，能打到端点的客户端可自批自发；HITL 审批防的是**模型**未经签核行动，端点认证与敏感操作的独立鉴权必须在工具函数内自理（https://ai.pydantic.dev/deferred-tools/ ）。
- 辅助：pydantic-graph 的中断/恢复做流程级 HITL；Harness `Ask User` capability 让模型在 run 中向用户发多选题。

### 1.6 可观测性（Logfire / OTel / InstrumentedModel）

**结论**：可观测性**一等公民且协议开放**——原生 OpenTelemetry（遵循 GenAI Semantic Conventions），Logfire 只是最顺手的查看器，任何 OTLP 后端皆可，且全套合规脱敏开关内置。

- **OTel 本体**：每次 run/model request/tool call 自动产 span；指标 `gen_ai.client.token.usage`、`operation.cost`（USD 估算）、`gen_ai.client.operation.time_to_first_chunk`；agent run span 用 `gen_ai.aggregated_usage.*` 避免父子 span 重复计数；遵循 GenAI semantic conventions v1.37.0，`InstrumentationSettings(version=...)` 控格式（默认 v5，v2–v4 已弃用兼容，v6 opt-in）；另有 prompt-cache 健康属性（hit_ratio/collapsed/collapse_reason，unexpected 塌陷才发告警事件）（https://ai.pydantic.dev/logfire/ ）
- **开启方式**：`Agent.instrument_all(InstrumentationSettings(...))`（用全局 TracerProvider）或单 agent `instrument=True` 或 `Instrumentation` capability；实现位于 `pydantic_ai.models.instrumented`（InstrumentedModel/InstrumentationSettings）（同上）。
- **Logfire 集成**：`logfire.configure()` + `logfire.instrument_pydantic_ai()` 两行接入；官方定性 "built-in (but completely optional)"——未装/未配则几乎零开销、零外发；Logfire 是商业产品（慷慨永久免费层，企业版可自托管），Logfire SDK 本身基于 OTel 可改发任意 OTLP 端点（`send_to_logfire=False` + `OTEL_EXPORTER_OTLP_ENDPOINT`）（同上）。
- **无 Logfire 的纯 OTel**：装 opentelemetry-sdk + exporter-otlp，标准 `OTLPSpanExporter` + `Agent.instrument_all()` 即可，官方附 otel-tui 演示（同上）。
- **脱敏开关（合规）**：`include_content=False`（提示/补全/工具参数与结果全不入遥测）、`include_binary_content=False`、`include_model_request_parameters=False`（大 schema 不入 span）；`logfire.instrument_httpx(capture_all=True)` 可看模型商原始 HTTP 请求（注意与 include_content 的边界）（同上）。

### 1.7 部署与许可证

- **MIT**（仓库 LICENSE；委托方核实），**纯库自托管**：pip/uv 安装即用，Python 3.10+；`pydantic-ai` 全家桶或 `pydantic-ai-slim[按 provider 裁剪]`（26 个 extras 全部可选：logfire/evals/openai/anthropic/google/mcp/fastmcp/a2a/ui/ag-ui/dbos/prefect 等）；CLI、web chat UI、UI 适配器均自托管，无官方云依赖（https://ai.pydantic.dev/install/ ）。
- **库层不管执行隔离（无沙箱）**：工具就是进程内 Python 函数，核心库不提供也不强制沙箱。隔离选项在 Harness 层：`Shell`（allowlist/denylist/超时/凭据剥离/命令可跨 run 存活）、`Modal Sandbox`（云沙箱）、`CodeMode`（Monty 沙箱内跑模型生成的脚本）、multi-agent 文档另链接 `DockerSandbox` 后端；或走 MCP 生态（官方 `pydantic/mcp-run-python` 在沙箱执行任意 Python）（https://pydantic.dev/docs/ai/harness/ ；https://ai.pydantic.dev/multi-agent-applications/ ；https://ai.pydantic.dev/mcp/overview/ ）。
- **版本策略**：核心库有严格版本策略页（Harness 文档对比自述 core "stricter version policy"，Harness 0.x 允许 minor 破坏+弃用警告）；v1→v2 有 Upgrade Guide（如 `result_type`→`output_type`、tool result 排序改为按模型发出顺序——deferred-tools 页 "intentional behavior change in v2"）（https://ai.pydantic.dev/version-policy/ ；https://ai.pydantic.dev/changelog/ ）。
- 伴生可观测平台 Logfire 为商业产品（免费层+企业自托管），不入 MIT 库本体（https://ai.pydantic.dev/logfire/ ）。

## 2 国内可用性

**结论：国产模型接入是一等公民级别——DeepSeek、通义（DashScope）、月之暗面有官方 Provider 类与文档小节，智谱走兼容端点两行配置；本地推理有官方 Ollama 小节；运行时零境外依赖，Logfire 完全可替换。**

- **DeepSeek**：官方 `DeepSeekProvider`，`Agent('deepseek:deepseek-chat')`，env `DEEPSEEK_API_KEY`；支持注入自定义 httpx 客户端（https://ai.pydantic.dev/models/openai/ "DeepSeek" 节）
- **通义千问**：官方 `AlibabaProvider`（阿里云百炼/DashScope 兼容模式），`Agent('alibaba:qwen-max')`，env `ALIBABA_API_KEY`/`DASHSCOPE_API_KEY`；**官方文档明示中国区端点覆盖写法** `base_url='https://dashscope.aliyuncs.com/compatible-mode/v1'`（默认是国际端点 dashscope-intl）（同上 "Alibaba Cloud Model Studio" 节）
- **智谱 GLM**：**未找到内置 provider 类**（核查路径：models/openai 全页 provider 清单 + llms.txt Models 目录均无 zhipu；尝试 `pydantic.dev/docs/ai/models/` 无 zhipu 页）。接入方式（框架能力已核实，端点值以智谱文档为准）：① OpenAI 兼容——`OpenAIProvider(base_url='https://open.bigmodel.cn/api/paas/v4', api_key=...)`（openai 页明示"任意 OpenAI 兼容 API"用 base_url+api_key）；② Anthropic 兼容——`AnthropicProvider(anthropic_client=AsyncAnthropic(base_url=...))`（anthropic 页支持注入任意 `AsyncAnthropic` 客户端，Bedrock/Vertex/Foundry 同法）。本工作区已有智谱 Anthropic 兼容网关实战配置（bigmodel.cn），佐证路径②可行。
- **月之暗面 Kimi**：官方 `MoonshotAIProvider`，`Agent('moonshotai:kimi-k2-0711-preview')`，env `MOONSHOTAI_API_KEY`（https://ai.pydantic.dev/models/openai/ "MoonshotAI" 节）
- **本地模型**：`OllamaProvider`（本地 `http://localhost:11434/v1`，env `OLLAMA_BASE_URL`/`OLLAMA_API_KEY`；亦支持 Ollama Cloud）；vLLM 等任何 OpenAI 兼容推理服务经 `OpenAIProvider(base_url=...)`；Outlines 本地结构化输出有专属 extras（`outlines-vllm-offline`/`outlines-transformers` 等）（https://ai.pydantic.dev/models/openai/ "Ollama" 节与 "OpenAI-compatible Models" 节；https://ai.pydantic.dev/install/ ）
- **无境外网络依赖**：核心功能零外联——模型端点全自定（含内网 vLLM），不连 Pydantic 任何服务器；Logfire 非必须（不装 logfire 包即无遥测外发；装了也可 `send_to_logfire='if-token-present'` 无 token 不发，或改发自建 OTel collector：`OTEL_EXPORTER_OTLP_ENDPOINT` 指内网，OTLP HTTP 协议可接 Jaeger/Tempo/SkyWalking 等国产可接 OTLP 的后端）（https://ai.pydantic.dev/logfire/ ）。唯一境外资源是文档站与 PyPI。
- **中文资料**：无官方中文文档；社区中文镜像 ai.pydantic.org.cn（内容停在 v1 时代，读时注意时效）、DataCamp 中文教程、知乎/CSDN 实践与部署文若干（搜索结果：https://ai.pydantic.org.cn ；https://www.datacamp.com/zh/tutorial/pydantic-ai-guide ；https://zhuanlan.zhihu.com/p/10703567230 ）。

## 3 企业落地注记（安全/合规视角）

- **依赖面极小是硬卖点**：`pydantic-ai-slim` 按 extra 显式引入每个 provider/功能（26 个 extras 全可选，logfire/evals/mcp/fastmcp 均不默认带入），核心依赖仅 pydantic 系 + pydantic-graph 等；SBOM 审计面小、供应链攻击面可控，镜像/离线环境友好（https://ai.pydantic.dev/install/ ）
- **密钥经 env 的全栈惯例**：`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`DEEPSEEK_API_KEY`/`ALIBABA_API_KEY`/`DASHSCOPE_API_KEY`/`MOONSHOTAI_API_KEY`/`OLLAMA_API_KEY` 等环境变量读取；MCP JSON 配置支持 `${VAR}`/`${VAR:-default}` 展开避免密钥落盘；provider 也接受显式 `api_key=`/`http_client=`，可接企业密钥管理器与出网代理（https://ai.pydantic.dev/models/openai/ ；https://ai.pydantic.dev/mcp/client/ ）
- **HITL 审批不是鉴权边界**（官方明示）：经 UI 适配器服务 agent 时审批决定由客户端提交，必须认证适配器端点并在敏感工具函数内独立鉴权；同理"客户端提交的 message_history"存在信任边界问题（Trust boundary for client-supplied history）。金融场景：审批 UI 放可信侧、交易类工具内做金额/权限二次校验（https://ai.pydantic.dev/deferred-tools/ ）
- **遥测合规开关内置**：`include_content=False` 一开关即让提示词/补全/工具内容不出境（境外 SaaS 观测平台场景），或整体换自建 OTel 后端实现数据驻留（https://ai.pydantic.dev/logfire/ ）
- **数据驻留**：库自托管 + 模型端点自定（可全内网 vLLM/Ollama）+ 持久化 schema 自主（"deliberately unopinionated about your database"）——对话数据落哪家库、带哪个租户列全由企业自定（https://ai.pydantic.dev/persistence/ ）
- **风险点**：① v1→v2 API 有破坏性更名（result_type→output_type 等），引用旧教程/旧中文镜像时注意；② Harness 包 0.x 版本策略（minor 可破坏，有弃用警告）——生产固化版本号；③ FastMCP/mcp 为可选第三方依赖，引入时纳入供应链审查；④ 无内置 RAG/向量检索，检索类需求须自建或用 ConversationSearch（BM25）。

## 4 关键源码文件清单（供深读入口）

说明：本调研环境无法直连 GitHub API（直连与本地代理均被拒），故按**官方 API Reference 的模块路径**给出（模块名即 import 路径，与仓库 `pydantic_ai_slim/pydantic_ai/` 下文件/子包一一对应）；目录浏览入口：https://github.com/pydantic/pydantic-ai/tree/main/pydantic_ai_slim/pydantic_ai （API Reference 索引见 https://ai.pydantic.dev/llms.txt ）。

核心包 `pydantic_ai_slim/pydantic_ai/`：
- `agent`（`pydantic_ai.agent`：Agent/AbstractAgent/run 系列方法/CancellationToken）— https://ai.pydantic.dev/api/agent/
- `run`（`pydantic_ai.run`：AgentRunResult、conversation 属性）— https://ai.pydantic.dev/api/run/
- `conversation` / `conversation_store`（Conversation、ConversationStore/InMemoryConversationStore；模块名出自 persistence 页代码 `from pydantic_ai.conversation_store import ...`）— https://ai.pydantic.dev/persistence/
- `tools`（`pydantic_ai.tools`：Tool、RunContext、DeferredToolRequests/Results、ToolApproved/ToolDenied）— https://ai.pydantic.dev/api/tools/
- `toolsets`（`pydantic_ai.toolsets`：FunctionToolset、组合/前缀/ApprovalRequiredToolset；含 `toolsets/fastmcp`：FastMCPToolset）— https://ai.pydantic.dev/api/toolsets/ ；https://ai.pydantic.dev/mcp/fastmcp-client/
- `output`（`pydantic_ai.output`：ToolOutput/NativeOutput/PromptedOutput/TextOutput/StructuredDict/Choices）— https://ai.pydantic.dev/api/output/
- `messages`（`pydantic_ai.messages`：ModelRequest/ModelResponse/各 Part/事件流类型）— https://ai.pydantic.dev/api/messages/
- `mcp`（`pydantic_ai.mcp`：MCPServerStdio/MCPServerStreamableHTTP/MCPServerSSE/load_mcp_servers）— https://ai.pydantic.dev/api/mcp/ ；https://ai.pydantic.dev/mcp/client/
- `models/`（`base`（Model/ModelProfile 基类）、`openai`（OpenAIChatModel/ResponsesModel）、`anthropic`、`google`、`groq`、`mistral`、`cohere`、`bedrock`、`xai`、`cerebras`、`openrouter`、`huggingface`、`outlines`、`fallback`（FallbackModel）、`function`（FunctionModel）、`test`（TestModel）、`wrapper`、`instrumented`（InstrumentedModel/InstrumentationSettings——OTel 实现）、`mcp_sampling`、`decision`）— https://ai.pydantic.dev/api/models/… 各页
- `providers/`（`pydantic_ai.providers.*`：openai/anthropic/deepseek/alibaba/ollama/moonshotai/grok/azure/github/fireworks/together/nebius/ovhcloud/heroku/litellm/vercel 等——DeepSeek=providers/deepseek.py、通义=providers/alibaba.py 佐证自 models/openai 文档 import 语句）— https://ai.pydantic.dev/api/providers/ ；https://ai.pydantic.dev/models/openai/
- `capabilities/`（`pydantic_ai.capabilities`：HandleDeferredToolCalls/Instrumentation/WebSearch/Thinking/Hooks/AbstractCapability 等——含 `pydantic_ai.capabilities` API 页）— https://pydantic.dev/docs/ai/capabilities/overview/
- `retries` / `exceptions`（ModelRetry/ApprovalRequired/CallDeferred/UnexpectedModelBehavior 等）— https://ai.pydantic.dev/api/retries/ ；https://ai.pydantic.dev/api/exceptions/
- `settings` / `profiles` / `usage` / `result` / `durable_exec` / `embeddings` / `builtin_tools` / `common_tools` / `direct` / `ext` / `concurrency` / `format_prompt` — 见 https://ai.pydantic.dev/llms.txt API 节

伴生包：
- `pydantic_graph/`（独立包：graph/nodes/persistence/mermaid/beta*）— https://ai.pydantic.dev/api/pydantic_graph/graph/
- `fasta2a/`（A2A server 库，同仓）— https://ai.pydantic.dev/api/fasta2a/
- `pydantic-ai-harness`（独立分发包：memory/（FileStore）、step-persistence、subagents、filesystem、shell、coder 等 capabilities）— https://pydantic.dev/docs/ai/harness/

## 5 信息源 URL 列表

官方文档（ai.pydantic.dev，2026-11-03 抓取；部分页 canonical 同步至 pydantic.dev/docs/ai/）：
1. 首页（定位/11 条特性/银行示例/Logfire 段）：https://ai.pydantic.dev/
2. 安装与 slim extras 全表：https://ai.pydantic.dev/install/
3. 持久化总纲（Conversation/ConversationStore/StepPersistence/Memory/ConversationSearch/durable execution）：https://ai.pydantic.dev/persistence/
4. 消息与聊天历史（history_processors）：https://ai.pydantic.dev/message-history/
5. 函数工具：https://ai.pydantic.dev/tools/
6. 高级工具特性（校验/ModelRetry/retries/timeout/并行）：https://ai.pydantic.dev/tools-advanced/
7. 输出（output_type/三模式/output validator）：https://ai.pydantic.dev/output/
8. Deferred Tools / HITL 工具审批（含"审批非授权边界"警告）：https://ai.pydantic.dev/deferred-tools/
9. MCP 总览 / 客户端 / 服务端 / FastMCP 客户端：https://ai.pydantic.dev/mcp/overview/ ; https://ai.pydantic.dev/mcp/client/ ; https://ai.pydantic.dev/mcp/server/ ; https://ai.pydantic.dev/mcp/fastmcp-client/
10. 多 agent 应用（五级模式/SubAgents/UsageLimits）：https://ai.pydantic.dev/multi-agent-applications/
11. pydantic-graph：https://ai.pydantic.dev/graph/
12. Logfire 与 OTel（含脱敏开关/指标/版本化格式）：https://ai.pydantic.dev/logfire/
13. OpenAI 模型页（DeepSeek/Alibaba/Ollama/MoonshotAI/OpenAI 兼容 base_url）：https://ai.pydantic.dev/models/openai/
14. Anthropic 模型页（anthropic_client 注入/prompt caching）：https://ai.pydantic.dev/models/anthropic/
15. Pydantic AI Harness（能力总表/安装/0.x 版本策略）：https://pydantic.dev/docs/ai/harness/
16. 文档全量索引（llms.txt）：https://ai.pydantic.dev/llms.txt
17. 版本策略：https://ai.pydantic.dev/version-policy/ ；升级指南：https://ai.pydantic.dev/changelog/
18. 仓库（MIT/license 徽章/源码目录）：https://github.com/pydantic/pydantic-ai

FastMCP 归属查证：
19. PrefectHQ/fastmcp#1370（Contribute FastMCP Toolset to Pydantic AI）：https://github.com/PrefectHQ/fastmcp/issues/1370
20. PrefectHQ/fastmcp#2246（FastMCPToolset 已并入 pydantic-ai，PR #2784）：https://github.com/PrefectHQ/fastmcp/issues/2246
21. FastMCP 官方 Pydantic AI 集成页：https://gofastmcp.com/integrations/pydantic-ai
22. FastMCP↔Pydantic AI 2.29.0 兼容迁移文（第三方佐证）：https://mcp.csdn.net/6a871f7a10ee7a33f29d428b.html

中文资料（非官方）：
23. 社区中文文档镜像（v1 时代内容）：https://ai.pydantic.org.cn
24. DataCamp 中文教程：https://www.datacamp.com/zh/tutorial/pydantic-ai-guide
25. 知乎工程实践解析：https://zhuanlan.zhihu.com/p/10703567230

未找到项与尝试路径：
- 智谱内置 provider 类：未找到（已查 models/openai 全页 provider 清单、llms.txt Models 目录、providers API 页索引）；
- "FastMCP 整体捐赠给 Pydantic"的官方公告：未找到（多轮中英文检索仅命中 Toolset 贡献类证据 #19/#20）；
- GitHub 目录文件级清单（api.github.com 直连与本地代理 7897 均连接失败），故 §4 以官方 API Reference 模块路径替代并注明。
