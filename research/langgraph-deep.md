# LangGraph 源码深读笔记（库/SDK 层代表）

- 深读时点：2026-11-03
- commit：`3c916d7`（3c916d750575b22c464790e9774494d810ccd322，2026-11-03 01:03 UTC，`feat(sdk-py): remove fairness_weight from PrioritySettings (#9441)`）
- 源码位置：`D:\workspace\agent-tools-survey\repos\langgraph\`（浅 clone monorepo）
- monorepo 布局：`libs/langgraph`（主包 v1.2.19）、`libs/checkpoint`（langgraph-checkpoint，含 store）、`libs/checkpoint-postgres`、`libs/checkpoint-sqlite`、`libs/checkpoint-conformance`（saver 一致性测试套件）、`libs/prebuilt`（langgraph-prebuilt）、`libs/sdk-py`（langgraph-sdk）、`libs/sdk-js`、`libs/cli`（langgraph-cli）
- 本文所有路径相对仓库根；行号以该 commit 为准。

---

## 1. 状态 / Checkpoint

### 1.1 接口层：BaseCheckpointSaver

位置：`libs/checkpoint/langgraph/checkpoint/base/__init__.py`

- `class BaseCheckpointSaver(Generic[V])`（L199），V 是版本号类型（sqlite/postgres 实现均传 `str`：`libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/__init__.py:49`、`libs/checkpoint-postgres/langgraph/checkpoint/postgres/base.py:547`）。默认序列化器 `serde: SerializerProtocol = JsonPlusSerializer()`（L232）。
- 核心方法签名（全部同步基类 + 各实现提供 a* 异步版）：
  - `get(config) -> Checkpoint | None`（L250）
  - `get_tuple(config) -> CheckpointTuple | None`（L262）
  - `list(config, *, filter=None, before=None, limit=None) -> Iterator[CheckpointTuple]`（L277-284）
  - `put(config, checkpoint, metadata, new_versions) -> RunnableConfig`（L301-307）
  - `put_writes(config, writes, task_id, task_path="")`（L324-330）——任务级中间写（pending writes），崩溃恢复的关键
  - `delete_thread(thread_id)`（L344）、`delete_for_runs(run_ids)`（L355）、`copy_thread(source, target)`（L374）、`prune(thread_ids)`（L398）
- `Checkpoint` TypedDict（L93-124）：`v / id / ts / channel_values / channel_versions / versions_seen / updated_channels`。**`id` 是 uuid6（时间有序）**，故可按 id 排序做历史回放——uuid6 实现内嵌在 `libs/checkpoint/langgraph/checkpoint/base/id.py`（"Bundled in to avoid install issues with uuid6 package"）。
- `versions_seen`（L116-121）：node→该 node 已消费的 channel 版本，Pregel 据此决定下一 superstep 哪些 node 被触发（见 1.4）。
- `CheckpointMetadata.source` 四种来源（L42-48）：`"input" | "loop" | "update" | "fork"`，与 1.4 的保存点一一对应。

### 1.2 序列化：SerializerProtocol

- `libs/checkpoint/langgraph/checkpoint/serde/base.py`：`SerializerProtocol`（dumps/loads/dumps_typed/loads_typed）。
- `libs/checkpoint/langgraph/checkpoint/serde/jsonplus.py:104`：`class JsonPlusSerializer`——**底层已是 ormsgpack**（L30 `import ormsgpack`），非 JSON；兼容旧 pickle 检查点（L65 注释）。带模块 allowlist（`allowed_msgpack_modules`，L124-141）做反序列化安全限制；`serde/encrypted.py` 提供 `EncryptedSerializer`（加密层）。
- checkpoint 包依赖仅两个：`langchain-core>=0.2.38`、`ormsgpack>=1.12.0`（`libs/checkpoint/pyproject.toml`）。

### 1.3 三个参考实现

- **InMemorySaver**：`libs/checkpoint/langgraph/checkpoint/memory/__init__.py:35`，三层 dict（storage 按 `thread_id → checkpoint_ns → checkpoint_id`），docstring 明言"Only use for debugging or testing"，生产推荐 Postgres。
- **SqliteSaver / AsyncSqliteSaver**：`libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/__init__.py:49`、`aio.py:42`。异步版对 sqlite 的写串行化处理是已知薄弱面（事务/锁路径都在单文件内）。
- **AsyncPostgresSaver**：`libs/checkpoint-postgres/langgraph/checkpoint/postgres/aio.py:45`。
  - `aput`（L237-304）：原始类型（str/int/float/bool/None）内联进 `checkpoints` 表 JSONB，其余走 `checkpoint_blobs` 表分开存（L266-279），写入用 psycopg pipeline 模式（L285 `async with self._cursor(pipeline=True)`）。
  - `aput_writes`（L312-343）：按 `(thread_id, checkpoint_ns, checkpoint_id, task_id, task_path)` upsert，同一 task 重跑幂等（UPSERT vs INSERT 两条 SQL 按是否是保留字 channel 选择，L322-326）。
  - 表结构/迁移：`libs/checkpoint-postgres/langgraph/checkpoint/postgres/base.py:46` 起 `MIGRATIONS`（checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations 四表）。
  - **time-travel 的排序依据就是 uuid6 checkpoint_id**：`aio.py:147` `query = self.SELECT_SQL + where + " ORDER BY checkpoint_id DESC"`；取最新 head 是 `aio.py:208` `ORDER BY checkpoint_id DESC LIMIT 1`。
- **langgraph-checkpoint-conformance**（`libs/checkpoint-conformance/README.md`）：给第三方 saver 的合同测试套件——"validates that a BaseCheckpointSaver subclass correctly implements the checkpoint storage contract"，说明官方有意把存储做成可插拔生态。

### 1.4 Pregel 执行模型：superstep 语义与 checkpoint 保存时机

主循环拆在两处：`libs/langgraph/langgraph/pregel/main.py`（Pregel 类，4557 行，stream/astream/get_state 等）和 `libs/langgraph/langgraph/pregel/_loop.py`（Sync/AsyncPregelLoop，2172 行）。

**superstep = tick() + after_tick() 一对**（`_loop.py`）：

1. `tick(self) -> bool`（L650-698）：
   - L658 步数超限 → `status="out_of_steps"`；
   - L664 `_prepare_tasks_and_emit_checkpoint()`（L778）调 `_algo.py:prepare_next_tasks`（L348）算出本步要跑的 tasks；
   - L667 无 task → `status="done"` 结束；
   - L677-681 `interrupt_before` 检查：`should_interrupt(...)`（`_algo.py:156`，比对 `versions_seen[INTERRUPT]` 与 channel_versions）命中则 `status="interrupt_before"` 并 `raise GraphInterrupt()`。

2. `after_tick(self)`（L700-776）——**checkpoint 保存点就在这里的 L766**：
   ```python
   # L724  all tasks have finished
   self.updated_channels = apply_writes(self.checkpoint, self.channels, self.tasks.values(), ...)
   ...
   # L766  save checkpoint
   self._put_checkpoint({"source": "loop"})
   # L768  after execution, check if we should interrupt
   if self.interrupt_after and should_interrupt(...): ... raise GraphInterrupt()
   ```
   即：**节点执行完 → apply_writes 把所有任务写合并进 channels → 每个 superstep 结束时统一保存一个 `source="loop"` 检查点 → 然后才做 interrupt_after 检查**。
3. `_put_checkpoint(metadata)`（L1237-1380）：
   - `exiting = metadata is self.checkpoint_metadata`（L1248）——只有退出时的最终保存复用同一 metadata 实例（源码注释自嘲"Could be replaced by an explicit exiting parameter"）；
   - L1289-1291 `do_checkpoint = ... and (exiting or self.durability != "exit")`——**durability="exit" 时中间步不落盘，只在退出时存一次**；
   - L1366-1370 checkpoint 保存不阻塞循环，通过 `self.submit(self._checkpointer_put_after_previous, getattr(self, "_put_checkpoint_fut", None), ...)` 链式提交；`_checkpointer_put_after_previous`（L1712-1725）先 `prev.result()` 再 `put()`，**保证同一 thread 的检查点按序到达存储**（即使异步并发写）。
4. 三个保存点 + durability 语义（`types.py:98-103`）：
   ```python
   Durability = Literal["sync", "async", "exit"]
   ```
   - `"async"`（默认，`main.py:2692`）：下一 superstep 与 checkpoint 写并发；
   - `"sync"`：stream 主循环里每步等待落盘——`main.py:3080-3081` `if durability_ == "sync": loop._put_checkpoint_fut.result()`（异步版 L3549 `await loop._put_checkpoint_fut`）；
   - `"exit"`：只在图退出时保存（`_loop.py:1507-1513` `_suppress_interrupt` 里补存）。
   - 其它两个保存点：输入检查点 `_loop.py:1185` `self._put_checkpoint({"source": "input"})`；time-travel fork `_loop.py:1108` `{"source": "fork"}`。
5. 任务级中间写 `put_writes`（`_loop.py:461`）：每个 task 完成即写 pending writes（L515 `if self.durability != "exit"` 才落库），这是"节点跑一半崩溃也能恢复到该 superstep 内部已完成部分"的机制。
6. **触发调度**（PULL 语义）：`_algo.py:prepare_next_tasks`（L348）遍历 candidate nodes，依据 `versions_seen` 里 node 已见版本 vs channel 当前版本决定触发；`trigger_to_nodes` 是 updated_channels→nodes 的倒排加速（L467-478，注释"This section is an optimization"）。`Send` 走 PUSH 通道（L444-465 消费 `TASKS` Topic channel）。

### 1.5 thread_id / config 体系

- `thread_id` 是 `config["configurable"]["thread_id"]`（langchain-core `RunnableConfig` 约定），无独立配置类；`main.py:1472-1474` get_state 前 coerce 成 str。
- 子图命名空间：`libs/langgraph/langgraph/_internal/_constants.py:90-92` `NS_SEP="|"`、`NS_END=":"`——checkpoint_ns 形如 `parent|node:task_idx`；`main.py:869-884 _own_checkpoint_config` 处理 `checkpointer=True` 子图"每次调用一条历史"的剥离；`main.py:886-901 _subgraph_for_namespace` 把 `get_state(config={"configurable":{"checkpoint_ns": ...}})` 路由到对应子图实例。
- **time-travel**：`main.py:1510-1548 get_state_history(config, *, filter, before, limit)` 直接 yield `checkpointer.list(...)` 结果包成 `StateSnapshot`；`before=`（传某 checkpoint 的 config）+ fork 一个新 thread / `update_state(as_node=...)` 即时间旅行。`StateSnapshot` 定义在 `libs/langgraph/langgraph/types.py:711`。

---

## 2. 记忆

### 2.1 短期记忆 = channels（StateGraph 状态本身）

位置：`libs/langgraph/langgraph/channels/`。State schema 字段 → channel 的映射在 `libs/langgraph/langgraph/graph/state.py:1815 _get_channels` / `1850 _get_channel` / `1904 _is_field_binop`：

- 无注解 → 兜底 `LastValue`（L1871-1873）；
- `Annotated[X, reducer]`（两参可调用）→ `BinaryOperatorAggregate`（L1917）；
- `Annotated[X, SomeChannel]`（BaseChannel 子类）→ 该 channel 实例（L1881-1900）。

各 channel reducer 语义：

- `LastValue`（`channels/last_value.py:23`）：`update()` 收到 >1 个值直接抛 `InvalidUpdateError`（L55-62 "Can receive only one value per step"）——这就是"同一 superstep 两个节点写同一 key 报 InvalidConcurrentGraphUpdate"的出处。
- `BinaryOperatorAggregate`（`channels/binop.py:89`）：`update()` 逐个 `self.value = operator(self.value, value)` 折叠（L139 起），首值不调用 operator（L134-137）；支持 `Overwrite` 包装绕过 reducer（L57-85 `_get_overwrite`，`types.py:1050`）。**常见 `Annotated[list, operator.add]` 的 append 语义就是它**。注意注释 L143-151：lambda operator 判等不可靠，重编译图时靠类名+身份近似。
- `AnyValue`（`channels/any_value.py:15`）：多值时假定全等、取最后一个（"assumes that if multiple values are received, they are all equal"）——多节点写同 key 但值相同（如同一子图多分支写同一结果）时的容错 channel。
- `EphemeralValue`（`channels/ephemeral_value.py:15`）：只保留上一步的值、消费即清（`update([])` 会清空，any_value.py 对比）。
- `Topic`（`channels/topic.py:23`）：追加语义 + `retain` 控制 consumer 独立消费位（内部 `TASKS` channel 就是 `Topic[Send]`）。
- 其它：`named_barrier_value.py`（屏障同步）、`untracked_value.py`、`delta.py`（beta 的 `DeltaChannel`——只存增量写、按 `snapshot_frequency` 周期落全量快照，见 `CheckpointMetadata.counters_since_delta_snapshot`，checkpoint/base/__init__.py:64-87；这是大消息列表场景的存储优化）。

### 2.2 长期记忆 = BaseStore（跨 thread，namespace 语义）

位置：`libs/checkpoint/langgraph/store/base/__init__.py:708 class BaseStore(ABC)`（注意：**store 放在 langgraph-checkpoint 包里**，不在主包）。

- 抽象核心只有两个 batch 方法：`batch(ops)` / `abatch(ops)`（L737-750）；`get/put/search/delete/list_namespaces` 都是"单操作包成 Op 数组走 batch"的便利封装（如 `put` L851 末尾 `self.batch([PutOp(...)])`）。
- **namespace 是层级元组**：`put(namespace: tuple[str, ...], key, value, index=None, *, ttl=...)`（L798-801 签名区）；`search(namespace_prefix, /, *, query=None, filter=None, limit=10, offset=0)`（L770-774）支持前缀过滤 + 自然语言 query（语义检索可选）。namespace 校验禁止空串/`.`/根为 `"langgraph"`（`_validate_namespace`，L905 附近 raise `InvalidNamespaceError`）。
- 语义检索依赖实现端注入 embeddings：`InMemoryStore`（`libs/checkpoint/langgraph/store/memory/__init__.py:136`）接受 `index={"dims","embed","fields"}`，embed 用 langchain-core 的 `Embeddings` 接口（L114 `from langchain_core.embeddings import Embeddings`），向量运算 lazy import numpy（L496-501）；`AsyncPostgresStore` 直接建 pgvector 扩展和 HNSW 索引（`libs/checkpoint-postgres/langgraph/store/postgres/base.py:98-129` `CREATE EXTENSION vector` / `store_vectors_embedding_idx ... USING %(index_type)s`）。
- **cross-thread memory 的接法**：图内节点通过 `get_store()`（`libs/langgraph/langgraph/config.py:35`，从 contextvar 取）或参数注入 `InjectedStore`（`libs/prebuilt/langgraph/prebuilt/tool_node.py` 导出）访问；同一用户多会话共享 `("memories", user_id)` namespace。本仓库 examples 里没有独立的 store 示例文件（docs 站在别仓），但 `libs/checkpoint/langgraph/store/base/__init__.py` docstring 即带 `store.put(("docs",), "report", {"memory": "Will likes ai"})` 示例。
- checkpoint（thread 内状态）与 store（跨 thread 记忆）是两套独立接口、两张独立表；Platform/SDK 把两者都暴露成 REST 资源（`libs/sdk-py/langgraph_sdk/client.py` 同时导出 `ThreadsClient/RunsClient/StoreClient`）。

---

## 3. 工具调用

### 3.1 ToolNode

位置：`libs/prebuilt/langgraph/prebuilt/tool_node.py:630 class ToolNode(RunnableCallable)`（文件 2064 行）。

- `_run_one` / `_arun_one`（L1031/L1191）逐 tool_call 执行；`_inject_tool_args`（L1349）在调用前注入 `InjectedState`（图状态）、`InjectedStore`（store）、`ToolRuntime`（运行时句柄）等系统参数——这些参数对 LLM 不可见。
- **pydantic 校验发生在 langchain-core 层**：`tool.invoke(call_args, config)`（L964）内部由 `BaseTool` 用 `args_schema` 做 `TypeAdapter` 校验；ToolNode 只在 `except ValidationError`（L965-982）把 pydantic 错误转成 `ToolInvocationError` 并用 `_filter_validation_errors`（L518-570）**把系统注入参数的错误从反馈给 LLM 的错误里剔除**（"only errors for arguments that the LLM controls"）。依赖边界清晰：langgraph-prebuilt 依赖 `langchain-core>=1.3.1`（`libs/prebuilt/pyproject.toml` L28），@tool 装饰器、结构化输出（`with_structured_output`）都在 langchain-core / langchain，LangGraph 只消费 `BaseTool`/`ToolCall`/`BaseMessage`。
- 工具可返回 `Command`（改状态/跳转）：`_validate_tool_command`（L1537）校验"工具返回 Command.update 必须携带匹配 tool_call_id 的 ToolMessage"（防止消息序列断裂）。
- 未知工具名不抛异常而是生成错误 ToolMessage 回给模型自纠（`_validate_tool_call` L1302-1310，`INVALID_TOOL_NAME_ERROR_TEMPLATE` L116）。
- `tools_condition`（导出于 `libs/prebuilt/langgraph/prebuilt/__init__.py`）：给 `add_conditional_edges` 用的标准"最后一条消息有无 tool_calls"路由。
- 结构化输出前置校验：`tool_validator.py ValidationNode`——bind 前统一验证工具定义合法。

### 3.2 create_react_agent

位置：`libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py:278`。

- **已标记 deprecated**：docstring 明确"This function is deprecated in favor of `create_agent` from the `langchain` package, which provides an equivalent agent factory with a flexible middleware system"——即官方方向是中件化迁去 langchain 仓。
- 图结构就是经典两节点环：L867-894 `workflow.add_node("agent", ...)` + `workflow.add_node("tools", tool_node)` + 条件边；支持 `pre_model_hook`/`post_model_hook`（消息裁剪/摘要挂点）、`response_format`（走模型的 `with_structured_output`，独立一次 LLM 调用，docstring "The graph will make a separate call to the LLM"）、`checkpointer=`/`store=` 直通、动态模型选择（`(state, runtime) -> BaseChatModel`）。
- prebuilt 全部导出就 8 个（`libs/prebuilt/langgraph/prebuilt/__init__.py`）：`create_react_agent / ToolNode / ToolCallTransformer / tools_condition / ValidationNode / InjectedState / InjectedStore / ToolRuntime`。**没有 supervisor、没有 swarm、没有任何多 Agent 编排器**（见 §4.3）。

### 3.3 interrupt 期间工具调用如何挂起

- 工具函数体内可以调 `interrupt()`：`tool.invoke` 抛出的 `GraphInterrupt` 属于 `GraphBubbleUp` 家族，ToolNode 的 except 链**原样上抛不吞**（tool_node.py:988-996 注释枚举三种场景：工具内 interrupt / graph-as-tool 内 interrupt / supervisor-with-tools 的子图 interrupt）；
- 上抛到 `_runner.py commit()`（L575-590）：`isinstance(exception, GraphInterrupt)` → `writes = [(INTERRUPT, exception.args[0])]` → `put_writes(task.id, writes)`——**中断状态本身作为该 task 的 pending write 持久化**，循环随后 `raise GraphInterrupt` 结束本轮；
- resume 时**整个节点（ToolNode）从头重执行**（`types.py:918` docstring："The graph resumes from the start of the node, re-executing all logic"），已答过的 interrupt 靠 scratchpad 里记录的 resume 序列（`types.py:1020-1035`）在重放中返回缓存值而非再次中断。
- 工具作为 graph 被 RemoteGraph/子图调用时的 resume 值不合法（`is_invalid_resume`）会 fail run 而非让模型重试（tool_node.py:1129-1131）。

---

## 4. 扩展机制

### 4.1 subgraph（静态嵌套）

- 编译后的 `CompiledStateGraph` 本身是 Runnable（Pregel 子类），直接 `parent.add_node("child", child_graph)` 即嵌套；子图 checkpoint 通过 `checkpoint_ns`（`parent|child:task_idx`，见 §1.5）与父图共用同一 thread 的存储。
- `StateGraph.compile(checkpointer=...)` 的三态（`graph/state.py:1196-1204` docstring + `types.py:108-116 Checkpointer` 类型）：`None`=继承父图 checkpointer（默认）、`False`=禁用、`True`=子图独立开关并存自己历史（`main.py:869 _own_checkpoint_config`）。
- `Pregel.get_subgraphs(namespace=..., recurse=True)`（`main.py:1140`）供 get_state/update_state 按命名空间路由进子图。

### 4.2 Send API（动态并行 / map-reduce）

- `libs/langgraph/langgraph/types.py:738 class Send`：`(node, arg, *, timeout=None)`；docstring 示例即 map-reduce（`continue_to_jokes` 返回 `[Send("generate_joke", {...}) for s in subjects]`）。
- 实现机制：Send 被写入 `TASKS` channel（一个 `Topic[Send]`），下一 tick 由 `prepare_next_tasks` 当 PUSH 任务消费（`_algo.py:444-465`）——与"边触发的 PULL 任务"合流成同一 superstep 的任务集。**Send 的 arg 不经过目标 node 的订阅 channel 聚合，直接作为该次调用的输入 state**，所以能"同一节点并行跑 N 份不同状态"。
- `Command(goto=Send|Sequence[Send|N])`（`types.py:847-852`）让节点内部也能动态派发任务；`Command(graph=Command.PARENT)`（L882 `"__parent__"`）支持子图改父图路由——多 Agent supervisor 模式的官方积木。
- 运行时控制：`Runtime`/`control.drain_requested`（`_loop.py:686-688`）可请求在断点前停机排空（配合 Platform 的Durability/后台运行）。

### 4.3 多 Agent：库里只有积木，没有编排器

- 如实结论：**monorepo 内不存在 supervisor/swarm/hierarchical 任何现成实现**。`grep -rln "supervisor|swarm|hierarchical" libs/ --include=*.py` 只命中 store docstring、ToolNode 注释和 sdk-py 的集成测试图（`libs/sdk-py/integration/graph/deep_agent.py`、`tools_agent.py` 是 SDK 联调 fixture）。
- 官方多 Agent 路径 = langgraph-supervisor / langgraph-swarm 等**独立外包仓库** + 本库的 Send/Command(PARENT)/subgraph 原语。examples 只有教学 notebook：`examples/multi_agent/multi-agent-collaboration.ipynb`、`hierarchical_agent_teams.ipynb`。
- `Command` 的 `ToolOutputMixin`（`types.py:57`）+ ToolNode 校验（§3.1）构成"agent-as-tool"形态。

### 4.4 langgraph-sdk 与 Platform 的关系

- `libs/sdk-py/langgraph_sdk/client.py` 只是薄门面：re-export `_async/_sync` 下的 `AssistantsClient / ThreadsClient / RunsClient / CronClient / StoreClient` + `LangGraphClient(url="http://localhost:2024")` 工厂。**它是 LangGraph Server/Platform REST API 的客户端，不是运行时**。
- `libs/langgraph/langgraph/pregel/remote.py:118 class RemoteGraph(PregelProtocol)`：把远端 deployment 包成本地 Runnable，可直接 `graph.add_node(remote_graph)` 当子图用——本地库与远端 Platform 的互操作层。
- 主包对 sdk 有硬依赖（`libs/langgraph/pyproject.toml` dependencies 含 `langgraph-sdk>=0.4.6,<0.5.0`），因为 RemoteGraph/`pregel/protocol.py` 需要 client；反过来纯离线用不需要任何 server。
- sdk-py 还带 `langgraph_sdk/auth`（自定义鉴权钩子）、`encryption`、`priority`、`runtime.py`（服务端运行时上下文），均为对接 server 侧的协议件。

---

## 5. HITL（human-in-the-loop）

### 5.1 interrupt() / Command(resume=...) 实现

- `interrupt(value, *, response_schema=None)`：`libs/langgraph/langgraph/types.py:903-1046`。机制（L1013-1046）：
  1. 从 `get_config()["configurable"]` 取 scratchpad（`CONFIG_KEY_SCRATCHPAD`）；
  2. `idx = scratchpad.interrupt_counter()` 给节点内多个 interrupt 记序号；
  3. 若 scratchpad.resume 里有本序号的值 → `conf[CONFIG_KEY_SEND]([(RESUME, ...)])` 把"已答"作为写回写（重放时也能恢复）并**返回该值**；
  4. 没有值 → `raise GraphInterrupt((Interrupt.from_ns(value=value, ns=..., response_schema=...),))`。
- `Command`（`types.py:833`）：dataclass，四字段 `graph / update / resume / goto`；resume 支持 dict（**按 interrupt id 精确应答**）——`_loop.py:1027-1037`：dict 且全 key 为 xxh3_128 hex → 存 `CONFIG_KEY_RESUME_MAP`；多个待决 interrupt 且不传 map → RuntimeError 提示必须指定 id。
- resume 进入循环的分支：`_loop.py:921 _first()`——`input_is_command` 判定（L940）、`is_resuming`（L941-952，None 输入 / Command 输入 / 同 run_id 重连三种信号）、time-travel 区分（L958-980 `is_time_traveling`，replay 时丢弃已存的 RESUME 写让 interrupt 重新触发，L981-984）、fork 分支保存（L992-998 + L1108）。
- **resume 时的节点重放语义**：中断节点从头重跑（§3.3），`is_replaying` 标志让"已完成的兄弟任务"不重复执行（`_loop.py:980`，其 writes 由 checkpoint 的 pending_writes 恢复，`_reapplies_pending_writes`/`_resume_loaded_writes` L671-673、L1005-1009）。
- 中断持久化与生命周期事件：`_loop.py:1516 _push_graph_lifecycle_event("interrupt", interrupts=...)` → `callbacks.py` 的 `GraphInterruptEvent`（run_id/status/checkpoint_id/checkpoint_ns/interrupts，L50-65）。

### 5.2 静态断点（breakpoints）

- `compile(interrupt_before=[...], interrupt_after=[...], checkpointer=...)`（`graph/state.py:1177-1187`）；`"*"` 表示全部节点（`types.py:105 All = Literal["*"]`）。
- 检查点：before 在 `tick()`（`_loop.py:677-681`）、after 在 `after_tick()` 存完 checkpoint 之后（`_loop.py:768-774`，注释点明"the next tick, which streams this checkpoint, never runs"——中断前的状态已可流式获取）。
- **静态断点 vs 动态 interrupt() 的分工**：静态=编译期按节点名挂（审批门、逐节点调试），动态=运行期按数据条件挂（节点内逻辑决定）。两者都依赖 checkpointer；`Command(resume=...)` 通用（`_loop.py:1021-1025` 无 checkpointer 直接 RuntimeError "Cannot use Command(resume=...) without checkpointer"）。
- `update_state(config, values, as_node=...)`（`main.py:2613`）/`bulk_update_state`（L1615）允许人工改状态后继续——HITL 审批改参的另一半。

---

## 6. 可观测性

- **callbacks 桥接 langchain callback 体系**：`libs/langgraph/langgraph/callbacks.py` 从 `langchain_core.callbacks` 导入 `BaseCallbackHandler/BaseCallbackManager/ahandle_event`（L15-17）；新增的是 LangGraph 特有的**图生命周期事件面**：`GraphCallbackHandler`、`GraphInterruptEvent/GraphResumeEvent`、状态枚举 `input/pending/done/interrupt_before/interrupt_after/out_of_steps`（L40-44）。装配点 `get_sync_graph_callback_manager_for_config(config)`（L363）只挑 `GraphCallbackHandler` 子类 handler。
- 节点内 LLM 调用的 tracing 走 langchain-core Runnable 的标准 tracer 管线，LangGraph 主循环通过 `_internal/_runnable.py` 的 config 传播接进去（`_set_config_context` L120-135 里 lazy import `langsmith.run_helpers._set_tracing_context`）。
- **OTel：仓库内零依赖、零实现**。全仓 grep `opentelemetry` 仅命中测试文件 `libs/langgraph/langgraph/tests/test_graph_callbacks.py`（且只是字符串），所有 pyproject 均无 OTel 依赖。otel 化要靠社区侧 wrapper。
- **LangSmith 非必需**：`langsmith` 不在任何包的 dependencies（`libs/langgraph/pyproject.toml` deps 只有 langchain-core/langgraph-checkpoint/langgraph-sdk/langgraph-prebuilt/xxhash/pydantic）；远程追踪、callback、checkpoint、interrupt、store、stream 全功能离线可用。LangSmith 相关代码只有两处软引用：`remote.py:14 import langsmith as ls`（RemoteGraph 里给远端 run 附加本地 trace 上下文）和 constants 的 `TAG_HIDDEN = "langsmith:hidden"`（`constants.py:25`）——都是"装了才用"的增强。
- 另有独立流事件面：`stream_events(version="v3")` + `StreamTransformer`（`compile(transformers=...)`，`graph/state.py:1187`；v3 事件循环 `main.py:3593 _pregel_stream_v3`），可自定义流转换器。

---

## 7. 部署与许可证

- **全部 MIT**：根 `LICENSE`（Copyright 2024 LangChain, Inc.）+ 七个包 pyproject 均 `license = "MIT"`（libs/langgraph、checkpoint、checkpoint-postgres、checkpoint-sqlite、cli、prebuilt、sdk-py 逐一核对）。
- **纯库自托管**：`pip install langgraph` 装完即用（InMemorySaver 内存态；配 langgraph-checkpoint-sqlite/postgres 得持久化），无任何服务依赖、无 license server、无遥测强制（cli 里有 `analytics.py` 但仅 dev-server 使用且可关）。
- **langgraph-cli 的角色**：`libs/cli/langgraph_cli/cli.py` 是 click 命令组（`langgraph up / build / dockerfile / ...`）。`up`（L276 起）尝试 `from langgraph_api.cli import run_server`（L785）——**API server 本体不在本仓库**，来自两个独立 PyPI 包：`langgraph-api`（>=0.5.35）+ `langgraph-runtime-inmem`（>=0.7），以 extra 形式安装（`libs/cli/pyproject.toml` L25-28 `inmem = [...]`；要求 Python>=3.11）。`langgraph dev`（`up --no-reload` 即 dev server）跑的就是这个进程内 API server，供 LangGraph Studio 联调。
- **开源边界**：本 monorepo = 引擎（langgraph/checkpoint*/prebuilt）+ SDK + CLI 壳。LangGraph Platform 的 server 运行时（langgraph-api / langgraph-runtime-inmem / runtime-advanced 的持久任务队列、postgres 元数据库、Studio 托管、Deployment API）为**独立分发**——CLI 的 docker build 路径（`docker.py`、`deploy.py`）会拉官方镜像。即：引擎 100% 开源自托管可用；"Platform"增量的开源程度以那几个 runtime 包的许可证为准（不在本 repo，本次未核）。
- 生产自托管最小组合：`langgraph + langgraph-checkpoint-postgres`（saver + store 都在）+ 自写 ASGI 入口；不需要 cli/sdk。

---

## 8. 附录：代码规模与依赖面

### 8.1 规模（排除 tests，py 文件数 / 行数）

| 包 | 文件数 | 行数 | 说明 |
|---|---|---|---|
| libs/langgraph | 88 | ~30,400 | 主包 1.2.19；单文件最大 pregel/main.py 4,557 行 + _loop.py 2,172 行 |
| libs/checkpoint | 17 | ~6,000 | saver 接口 + serde + InMemorySaver + **store base/memory** |
| libs/checkpoint-sqlite | 8 | ~4,000 | sync/async 两实现 |
| libs/checkpoint-postgres | 9 | ~5,400 | saver + pgvector store |
| libs/prebuilt | 7 | ~3,700 | tool_node.py 2,064 行 + chat_agent_executor.py 1,015 行占 84% |
| libs/cli | 47 | ~11,300 | 含大量 example fixture |
| libs/sdk-py | 54 | ~20,200 | sync/async 双栈客户端 |

### 8.2 依赖面（runtime deps）

- `libs/langgraph`：`langchain-core>=1.4.7,<2`、`langgraph-checkpoint>=4.3,<5`、`langgraph-sdk>=0.4.6,<0.5`、`langgraph-prebuilt>=1.1,<1.2`、`xxhash>=3.5`、`pydantic>=2.7.4`——**核心外部依赖只有 langchain-core 一族**（RunnableConfig/callbacks/消息类型），网络、DB、重试、tracing 均零第三方。
- `libs/checkpoint`：`langchain-core>=0.2.38` + `ormsgpack`（Embeddings 仅类型引用）。
- `libs/checkpoint-postgres`：`langgraph-checkpoint` + `orjson` + `psycopg>=3.2` + `psycopg-pool`（原生 psycopg3，不用 ORM/SQLAlchemy）。
- `libs/prebuilt`：`langgraph-checkpoint` + `langchain-core>=1.3.1`。
- 有趣的依赖倒挂：主包反向依赖 prebuilt/sdk（v1.x 起拉平安装体验），monorepo 内用 uv path source 联编（`libs/langgraph/pyproject.toml [tool.uv.sources]`）。

### 8.3 一句话定位（源码视角）

LangGraph 本体 = **一个 Pregel 风格的 BSP 调度器**（superstep=版本化 channel 的批量读写，`_algo.py`/`_loop.py`）+ **一套以 channel reducer 为核心的状态代数**（`channels/`）+ **把中断/恢复物化为可持久化写**的 HITL 机制（INTERRUPT/RESUME 也是 pending writes）——三者的粘合剂是 uuid6 时间线上的 checkpoint 树（thread_id × checkpoint_ns × checkpoint_id）。工具调用、React 循环、多 Agent 全部退化为这张图上的节点与边，库本身不提供也不绑定任何编排器。
