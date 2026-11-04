# agent-tools-survey

题目 1「Agent 开发工具的分层定位与企业选型调研」的交付仓库。

对 7 个 Agent 开发工具做分层定位与选型调研：LangChain/LangGraph、Pydantic-AI、OpenClaw、Nous Hermes-Agent、DeepSeek Harness（dsh）、开源版 Codex、Pi。题目源见 `question.txt`（含题目 2，题目 2 交付在 [alicexl/prospectus-qa](https://github.com/alicexl/prospectus-qa)）。

## 交付物

- **`report/question1-调研报告.pdf`** — 调研报告定稿（18 页，2026-11-05）
- `report/report.html` — 报告源文件（单文件，内嵌 CSS + SVG）
- `research/` — 10 份调研卡（7 工具文档级调研 + LangGraph/Codex/OpenClaw 三深读笔记），报告论断的证据链
- `spec.md` — 立项规格（调研方法、分层假设、深读代表选择）

## 报告结构

§0 结论摘要（三发现 + 总览矩阵）→ §1 存在性核实 → §2 三层分层与关系图 → §3 深析 ×3 + 简述 ×5（状态/记忆/工具调用/扩展/HITL/可观测/部署许可七维度）→ §4 企业落地五维（安全/合规/成本/成熟度/国内可用性）→ §5 四场景选型 + 题目 2 工具组合论证 → §6 附录（版本锚定表）。

核心结论：七者分三层（库/SDK → Runtime/Harness → 成品/产品），层间是上下游关系而非竞品关系；跨层直比会误导，应按层各回答一个问题比较；企业落地的门槛不在许可证而在治理配套。

## 打印管线（重新生成 PDF）

```bash
cd report
python -X utf8 print_report.py && mv -f report_new.pdf question1-调研报告.pdf
```

CDP 驱动 Chrome headless 打印（页眉 + 页码页脚）。依赖：`websocket-client`、Chrome（Program Files (x86)）、系统安装 Noto Sans CJK SC **静态 TTF**（Chrome 打印时 CFF/可变字体会被转 Type3 轮廓，导致 PDF 文本不可检索）。

## 调研方法

2026-11-03 单一时点快照：GitHub API + 官网 + npm/PyPI registry 双通道核实 → 7 份统一模板调研卡 → 三代表仓库（LangGraph `3c916d7` / Codex CLI `bfdbf01` / OpenClaw `4285c6ce`）源码级深读。`repos/` 为第三方浅 clone，仅本地调研用，不入库。星数等时间敏感数据以调研当日为准，未做滚动更新。
