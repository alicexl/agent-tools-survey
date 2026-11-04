# 题目 1 调研项目 Spec：Agent 开发工具的分层定位与企业选型

- 立项日期：2026-11-03
- 状态：已完成
- 题目源：`question.txt`

## 1. 目标与交付物

回答题目 1 的 A / B / C / D1 四部分（D2 挂起，见 §8 待办），交付一份 **PDF 调研报告**（约 20–35 页），受众=出题方/评分者，结构对齐采分点。

- 主交付：`report/question1-调研报告.pdf`
- 源文件：`report/report.html`（单文件，内嵌 CSS + SVG）
- 过程交付：`research/` 下调研卡与深读产物（可复查的证据链）

不编写业务代码；允许浅 clone 源码用于阅读引用。报告语言：中文。

## 2. 七工具清单与指代锁定（防混淆）

| 名称 | 题目限定指代 | 预期分层（待阶段1核实修正） |
|---|---|---|
| LangChain / LangGraph | langchain-ai/langchain、langchain-ai/langgraph | 库/SDK |
| Pydantic-AI | pydantic/pydantic-ai | 库/SDK |
| OpenClaw | openclaw/openclaw | 成品/产品（待核实） |
| Nous Hermes-Agent | NousResearch/hermes-agent（非 Hermes 系列模型） | 待核实 |
| DeepSeek Harness | deepseek-ai/deepseek-harness（CLI `dsh`） | Runtime/Harness |
| 开源版 Codex | openai/codex（Codex CLI，非 2021 模型、非云产品） | Runtime/Harness |
| Pi | pi.dev；earendil-works/pi；npm @earendil-works/picoding-agent；CLI `pi` | Runtime/Harness |

## 3. 报告结构（对齐采分点）

```
1 结论摘要          一句总起 + A/B/C/D 各 1–2 句结论 + 7 工具信息总表
2 分层定位与关系图（A）
   2.1 三层定位表（开发库/SDK → Runtime/Harness → 成品/产品，7 工具各归一层+依据）
   2.2 关系图（SVG：嵌入 / 后端·模型提供方 / MCP·ACP 协议互通，分边型）
   2.3 公平比较方法论（跨层直比为何误导 + 对策：分层归位、同层对比、
       以"自建 vs 复用维度"统一坐标系）
3 架构与核心抽象（B）
   3.1 三个代表工具深析 × 7 维度
       （状态/记忆/工具调用/扩展机制/HITL/可观测性/部署与许可证）
       每条论断附源码文件路径或官方文档 URL
   3.2 其余 4 工具简述（对照表 + 差异点）
4 企业落地考量（C）
   5 维度（安全/合规/成本/维护成熟度/国内可用性）× 7 工具评估矩阵，
   聚焦中国境内金融或大型企业场景
5 场景选型建议（D1）
   4 场景各：推荐方案 + 否决名单及理由
   (a) 金融机构内网合规问答 (b) C 端高并发客服
   (c) 研发团队内部编码与运维助手 (d) 需要长期记忆的个人/团队助理
6 附录            调研方法与时间窗、信息来源清单、"查无实据"核实记录
```

## 4. 深读代表选择（B 部分）

| 层 | 深读代表 | 理由 | 其余简述 |
|---|---|---|---|
| 库/SDK | LangGraph | checkpoint/持久化最丰富，支撑"状态"维度 | LangChain、Pydantic-AI |
| Runtime/Harness | Codex CLI | 最成熟，7 维度全覆盖（sandbox/HITL/会话），资料最全 | dsh、Pi |
| 成品/产品 | OpenClaw（以核实为准） | 产品层预期唯一代表 | Hermes-Agent |

**保底链**：若阶段 1 核实后 OpenClaw 不属产品层或查无此物 → 产品层代表改选 Hermes-Agent；若其也不适格 → Pi 升级为深读，OpenClaw/Hermes 按核实结果如实处理。

## 5. 调研执行管线（方案一：subagent 分工 + 主会话综合）

```
阶段1 存在性核实  7 仓库并行：org/repo 存在性、活跃度、最新 release、定位证据
                 （结果已并入报告 §1 存在性核实表，独立调研卡已删）
                 验收：每工具有"存在/存疑/查无"三态结论；"查无"须双源核验
                 （GitHub 搜索 + Web 搜索/npm/PyPI 交叉）
阶段2 调研卡      每工具 1 个 subagent，统一模板 → research/<tool>.md
                 模板：分层归属 / 7 维度逐项 / 许可证 / 国内模型接入 /
                 中文资料 / 关键源码文件清单 / 信息源 URL 列表
                 验收：7 文件齐全，每条关键论断有 URL 或文件路径
阶段3 源码深读    3 代表浅 clone 至 repos/，深读 agent
                 → research/<tool>-deep.md（证据链带文件路径引用）
                 验收：7 维度 × 3 工具全部有源码级佐证
阶段4 综合写作    主会话汇总：SVG 分层图+关系图、C 评估矩阵、D1 选型论证
                 → report/report.html
                 验收：结构覆盖 §3 全部章节；结论与调研卡无矛盾
阶段5 PDF+终检    Edge headless 打印 PDF；终检
                 验收：PDF 生成成功、图表完整；抽查 ≥10 处引用可回溯；
                 所有"查无/存疑"结论经双源核验
```

写作只依赖 `research/` 落盘产物，不依赖会话记忆。

## 6. PDF 生产管线

HTML 单文件（内嵌 CSS + SVG；字体微软雅黑/宋体系统自带）→ Chrome headless 打印：

```
"/c/Program Files (x86)/Google/Chrome/Application/chrome.exe" --headless --disable-gpu --no-first-run \
  --user-data-dir="$TEMP/chrome-print-profile" \
  --print-to-pdf="D:/workspace/agent-tools-survey/report/question1-调研报告.pdf" \
  "file:///D:/workspace/agent-tools-survey/report/report.html"
```

Win10 零新依赖。关系图手写 SVG 内嵌（不依赖 mermaid CDN，离线打印不丢图）。用 Chrome 而非 Edge 的原因：实测 Edge headless 打印会丢失文档内部链接标注，Chrome 正常保留。注意：Chrome 装在本机 `Program Files (x86)`，且必须带独立 `--user-data-dir`（否则打印被转交给正在运行的浏览器实例而静默失败）；重打后核对 `N bytes written` 输出与 PDF 时间戳，勿只看旧文件（2026-11-04 曾因路径写错导致 PDF 静默停留旧版）。

## 7. 目录规划

```
question1/
  question.txt
  spec.md          本设计文档
  research/        <tool>.md 调研卡、<tool>-deep.md 深读
  repos/           浅 clone 源码（--depth 1）
  report/          report.html、question1-调研报告.pdf
```

## 8. 待办与挂起项

- **D2 已完成**（2026-11-03）：题目 2（金融招股书可溯源问答 Agent）随 question.txt 补充后，D2 论证已作为报告 §5.5 落入并重打 PDF——结论：Pydantic-AI + DeepSeek-chat + 自建确定性层；harness/产品层形态错配出局。
- 网页版报告：题目说明"可生成网页报告但不计分"——不做，除非用户另行要求。

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| 编造工具内容（最高优先） | 调研卡每条附 URL；"查无此物"双源核验才可下结论；附录记录核实过程 |
| subagent 低质/幻觉 | 统一模板产出；主会话抽查关键论断；深读以源码为准覆盖文档口径 |
| 信息时效 | 以 2026-11 当前官方仓库为准，报告标注调研时间窗 |
| 同名项目混淆 | §2 指代表先行锁定，调研卡首节即"身份核实"（org/repo/npm 包名对号） |
| 上下文溢出 | 中间产物全落盘；阶段间只传递文件路径与结论摘要 |
