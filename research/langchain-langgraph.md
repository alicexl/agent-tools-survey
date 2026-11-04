# LangChain / LangGraph 调研卡

核实时间窗：2026-11-03。手段：官方 docs（docs.langchain.com，多数页面走 `.md` 原文端点直抓）、GitHub API（源码树/仓库元数据）、raw.githubusercontent.com、PyPI JSON API、WebSearch。
版本锚：langchain-ai/langchain stars 148,940 / langchain-ai/langgraph stars 44,393（langgraph 最新 release 1.2.19，langchain-core 1.7.4，均 MIT，当日有 push，2026-11-03 gh 直连核实）。

---

## 0 身份与分层归属

**一句话定位**：LangChain 与 LangGraph 是 LangChain Inc. 开源（MIT）的 Python/TS **开发库/SDK 层**——LangGraph 是低层、长时运行有状态工作流/Agent 的图编排运行时；LangChain 是其上的 Agent 框架生态（标准模型接口 + `create_agent` harness + 集成包体系）。两者都是 `pip install` 的库而非产品，不含托管服务（托管部分是商业产品 LangSmith）。

**官方分层表述**（来源：docs.langchain.com/oss/python/langchain/overview 与 /oss/python/langgraph/overview，2026-11-03 抓取）：

- LangGraph："LangGraph provides **low-level supporting infrastructure** for *any* long-running, stateful workflow or agent. LangGraph **does not abstract prompts or architecture**"；"built by LangChain Inc, the creators of LangChain, **but can be used without LangChain**"。
- LangChain："LangChain provides `create_agent`: a minimal, highly configurable **agent harness**"；"**LangChain's agents are built on top of LangGraph**. This allows us to take advantage of LangGraph's durable execution, human-in-the-loop support, persistence"。
- 官方选型建议（同页 Tip）："Use LangChain (`create_agent`) for a highly customizable harness … Use **LangGraph, our low-level orchestration framework**, for advanced needs combining deterministic and agentic workflows"，更高层还有 Deep Agents（batteries-included，built on LangChain agents）。
- 归层依据：安装即库（`pip install -U langgraph` / `langchain`），无守护进程、无 CLI 产品形态；商业运行时（LangSmith Deployment / Agent Server）为独立产品线。

---

## 1 七维度逐项

### 1.1 状态（checkpoint / persistence / 断点恢复）——结论：**体系完整，是该项目的核心卖点**

- **双持久化抽象**：Checkpointers（thread 内图状态快照=短期记忆）+ Stores（跨线程应用数据=长期记忆），`builder.compile(checkpointer=..., store=...)` 两者可同时挂。来源：https://docs.langchain.com/oss/python/langgraph/persistence
- **checkpointer 家族**（均为独立可安装库，来源：https://docs.langchain.com/oss/python/langgraph/checkpointers ）：
  - `langgraph-checkpoint`（随 langgraph 自带）：接口 `BaseCheckpointSaver` + 内存实现 `InMemorySaver`/`MemorySaver`（重启即失，官方明示仅实验用）；
  - `langgraph-checkpoint-sqlite`：`SqliteSaver`/`AsyncSqliteSaver`（本地开发）；
  - `langgraph-checkpoint-postgres`：`PostgresSaver`/`AsyncPostgresSaver`，官方原文 "used in LangSmith. Ideal for using in production"；
  - 另有 `langgraph-checkpoint-mongodb`、`langchain-azure-cosmosdb`（CosmosDBSaver）。
- **thread 概念**：`thread_id` 是 checkpoint 主键（config `{"configurable": {"thread_id": ...}}` 必填），"effectively your persistent cursor"；PostgresSaver 中 thread_id 列长受限（<255 字符）。
- **checkpoint 结构**：每个 **super-step**（Pregel 式单步，步内节点可并行）边界落一个 `StateSnapshot`（字段：`values`/`next`/`config`/`metadata(step,writes,source)`/`created_at`/`parent_config`/`tasks`）；节点级写入另存 writes 表（**pending writes**：super-step 内某节点失败时，成功节点的写入已持久化，恢复时不重跑）。
- **durability 三档**：`"exit"`（仅退出时持久化，性能最好但中途崩溃不可恢复）/ `"async"`（异步落盘）/ `"sync"`（每步前同步落盘）。来源：checkpointers 页。
- **time travel**：`get_state_history` 全历史、按 `checkpoint_id` replay（该点之前的节点跳过、之后重跑，interrupt 一律重新触发）、`update_state` 改状态（生成新 checkpoint，不改原件，可 `as_node` 控制续跑点）。来源：checkpointers 页 + https://docs.langchain.com/oss/python/langgraph/use-time-travel
- **节点级容错**（多数要求 langgraph>=1.2）：`RetryPolicy`（指数退避+异常过滤，默认排除 ValueError 等编程错误、HTTP 库仅 5xx 重试）、`timeout=`/`TimeoutPolicy(run_timeout, idle_timeout)`（仅异步节点）、`error_handler=`（收 `NodeError`，可 `Command(goto=...)` 走补偿/Saga）、`set_node_defaults()` 全图默认；**graceful shutdown**：`RunControl.request_drain()` → 当前 super-step 完成后存 checkpoint 抛 `GraphDrained`，`invoke(None, config)` 原线程恢复（官方给了 SIGTERM 处理模式）。来源：https://docs.langchain.com/oss/python/langgraph/fault-tolerance
- **静态加密**：`EncryptedSerializer.from_pycryptodome_aes()`（读 `LANGGRAPH_AES_KEY`）可给任意 checkpointer 的落盘状态整体 AES 加密。来源：checkpointers 页。
- **存储优化与自定义**：`DeltaChannel`（beta，langgraph>=1.2，append-heavy 通道只存增量）；自定义后端需实现五方法（put/put_writes/get_tuple/list/delete_thread），官方提供 `langgraph-checkpoint-conformance` 一致性测试套件跑 CI。

### 1.2 记忆（短期 / 长期）——结论：**两级记忆与检索方式均有明确 API**

来源：https://docs.langchain.com/oss/python/langgraph/memory （旧 langchain-ai.github.io/concepts/memory 已 301 至此）

- **短期记忆 = thread 内 state**：由 checkpointer 持久化的消息历史+自定义字段；官方同时给出长对话治理手段（裁剪/摘要等，见 Add and manage memory 指南）。
- **长期记忆 = Store（跨线程）**：JSON 文档按 `namespace`（类似文件夹，常含 user/org ID）+ `key`（文件名）组织；API：`store.put(namespace, key, dict)` / `store.get` / `store.search(namespace, filter={...}, query="...")`——**search 同时支持内容过滤和向量语义检索**（构造 `InMemoryStore(index={"embed": embed_fn, "dims": N})` 注入 embedding；生产用 `PostgresStore`）。来源：memory 页 + https://docs.langchain.com/oss/python/langchain/tools （`runtime.store`）。
- **记忆类型学**（官方按 CoALA 框架给出）：semantic（事实：profile 单文档 vs collection 文档集两种管理策略，各自权衡有明述）/ episodic（经验，常以 few-shot 形式从 store 检索）/ procedural（规则，典型=agent 用 store 重写自己的 system prompt，官方给了伪代码）。
- **写入时机**："in the hot path"（agent 调 save_memories 工具实时写）vs "in the background"（后台任务/cron 写），官方有 memory-agent / memory-service 两个参考模板。
- 检索方式总结：thread 内=state 直读；跨线程=namespace 直取 / filter / 语义 search 三种。

### 1.3 工具调用——结论：**声明式 @tool + pydantic 校验贯穿 + 结构化输出双策略；MCP 已并入主包**

- **`@tool` 装饰器**：`from langchain.tools import tool`；docstring 即工具描述、**类型注解必填**（生成输入 schema）；复杂入参用 `args_schema=PydanticModel` 或裸 JSON Schema；工具名建议 snake_case（部分 provider 拒绝空格/特殊字符）。来源：https://docs.langchain.com/oss/python/langchain/tools
- **ToolRuntime 注入**：工具签名加 `runtime: ToolRuntime`（对 LLM 隐藏）可访问 state（短期记忆）/ context（不可变调用配置，泛型 `ToolRuntime[UserContext]`）/ store（长期记忆）/ stream_writer / execution_info（thread_id、run_id、重试次数 node_attempt）/ server_info；工具可返回 `Command(update=...)` 直接改图状态。工具执行由 langgraph `ToolNode` 承担；错误处理走 middleware（`@wrap_tool_call`）。
- **结构化输出**：agent 层 `create_agent(response_format=...)` 双策略——`ProviderStrategy`（provider 原生 structured output，OpenAI/xAI/Anthropic/Gemini 等）与 `ToolStrategy`（tool calling 实现，任何支持工具调用的模型可用）；直接传 schema 类型时按模型能力自动选择（langchain>=1.1 从 model profile 动态读）；schema 支持 pydantic/dataclass/TypedDict/JSON Schema/Union；校验失败自动把 pydantic 错误回喂模型重试（`handle_errors` 可配）。模型层另有 `with_structured_output`（Models: Structured output 节，同一文档体系）。来源：https://docs.langchain.com/oss/python/langchain/structured-output
- **MCP**：`langchain-mcp-adapters`（PyPI 0.3.2，依赖 langchain-core>=1.3.3 + mcp>=1.24；GitHub 3,645★ MIT）**已停止积极维护**——README 置顶声明 MCP 支持并入主包 `langchain.mcp` 命名空间，安装改 `pip install "langchain[mcp]"`，源码在 monorepo `libs/langchain_v1/langchain/mcp`，官方迁移指南 /oss/python/migrate/langchain-mcp-adapters。功能=把 MCP 工具转成 LangChain 工具 + 多 MCP server 客户端（README: "Convert MCP tools into LangChain tools … connect to multiple MCP servers and load tools"）。来源：https://github.com/langchain-ai/langchain-mcp-adapters （README 实抓 2026-11-03）+ https://docs.langchain.com/oss/python/langchain/mcp

### 1.4 扩展机制——结论：**子图/多智能体是官方一等公民；平台化走 LangSmith Agent Server**

- **subgraph**：图可作节点嵌入父图；每个 subgraph 有独立 checkpoint namespace（`checkpoint_ns="node_name:uuid"`，嵌套用 `|` 连接）；父子状态默认隔离，跨边界共享数据官方建议走 Store 或配置写父 checkpoint。来源：checkpointers 页 checkpoint namespace 节 + https://docs.langchain.com/oss/python/langgraph/use-subgraphs + interrupts 页 "Using with subgraphs called as functions"（子图 interrupt 冒泡到父图节点重跑语义）。
- **multi-agent 官方模式谱**（https://docs.langchain.com/oss/python/langchain/multi-agent ）：Subagents（主 agent 把子 agent 当工具）/ Handoffs（状态驱动交接）/ Skills（按需加载专用提示词）/ Router / Custom workflow（LangGraph 自定义流），并给了各模式 model-call/token 成本对比表；更高层 **Deep Agents**（/oss/python/deepagents/overview）自带 subagents、skills、虚拟文件系统、上下文压缩。
- **官方多智能体库**（GitHub API 实查 2026-11-03）：`langgraph-supervisor-py`（原名 langgraph-supervisor，1,655★ MIT，最近 push 2026-07-15）、`langgraph-swarm-py`（1,588★ MIT，最近 push 2026-10-28）。
- **双 API 面向扩展**：Graph API（StateGraph/节点/边/reducer）与 Functional API（`@task`/`@entrypoint`，同样支持 timeout/retry，见 fault-tolerance 页）；编排内核即 Pregel 运行时（docs 专页 /oss/python/langgraph/pregel，灵感来自 Google Pregel/Apache Beam，见 langgraph overview 页致谢）。
- **LangGraph Platform / Server**：产品线已更名整合进 **LangSmith Deployment / Agent Server**——三种形态：LangSmith Cloud（托管）、control plane（hybrid/self-hosted）、standalone server（纯自托管 docker 部署）；本地开发用 `langgraph-cli` 起 dev server（repo `libs/cli`；docs /oss/python/langgraph/local-server）；Agent Server 自动接管 persistence（"you do not need to implement or configure checkpointers or stores manually"，persistence 页 Info 框）、并暴露 threads/runs/crons/store/MCP/A2A 等 API（docs 导航 Agent Server API 实见）。来源：https://docs.langchain.com/oss/python/langgraph/deploy + 导航树。

### 1.5 HITL——结论：**interrupt()/Command 是当前推荐主路径，机制文档极其详尽（含反模式）**

来源：https://docs.langchain.com/oss/python/langgraph/interrupts （2026-11-03 实抓全文）

- **`interrupt()` 动态断点**：节点内任意位置调用，payload 需 JSON 可序列化；触发即由 checkpointer 存状态并无限期等待；值经 `stream.interrupts`（`stream_events(..., version="v3")`）或 `invoke()` 的 `result["__interrupt__"]` 交给调用方。与静态 breakpoint 的区别官方明示：interrupt 是动态、可条件化的。
- **`Command(resume=...)` 恢复**：同一 thread_id 重入，resume 值成为 `interrupt()` 调用点的返回值；并行多中断时传 `{interrupt_id: resume_value}` map 配对；`Command(resume=...)` 是唯一允许作为 invoke 输入的 Command 形态（update/goto 只能从节点返回）。
- **类型化审批表单**：`interrupt(value, response_schema=PydanticModel)`（要求 langgraph>=1.2.12）——resume 值按 schema 校验（非法输入抛 `pydantic.ValidationError`，可携修正值重试），LangSmith Studio 将其渲染成 typed 输入表单（文档含截图说明）。
- **approval 模式**：approve/reject 用 `Command(goto="proceed"/"cancel")` 路由；**工具内审批**：`@tool` 函数体里直接 `interrupt()`，resume 值可覆盖工具入参（approve + 改写参数再执行），官方给了 send_email 完整示例。
- **静态断点**：`compile(interrupt_before=[...], interrupt_after=[...])` 或运行时同参传入，`invoke(None, config)` 续跑；官方明确"**not recommended for human-in-the-loop workflows. Use the `interrupt` function instead**"（静态断点定位为逐步调试手段）。
- **关键规则（官方 Rules of interrupts）**：恢复时**整个节点从头重跑**（interrupt 前的代码会再执行→副作用须幂等/后置）；不可用裸 try/except 包裹（interrupt 靠异常实现，会被吞）；同节点多 interrupt 按严格索引配对、顺序不可条件改变；避免 `while True`+interrupt 循环（校验重试应改用条件边回环）。interrupt 异常走 `GraphBubbleUp`，**不触发** retry/error_handler。

### 1.6 可观测性——结论：**官方路径=LangSmith（商业 SaaS，一行环境变量开启）；OSS 层有 callback 体系，LangSmith 非必需**

- **LangSmith 集成**：`export LANGSMITH_TRACING=true` + `LANGSMITH_API_KEY` 即全链路 trace（无侵入）；`langsmith.tracing_context(enabled=True, project_name=..., tags=..., metadata=...)` 选择性追踪；trace 脱敏用 `langsmith.anonymizer.create_anonymizer`（正则规则，如 SSN）挂到 `LangChainTracer` callbacks。来源：https://docs.langchain.com/oss/python/langgraph/observability
- **LangSmith Studio**：图结构可视化 + 断点/状态检视 + interrupt 表单（interrupts 页与 studio 均有）；这是调试 LangGraph 图的官方 GUI。
- **OTel**：LangSmith 侧有官方 "Trace with OpenTelemetry" 集成页（docs 导航 Monitor→Tracing setup→Integrations→OpenTelemetry，即 OTel 数据可接入 LangSmith 体系）。**未找到** LangGraph/LangChain Python OSS 层的"原生 OTel exporter"官方文档证据——Python 侧可观测扩展点仍是 langchain-core **callback 体系**（`RunnableConfig.callbacks` / `LangChainTracer`，源码 `libs/langgraph/langgraph/callbacks.py` + `langchain_core.tracers`）。如需开源后端，业界通行做法是 callback/OTel 桥接第三方（如 Langfuse 的 LangChain 集成——本卡未逐项核实，标注为生态选项而非官方能力）。
- 结论性判断：LangSmith 是可选增强而非运行时依赖——不设环境变量时 OSS 功能完整（见 1.7 与 §2）。

### 1.7 部署与许可证——结论：**核心全 MIT 可自托管；商业产品线（LangSmith）边界清晰**

- **许可证**：两仓 MIT（langgraph 仓内 `libs/prebuilt/LICENSE` 等逐库放证，实见源码树）；langgraph 最新 release **1.2.19**、langchain-core **1.7.4**（2026-11-03 `gh` 直连核实）。
- **自托管路径**：① 纯库内嵌（自建 FastAPI/Flask 包 `graph.invoke`，最常见）；② `langgraph-cli` 本地 dev server（`langgraph dev`，配 `langgraph.json`）；③ **Agent Server 自托管**：standalone server（docker 镜像跑在自己机房，不接 LangSmith 控制面也可运行）或 control plane（自托管数据面+LangSmith 控制面的混合）。docs: /langsmith/deploy-standalone-server、/langsmith/deploy-with-control-plane。LangSmith Cloud 为全托管商业选项。
- **依赖供应链**（PyPI JSON + monorepo 实查）：
  - langchain 仓 monorepo 结构：`libs/core`（langchain-core）、`libs/langchain_v1`（langchain 1.x 主包，含 `langchain/mcp`）、`libs/partners/`（18 个官方 partner 包：openai/anthropic/**deepseek/ollama**/huggingface/fireworks/groq/mistralai/openrouter/xai/qdrant/chroma…）、`libs/standard-tests`；
  - langgraph 仓：`libs/langgraph`、`libs/prebuilt`、`libs/checkpoint{,-sqlite,-postgres,-conformance}`、`libs/cli`、`libs/sdk-py/-js`；
  - serde 默认 `JsonPlusSerializer`（ormsgpack+JSON，可选 `pickle_fallback`）；
  - **风险事件两例**（均在 2026 年内）：`langchain-mcp-adapters` 停维护并入主包（§1.3）；`langchain-community` 处于 sunset/收缩期（ChatTongyi 相关 issue #493 被"closed as part of langchain-community sunset"关闭，WebSearch 实证）——选型时应只依赖 core/langchain_v1/partners/langgraph 五条主线，避开 community。

---

## 2 国内可用性

- **DeepSeek（官方 partner 包）**：`pip install langchain-deepseek`，`ChatDeepSeek`（源码 monorepo `libs/partners/deepseek`）；能力矩阵：tool calling ✅ / structured output ✅ / 流式 ✅ / 原生 async ✅（注意 `deepseek-reasoner` 即 R1 不支持工具调用与结构化输出，仅 `deepseek-chat` 即 V3 支持）。来源：https://docs.langchain.com/oss/python/integrations/chat/deepseek
- **通义/Qwen（官方文档已指向第三方专用包 + 阿里云官方指南）**：官方 ChatQwen 文档现指向 PyPI `langchain-qwq` 包（`ChatQwen`/`ChatQwQ`，支持流式/工具调用/结构化输出）；旧 `ChatTongyi`（dashscope SDK）在 langchain-community，随 community 收缩进入维护尾期；另有社区 `langchain-qwen3`（embedding）；阿里云官方文档有专页 "Use Alibaba Cloud Model Studio in LangChain"（help.aliyun.com/en/model-studio/use-bailian-in-langchain）。来源：https://docs.langchain.com/oss/python/integrations/chat/qwen + WebSearch（pypi.org/project/langchain-qwq 等）。
- **智谱 GLM（社区包 + 智谱官方指南）**：`ChatZhipuAI` 在 langchain-community（依赖 `zhipuai` 包，`ZHIPU_API_KEY`）；智谱官方（Z.AI）有 LangChain 接入专页 docs.z.ai/guides/develop/langchain/introduction；MetaGLM（智谱组织）维护 `langchain-glm` 仓。**未找到** langchain-ai 官方 zhipu partner 包（partners 目录 18 包无 zhipu/tongyi，GitHub API 实查）。来源：WebSearch（reference.langchain.com/python/langchain-community/chat-models、docs.z.ai、github.com/MetaGLM/langchain-glm）。
- **本地模型**：`langchain-ollama` 官方 partner 包（`libs/partners/ollama`；官方文档示例直接 `model="ollama:devstral-2"`）；DeepSeek 开源权重官方文档明示可本地跑 Ollama（chat/deepseek 页 Tip）。vLLM：走 OpenAI 兼容端点（`ChatOpenAI(base_url=...)`）——该路径为公知做法，但 vLLM provider 页本次两次抓取失败（webReader 限流），**页面级证据未核实**（尝试路径：/oss/python/integrations/providers/vllm.md）。
- **无境外网络可否运行**：可以。核心链路（langgraph + langchain + langchain-deepseek/langchain-qwq/langchain-ollama + 本地 Ollama/vLLM + Sqlite/Postgres checkpointer）**零境外依赖**，PyPI 走国内镜像即可；LangSmith 完全可选（不设 `LANGSMITH_*` 即无任何外呼），需要观测又不出境时可用 callback 接自建后端（§1.6）。
- **中文资料**：极多。除官方英文文档外：中文教程站 python.langchain.ac.cn（tongyi/zhipuai 专页）、阿里云/智谱官方接入文档、知乎大量实践帖（WebSearch 实见）。国内社区成熟度在同类框架中属第一梯队。

---

## 3 企业落地注记（安全/合规视角）

- **沙箱**：OSS 层**无内置工具执行沙箱**——`ToolNode` 在宿主进程内直接执行 Python 函数；需要隔离（金融场景代码执行类工具必须有）时自行包容器/gVisor，或用商业线 LangSmith Sandboxes（docs /langsmith/sandboxes，按需起停的云端沙箱）——后者为 SaaS，境内自托管需评估。
- **密钥管理**：无集中式密钥管理组件，约定为环境变量（`DEEPSEEK_API_KEY`/`DASHSCOPE_API_KEY`/`ZHIPU_API_KEY`…），企业内应接 KMS/Vault 注入；**checkpoint 静态加密**是一等能力（`EncryptedSerializer.from_pycryptodome_aes` + `LANGGRAPH_AES_KEY`，任意 checkpointer 通用）——金融落库建议强制开启；LangSmith 侧另有 workspace secrets、trace 脱敏 anonymizer（均为商业功能）。
- **数据驻留/合规**：checkpoint 默认**明文**存全量对话与状态于 Postgres——须加密+库权限管控+保留策略（官方 troubleshooting 明示 checkpoint 无限增长的清理责任在用户：cron 删旧或 TTL）；LangSmith SaaS 为境外服务，敏感行业应关闭 tracing 或走自托管 Agent Server + 不出网部署。
- **供应链风险**：依赖面=pydantic v2、langchain-core、ormsgpack、（可选）psycopg/aiosqlite/mcp——面不算大，但**包拓扑变化快**（v1.0 大重构、community sunset、mcp-adapters 并包，均发生在近一年），建议：锁版本 + 私有 PyPI 镜像 + 依赖审查纳入 CI；monorepo 每日子提交量大，升级窗口需主动管理。
- **版本节奏锚点**：langgraph 1.2 系引入 timeout/error_handler/set_node_defaults/DeltaChannel，`response_schema` 需 ≥1.2.12，`strict` 需 langchain≥1.2，`server_info/execution_info` 需 langgraph≥1.1.5——POC 用旧版会踩到文档与实现不一致。

---

## 4 关键源码文件清单（供深读入口；GitHub API git/trees 实查 main 分支 2026-11-03）

**langchain-ai/langgraph 仓**（861 个 tree 条目中筛出）：

- `libs/langgraph/langgraph/pregel/` — Pregel 运行时内核：`main.py`（Pregel 类）、`_loop.py`（super-step 主循环）、`_algo.py`、`_read.py`/`_write.py`（通道读写）、`_retry.py`、`_checkpoint.py`、`_executor.py`、`_validate.py`
- `libs/langgraph/langgraph/graph/` — StateGraph 构建层（Graph API）
- `libs/langgraph/langgraph/func/` — Functional API（`@task`/`@entrypoint`）
- `libs/langgraph/langgraph/channels/`、`managed/` — channel/reducer 实现（含 DeltaChannel）
- `libs/langgraph/langgraph/types.py`（`Command`/`interrupt`/`RetryPolicy`/`TimeoutPolicy`/`Send`）、`errors.py`（`GraphBubbleUp`/`NodeError`/`NodeTimeoutError`/`GraphDrained`）、`runtime.py`（`Runtime`/`RunControl`）、`callbacks.py`、`constants.py`
- `libs/checkpoint/langgraph/checkpoint/base/`（`BaseCheckpointSaver`）、`memory/`（InMemorySaver）、`serde/`（`JsonPlusSerializer`/`EncryptedSerializer`）
- `libs/checkpoint-sqlite/`、`libs/checkpoint-postgres/`、`libs/checkpoint-conformance/`（一致性测试套件）
- `libs/prebuilt/langgraph/` — `ToolNode` 等 prebuilt
- `libs/cli/` — langgraph-cli（dev server）；`libs/sdk-py/` — LangGraph SDK

**langchain-ai/langchain 仓**：

- `libs/core/langchain_core/` — runnables/messages/tools/tracers（callback 体系所在地）
- `libs/langchain_v1/langchain/` — `agents/`（`create_agent` 工厂）、`agents/structured_output.py`（ToolStrategy/ProviderStrategy）、`mcp/`（MCP 支持新家）
- `libs/partners/deepseek/`、`libs/partners/ollama/` 等 18 个官方 partner 包

---

## 5 信息源 URL 列表（本次实际访问）

官方文档（docs.langchain.com，多为 .md 原文端点直抓）：

1. https://docs.langchain.com/oss/python/langchain/overview （LangChain 定位/harness 分层）
2. https://docs.langchain.com/oss/python/langgraph/overview （LangGraph 定位/低层编排/可不依赖 LangChain）
3. https://docs.langchain.com/oss/python/langgraph/persistence （checkpointer vs store 总览）
4. https://docs.langchain.com/oss/python/langgraph/checkpointers （threads/super-step/pending writes/durability/加密/DeltaChannel/自定义契约）
5. https://docs.langchain.com/oss/python/langgraph/memory （短/长期记忆、三类记忆、hot path vs background、Store API）
6. https://docs.langchain.com/oss/python/langgraph/interrupts （interrupt/Command/response_schema/规则与反模式/静态断点）
7. https://docs.langchain.com/oss/python/langgraph/fault-tolerance （retry/timeout/error_handler/graceful shutdown）
8. https://docs.langchain.com/oss/python/langgraph/observability （LangSmith 开启/选择性追踪/脱敏）
9. https://docs.langchain.com/oss/python/langgraph/deploy （LangSmith Cloud/自托管部署入口）
10. https://docs.langchain.com/oss/python/langchain/tools （@tool/ToolRuntime/ToolNode/工具内 Command）
11. https://docs.langchain.com/oss/python/langchain/structured-output （ToolStrategy/ProviderStrategy/错误重试）
12. https://docs.langchain.com/oss/python/langchain/multi-agent （五种多智能体模式+成本对比）
13. https://docs.langchain.com/oss/python/integrations/chat/deepseek （ChatDeepSeek 能力矩阵）
14. https://docs.langchain.com/oss/python/integrations/chat/qwen （官方指向 langchain-qwq，WebSearch 结果）
15. https://docs.langchain.com/oss/python/langgraph/use-subgraphs 、/use-time-travel 、/pregel 、/local-server 、/langsmith/deploy-standalone-server 、/langsmith/sandboxes （docs 导航树实证的页面路径）
16. https://docs.langchain.com/oss/python/migrate/langchain-mcp-adapters （MCP 迁移指南，README 引用）

GitHub / PyPI（API 或 raw 直连）：

17. https://api.github.com/repos/langchain-ai/langgraph/git/trees/main?recursive=1 （源码树实查）
18. https://github.com/langchain-ai/langchain-mcp-adapters （README raw 实抓：停维护并入 langchain.mcp）
19. https://pypi.org/pypi/langchain-mcp-adapters/json （0.3.2；依赖 langchain-core>=1.3.3、mcp>=1.24）
20. langgraph-supervisor-py / langgraph-swarm-py（GitHub search API 实查 stars/license/pushed）
21. langchain 仓 `libs/partners` 目录（GitHub contents API：deepseek/ollama 在列，tongyi/zhipu 不在）

第三方/中文生态（WebSearch）：

22. https://docs.z.ai/guides/develop/langchain/introduction （智谱官方 LangChain 指南）
23. https://help.aliyun.com/en/model-studio/use-bailian-in-langchain （阿里云官方）
24. https://python.langchain.ac.cn/docs/integrations/chat/tongyi 、/chat/zhipuai （中文教程站）
25. https://github.com/MetaGLM/langchain-glm 、https://pypi.org/project/langchain-qwq 、https://libraries.io/pypi/langchain-qwen3
26. https://github.com/langchain-ai/langchain-community/issues/493 （community sunset 佐证）

**未找到项**（反幻觉声明）：

- Python 版 "concepts/products"（Runtimes/frameworks/harnesses 专页）：`/oss/python/concepts/products.md` 抓取失败/限流——分层结论改用 langchain/overview 页官方原句（第 0 节），证据效力等同。
- LangGraph/LangChain Python OSS 层原生 OTel exporter：官方文档未检得，仅 LangSmith 侧有 OTel 接入页（§1.6 如实标注）。
- vLLM 官方 provider 集成页：两次抓取失败（路径 /oss/python/integrations/providers/vllm.md），OpenAI 兼容端点接法未做页面级核实。
- langchain-ai 官方 Tongyi/ZhipuAI partner 包：不存在（partners 目录实查），现状=第三方包+厂商官方指南（§2）。
