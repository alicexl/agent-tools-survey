# Codex CLI 调研卡

> 调研时点 2026-11-03。版本锚：最新稳定版 `rust-v0.166.0`（2026-10-29 发布，0.167.0 alpha 已滚动至 alpha.4）；仓库 openai/codex，Apache-2.0。星数：repo 页本次实抓显示 132.1k（任务书给定 132,982，同一量级，日常波动）；12,940 commits、最新 commit 2026-10-31（当日活跃已核实）。所有论断附来源；文档站版本迭代极快，本卡以 2026-11 官方现行文档为准，已剔除过时口径（见 1.5/1.7 两处重要修正）。

## 0 身份与分层归属

**一句话定位**：OpenAI 官方开源的终端 coding agent——"Codex CLI is a coding agent from OpenAI that runs locally on your computer"（README）/ "you can run locally from your terminal … open source and built in Rust"（官方 CLI 页）。

**归层：Agent 运行时 / Harness 层**。依据：
- 它是一个自包含的执行框架（Rust 核心 `codex-rs/` 136 个 crate + TS/npm 壳 `codex-cli/` + SDK `sdk/`），自带 TUI、工具循环、沙箱、审批、会话持久化——是"跑 agent 的壳"，不是供二次开发嵌入的库，也不是托管服务。
- 注意与 **Codex 云端产品线区分**（README 原文明确划界）：Codex 家族 = CLI（本卡）+ 桌面 App（`codex app`）+ IDE 插件（VS Code/Cursor/Windsurf）+ **Codex Web**（chatgpt.com/codex，云端 agent，非本卡对象）+ Codex cloud tasks（`codex cloud` 从 CLI 提交云端任务）。四形态共用 `~/.codex/config.toml` 一份配置（DeepSeek 官方接入文档证实）。
- 来源：https://github.com/openai/codex ；https://developers.openai.com/codex/cli

## 1 七维度逐项

### 1.1 状态：session 持久化与断点恢复 —— **结论：完整且是一等公民**

- 交互会话本地落盘为 rollout 文件，目录 `~/.codex/sessions/`（官方 CLI Features 页原文："You can copy the ID from the picker, `/status`, or the files under `~/.codex/sessions/`"）。
- 恢复入口：`codex resume`（选择器）、`codex resume --all`（跨目录）、`codex resume --last`、`codex resume <SESSION_ID>`；**非交互 run 也可恢复**：`codex exec resume --last "…"` / `codex exec resume <UUID> "…"`。恢复时保留原 transcript、plan history 与 approvals（原文："Each resumed run keeps the original transcript, plan history, and approvals"）。来源：https://developers.openai.com/codex/cli/features
- 会话管理命令：`/fork`（分叉）、`/side`（临时旁路对话）、`codex fork`、`/archive` + `codex unarchive <SESSION>`、`/delete`（永久删除含派生会话）。来源：https://developers.openai.com/codex/cli/slash-commands
- 相关持久化配置：`history.persistence = save-all|none`（history.jsonl 转写文件）、`sqlite_home`（SQLite 状态库，存 agent jobs 等可恢复运行态）。来源：https://developers.openai.com/codex/config-reference
- 源码入口：`codex-rs/rollout/`、`codex-rs/history/`、`codex-rs/thread-store/`、`codex-rs/state/`（目录名经 GitHub API 实抓核实）。

### 1.2 记忆 —— **结论：AGENTS.md 分层指令 + Memories 长期记忆（默认关）**

- **项目记忆 = AGENTS.md**：`/init` 在当前目录生成 AGENTS.md 脚手架（"Capture persistent instructions for the repository or subdirectory"）。配置键：`project_doc_max_bytes`（读取上限）、`project_doc_fallback_filenames`（缺失时的替代文件名）、`model_instructions_file`（整体替换内置指令）。来源：slash-commands 页 + config-reference。
- **全局记忆 `~/.codex/AGENTS.md`**：官方 Configuration > AGENTS.md 专页本次连续抓取失败（`developers.openai.com/codex/agents` ×3、learn.chatgpt.com 镜像 ×1、GitHub repo 内 `docs/agents_md.md` 已改为一行外链占位），合并顺序（全局→仓库根→子目录）未能逐字核对——**待核**。实现入口可定位：`codex-rs/core/src/agents_md.rs`、`agents_md_manager.rs`（文件路径经 GitHub API 实抓核实）。
- **长期记忆 = Memories**：`features.memories`（**默认 off**）；`memories.*` 一族参数控制按线程提取（`extract_model`）与全局整合（`consolidation_model`、`max_rollout_age_days` 默认 30、`min_rollout_idle_hours` 默认 6 等）；TUI 内 `/memories` 开关注入/生成。概念页 "Memories > Chronicle" 在文档导航中在列。企业侧 `requirements.toml` 可 `features.memories` 强制 pin。来源：https://developers.openai.com/codex/config-reference ；导航树见 https://developers.openai.com/codex/cli

### 1.3 工具调用 —— **结论：小而硬的内置集 + MCP 一等支持；协议=Responses API**

- 内置工具：shell（`features.shell_tool` 默认 on；`features.unified_exec` PTY 统一执行，默认开、Windows 除外）、`apply_patch` 文件编辑（`codex-rs/apply-patch/`、`core/src/apply_patch.rs`；OpenAI API 工具页亦有独立 Apply Patch 工具）、`web_search`（`disabled|cached|live`，默认 cached=OpenAI 维护索引，不抓活页）、`view_image`、多 agent 工具组（见 1.4）、图片输入（`-i/--image`）与图片生成（gpt-image-2，`$imagegen`）。来源：features 页 + config-reference。
- **MCP 支持**：`config.toml` 中 `[mcp_servers.<id>]`——stdio（`command/args/env/cwd/env_vars`）与 streamable HTTP（`url/http_headers/bearer_token_env_var`）双形态；`enabled_tools`/`disabled_tools` 工具级 allow/deny；`tools.<tool>.approval_mode`（auto|prompt|approve）逐工具审批；MCP OAuth（`scopes`、`mcp_oauth_callback_port/url`、`mcp_oauth_credentials_store`）；`startup_timeout_sec`（默认 10s）、`tool_timeout_sec`（默认 60s）、`required`（失败即阻止启动）。管理入口 `codex mcp` CLI 子命令 + TUI `/mcp [verbose]`。Codex 自身还可作为 MCP server 供其他 agent 调用（文档 Automation > MCP Server；crate `codex-rs/codex-mcp/`）。来源：https://developers.openai.com/codex/cli/features ；https://developers.openai.com/codex/config-reference
- **类型校验方式**：工具定义走 OpenAI **Responses API function-calling**（JSON Schema；`model_providers.<id>.wire_api` 官方注明 "`responses` is the only supported value"）；config.toml 自身有官方 JSON Schema（`https://developers.openai.com/codex/config-schema.json`，配 Even Better TOML 做编辑器校验），crate `codex-rs/config-schema/`。来源：config-reference。

### 1.4 扩展机制 —— **结论：custom prompts 已弃用，收敛到 Skills/Plugins/Hooks/MCP 四通道；多 agent 原生**

- **custom prompts（旧机制）**：`~/.codex/prompts/` 顶层 `.md` → `/prompts:name` 调用。**0.89.0（2026-01）官方弃用**；后续版本出现不再显示的回归（issue #15941），社区正在请求 Claude Code 式 `.codex/commands/` 替代（issue #51174，2026-10）。crate `codex-rs/prompts/` 仍在（遗留）。来源：https://github.com/openai/codex/issues/15941 ；https://github.com/openai/codex/issues/51174 ；https://www.reddit.com/r/CodexAutomation/comments/1qkk2sr/
- **现行扩展四通道**（均为官方文档一级页面）：
  1. **Skills**：SKILL.md 文件夹（`skills.config[].path` 逐个启停；skill 脚本有独立审批 `granular.skill_approval`；`features.skill_mcp_dependency_install` 允许装 MCP 依赖；TUI `/skills`）。
  2. **Plugins**：插件可捆绑 MCP server（`plugins.<plugin>.mcp_servers.*` 逐服务器 allow/deny/审批），插件市场（marketplace）可配 Git 源，企业可 `marketplaces.restrict_to_allowed_sources` 白名单化；TUI `/plugins` 浏览/启停。
  3. **Hooks**：`hooks.json` 或 config 内联 `[hooks]`，事件含 PreToolUse/PermissionRequest/PostToolUse/PreCompact/PostCompact/SessionStart/SubagentStart/SubagentStop/UserPromptSubmit/Stop；企业可 `allow_managed_hooks_only` 只留托管 hooks；TUI `/hooks` 审查与信任。
  4. **MCP**：见 1.3。
  来源：https://developers.openai.com/codex/config-reference ；https://developers.openai.com/codex/cli/slash-commands
- **多 agent / 编排：有，且已稳定默认开启**。`features.multi_agent`（stable，**默认 on**）提供 `spawn_agent / send_input / resume_agent / wait_agent / close_agent` 五个工具；`[agents.<name>]` 定义角色（`description` 角色引导、`config_file` 角色级配置层、`nickname_candidates`）；`agents.max_threads` 默认 6、`agents.max_depth` 默认 1（root 为 0）；另有 `spawn_agents_on_csv` 批处理作业（`agents.job_max_runtime_seconds` 默认 1800s）。TUI `/agent` 切换线程。官方明确"只在显式要求时才 spawn 子代理，token 消耗高于单代理"。来源：config-reference + features 页。

### 1.5 HITL：审批与人工确认 —— **结论：两轴正交模型；注意两处已过时的常见口径**

- **演进**：2025 年早期的三档 UX（`--suggest` / `--auto-edit` / `--full-auto`）已演进为**两个正交轴**：
  - `sandbox_mode = read-only | workspace-write | danger-full-access`（沙箱边界）
  - `approval_policy = untrusted | on-request | never | granular{...}`（何时暂停问人）
- **重要修正 1**：`untrusted` **已退役，不再是可选策略**（官方沙箱页原文："Codex and ChatGPT Work no longer support `untrusted` as a selectable approval policy"，存量配置有迁移文档）；`on-failure` **已弃用**（config-reference 原文："on-failure is deprecated; use on-request … or never"）。granular 结构可对 sandbox_approval / rules / mcp_elicitations / request_permissions / skill_approval 五类提示逐类放行或自动拒绝。
- **重要修正 2**：现行 CLI 旗标为组合式 `--sandbox workspace-write --ask-for-approval on-request`（官方沙箱页原文），完全访问快捷方式为 `--yolo`（features 页）；`--full-auto` 旧旗标在现行 command-line-options 页的状态**未能核实**（该页本次两次抓取失败：https://developers.openai.com/codex/cli/command-line-options ）。
- TUI 内 `/permissions` 三档预设：**Auto（默认，工作目录内自读自写自跑，越界/联网才问）／Read-only／Full Access**；步骤级确认：watch plan + "approve or reject steps inline"；`/plan` 计划模式（`plan_mode_reasoning_effort` 可单独调推理力度）。
- **审批人可换成模型**：`approvals_reviewer = user | auto_review`（auto_review=reviewer 子代理自动审，即 Guardian 系；`/approve` 人工复议一次被拒动作）。
- 非交互 `codex exec` 默认走 never 语义；企业可用 `requirements.toml` 的 `allowed_approval_policies` 锁死可选值。来源：https://developers.openai.com/codex/sandboxing ；https://developers.openai.com/codex/config-reference ；https://developers.openai.com/codex/cli/features

### 1.6 可观测性 —— **结论：OTel 三信号导出 + 本地日志 + 平台用量视图**

- **OTel 导出**：`otel.exporter / otel.trace_exporter / otel.metrics_exporter = none | otlp-http | otlp-grpc | statsig`；OTLP 端点可配 headers、binary/json 协议、**mTLS 客户端证书**（ca-certificate / client-certificate / client-private-key）；`otel.environment` 环境标签；`otel.log_user_prompt`（导出原始用户提示，**默认关=显式 opt-in**）。指标默认 `statsig`（OpenAI 自有指标通道）。来源：https://developers.openai.com/codex/config-reference
- 本地日志：`log_dir`（默认 `$CODEX_HOME/log`；显式设置后另开明文 `codex-tui.log`）；session transcript 本地落盘（1.1）；沙箱日志 `CODEX_HOME/.sandbox/sandbox.log`（排障官方指定文件）。来源：config-reference + Windows 页。
- 机器可读输出：`codex exec --json`（features 页证实事件流含 web_search 条目）；`/debug-config` 打印配置层叠与策略（`allowed_approval_policies`、`mcp_servers`、`rules`、`enforce_residency` 等）。
- 与 OpenAI 平台关联：`/usage` 展示 ChatGPT 账号 token 用量（daily/weekly/cumulative）；`/status` 展示会话模型/审批策略/可写根/剩余上下文。源码：`codex-rs/otel/`、`otel-trace-websocket/`、`rollout-trace/`。

### 1.7 部署与许可证 —— **结论：Apache-2.0 本地运行；沙箱三平台原生；模型可换但 wire 协议锁 Responses**

- **许可证**：Apache-2.0（README + LICENSE）。**本地运行**，无强制云组件（云功能为可选叠加）。
- **安装四通道**：standalone 脚本（macOS/Linux `curl -fsSL https://chatgpt.com/codex/install.sh | sh`；Windows `powershell … irm https://chatgpt.com/codex/install.ps1 | iex`）；npm `npm install -g @openai/codex`；`brew install --cask codex`；GitHub Release 直下二进制。standalone 默认从 `releases.openai.com` 下载、GitHub Releases 兜底，`CODEX_INSTALLER_USE_RELEASES_OPENAI_COM=false` 可强制只走 GitHub。来源：README。
- **沙箱平台矩阵**（现行，来源：https://developers.openai.com/codex/sandboxing + /codex/windows ）：
  - macOS：**Seatbelt** 开箱即用，无需安装。
  - Linux/WSL2：**bubblewrap（bwrap）**——"Starting in Codex 0.115, the Linux sandbox moved to bubblewrap"；无 bwrap 时回退捆绑 helper（需非特权 user namespace）。**注意：早期版本（2025）的 Landlock+seccomp 实现已不是现行口径**（任务书假设已过时）。Ubuntu 24.04 有 AppArmor 附加配置项；WSL1 自 0.115 起不支持。
  - Windows：**原生支持已成现实**——PowerShell 原生跑，`[windows] sandbox = "elevated"（推荐：专用低权沙箱用户+文件系统边界+防火墙规则）| "unelevated"（受限 token 回退档）`，默认叠加**私有桌面** UI 隔离；或 WSL2 走 Linux 沙箱。Win11 推荐，Win10 1809+ best effort。企业可 `windows.allowed_sandbox_implementations` 锁实现。
- **模型绑定（国内选型重点）**：默认 ChatGPT 订阅 OAuth（Plus/Pro/Business/Edu/Enterprise 含额度）或 API key。自定义端点：`config.toml` `[model_providers.<id>]`（`base_url`、`env_key` 环境变量供 key、命令式 `auth`（外部命令产 bearer token、可定时刷新）、`http_headers`、`query_params`、重试/超时参数）；保留 id：`openai`/`ollama`/`lmstudio`，另有内置 `amazon-bedrock`（企业部署通道，Administration > Deployment > Amazon Bedrock）；`--oss` + `oss_provider = ollama|lmstudio` 跑本地 gpt-oss；`openai_base_url`/`chatgpt_base_url` 覆盖官方端点；`model_catalog_json` + `~/.codex/models.json` 声明第三方模型元数据（上下文窗/推理档位/工具格式/多 agent 版本）。
- **硬约束**：`wire_api` **只支持 `"responses"`**（官方 config-reference 明示唯一值）——第三方端点必须实现 OpenAI **Responses API**，纯 Chat Completions 兼容端点不可直接接（旧文档的 `wire_api="chat"` 已移除）。

## 2 国内可用性

- **不用 ChatGPT 账号能跑吗：能，且有国产厂商官方支持路径。**
  - API key 模式：`preferred_auth_method = "apikey"` + `forced_login_method = "api"` 可整体跳过 ChatGPT 登录（DeepSeek 官方接入文档原文）。认证管理：`forced_login_method = chatgpt|api`、凭据存储 `cli_auth_credentials_store = file|keyring|auto`（config-reference）。
  - **DeepSeek 官方一级支持**：官方中文文档《接入 Codex》（api-docs.deepseek.com），提供一键脚本（bash + **Windows PowerShell 双版本**）、自动备份/写入 `~/.codex/models.json` 模型目录/校验 config.toml；DeepSeek API **原生支持 Responses API**；模型 deepseek-v4-flash / deepseek-v4-pro / deepseek-v4-flash-vision-exp（1M 上下文）。配置示例（官方原文）：
    ```toml
    model = "deepseek-v4-flash"
    model_provider = "deepseek"
    preferred_auth_method = "apikey"
    forced_login_method = "api"
    [model_providers.deepseek]
    name = "deepseek"
    base_url = "https://api.deepseek.com/"
    wire_api = "responses"
    ```
    来源：https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/codex
  - 其他国产端点：智谱 GLM、Kimi 有社区教程（知乎）；据腾讯云开发者文章，MiniMax、阿里云百炼、百度千帆、火山方舟已适配 Responses API（二手信息，接入前以各家官方文档为准）；聚合器 AIHubMix 有官方接入文档。本地模型走 ollama/lmstudio（`--oss`）。
  - 一份配置三端共用（CLI / ChatGPT 桌面端 / VS Code 插件）；会话按登录方式分组存放（切换 provider 不丢历史，仅分组隐藏）。
- **网络依赖**：安装脚本与 standalone 下载默认指向 chatgpt.com / releases.openai.com（可用 `CODEX_INSTALLER_USE_RELEASES_OPENAI_COM=false` 强制 GitHub Releases）；ChatGPT OAuth 登录流程指向 OpenAI 域名——墙内通常需代理（此为域名连通性判断，非官方声明）。**API-key + 国内/自定义端点模式下，日常推理流量不经 OpenAI**，登录墙问题消失。
- **中文资料多寡**：丰富——DeepSeek 官方中文文档为最高质量来源；知乎/腾讯云/开源中国/SegmentFault 教程多篇（2026 年持续更新）；CC Switch 等第三方切换工具生态。

## 3 企业落地注记（安全/合规视角）

- **沙箱强度（相对竞品的核心卖点）**：平台原生内核级强制而非包装层——macOS Seatbelt / Linux bubblewrap（user namespace 隔离）/ Windows 专用低权沙箱用户 + 防火墙规则 + 私有桌面；**沙箱对 spawn 出的命令（git、包管理器、测试）同样生效**（官方原文）；网络可域名级管控（`features.network_proxy` 域名 allow/deny + SOCKS5/HTTP 代理，experimental；`permissions.<name>.network` 权限档内网络策略）。
- **治理面（金融/大型企业关键）**：`requirements.toml` 管理员强制配置，用户不可削弱——`allowed_sandbox_modes`、`allowed_approval_policies`、`allowed_permission_profiles`（白名单式，未来新增档默认拒绝）、MCP 服务器**身份匹配** allowlist（stdio 按可执行文件+有序参数 matcher，HTTP 按 URL exact/prefix/regex）、`permissions.filesystem.deny_read` 不可削弱禁读、`marketplaces` 插件源限制、`enforce_residency = "us"` 数据驻留锁定；ChatGPT Business/Enterprise 还能云端下发 requirements。来源：config-reference（requirements.toml 全表）。
- **prompt 注入防护**：1) `web_search` 默认 `cached`——官方明示"reduces exposure to prompt injection from arbitrary live content, but you should still treat web results as untrusted"；2) **项目信任**：`projects.<path>.trust_level = trusted|untrusted`，untrusted 项目跳过项目级 `.codex/` 全层（config/hooks/rules），防仓库内夹带配置；3) **命令规则** `.rules`：按命令前缀 allow/prompt/forbid（管理员侧只能 prompt/forbid）；4) skill 脚本独立审批；5) MCP elicitation 默认自动拒绝（granular 开关放行）；6) `approvals_reviewer = auto_review` 由评审代理复核越界请求。
- **密钥管理**：凭据三选一存储（文件 auth.json / OS keyring / auto，`cli_auth_credentials_store`；crate `keyring-store`）；provider key 推荐 `env_key`（环境变量）或命令式 `auth.command`（外部密钥管理器/云 IAM 短时 token，5 分钟级刷新），明文 `experimental_bearer_token` 官方标注 discouraged；MCP OAuth 凭据独立存储策略（`mcp_oauth_credentials_store`）；子进程环境变量策略 `shell_environment_policy`（默认过滤含 KEY/SECRET/TOKEN 变量）；排障明确**禁止外发 `.sandbox-secrets/` 目录**。
- **供应链**：Apache-2.0 + SECURITY.md 漏洞报告通道；cargo 依赖审计（`codex-rs/deny.toml` cargo-deny）+ Bazel 构建 + CI worktree 脏检测；安装渠道可钉死 GitHub Releases。风险点：**发版节奏极快**（周级，本卡截稿时 0.167 已出 4 个 alpha），企业需锁版本 + 跟踪官方 changelog；无第三方插件签名机制（插件市场靠 Git 源 + 企业白名单管控）。
- **数据出域提示**：默认模式模型流量与 OTel 指标（statsig 默认）出境至 OpenAI（美国，可 `enforce_residency` 仅锁 us）；金融敏感仓库建议 API-key 模式配境内端点，或接受企业协议内的 OpenAI 处理。Bedrock 通道仍为 AWS 美区语义，非境内合规替代。

## 4 关键源码文件清单（深读入口；`codex-rs/` 共 136 个 crate，路径经 GitHub contents API 实抓）

**主循环与会话**
- `codex-rs/core/` —— agent 核心循环（最重 crate）。`core/src/` 关键文件：`codex_thread.rs`（线程/会话编排）、`agent/`、`agent_communication.rs`、`client.rs` + `client_common.rs`（模型客户端）、`exec.rs` / `exec_env.rs` / `exec_policy.rs` + `exec_policy/`（命令执行与策略）、`agents_md.rs` + `agents_md_manager.rs`（AGENTS.md 发现/合并）、`apply_patch.rs`、`compact.rs`（历史压缩）、`mcp.rs` + `mcp_tool_call/`、`hook_runtime.rs`、`guardian/` + `guardian_review.rs`（auto review）、`elicitation.rs`、`function_tool.rs`、`cyber_access_program.rs`、`code_mode_host.rs`
- `codex-rs/rollout/`（session rollout 落盘）、`codex-rs/history/`（history.jsonl）、`codex-rs/thread-store/` + `thread-manager-sample/`、`codex-rs/state/`（SQLite 状态库）
- `codex-rs/cli/`（CLI 入口）、`codex-rs/tui/`（终端 UI）、`codex-rs/exec/`（非交互 exec）+ `exec-server*/`

**沙箱与安全（本卡重点）**
- `codex-rs/sandboxing/`（沙箱抽象）、`codex-rs/linux-sandbox/`、`codex-rs/bwrap/`（bubblewrap 集成）、`codex-rs/windows-sandbox-rs/` + `codex-rs/windows-sandbox-service/`（Windows 双模式沙箱）、`codex-rs/mxc-sandbox/`（macOS 侧）、`codex-rs/process-hardening/`、`codex-rs/network-proxy/`（沙箱网络代理/域名策略）、`codex-rs/execpolicy/`（命令规则引擎）、`codex-rs/user-verification/`

**配置 / 认证 / 模型接入**
- `codex-rs/config/` + `codex-rs/config-schema/`（含 in-tree `codex-rs/config.md` 配置文档）、`codex-rs/model-provider-info/` + `codex-rs/model-provider/`、`codex-rs/ollama/` + `codex-rs/lmstudio/`（本地 provider）、`codex-rs/login/` + `codex-rs/keyring-store/` + `codex-rs/secrets/`、`codex-rs/chatgpt/` + `codex-rs/backend-client/`（ChatGPT 后端）、`codex-rs/aws-auth/`（Bedrock）

**扩展机制**
- `codex-rs/skills/`、`codex-rs/hooks/`、`codex-rs/plugin/` + `plugin-host/` + `core-plugins/` + `core-plugin-common/`、`codex-rs/prompts/`（custom prompts 遗留）、`codex-rs/codex-mcp/`（Codex 作为 MCP server）、`codex-rs/rmcp-client/`（MCP 客户端）、`codex-rs/agent-roles/` + `agent-graph-store/` + `agent-message-board*/`（多 agent 基建）

**可观测与其它**
- `codex-rs/otel/` + `otel-trace-websocket/` + `rollout-trace/` + `codex-rs/diagnostics/`、`codex-rs/app-server/`（+ protocol/transport/daemon，远程 TUI 与 SDK 底座）、`codex-rs/cloud-tasks*/`（Codex cloud 客户端）、`codex-rs/worktree/`、`codex-rs/apply-patch/`、仓库根 `codex-cli/`（TS/npm 壳）与 `sdk/`（Codex SDK）

## 5 信息源 URL 列表

官方（本次实抓）：
1. GitHub 仓库/README（定位、安装、许可证、目录结构、star/commit 活跃度）：https://github.com/openai/codex
2. CLI 总览页：https://developers.openai.com/codex/cli
3. CLI Features（resume/sessions、approval 三档、MCP、subagents、web search、exec、/review）：https://developers.openai.com/codex/cli/features
4. Slash commands 全表（含 /init /memories /import /permissions /agent /debug-config）：https://developers.openai.com/codex/cli/slash-commands
5. Configuration Reference（config.toml + requirements.toml 全键，本卡最重要单一来源）：https://developers.openai.com/codex/config-reference
6. Sandboxing 概念页（Seatbelt/bubblewrap/审批模型/untrusted 退役）：https://developers.openai.com/codex/sandboxing （canonical 镜像 https://learn.chatgpt.com/docs/sandboxing ）
7. Windows 页（elevated/unelevated 沙箱、企业阻塞项、错误 1385）：https://developers.openai.com/codex/windows
8. Codex Security 产品页（与"沙箱安全"区分）：https://developers.openai.com/codex/security
9. Releases（rust-v0.166.0 2026-10-29）：https://github.com/openai/codex/releases （经 GitHub API 核实）
10. codex-rs 目录与 core/src 文件清单：GitHub contents API `repos/openai/codex/contents/codex-rs[/core/src]`（2026-11-03 实抓）

第三方（国内接入实证）：
11. DeepSeek 官方《接入 Codex》（中文，一键脚本/models.json/跳过 ChatGPT 登录）：https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/codex
12. 知乎《Codex 接入第三方模型 DeepSeek、GLM、Kimi 教程》：https://zhuanlan.zhihu.com/p/2045207013248995839
13. 腾讯云《Codex 官方宣布支持第三方模型》：https://cloud.tencent.com/developer/article/2696298
14. AIHubMix Codex CLI 接入文档：https://docs.aihubmix.com/cn/api/Codex-CLI
15. custom prompts 弃用与回归：https://github.com/openai/codex/issues/15941 ；替代方案请求 https://github.com/openai/codex/issues/51174

抓取失败待补（尝试路径已记录）：
- `https://developers.openai.com/codex/agents`（AGENTS.md 专页，×3 网络失败；repo 内 `docs/agents_md.md` 已外链化）→ 全局 ~/.codex/AGENTS.md 合并顺序待核
- `https://developers.openai.com/codex/cli/command-line-options`（×2 失败）→ `--full-auto` 等旧旗标现状待核
- `https://developers.openai.com/codex/authentication`（×2 失败；认证事实已由 README + config-reference + DeepSeek 文档交叉覆盖）
