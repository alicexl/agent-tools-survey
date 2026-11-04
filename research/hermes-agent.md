# Hermes-Agent 调研卡

> 调研日期 2026-11-03。基础事实（stars/协议/版本等）来自当日 GitHub API 直查；行为细节全部核自仓库 `main` 分支源码与 `website/docs/`（浅克隆逐文件阅读，非二手转述）。文档 URL 给 hermes-agent.nousresearch.com/docs 线上页，对应仓库路径 `website/docs/`。

## 0 身份与分层归属

**成品/产品层，兼运行时**（不属于框架/库层）。

- 对终端用户的形态是"个人 AI 助手产品"：CLI/TUI（`hermes`）、Desktop 桌面应用、Web dashboard、20+ 消息平台聊天机器人（Telegram/Discord/Slack/WhatsApp/Signal + 微信/企微/钉钉/飞书/QQ 机器人/腾讯元宝等）。用户装好即用，不是拿来"搭 Agent"的框架。
- 同时具备完整**运行时**能力，可被二次开发：模型无关 provider 层（Nous Portal/OpenRouter/OpenAI/DeepSeek/通义/GLM/Ollama/vLLM/自定义端点，`hermes model` 热切换）、40+ 工具（开发者文档口径 70+ 注册工具/28 toolset）、MCP 客户端、插件系统、Python 库入口（guides/python-library.md）、ACP 协议适配器（VS Code/Zed/JetBrains）与 API Server。
- 官方自述："The self-improving AI agent built by Nous Research"——核心卖点是内置学习闭环（自动沉淀 skills、后台自我改进 review、跨会话 FTS5 检索、用户建模），并明确对标 OpenClaw/Claude Code 迁移（README 有 "Migrating from OpenClaw" 专节 + `hermes import-agent`）。
- 量级佐证活跃度：260,102 stars / MIT / 395 contributors / repo 建于 2025-07-22 / 最新 release v0.22.6（11-02）/ 调研当日仍有 teknium1 提交。测试规模 ~25,000 tests / ~1,250 文件（architecture.md 自述）。

## 1 七维度逐项

### 1.1 状态：会话/任务持久化与断点恢复 —— 强项，工程化极深

**持久化载体**：每 profile 一个 SQLite 库 `~/.hermes/state.db`（+`-wal`/`-shm` sidecar），FTS5 全文索引，会话有 lineage 跟踪（压缩产生父子链）、per-platform 隔离、原子写与争用处理。仓库根部约 60 个 `hermes_state_*.py`（wal/lockguard/reconcile/repair/rewind/timeline…）专职此事。

**多进程共享**：gateway、Desktop、dashboard、cron、CLI 并发写同一 `state.db`，靠 SQLite 自身锁 + "retired-WAL 拒写"协议——检测到旧 WAL 持有者时新进程主动停写并落 `state.db.retired-wal-<ts>-<pid>/` 取证目录，`hermes doctor` 列出持锁 PID，保证不丢已提交对话。

**恢复能力**：
- CLI 恢复：`hermes --continue`（`-c`，按终端面包屑 per-pane 各自续接）/ `--resume <id|title|latest>` / `--in <dir>` 按工作区恢复；恢复时显示上轮对话 recap 面板。
- 消息平台会话 = 故意设计成"一个聊天一个连续会话"，**跨 gateway 重启、崩溃、整机重启自动续接**（docs 原文： shutting the machine down overnight does not end the session）。
- 崩溃修复工具链：`hermes sessions recover --inspect-only`（只读检查损坏库）、`state-snapshots/` 快照、`hermes backup`、损坏文件自动隔离为 `.corrupt.*.bak`。
- daemon 常驻：`hermes gateway start/install` 装成 systemd（Linux）/launchd（macOS）/s6-overlay（Docker 镜像）/Windows 计划任务，受监督自动重启；仓库根有 `hermes_startup_watchdog.py`。终端工具内置不可绕过的"自杀防护"（拒绝杀 gateway 自身进程/解释器镜像）。
- 文件级回滚：`/checkpoints`+`/rollback`（v2 起 opt-in），shadow git 仓库存于 `~/.hermes/checkpoints/store/`，破坏性操作前快照、按项目去重，不碰真实 `.git`。
- cron 任务持久化于 `jobs.json`，执行态 claimed/running/completed/failed 落库可观测。

### 1.2 记忆：文件系统式 + FTS 检索，默认**无向量**

- **短期**：会话内活上下文 + 压缩子系统（`agent/context_compressor.py` 有损摘要中间轮次、micro-compaction、压缩 lineage），Anthropic prompt caching 断点。
- **长期（内置）**：两份 Markdown 文件 `~/.hermes/memories/MEMORY.md`（agent 笔记，2,200 字符上限）与 `USER.md`（用户画像，1,375 字符），条目以 `§` 分隔，会话开始时**冻结快照注入 system prompt**（保前缀缓存；会话中写入立即落盘、下个会话可见）。agent 用 `memory` 工具自维护（add/patch/remove，超限报错强制自我整理，不做静默压缩）。后台"自我改进 review"每轮后跑（可切便宜模型、可设 token 上限、写入门控）。
- **检索**：非向量。跨会话靠 `session_search` 工具直查 state.db 的 **SQLite FTS5** 全文索引（~20ms，返回原文不摘要，可前后滚动翻阅任意历史会话）。语义/向量检索交给**外置 memory provider 插件**（单选，`memory.provider`）：随包 RetainDB、ByteRover，目录里还有 Honcho/Hindsight/Supermemory/Mem0 等（`hermes memory setup`）。
- skills 系统即"程序性记忆"（agentskills.io 开放标准，SKILL.md 渐进披露）。
- 安全：记忆条目写入前做注入/渗出/不可见 Unicode 扫描（防记忆投毒进 system prompt）。

### 1.3 工具调用（已核实事实整理）

- 中央注册表 `tools/registry.py`，各工具文件 import 时自注册；开发者文档口径 **70+ 工具 / ~28 toolset**（README 宣传口径 40+），`hermes tools` 按 platform 粒度开关。
- 三种 API 模式适配：chat_completions / codex_responses / anthropic_messages（`agent/anthropic_adapter.py` 等），transport 覆盖 Anthropic Messages、OpenAI ChatCompletions/Responses、Bedrock Converse。
- 终端执行 6 内置后端（local/Docker/SSH/Modal/Daytona/Vercel Sandbox）+ 插件后端（Singularity/Apptainer，HPC 场景）；Daytona/Modal 支持 serverless 休眠唤醒。
- MCP：`hermes mcp add`（stdio/HTTP/OAuth-PKCE 全支持，RFC 9728/8414/7591 自动发现注册），tool 级 include/exclude 过滤、spend_limit 预算、elicitation。
- 子代理：`delegate_task` 递归委派 + `max_spawn_depth`，支持 fan-out 并行批、后台 detached 子代理、Desktop 里可给子代理单独选 provider/model。
- `execute_code` 沙箱内 Python 直调工具 RPC（多步管线折叠为零上下文开销单轮）。

### 1.4 扩展

- **插件**：三源发现（`~/.hermes/plugins/` 用户级、项目级、pip entry-points）；`hermes plugins install owner/repo` git 安装可 pin SHA；`plugins.enabled` 白名单制（装了不等于启用）。特化插件类型：memory provider、context engine（各单选）。
- **skills**：`~/.hermes/skills/`，Skills Hub（agentskills.io）一键装（`official/`、`clawhub/` 命名空间，`.hub/lock.json` 锁定），安装时内置安全扫描器强制过检，可选挂 NVIDIA SkillEvaluator/SkillSpector 做第二意见；agent 可在对话中自建/自改 skill（`skills.write_approval` 门控 + `/skills diff` 全量审）。
- **hooks**：gateway 事件钩子 `<profile>/hooks/<name>/`（文档明示"trusted-by-placement"——放进目录即加载，无额外确认，审计时须视同 config.yaml）。
- **平台适配器**：`plugins/platforms/` 25+ 个（含国产全家桶，见 §2）；developer-guide 有 adding-platform-adapters.md/adding-providers.md/adding-tools.md 扩展三件套。
- ACP（编辑器）、API Server、Python library、batch_runner（轨迹批量生成）、cron（自然语言定时任务投递到任意平台）。

### 1.5 HITL：审批/确认机制 —— 分层精细，超过同类

- **三档审批模式** `approvals.mode`：`smart`（默认，辅助 LLM 评估风险：低险自动放行单次、真危险自动拒、拿不准问人）/ `manual`（全部人工）/ `off`（全放行）。上下文分立开关：`cron_mode`/`single_query_mode`/`unattended_mode` 默认 **deny**（无人值守场景命中危险命令直接挡，fail-closed）。
- **YOLO 模式**：`--yolo`/`/yolo`（会话级 toggle，随会话持久化，重启保留）/ `HERMES_YOLO_MODE=1`；开启有红幅横条+状态栏 `⚠ YOLO` 常驻提醒。
- **硬底线黑名单**（`tools/approval.py::UNRECOVERABLE_BLOCKLIST`）：`rm -rf /`、fork 炸弹、mkfs 活根、dd 裸盘、根目录管道远程脚本到 sh——**YOLO/off/always 均不可越过**；解析不了的畸形引号命令 fail-closed。用户侧 `approvals.deny` fnmatch 规则同样先于 YOLO 生效（"yolo-with-exceptions"）。
- CLI 审批四选：once/session/**always（沉淀进 config.yaml `command_allowlist`）**/deny；`hermes approvals suggest` 从审批历史挖允许清单提案（破坏性类别永不提案、凭据自动打码）。
- 网关侧：聊天回复 yes/no 审批；api_server 会话走 `POST /v1/runs/{id}/approval` 审批卡。
- 记忆/技能写入门控：`memory.write_approval`/`skills.write_approval`（staged + `/memory pending`、`/skills diff` 审后落盘）；破坏性 slash 命令（/clear /new /reset /undo）二次确认。
- 容器后端（docker/modal/daytona/vercel_sandbox/singularity）内**跳过**危险命令审批——容器即边界。

### 1.6 可观测性

- **日志**：本地文件 `~/.hermes/logs/`（agent.log / gateway.log），`hermes doctor` 全量诊断（含 DB 持锁者列出、advisory 修复建议、`--fix`）。
- **OTLP 监控面**（`agent/monitoring/otlp_exporter.py`）：gateway/cron 生命周期、平台连接器健康、无内容诊断事件 → OTLP/HTTP（traces/metrics/logs 三路），可接任意 OTel Collector/DataDog；**"content-free by construction"**——明确不导出 prompt/消息/工具参数/会话史；无默认目的地（operator 配 `monitoring.export.otlp`），SDK 为可选 extra `hermes-agent[otlp]`。
- **轨迹面**：NeMo Relay SDK 集成（显式配置订阅者才导出）+ `batch_runner.py` 批量轨迹生成 + ShareGPT 格式导出（训练数据管线，research-ready 卖点）。
- **评测**：仓库 `evals/` 目录几十个场景脚本（approval_deny_dispatch、compaction、browser_use、botmode-dm-matrix 等）；~25,000 pytest。
- 会话内：`/usage` token 用量、`/insights --days N` 使用洞察、`/platforms` 网关状态、journey 记忆时间线可视化。
- 未发现第三方遥测 SDK（grep posthog/sentry 无命中），运行时默认不外发数据。

### 1.7 部署与许可

**沙箱/隔离**：执行隔离靠终端后端——local 无隔离（靠审批层）、Docker 硬化容器（`--cap-drop ALL`、no-new-privileges、pids-limit 256、nosuid/noexec tmpfs、CPU/内存/磁盘限额、持久或临时工作区）、SSH 远程机、Modal/Daytona/Vercel 云沙箱、Singularity 插件；另有 `HERMES_WRITE_SAFE_ROOT` 文件写白名单根。生产建议明示：网关部署用容器后端。

**密钥管理**：`~/.hermes/.env`（chmod 600）；凭据库特性（credential vault）支持 **Bitwarden/1Password** secret source 与 command source；`hermes auth` 凭据池/多 key 轮换；MCP 子进程环境变量白名单过滤（只透传 PATH/HOME 等安全项）；`execute_code`/terminal 子进程默认剥含 KEY/TOKEN/SECRET/PASSWORD 变量；OAuth token 存 `~/.hermes/mcp-tokens/` 不进 config.yaml；`.env` 类文件对 agent **读拒绝**（可写不可读回）；错误信息凭据自动 `[REDACTED]`。

**数据本地性**：所有状态（会话库/记忆/skills/密钥/日志）在本地 `~/.hermes/`；消息网关是**自托管长驻进程，各适配器直连平台官方 API，消息路径不经 Nous 服务器**（微信=iLink 长轮询、钉钉=Stream Mode WebSocket、飞书=WebSocket/Webhook、企微=AI Bot WebSocket——均无需公网端点，公司内网可部署）。用 Nous Portal 时模型流量与 Tool Gateway 流量才过 Nous。监控 OTLP 无默认外发。

**许可**：MIT（商用友好，无 CLA 门槛）。安装渠道：install.sh/install.ps1（托管在 hermes-agent.nousresearch.com）、MSIX/Microsoft Store、Docker（`nousresearch/hermes-agent` / 沙箱镜像 `nousresearch/hermes-sandbox:desktop`）、源码；PyPI `hermes-agent` 官方但版本滞后，npm 同名包第三方非官方。

## 2 国内可用性

**国产模型 provider 全部原生内置**（integrations/providers.md，无需改代码，设 env + `hermes model` 或 `hermes chat --provider X`）：

| 模型 | 配置 | provider |
|---|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek` |
| 通义 Qwen（DashScope） | `DASHSCOPE_API_KEY` | `alibaba`（国际）/ `alibaba-cn`（**大陆端点**） |
| 智谱 GLM（z.ai） | `GLM_API_KEY` | `zai`（自动探测 global/China/coding 端点，免手配 base_url） |
| Kimi（月之暗面） | `KIMI_API_KEY` / `KIMI_CN_API_KEY` | `kimi-coding` / `kimi-coding-cn`（api.moonshot.cn） |
| MiniMax | `MINIMAX_API_KEY` / `MINIMAX_CN_API_KEY` | `minimax` / `minimax-cn` |

另有自定义 OpenAI 兼容端点（`model.base_url`）、Ollama/vLLM/LM Studio 本地推理（guides/local-ollama-setup.md、local-models.md、local-llm-on-mac.md）。

**Nous Portal 非必需**：README/docs 明示 provider 完全自选，Portal 只是一站式订阅可选项；配国产 provider + 本地网关即可整链路境内。

**国内消息平台原生适配器**（plugins/platforms/，消息直连腾讯/阿里官方 API，自托管网关进程内运行）：weixin（个人微信，腾讯 iLink Bot API 长轮询、QR 扫码登录、AES-128-ECB 加密 CDN 媒体；注意 iLink bot 身份进普通微信群受限，文档如实警告）、wecom + wecom-callback（企微 AI Bot WebSocket / 回调）、dingtalk（官方 `dingtalk-stream` SDK，免公网端点）、feishu（WebSocket 或 Webhook 双模）、qqbot、yuanbao。社区另有 HermesClaw 微信桥（同号共跑 OpenClaw）。

**中文资料**：README 有官方完整中文版 README.zh-CN.md；CLI/TUI i18n locales 含 `zh.yaml`/`zh-hant.yaml`（界面中文化）；文档站本体英文。

**剩余境外依赖点**（如实提示）：① 安装与更新链路——install 脚本托管在 hermes-agent.nousresearch.com、release 更新走 GitHub、Docker 镜像在 Docker Hub，首次部署需可达境外源（可内网镜像/离线源码安装绕过）；② Skills Hub（agentskills.io）与插件目录默认指向 GitHub/git 源；③ 本地运行时（模型 API + 消息平台均境内/local）不依赖 Nous 任何服务器。另：SSRF 防护对 Clash/Mihomo fake-ip（198.18.0.0/15）有专门配置项 `security.fake_ip_ranges`，国内代理环境友好。

## 3 企业落地注记

**优势**
1. 消息平台覆盖是企业场景稀缺项：钉钉/企微/飞书/微信原生官方 API 接入（非 webhook 转发层），且免公网端点（长轮询/WebSocket 出站），内网/专线可部署。
2. HITL 审批分层成熟（smart LLM 评估 + 无人值守 fail-closed + 不可越过硬底线 + 用户 deny 规则），比一般开源 agent 的 on/off 两档细得多。
3. 状态持久化工程化深度罕见（WAL 拒写协议、多进程仲裁、损坏恢复工具链、受监督 daemon），适合 7×24 无人值守。
4. MIT + 数据全本地 + OTLP 无内容导出，合规面（数据不出境）可自证。
5. 模型无关 + 国产 provider 原生内置，满足"不绑单一境外模型商"的采购约束。

**风险/红旗**
1. **供应链面**：插件/skills 支持 git 任意源与 Hub 安装（虽有锁文件+安装扫描器+白名单启用制，默认仍需企业自有准入控制）；`~/.hermes/hooks/` 目录"放入即加载"无确认门（文档自认 trusted-by-placement，须纳入基线审计）。
2. 个人产品基因：单 profile = 单用户记忆/会话，多用户共享需靠 profile 隔离 + 网关 allowlist/pairing，不是多租户 SaaS 架构；"one agent per Hermes home" 是文档明示约束。
3. 微信个人号接入走 iLink bot 身份，群消息能力受限（Tencent 侧限制，文档已如实标注）；企业场景更稳的是企微/钉钉/飞书路径。
4. 迭代极快（v0.22.x、日更量级），跟进成本与回归风险并存；PyPI 版本滞后，生产建议 git/容器钉版本。
5. 无 RBAC/审计日志产品化模块（有 `/insights`、OTLP 生命周期指标，但无企业级权限体系）——需在网关层与 OS 层自建。
6. 官方托管 Hermes Cloud 在 Nous 境外云上，境内企业若用即数据出境，建议只走自托管路径。

**Hermes Cloud vs 自托管边界**：Hermes Cloud（portal.nousresearch.com/cloud）= Nous 托管的实例，经 Portal MCP（OAuth，`hermes mcp add --url https://portal.nousresearch.com/mcp --auth oauth hermes-cloud`）会话式管理 list/start/stop/create/destroy/cost。自托管 = 全部状态本地 `~/.hermes/`、模型密钥自持、消息路径不经 Nous，二者边界清晰；中间态是"自托管 + Nous Portal 订阅"（仅模型/工具中转过 Nous）。

## 4 关键源码文件清单

```
仓库根（GitHub NousResearch/hermes-agent，main@2026-11-03）
├─ cli.py                     # HermesCLI 门面（mixins 在 hermes_cli/cli_*_mixin.py）
├─ run_agent.py               # AIAgent 门面；主循环 agent/conversation_loop.py + agent/turn_*.py
├─ model_tools.py             # 工具发现/schema 收集/分发
├─ toolsets.py                # 工具分组与平台预设
├─ hermes_state.py            # SQLite 会话/状态库门面（+~60 个 hermes_state_*.py：
│                             #   wal/lockguard/repair/rewind/reconcile/sessions/fts/search…）
├─ hermes_constants.py        # HERMES_HOME、profile 路径
├─ hermes_logging.py / hermes_startup_watchdog.py / hermes_dir_acl.py
├─ batch_runner.py / trajectory_compressor.py     # 批量轨迹生成/压缩（训练数据线）
├─ agent/                     # conversation_loop.py、prompt_builder.py、context_compressor.py、
│                             #   prompt_caching.py、memory_manager.py、memory_provider.py、
│                             #   anthropic_adapter.py、auxiliary_client.py、
│                             #   monitoring/otlp_exporter.py + gateway_health_export.py
├─ hermes_cli/                # main.py、config.py(DEFAULT_CONFIG)、commands.py、auth.py
│                             #   (PROVIDER_REGISTRY)、setup.py、plugins.py、
│                             #   callbacks.py(clarify/sudo/approval)、observability/、
│                             #   security_advisories.py(供应链投毒扫描)
├─ tools/                     # registry.py、approval.py(危险模式+UNRECOVERABLE_BLOCKLIST)、
│                             #   terminal_tool.py、file_tools.py、web_tools.py、
│                             #   code_execution_tool.py、delegate_tool.py、mcp_tool.py、
│                             #   credential_files.py、env_passthrough.py、
│                             #   environments/(local|docker|ssh|modal|daytona|vercel_sandbox)
├─ gateway/                   # run.py(GatewayRunner)、session.py(SessionStore)、delivery.py、
│                             #   pairing.py(DM 配对授权)、hooks.py、mirror.py、
│                             #   hosted_room_*.py(多网关托管房间)
├─ plugins/platforms/         # 25+ 平台适配器：telegram/discord/slack/…/dingtalk/feishu/
│                             #   wecom/weixin/qqbot/yuanbao
├─ plugins/memory/ + plugins/context_engine/    # 特化单选插件槽位
├─ skills/ + optional-skills/ # 内置技能与官方可选技能
├─ cron/                      # jobs.py、scheduler.py
├─ acp_adapter/               # ACP stdio/JSON-RPC（VS Code/Zed/JetBrains）
├─ evals/ + tests/            # 评测场景脚本；~25,000 pytest
├─ website/docs/              # Docusaurus 文档源（user-guide/developer-guide/integrations/…）
├─ SECURITY.md / .env.example(27KB 全量 env 样例) / cli-config.yaml.example
```

## 5 信息源 URL 列表

- 仓库与 README：https://github.com/NousResearch/hermes-agent （中文版 README.zh-CN.md 同仓）
- 安全模型（审批/黑名单/沙箱/密钥/SSRF/注入扫描/供应链通告）：https://hermes-agent.nousresearch.com/docs/user-guide/security
- 会话持久化与恢复：https://hermes-agent.nousresearch.com/docs/user-guide/sessions ；https://hermes-agent.nousresearch.com/docs/user-guide/session-storage-recovery
- 记忆系统：https://hermes-agent.nousresearch.com/docs/user-guide/features/memory ；外置 provider：…/features/memory-providers
- 工具与终端后端：https://hermes-agent.nousresearch.com/docs/user-guide/features/tools ；代码执行沙箱：…/features/code-execution
- 子代理/委派：https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation
- Skills 系统（Hub/安装扫描/write_approval）：https://hermes-agent.nousresearch.com/docs/user-guide/features/skills
- 插件系统：https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins
- MCP：https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp ；配置参考：…/docs/reference/mcp-config-reference
- 消息网关总览：https://hermes-agent.nousresearch.com/docs/user-guide/messaging
  - 微信：…/docs/user-guide/messaging/weixin ；企微：…/messaging/wecom ；钉钉：…/messaging/dingtalk ；飞书：…/messaging/feishu
- Provider（DeepSeek/通义/GLM/Kimi CN/本地模型）：https://hermes-agent.nousresearch.com/docs/integrations/providers
- 网关监控（OTLP 无内容导出）：https://hermes-agent.nousresearch.com/docs/developer-guide/gateway-monitoring
- 架构与目录结构：https://hermes-agent.nousresearch.com/docs/developer-guide/architecture
- 会话存储内幕（SQLite/FTS5/lineage）：https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage
- Hermes Cloud 托管版管理：https://hermes-agent.nousresearch.com/docs/guides/manage-hermes-cloud-with-mcp
- Nous Portal：https://hermes-agent.nousresearch.com/docs/integrations/nous-portal
- checkpoints/rollback：https://hermes-agent.nousresearch.com/docs/user-guide/checkpoints-and-rollback
- egress 隔离代理：https://hermes-agent.nousresearch.com/docs/user-guide/egress/
- CLI 命令参考（gateway 服务化 systemd/launchd）：https://hermes-agent.nousresearch.com/docs/reference/cli-commands
