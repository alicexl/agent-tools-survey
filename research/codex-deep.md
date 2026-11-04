# Codex CLI 源码深读笔记（Runtime/Harness 层代表，Rust）

- **深读时点**：2026-11-03
- **commit**：`bfdbf01`（main 分支，`git log -1` = "Move agent message board host integration to app-server (#59162)"，2026-11-03 13:38 UTC）
- **仓库**：`D:\workspace\agent-tools-survey\repos\codex\`（浅 clone，无 tag）
- ⚠️ **版本口径重要提示**：任务假设源码在 "rust-v0.166.0 附近"，但实际 clone 的是**当日 main 头部**，已远超 v0.166.0。本笔记以实际源码为准；凡与 v0.166.0 时代常识不同处（如 `wire_api="chat"` 已删除、`on-failure` 档位已并入 `on-request`、`~/.codex/prompts` 自定义斜杠命令已被 skills 取代、插件系统已存在），均特别标注"**较 0.166 演化**"。报告中引用时须注明"main@bfdbf01 口径"。
- **规模**：workspace 成员 crate **166 个**（`codex-rs/Cargo.toml` members 计数）；`.rs` 文件 5,769 个、约 **225 万行** Rust（`find . -name '*.rs' | wc -l` / 累计 `wc -l`）。workspace 版本号统一为 `0.0.0`（发版由 CI 注入），`codex-rs/Cargo.toml: [workspace.package] version = "0.0.0"`。
- 许可证：**Apache-2.0**（仓库根 `LICENSE`；`codex-rs/Cargo.toml:179 license = "Apache-2.0"`）。

## 0. Crate 地图与 CLI 入口

`codex-rs/` 下按关注点分层（只列与七维度相关者）：

| 层 | crate（路径相对 `codex-rs/`） | 职责 |
|---|---|---|
| 入口 | `tui/`（交互 TUI）、`cli/`（`codex` 主命令 + 全部子命令）、`exec/`（非交互）、`arg0/`（单二进制多 arg0 分发：apply_patch / codex-linux-sandbox / windows-sandbox 等辅助进程由同一可执行文件按 argv[0] 重派） |
| 协议 | `protocol/`（协议枚举/配置类型）、`app-server-protocol/`（v2 JSON-RPC 协议）、`codex-api/`（Responses API 请求构造） |
| 核心 | `core/`（会话/turn/工具编排/压缩/rollout 桥接）、`app-server/`（内嵌本地服务器，供 TUI/IDE/桌面共用的进程内 JSON-RPC 层）、`exec-server/`（沙箱内执行器） |
| 持久化 | `rollout/`（JSONL 会话落盘+索引）、`state/`（SQLite 状态/日志库）、`history/`（历史 envelope 模型）、`thread-store/`、`thread-manager`（core 内） |
| 工具 | `tools/`（ToolSpec/JSON schema/Responses 工具序列化）、`apply-patch/`、`shell-command/`、`file-search/` |
| MCP | `rmcp-client/`（MCP 客户端全家桶：stdio/streamable-HTTP/OAuth/EMA）、`codex-mcp/`（MCP 配置归并+工具目录缓存）、`message-board-mcp/` |
| 沙箱 | `sandboxing/`、`linux-sandbox/`、`bwrap/`（内置 bubblewrap）、`windows-sandbox-rs/`、`windows-sandbox-service/`、`mxc-sandbox/`、`execpolicy/` |
| 模型接入 | `model-provider-info/`、`model-provider/`、`models-manager/`（模型目录缓存）、`ollama/`、`lmstudio/`、`responses-api-proxy/` |
| 认证 | `login/`（ChatGPT OAuth/API key/Bedrock/GCP/agent identity）、`keyring-store/`、`aws-auth/` |
| 扩展 | `skills/`、`plugin/`、`plugin-host/`、`core-plugins/`（marketplace）、`hooks/`、`prompts/`（内置提示词模板 crate，非用户自定义） |
| 云 | `cloud-client/`（云线程 gRPC）、`cloud-tasks*`、`backend-client/`（ChatGPT 后端 HTTP） |
| 观测 | `otel/`、`otel-trace-websocket/`、`analytics/`、`rollout-trace/`、`diagnostics/`、`feedback/` |

CLI 子命令全景（`codex-rs/cli/src/main.rs:145-245` `enum Subcommand`）：`agents`（会话指挥中心）、`exec`/`e`、`review`、`login`/`logout`、`mcp`、`plugin`、`app-server`、`remote-control`、`app`（桌面）、`completion`、`update`、`doctor`、`sandbox`、`debug`、`execpolicy`、`apply`、`resume`、`queue`、`archive`/`unarchive`/`delete`、`migrate-rollouts`、`fork`、`cloud`、`exec-server`、`features` 等。TUI 入口是 `tui/src/main.rs`（薄壳，调 `codex_tui::run_main`，内部再拉起进程内 app-server）。

---

## 1. 状态：session / rollout 体系、resume、checkpoint

### 1.1 rollout = 会话级 JSONL 落盘（append-only，可 jq 直接读）

- 落盘格式：**JSONL**，每行一个 `RolloutLine { timestamp, ordinal, item: RolloutItem }`，解析器 `codex-rs/rollout/src/lib.rs:52-84`（`decode_rollout_line`，模块注释明确这是持久化边界的 serde 浮点 workaround）。首行为 `SessionMeta`（含 git 信息、originator、forked_from_id 等，见 `recorder.rs` 的 `RolloutRecorderParams::Create` 字段列表，`codex-rs/rollout/src/recorder.rs:91-120`）。
- 文件路径：`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<UTC时间戳>-<thread_id>.jsonl`。目录常量 `SESSIONS_SUBDIR: &str = "sessions"`（`codex-rs/rollout/src/lib.rs:86`），年/月/日三级目录扫描见 `codex-rs/rollout/src/list_files.rs:145-157`（`for (_, year_path) in … for (_, month_path) … for (_, day_path)`）。归档目录 `archived_sessions`（lib.rs:87）。文件名解析/渲染 `codex-rs/rollout/src/rollout_file_name.rs:39-74`，时间戳格式 `[year]-[month]-[day]T[hour]-[minute]-[second]`。
- 写入是**异步单写者**：`RolloutRecorder` 通过 mpsc `Sender<RolloutCmd>` 把行写入交给后台 `RolloutWriterTask`（`recorder.rs:82-87`），并有 `writer_lock.rs`、`reverse_jsonl_scanner.rs`（倒序扫描做 resume/搜索）、gzip `compression.rs`（冷数据压缩，文件名可带压缩后缀）。
- recorder 模块 doc 原话（`recorder.rs:74-81`）：
  ```rust
  /// Writes canonical session rollout items to JSONL.
  /// Rollouts are recorded as JSONL and can be inspected with tools such as:
  /// $ jq -C . ~/.codex/sessions/rollout-2025-05-07T17-24-21-<uuid>.jsonl
  ```
- **不可变 rollout + revert 语义**：fork/revert 不改写旧文件，而是新开一个 rollout 文件、thread_id 保持稳定：`rollout_file_name.rs:12-14` 注释 "Ordinary rollout filenames encode one ID… Filenames for reverted threads append an underscore and a distinct rollout ID"；`recorder.rs:96-101` "thread/revert sets it, producing `rollout-<ts>-<conversation_id>_<rollout_id>.jsonl`, because revert keeps the thread ID stable while creating a new immutable rollout file"。
- **SQLite 双层索引**（较 0.166 演化）：JSONL 之外有 `state_db`（`codex-rs/rollout/src/state_db.rs`，"Core-facing handle to the SQLite-backed state runtime"，基于 `codex-state` crate），保存线程元数据、分页历史（`migrate-rollouts` 子命令 = "Inspect or migrate legacy local sessions to paginated thread history"，`cli/src/main.rs:216-217`）、memories 的 stage-1 输出等。启动时做 metadata 回填（state_db.rs `init`）。

### 1.2 resume / fork / queue

- 历史重建类型：`codex-rs/history/src/lib.rs:388-403`：
  ```rust
  pub struct ResumedHistory { pub conversation_id: ThreadId, pub history: Arc<Vec<RolloutItem>>, … pub rollout_path: Option<PathBuf> }
  pub enum InitialHistory { New, Cleared, Resumed(ResumedHistory), Forked(Vec<RolloutItem>) }
  ```
  即 resume = 反序列化 rollout JSONL → `Vec<RolloutItem>` 原样回放为下一轮请求的 input（含压缩项 `CompactedItem` / `CompactionResumeMetadata`，同文件 re-export）。
- CLI 层：`codex resume`（picker 默认、`--last` 续最近一条、`--include-non-interactive`，`cli/src/main.rs:204-205,355-369`）、`codex fork`（从任一点分叉新线程）、`codex queue`（向既有会话排队消息，`main.rs:207-208`）。会话列表/搜索由 `rollout/src/list.rs`、`search.rs`（对 JSONL 做内容搜索）与 `session_index.rs` 支撑。

### 1.3 "checkpoint" 概念辨析

- 代码中 `checkpoint` 有两义，**都不是**"git 快照/恢复点"类产品功能：
  1. **压缩检查点**：compaction 产物称为 checkpoint —— `core/src/compact.rs:69-81` `CompactedHistoryMetadata`（"Metadata for a new compaction checkpoint"）；`history/src/compaction_checkpoint.rs`。
  2. **落盘物化点**：`core/src/codex_thread.rs:455-459` `checkpoint_preparation()` → `try_ensure_rollout_materialized(PersistContext::ThreadPreparation)`、`:865` `checkpoint_thread_settings()`（把线程设置写入 state DB）。
- 用户可感知的"回退"是 **backtrack/revert**：TUI 中 Esc-Esc 进入 transcript 浏览、Enter 选定某条历史 prompt 后"revert before the selected prompt and restores its prompt in the composer"，源码 doc：`codex-rs/tui/src/app_backtrack.rs:1-14`（"Backtracking … Confirming a selection reverts before the selected turn and restores its prompt in the composer"），底层对应 rollout 的新不可变文件（见 1.1）。

---

## 2. 记忆：AGENTS.md、压缩、长期记忆

### 2.1 AGENTS.md 发现链（项目根→cwd 拼接；全局层单独加载）

- **项目层**：`codex-rs/core/src/agents_md.rs:1-18` 模块 doc 逐步写明算法：从 cwd 向上找 `project_root_markers`（默认 `.git`）定项目根 → 收集项目根→cwd 路径上**每个** `AGENTS.md` 按序拼接 → **不越过项目根**。多文件拼接分隔符 `"\n\n--- project-doc ---\n\n"`（agents_md.rs:49）。默认文件名常量 `DEFAULT_AGENTS_MD_FILENAME: &str = "AGENTS.md"`，本地覆盖 `AGENTS.override.md`（agents_md.rs:43-45）。总字节预算 `project_doc_max_bytes`（config_toml.rs:336-338）。**注意**：与早期版本"cwd→根逐级"不同，此实现是"根→cwd 顺序拼接"且根由 marker 决定。
- **全局层（~/.codex/AGENTS.md）**：由独立 provider 加载，`codex-rs/codex-home/src/instructions/mod.rs:12-13,41-84` —— 依次探测 `$CODEX_HOME/AGENTS.override.md`、`$CODEX_HOME/AGENTS.md`，取第一个非空文件；带缓存与"失败保留上次成功值"逻辑（`InstructionsState`）。两层最终在 `load_project_instructions`（agents_md.rs:58-116）合并（user instructions + project docs）。
- 项目不可信时直接跳过项目层（agents_md.rs:64-66 `if config.active_project.is_untrusted()`）。

### 2.2 会话内上下文压缩（compaction）：本地/远程/纯预算三路实现

- 触发配置（`codex-rs/config/src/config_toml.rs:182-193`）：`model_auto_compact_token_limit`（token 阈值）、`model_auto_compact_token_limit_scope`（全上下文 vs 当前压缩窗口）、`model_post_turn_compact_threshold_percent`（turn 结束后按可用窗口百分比触发，0-100，0=关闭）。手动 `/compact`、`/recap` 斜杠命令（`tui/src/slash_command.rs:41,44`）。
- 实现族谱（`core/src/`）：`compact.rs`（本地模型摘要式压缩）、`compact_remote_v2*.rs`（**服务端压缩**，较 0.166 演化：把摘要工作交给后端）、`compact_token_budget.rs`（token 预算式：不做摘要、直接开新窗口）。统一生命周期：Pre/Post compact hooks + `TurnItem::ContextCompaction`（`compact_token_budget.rs:26-62` doc："Token-budget compaction skips model/server summarization and installs a fresh context window instead. It is still modeled as compaction so compact hooks … observe the same lifecycle"）。
- 摘要提示词（`codex-rs/prompts/templates/compact/prompt.md`）：
  ```
  You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary
  for another LLM that will resume the task. …
  ```
  下一窗口前缀（`summary_prefix.md`）："Another language model started to solve this problem and produced a summary…"。两常量由 `core/src/compact.rs:61-62` re-export（`SUMMARIZATION_PROMPT`/`SUMMARY_PREFIX`，模板 include_str 进 `codex-rs/prompts/src/compact.rs`）。单条压缩用户消息上限 `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000`（compact.rs:63）。子代理可关闭自动压缩（`AutoCompactionDisabled`，compact.rs:65-67）。

### 2.3 长期记忆（较 0.166 全新：memories 子系统）

v0.166 时代没有长期记忆；main@bfdbf01 已有完整两阶段管线，crate 对 = `codex-rs/memories/read`（读路径：注入、引用解析、遥测分类）与 `codex-rs/memories/write`（写路径：提示词渲染、工件、工作区 diff），运行时编排在 `core/src/memories/`。要点（`memories/README.md`）：

- 存储位置 `$CODEX_HOME/memories/`（`memories/read/src/lib.rs: memory_root()`），工件 `raw_memories.md` + `extensions/` + `rollout_summaries/`（`memories/write/src/lib.rs` `artifacts` 常量块），扩展资源保留 14 天（`RETENTION_DAYS: i64 = 14`）。
- **Phase 1（每线程 rollout 抽取）**：根会话启动时后台异步跑（非 ephemeral、开了 feature、非子代理、state DB 可用才跑）；从 state DB 认领"足够久未活动、在时间窗内"的 rollout，并发（有上限）送模型做**结构化输出**抽取 `{raw_memory, rollout_summary, rollout_slug}`，脱敏后写回 state DB；任务有租约/退避防重复。**Phase 2（consolidation）**：把 raw memories 合并整编（`build_consolidation_prompt`）。
- TUI 有 `/memories` 命令（`tui/src/slash_command.rs:27`）；线程级开关 `SetThreadMemoryMode` 协议事件（`protocol/src/protocol.rs:1136`）。

---

## 3. 工具调用：内置工具、schema、结构化输出、MCP、参数校验

### 3.1 工具如何声明与传给模型（Responses API function calling + 两种新形态）

- 工具统一类型 `ToolSpec`（`codex-rs/tools/src/tool_spec.rs:20-56`），serde 序列化即 Responses API 的 tool JSON：
  ```rust
  pub enum ToolSpec {
      Function(ResponsesApiTool),      // {"type":"function", name/description/parameters/strict/output_schema/defer_loading}
      Namespace(ResponsesApiNamespace), // {"type":"namespace", tools:[…]} —— 工具按命名空间打包
      ToolSearch {…},                   // {"type":"tool_search"} —— 可搜索+按需加载的工具目录
      WebSearch {…},                    // 内置 web_search 工具直传
      Freeform(FreeformTool),           // {"type":"custom"} 自由文本工具（apply_patch 用）
  }
  ```
  `create_tools_json_for_responses_api`（tool_spec.rs:82-93）直接把这些 serde 值塞进请求 `tools` 数组；`create_tools_json_for_responses_lite`（:95-142）把 Function/Freeform 折叠进默认 `functions` 命名空间以省 token。**工具规模治理**：`defer_loading`（延迟加载 schema）+ `tool_search`（模型先搜后载，`tools/src/tool_search.rs`：`ToolSearchEntry::tool_names()` 注释 "Iterate candidate identities without materializing their schemas"）。
- **内置 shell 工具 = `exec_command` + `write_stdin`（unified exec）**：定义在 `codex-rs/core/src/tools/handlers/shell_spec.rs:22-152`。`exec_command` 参数：`cmd`(必填)/`workdir`/`tty`/`yield_time_ms`(默认 10s, 区间 250-30000ms; Windows 10000-30000)/`max_output_tokens`(默认 10000)/`shell`/`login`/`environment_id`，以及沙箱升级参数 `sandbox_permissions`(`use_default`|`with_additional_permissions`|`require_escalated`) + `justification` + `prefix_rule` + `additional_permissions`（shell_spec.rs:225-271）。工具级**结构化输出 schema** `output_schema`（`:106 output_schema: Some(unified_exec_output_schema().into())`），输出对象含 `chunk_id/wall_time_seconds/exit_code/session_id/original_token_count/output`（`:191-223`）。长命令返回 `session_id`，用 `write_stdin` 续写/轮询（`:110-152`）—— PTY 会话模型。Windows 下描述符里内嵌 PowerShell 安全规则（`windows_shell_guidance()`，`:332-337`，如 "Do not compose destructive filesystem commands across shells… pass `-WindowStyle Hidden`"）。
- 其他内置 handler（`core/src/tools/handlers/` 目录）：`apply_patch`、`view_image`、`plan`、`sleep`、`current_time`、`get_context_remaining`、`new_context_window`、`request_permissions`、`request_user_input`、`tool_search`、`mcp`/`mcp_resource`、`multi_agents*`（子代理）、`list_available_plugins_to_install`/`request_plugin_install`、`wait_for_environment`、`dynamic`（动态工具）等；编排/注册在 `core/src/tools/registry.rs`（`IndexMap<ToolName, RegisteredTool>` + 命名冲突检测 `record_collision`，registry.rs:429-617）。
- **apply_patch 是 Freeform（grammar）工具**（较 0.166 演化）：`core/src/tools/handlers/apply_patch_spec.rs:8-26`：
  ```rust
  ToolSpec::Freeform(FreeformTool {
      name: "apply_patch",
      description: "…This is a FREEFORM tool, so do not wrap the patch in JSON.",
      format: FreeformToolFormat { r#type: "grammar", syntax: "lark",
          definition: include_str!("…/assets/tools/apply_patch.lark") }, })
  ```
  即用 Responses **custom tool + Lark 约束文法**让模型输出补丁文本而非 JSON 参数；解析/执行在 `codex-rs/apply-patch/`（`parse_patch`/`StreamingPatchParser`/`maybe_parse_apply_patch`，可独立成命令，`arg0` 分发 `apply_patch`）。
- **最终答案级结构化输出**：请求侧 `text.format = { type: "json_schema", strict, schema, name: "codex_output_schema" }` —— `codex-rs/codex-api/src/common.rs:394-412 create_text_param_for_request`，由 turn 级 `final_output_json_schema` 传入（`core/src/session/turn.rs:1774-1775`，app-server `turn_processor.rs:699` 暴露给调用方）。所以"结构化输出"存在于**两级**：工具结果（output_schema）与回合最终输出（text.format json_schema）。

### 3.2 MCP 支持（客户端 + Codex 自身可当 MCP 服务器）

- 配置：`~/.codex/config.toml` 的 `[mcp_servers.<name>]` —— `codex-rs/config/src/config_toml.rs:297-301` `pub mcp_servers: HashMap<String, McpServerConfig>`。`McpServerConfig`（`config/src/mcp_types.rs:232-299`）支持：stdio 与 streamable-HTTP 两种 transport（`transport` flatten）、`enabled`/`required`（exec 模式初始化失败是否致命）、`startup_readiness`（connection/catalog 缓存先行）、`startup_timeout_sec`/`tool_timeout_sec`、`enabled_tools`/`disabled_tools` 白黑名单、`tool_input_schema_max_bytes`（默认 5000B 压缩输入 schema）、`supports_parallel_tool_calls`、per-server `default_tools_approval_mode`、OAuth 配置（client_id/secret/callback、EMA 企业 IdP：`McpServerAuth::{OAuth, ChatGpt, EmaAuth}`，mcp_types.rs:196-213）。
- 客户端实现：`codex-rs/rmcp-client/`（自研全家桶：`stdio_server_launcher.rs`、`streamable_http_retry.rs`、`oauth.rs`/`oauth_client_registration.rs`、`ema_*.rs` 企业托管认证、`user_verification.rs`/elicitation、`tool_list_changes.rs`、`trace_context.rs`）。凭据可存 OS keyring（`mcp_oauth_credentials_store`，config_toml.rs:307-313）。
- 连接管理与归并：`codex-rs/core/src/mcp.rs`（effective servers = config + 插件贡献 + 内置 apps server，`effective_mcp_servers`/`McpToolCatalogCache` 工具目录缓存）；工具以 `mcp.rs`→`tools/src/mcp_tool.rs` 包装进统一 ToolSpec（可挂 output_schema，`tools/src/output_schema.rs: from_mcp_output_schema`）。工具名冲突有 namespace 处理（`tools/src/functions_namespace_function_prefixes.rs`、registry `first_collision`）。
- Codex 自身可作为 MCP server：`codex-mcp/` crate + `cli/src/mcp_cmd.rs`（`codex mcp add/remove/list/login` 等管理子命令）；`message-board-mcp/` 把代理消息板暴露为 MCP。
- elicitation（服务器反向问用户）有完整支持（`core/src/elicitation.rs`、rmcp-client `elicitation_client_service.rs`）。

### 3.3 参数校验失败的处理

serde 反序列化失败**不打断回合，错误文本回喂模型**：`codex-rs/core/src/tools/handlers/mod.rs:90-97`：
```rust
pub(crate) fn parse_arguments<T>(arguments: &str) -> Result<T, FunctionCallError> {
    serde_json::from_str(arguments).map_err(|err| FunctionCallError::RespondToModel(
        format!("failed to parse function arguments: {err}")))
}
```
错误二分（`codex-rs/tools/src/function_call_error.rs:4-10`）：`RespondToModel(String)`（可恢复，回给模型自纠）vs `Fatal(String)`（终止）。语义级校验同理，如 `justification` 必须伴随显式 `sandbox_permissions`（handlers/mod.rs:99-110）。

---

## 4. 扩展机制：prompts→skills、profiles、插件、hooks、多 agent、exec

- **custom prompts 已被 skills 取代（较 0.166 演化，硬证据）**：`tui/src/slash_command.rs` 的 `SlashCommand` 枚举（:12-89）**没有** Custom 变体；全仓 grep 无 `~/.codex/prompts` 加载逻辑。取而代之是 `codex-rs/skills/` crate：技能放在 `$CODEX_HOME/skills/`（`SKILLS_DIR_NAME`）+ 内置系统技能解包到 `skills/.system`（`skills/src/lib.rs:41-56 install_system_skills`，include_dir 内嵌）；带 frontmatter 元数据/依赖/接口（`parser.rs`/`metadata.rs`）；调用方式 = 显式 mention 或**隐式识别**（模型跑 shell 命令读技能文档/执行技能脚本时被 `detect_implicit_skill_invocation_for_command` 识别，`skills/src/invocation.rs:29-43`，Windows 用 PowerShell 分词器）。插件也能贡献技能（plugin skill roots）。
- **profiles**：`[profiles.<name>]` 表（`config/src/config_toml.rs:364-369`），`ConfigProfile`（`config/src/profile_toml.rs:24+`）可覆盖 model/provider/approval_policy/sandbox_mode/reasoning/verbosity 等；`--profile work` 选择（`ProfileV2Name` 校验：`protocol/src/config_types.rs:143-159` 只允许字母数字和 `_-`）。另有一套 v2 按 `$CODEX_HOME/<name>.config.toml` 独立文件的形式（config_types.rs:116 doc）。
- **插件系统已存在（较 0.166 演化，"无插件架构"的说法过时）**：`plugin/`+`plugin-host/`+`core-plugins/`+`core-plugin-common/` + CLI `codex plugin` 子命令与 `marketplace_cmd.rs`；支持 npm 源、bundle 归档、远程目录、marketplace add/remove/upgrade、安装度量（`core-plugins/src/` 模块名 npm_source/plugin_bundle_archive/marketplace_*）。模型侧还有 `request_plugin_install` 工具（handlers 目录）。
- **hooks**：`codex-rs/hooks/`，14 个生命周期事件（`hooks/src/lib.rs:29-44`）：`PreModelRequest/PostModelRequest/PreToolUse/PermissionRequest/PostToolUse/PreCompact/PostCompact/SessionStart/SessionEnd/UserPromptSubmit/SubagentStart/SubagentStop/Stop/Interrupt`；PreToolUse 等 9 个支持 matcher（:51-53）。hook 可改写命令（`updatedInput`，见 handlers/mod.rs:112-129）。
- **多 agent/编排**：核心是 `core/src/tools/handlers/multi_agents*.rs`（`MULTI_AGENT_V1_NAMESPACE`，spawn/manage sub-agents；子代理继承 turn 的 provider/approval/sandbox/cwd 再叠角色配置，模块 doc `multi_agents.rs:1-7`），深度上限 `exceeds_thread_spawn_depth_limit`；配套 crate `agent-roles/`、`message-board/`（代理间消息板）、`agent-graph-store/`、`runtime-agent-instance/`。`codex_delegate.rs` 为委托入口。云侧编排走 `cloud-tasks*`（`codex cloud` 子命令：浏览 Codex Cloud 任务并本地 apply，cli/src/main.rs:228-230）。
- **`codex exec` 非交互模式**：单轮/脚本化运行，stdout 契约严格（`exec/src/lib.rs:1-6`："In the default output mode… the only thing written to stdout is the final message… In --json mode, stdout must be valid JSONL, one event per line"，`#![deny(clippy::print_stdout)]`）。底层走进程内 app-server client（lib.rs:19-38）。用途 = CI/脚本/管道编排单元（配合 `--json` 事件流做上游编排器）。
- 其他扩展点：`config.toml` `notify`（外部通知命令，config_toml.rs:247-249）、`[permissions]` 命名权限档（:238-245）、worktree 会话（`worktree/` crate + `/worktree` 命令）、`code-mode*`（把代码执行/环境当一等模式）、`connectors/`（外部连接器）。

---

## 5. HITL：approval 全档位、sandbox 正交、exec 绕过、Esc 中断

### 5.1 AskForApproval 档位（较 0.166 演化：on-failure 已并入 on-request）

定义：`codex-rs/protocol/src/protocol.rs:1163-1186`：
```rust
pub enum AskForApproval {
    UnlessTrusted,          // serde "untrusted"：项目不可信时命令一律要批（除非 execpolicy 规则放行）
    #[serde(alias = "on-failure")] #[default]
    OnRequest,              // 模型自行决定何时请求批准（旧值 "on-failure" 现在是它的别名！）
    Granular(GranularApprovalConfig),  // 细粒度开关（experimental "askForApproval.granular"）
    Never,                  // 从不询问；失败直接回给模型，不升级到用户
}
```
`GranularApprovalConfig`（:1188-1204）五开关：`sandbox_approval`（沙箱升级/require_escalated 请求）、`rules`（execpolicy prompt 规则）、`skill_approval`（技能脚本执行）、`request_permissions`、`mcp_elicitations`；false = 该类请求**自动拒绝**而非弹出。**注意**：v0.166 的四档 `untrusted/on-failure/on-request/never` 在 main 上已变为 `untrusted/on-request(+on-failure 别名)/granular/never`，写报告时必须用新口径。
- 审批路由（谁审）：`approvals_reviewer` 配置（user/auto_review/guardian_subagent，`protocol/src/config_types.rs:175-179`）。

### 5.2 sandbox_mode 与 approval 的正交关系

- `SandboxMode`（`protocol/src/config_types.rs:104-114`）：`read-only`（**默认**）/`workspace-write`/`danger-full-access`；细粒度 `SandboxPolicy`（`protocol/src/protocol.rs:1250-1298`）另含 `external-sandbox{network_access}` 与 `workspace-write{writable_roots, network_access, exclude_tmpdir_env_var, exclude_slash_tmp}`。正交性：sandbox 决定"命令能干什么"，approval 决定"越界时问不问用户"。矩阵证据：
  - 初始是否要批：`core/src/tools/sandboxing.rs:200-236 default_exec_approval_requirement`——`Never`→不批；`OnRequest|Granular`→仅当文件系统策略为 Restricted 才可能批；`UnlessTrusted`→总是批。
  - **沙箱失败→脱沙箱重试（经典 on-failure 行为现在长这样）**：`core/src/tools/orchestrator.rs`——首次沙箱内执行，捕获 `SandboxErr::Denied` 后走 `escalate_on_failure()`（`tools/sandboxing.rs:361-366` 默认 true），门控 `wants_no_sandbox_approval`（:349-356：UnlessTrusted→true、Never→false、OnRequest→false、Granular→看 sandbox_approval）；允许时弹"command failed; retry without sandbox?"并把重试设为 `SandboxType::None`。orchestrator.rs:446-449 注释："Under `Never` or `OnRequest`, do not retry without sandbox; surface a concise sandbox denial"。
  - **防绕道**：策略含 denied-reads 时禁止脱沙箱升级（`tools/sandboxing.rs:294-298`："Denied reads only exist inside the sandbox… bypassing the sandbox would silently grant those reads"→改用 `EscalatedSandboxWithRestrictions`）。
  - apply_patch 走 `assess_patch_safety`（`core/src/safety.rs:67-125`）：补丁限定在可写根且（无沙箱可用或 profile Disabled/External）→AutoApprove；Never/Granular 关 sandbox_approval→Reject（"writing is blocked by read-only sandbox; rejected by user approval settings"，:15-16）；否则 AskUser。
- 预设三档（`codex-rs/utils/approval-presets/src/lib.rs:28-60`）：`read-only`（OnRequest+只读 profile）、`auto`（默认，OnRequest+workspace-write）、`full-access`（Never+`PermissionProfile::Disabled`）。
- untrusted 的规则引擎 = **execpolicy**（`codex-rs/execpolicy/`）：Starlark `prefix_rule(pattern=[…], decision= allow|prompt|forbidden, justification, match/not_match)` + `host_executable`（`execpolicy/README.md:3-15`），Rust 原生 starlark 解释器（Cargo.toml 依赖 `starlark`；早期版本的 wasm 方案已不在）。

### 5.3 exec 模式绕过交互 & 危险开关

- headless 默认**不询问**：`codex-rs/exec/src/lib.rs:595-598`："// Default to never ask for approvals in headless mode." `approval_policy: Some(AskForApproval::Never)`；沙箱仍按 `--sandbox` 参数执行（lib.rs:352-355）。
- `--dangerously-bypass-approvals-and-sandbox`：同时强制 `Never` + `DangerFullAccess`（`cli/src/main.rs:2084-2089`），且与显式 approval 参数冲突会报错（main.rs:3233 测试 `dangerous_bypass_conflicts_with_approval_policy`）；resume 时也合并该旗标（:4145）。
- 交互侧的中断/恢复：Esc → `AppCommand::interrupt()` → app-server `turn_interrupt` → core `Session::interrupt_task()` → `abort_all_tasks(TurnAbortReason::Interrupted)`（`core/src/session/mod.rs:5784-5787`，状态机 `AgentStatus::Interrupted` :1535）；有 `Feature::InstantInterrupt` 快速中断开关（:4410）。**Esc-Esc backtrack**：浏览历史 prompt、Enter 回退到该点重开编辑（`tui/src/app_backtrack.rs:5-14`，见 §1.3）。恢复 = resume/fork 天然由 rollout 支撑。

---

## 6. 可观测性：OTel、日志、云端关联

- **OTel 全家桶 crate `codex-rs/otel/`**（`codex_otel::OtelProvider` 聚合 logger/tracer/metrics 三个 provider；`otel/src/provider.rs:64-71`）。导出器枚举 `OtelExporter::{None, Statsig, OtlpGrpc, OtlpHttp}`（`otel/src/config.rs:88-106`），**无 stdout/file 导出器**；默认 metrics 走内置 Statsig OTLP 端点 `https://ab.chatgpt.com/otlp/v1/metrics`（config.rs:9-11，debug 构建关闭）。启用全靠 config.toml `[otel]`（`config/src/types.rs:637-672`：`exporter/trace_exporter/metrics_exporter/span_attributes`，隐私开关 `log_user_prompt` 默认 false、`log_agent_responses` 默认 false 且截 64KiB），没有 `OTEL_CODEX*` 专有 env。装配在 `core/src/otel_init.rs`，TUI/app-server/exec 各自挂 layer（`tui/src/startup_orchestration.rs:957-964` 的 registry 链）。事件/指标样例：`codex.user_prompt`（默认 `[REDACTED]`）、`codex.tool_decision`、`codex.sandbox_outcome`、`codex.turn_ttft`、`codex.turn_cost_microusd`、`codex.api_request`（`otel/src/events/session_telemetry.rs`、`metrics/names.rs`）。
- **产品分析（独立于 OTel）`codex-rs/analytics/`**：后台 mpsc 队列批量 POST 到 `{chatgpt_backend}/codex/analytics-events/events`，仅 ChatGPT 登录可用（API key 鉴权只放行插件/技能/MCP 等非敏感事件，`analytics/src/events.rs:167-177`）；`[analytics] enabled=false` 一键关（`config/src/types.rs:250-254`），TUI/exec 默认开、app-server 默认关。git URL 走 `SanitizedGitUrl` 脱敏（`analytics/src/reducer.rs:1461`）。
- **会话日志三层**：① rollout JSONL（§1.1，完整会话真相源）；② `$CODEX_HOME/log/` 文件日志——TUI 仅在显式配置 `log_dir` 时写 `codex-tui.log`（`tui/src/startup_orchestration.rs:882-916`，默认 filter `codex_core=info,codex_tui=info,…`；旧的无界共享日志启动时删除 `tui/src/lib.rs:425-430`）；login 写 `codex-login.log`（`cli/src/login.rs:86`）；③ **SQLite logs 库**（`state/src/log_db.rs`：tracing Layer→`LogEntry`→有界队列→独立 logs DB），供 `/feedback` 打包上报（`feedback/src/lib.rs`、app-server `feedback_processor.rs:201-217` 生成 `codex-logs.log` 附件）。
- **rollout-trace**：`codex-rs/rollout-trace/`——env `CODEX_ROLLOUT_TRACE_ROOT` 开启后把每回合的请求/响应/工具 IO 写成本地 bundle（manifest.json+trace.jsonl+payloads/），README 明示"Rollout tracing is not telemetry. Codex does **not** upload or report these traces"（本地调试用，`codex debug trace-reduce` 可归约）。
- **云端关联**：`backend-client/`（ChatGPT Codex 后端 HTTP：用量/费率/配置包，`Authorization: Bearer` + `chatgpt-account-id` 头，`backend-client/src/client.rs:305`）；`cloud-client/`（云线程 ThreadService gRPC，Resume/Attach 云端历史，README:1-9）；遥测与账号关联通过 `to_auth_headers()` 复用同一 ChatGPT 会话，SessionTelemetry 携带 `account_id/user_id/originator`（`otel/src/events/session_telemetry.rs:99-120`）。`otel-trace-websocket/` 是浏览器 trace 查看器桥（loopback OTLP→WebSocket 转发，拒绝带 Origin 的请求），非上报通道。

---

## 7. 部署与许可证、模型接入、沙箱三平台、认证

### 7.1 许可证与分发

- **Apache-2.0**（根 `LICENSE`；`codex-rs/Cargo.toml:179`）。分发：npm 包 `codex-cli`（`codex-cli/package.json`，version 由 CI 注入 `0.0.0-dev`）、原生二进制（含 brew 等安装渠道，`docs/install.md`）。单二进制多 arg0：`codex`/`apply_patch`/`codex-linux-sandbox`/`bwrap`/windows sandbox helper 都从同一可执行文件按 argv[0] 重派（`arg0/src/lib.rs:17-30`）。

### 7.2 模型接入层（model_providers / wire_api / 内置 provider / OSS）

- 自定义 provider：`[model_providers.<id>]`（`config/src/config_toml.rs:331-334`，"User-defined provider entries that extend the built-in list. Built-in IDs cannot be overridden"）。`ModelProviderInfo`（`model-provider-info/src/lib.rs:148-236`）字段：`base_url`、`env_key`（API key 的环境变量名）、`wire_api`、`http_headers`/`env_http_headers`、重试/流超时（`request_max_retries`/`stream_max_retries`/`stream_idle_timeout_ms`）、`requires_openai_auth`、capabilities 覆盖等。
- **wire_api 只剩 `responses`（较 0.166 重大演化）**：`model-provider-info/src/lib.rs:109-143`——
  ```rust
  const CHAT_WIRE_API_REMOVED_ERROR: &str = "`wire_api = \"chat\"` is no longer supported.\nHow to fix: set `wire_api = \"responses\"` …";
  pub enum WireApi { #[default] Responses }
  ```
  即 **Chat Completions 网关已被移除**（v0.166 时代 openai/chat 双协议并存，2025-10 起废弃 chat，讨论帖 openai/codex#7782）。第三方网关必须实现 Responses 兼容端点。
- 内置 provider 表（同文件构造函数）：`openai`（`create_openai_provider` :568-614，默认走 ChatGPT 后端 `https://chatgpt.com/backend-api/codex`，`:86 CHATGPT_CODEX_BASE_URL`，`requires_openai_auth=true`、支持 WebSocket transport）、`amazon-bedrock` / `amazon-bedrock-runtime`（SigV4 + Mantle 端点，:616-657）、`gcp`（Vertex，:667-696）。模型清单不再硬编码：`models-manager/` 从后端拉**模型目录**并本地缓存（含 TTL 与 offline 策略，`models-manager/src/cache.rs/catalog.rs`）。
- **OSS/开源模型**：`--oss` 走本地推理——`codex-rs/ollama/`（默认拉 `gpt-oss:20b`，`ollama/src/lib.rs:12 DEFAULT_OSS_MODEL`，缺模型自动 pull）与 `codex-rs/lmstudio/`（默认 `openai/gpt-oss-20b`，自动下载）；旧 `ollama-chat` provider 已删除（`model-provider-info/src/lib.rs:110-111 OLLAMA_CHAT_PROVIDER_REMOVED_ERROR`）。两者能接入的前提正是 Ollama/LM Studio 实现 Responses 兼容 API。

### 7.3 沙箱三平台（macOS Seatbelt / Linux bwrap+seccomp(landlock 遗留) / Windows 受限令牌）

- 总开关/后端选择：`codex-rs/sandboxing/src/manager.rs:50-64 get_platform_sandbox`——macOS→`MacosSeatbelt`、Linux→`LinuxSeccomp`、Windows→`WindowsRestrictedToken`（仅当启用）。`SandboxType { None, MacosSeatbelt, LinuxSeccomp, WindowsRestrictedToken, WindowsMxc }`（`protocol/src/sandbox.rs:29-35`）。
- **macOS**：Seatbelt，策略=4 个内嵌 `.sbpl` 片段（`include_str!`，`sandboxing/src/seatbelt.rs:20-27`）+ 运行时按可写根/网络拼装 profile；基线片段 `seatbelt_base_policy.sbpl:3-13` "inspired by Chrome's sandbox policy… (deny default) (allow process-exec) (allow process-fork)"；网络默认**fail closed**（`dynamic_network_policy_for_network` 注释 "Fail closed to avoid silently widening network access"）；执行 `/usr/bin/sandbox-exec -p <profile> …` 且**硬编码绝对路径**防 PATH 注入（seatbelt.rs:57-61 注释）。
- **Linux**：默认 **bubblewrap（命名空间）+ seccomp**；landlock 降级为遗留路径且 fs 受限策略下直接拒绝（`linux-sandbox/README.md:47-49`："Filesystem-restricted execution requires bubblewrap. The legacy Landlock option is rejected…"，feature `use_legacy_landlock` 标记 Deprecated）。helper = 独立二进制 `codex-linux-sandbox`（`linux-sandbox/`，由 arg0 同体重派），参数见 `sandboxing/src/landlock.rs:16-25`（`--sandbox-policy-cwd --permission-profile <json> …`）；seccomp 细节（`linux-sandbox/src/seccomp.rs`）：`PR_SET_NO_NEW_PRIVS`、封 io_uring（"io_uring can create AF_VSOCK sockets without a socket() syscall"）、封 AF_VSOCK（防 WSL2 互操作套接字逃逸）、`--apply-seccomp-then-exec` 先 bwrap（可能 setuid）后收紧。系统 bwrap 太老/缺失时用**内置编译的 bwrap**（`codex-rs/bwrap/` 直接编 vendor/bubblewrap C 源，build.rs `define("main","bwrap_main")`）；**WSL1 不支持**（README:25-28）。
- **Windows**：从"experimental"进阶为**三后端可选但默认关闭**：`core/src/windows_sandbox.rs:125-143` 由 `[windows] sandbox = "elevated"|"unelevated"|"mxc"` 或 feature 决定，默认 `Disabled`（旧 feature `experimental_windows_sandbox`/`elevated_windows_sandbox` 已标 `Stage::Removed`，`features/src/lib.rs:1540-1557`；新后端 mxc = 微软 MXC 容器，`mxc-sandbox/README.md`，feature `windows_sandbox_service`/`prefer_mxc` 均 UnderDevelopment）。实现是完整的（`windows-sandbox-rs/`：受限令牌/拒绝读 ACL/AppContainer/WFP 网络过滤/ConPTY；`windows-sandbox-service/` 为 Windows 服务）。**默认关闭时的降级行为有明确代码**：`config/src/config_toml.rs:834-852 effective_sandbox_mode`——Windows + Disabled + `workspace-write` → **降级为 `read-only`**（测试表 :1077-1086；`cli/src/debug_sandbox.rs:1033` "workspace-write downgrades to read-only when the Windows sandbox is disabled"）。且受限令牌后端无法表达的限制会**拒绝执行而非裸奔**（`sandboxing/src/windows.rs:100-119` "…refusing to run unsandboxed"）。
- **external-sandbox**：表示"进程已在容器/K8s 等外部沙箱内"，不来自 env 探测、不走 config.toml，仅程序化/API 注入（`protocol/src/protocol.rs:1264-1271`；`config/src/config_requirements.rs:1632` 注释 "Currently, `external-sandbox` is not supported in config.toml, but it is supported through programmatic use"）。此时不再套 Seatbelt/bwrap（`sandboxing/src/policy_transforms.rs:646-666 should_require_platform_sandbox` 对 External 返回 false）。

### 7.4 认证：ChatGPT OAuth 与 API key 两条路（+ 企业路）

- 凭据文件 `$CODEX_HOME/auth.json`（可迁 OS keyring：`cli_auth_credentials_store = file|keyring|auto`，config_toml.rs:290-295）。结构 `AuthDotJson`（`login/src/auth/storage.rs:52-77`）：`auth_mode`、`OPENAI_API_KEY`、`tokens: TokenData`（OAuth access/refresh）、`last_refresh`、以及 PAT/Bedrock/agent-identity 等扩展位。
- 路线一 **ChatGPT OAuth（推荐主路）**：`codex login` 走 PKCE + 本地回调 server（`login/src/oauth*`、`server.rs run_login_server`；设备码流程备选 `device_code_auth.rs`），token 定期刷新；运行时枚举 `CodexAuth`（`login/src/auth/manager.rs:110-119`）`{ApiKey, Chatgpt, ChatgptAuthTokens, Headers, AgentIdentity, PersonalAccessToken, BedrockApiKey, BedrockAccessKeys}`。OAuth 登录后推理走 `chatgpt.com/backend-api/codex`（CHATGPT_CODEX_BASE_URL），与 ChatGPT 订阅额度绑定；workspace 可锁定（`forced_chatgpt_workspace_id`，config_toml.rs:282-284）。
- 路线二 **API key**：`codex login --api-key` / `OPENAI_API_KEY` env（`cli/src/login.rs:257 run_login_with_api_key`；api_key 存 auth.json 的 `OPENAI_API_KEY` 字段）。自定义 provider 则用 `env_key` 指定各自的环境变量（ModelProviderInfo.env_key）。
- 企业路：Bedrock SigV4（`aws-auth`）、GCP ADC、MCP EMA（企业 IdP refresh token 换资源授权）。两种登录可被 `forced_login_method` 限制（config_toml.rs:286-288）。

---

## 附：与 v0.166.0 口径的差异清单（写报告时的防错提示）

| 主题 | v0.166.0 时代常识 | main@bfdbf01 实况（证据） |
|---|---|---|
| wire_api | openai/responses 双协议 | 仅 `responses`，`"chat"` 报错（model-provider-info/src/lib.rs:109） |
| approval 档位 | untrusted/on-failure/on-request/never | untrusted/on-request(吸收 on-failure 为 serde 别名)/granular(实验)/never（protocol/src/protocol.rs:1163-1186） |
| 自定义斜杠命令 | `~/.codex/prompts/*.md` | 已无此加载；由 skills（`$CODEX_HOME/skills/`）承担（skills/src/lib.rs；slash_command.rs 无 Custom） |
| 插件系统 | 无（架构取舍点） | 已有完整 plugin+marketplace（core-plugins/、`codex plugin` 子命令） |
| 长期记忆 | 无 | memories 两阶段管线（memories/README.md） |
| 压缩 | 本地摘要 | 本地摘要 + 服务端 compact_remote_v2 + token-budget 开新窗三路（core/src/compact*） |
| execpolicy | wasm/starlark 混合 | Rust 原生 starlark crate（execpolicy/Cargo.toml） |
| 内置 shell 工具 | `shell`（container.exec） | `exec_command`+`write_stdin` unified exec，带 output_schema（tools/handlers/shell_spec.rs:88-107） |
| apply_patch | 函数工具 | Freeform custom tool + Lark 文法（apply_patch_spec.rs:9-26） |
| Windows 沙箱 | 不支持 | 三后端（restricted-token/elevated/MXC）可选、默认 Disabled、workspace-write 降级 read-only（config_toml.rs:834-852） |
