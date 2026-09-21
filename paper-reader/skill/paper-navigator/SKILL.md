---
name: paper-navigator
description: 通过检索学术论文及研究文献，来回答专业领域的深度问题与文献诉求。【命中场景】（满足以下任一即可触发）：1.明确文献诉求：有论文/综述/原始证据/奠基作/SOTA/演进脉络/实验数据等意图时，明确需要寻找学术文献来源。2.深度机制与原理探讨：针对具体算法、模型、参数、数学推导、实验设计等，探究其底层实现或优化机制。3.专业对比与前沿进展：对比复杂技术/架构的优劣、局限性，或询问垂直领域（理工/人文/社科等）的最新学术突破与成因。4.需要专业领域知识的。不适用：基础概念科普、通识百科问答
version: 1.6.6
category: 工具类
scope: 对话内可用
---

# 技能说明

把用户一句查询转成**有证据、有判断、贴合真实意图**的论文回答。唯一工具 `WebSearch`。核心做法是先诊断信息缺口,再用上一轮真实返回决定下一轮搜什么,避免凭记忆补标题、作者、数字或结论。

先读懂"查询背后的真问题": 用户表面搜一个词,实际可能要原始工作、SOTA、方法对比、可复现实验、定量证据或脉络梳理。每轮检索前都要明确: 核心对象是什么、缺口是什么、需要哪类证据、是否有时序意图。

固定执行 **3 轮请求**: 第 1 轮网页搜索,后 2 轮论文搜索。每轮只调用一次 `WebSearch`,每次 `queries` 中**并行搜索 4 个关键词**。不得少于或多于 4 条;缺口不足时用上位词、同义术语、代表方法、验证词补足。

---

# WebSearch 请求规范

请求体顶层只放 `queries` 与可选 `additional_params`:

- `queries`: 4 条查询词数组,承载并行检索。
- 网页模式(R1): 只允许 `queries`,**不得带 `additional_params`**。
- 论文模式(R2/R3): 必须带 `additional_params`,固定包含 `"domain":"paper"`、`"search_agent":"creative_qa_agent"`、`"agent_size":7`、`"lang"`;仅在规则触发时添加 `filter_by.pb_time_start`/`pb_time_end`。

## R1 网页模式

只用于建立术语、脉络和常见分支,不是论文结论来源:

```json
{"queries":["transformer architecture overview","transformer architecture explained","transformer architecture history","transformer architecture introduction"]}
```

## R2/R3 论文模式

默认不加时间过滤:

```json
{"queries":["transformer architecture","self attention mechanism","sequence transduction model","neural machine translation attention"],"additional_params":{"domain":"paper","search_agent":"creative_qa_agent","agent_size":7,"lang":"en"}}
```

需要时间过滤时才加 `filter_by`:

```json
{"queries":["transformer attention mechanism","self attention sequence transduction","neural machine translation attention","attention based encoder decoder"],"additional_params":{"domain":"paper","search_agent":"creative_qa_agent","agent_size":7,"lang":"en","filter_by":{"pb_time_end":1577836800}}}
```

返回字段形如:
`[序号]WebpageTitle:...|||WebpageTime:...|||WebsiteName:...|||WebpageRelevance:...|||WebpageContent:...|||cite_num:...|||author:...`。
`WebpageContent` 是摘要和相关性判断的主要依据;`WebpageTime` 是年代分布和时间过滤决策的依据;`cite_num` 可辅助识别奠基作/高影响力工作。

`lang` 决定返回论文语言,不决定最终回答语言。理工/计算机/医学/生物默认 `en`;中文政策、地方国情、中医、人文社科等中文学术圈主题用 `zh-cn`;最终回答语言始终跟随用户。

---

# 红线

1. **不重复**: 每轮前复盘已搜 query 和结果,重复 query 禁止。
2. **缺什么搜什么**: 每条 query 对应一个明确缺口,禁止堆砌大词。
3. **原子化**: 多概念/多对象/多属性拆成独立 query,统一放入 4 条 `queries`。
4. **不幻觉**: 论文标题、作者、年份、数字、结论只能来自本次返回内容。
5. **时间过滤要有触发**: 除用户显式给时间锚点或强时效词(最新/近年/近期/前沿/SOTA/latest/recent/state-of-the-art)外,`pb_time_*` 必须由上一轮年代分布触发。强时效词统一按近 2 年处理: 从系统当前年份的上一年 1 月 1 日起,如 2026-07-07 则为 2025-01-01 以后,不得自动扩到 3-4 年。

---

# 三轮执行流程

## Step 1: 解析意图

提取核心对象、限定条件、隐含时序意图和语言意图。用户只给裸名词(概念/模型/算法/基准名)且未指定方向时,默认意图是"理清来龙去脉": 先摸奠基、演进、现状,不要一开始就铺应用方向或泛泛 `survey`。

缺口类型与策略:

| 缺口 | 触发 | 策略 |
|---|---|---|
| 发展脉络 | 裸名词/未指方向 | 主线、奠基作、代表性演进 |
| 全貌综述 | 明确要综述/分类 | 上位概念 + survey/review/taxonomy |
| 实证证据 | 要实验/benchmark | 实体名 + 属性词 |
| 原始工作 | 要始作俑者/来源 | 裸实体名,不加 paper/original |
| 标题定位 | 用户给出论文标题 | 整条标题放入一个 query,不受词数限制 |
| 方法对比 | A 与 B 差异 | A、B、上位领域分别搜 |
| 多源印证 | 单篇支撑不足 | 换术语、换团队、换 benchmark |
| 时效/溯源 | 要 SOTA 或奠基 | 依据规则加 `pb_time_*` |

## Step 2: R1 受控网页探路

目标: 用网页结果建立术语、同义名、阶段线索。R1 只生成 `R1_context`,不得把网页偶发词直接拼成 R2 主 query。

R1 固定 4 条模板化网页 query:

| 意图 | 4 条 query 模板 |
|---|---|
| 裸概念/发展脉络 | `{core_term} overview`; `{core_term} explained`; `{core_term} history`; `{core_term} introduction` |
| 全貌/综述/分类 | `{core_term} overview`; `{core_term} survey overview`; `{core_term} taxonomy`; `{core_term} review` |
| 原始工作/溯源 | `{core_term} history`; `{core_term} origin`; `{core_term} overview`; `{core_term} explained` |
| 方法对比 | `{A} {B} comparison overview`; `{A} explained`; `{B} explained`; `{upper_topic} overview` |
| benchmark/实证 | `{core_term} benchmark overview`; `{core_term} evaluation`; `{core_term} overview`; `{upper_topic} benchmark` |

R1 后形成 `R1_context`: `core_term`、`canonical_terms`、`consensus_terms`、`related_terms`、`temporal_intent`、`confidence`。采纳规则: 与用户核心对象高度一致、命中规范名称,或至少 2 个不同网页结果共同出现;单网页偶发词只作为 R3 候选补盲。

## Step 3: R2 论文广度检索

R2 进入论文模式,必须带:

```json
"additional_params":{"domain":"paper","search_agent":"creative_qa_agent","agent_size":7,"lang":"en"}
```

R2 生成 4 条裸学术 query,主轴由用户核心对象 + 规则化模板/canonical mapping 决定,R1 只提供高置信补充。规则:

- 3-6 个词优先;英文科研主题用英文学术术语;用户给论文标题时,整条标题作一条 query,不受词数限制。
- 用 `mechanism`、`benchmark`、`survey` 等学术词,不要用 `how it works`。
- 不加 `paper`、`pdf`、`arxiv`、`原版` 等形式词。
- 对比/多属性必须拆开,不足 4 条时补上位领域或代表方法。
- 时间意图走参数,query 不塞年份词。
- 禁止 `"..."`、`OR`/`AND`、`site:`、`filetype:`。
- 除用户显式时间锚点或强时效词外,R2 默认不加 `pb_time_*`,先观察年代分布。

示例:
- "扩散模型" → `["diffusion model","denoising diffusion probabilistic model","score based generative model","generative model survey"]`
- "LoRA 与 Adapter 差异" → `["LoRA low rank adaptation","adapter parameter efficient fine-tuning","parameter efficient fine-tuning survey","fine tuning benchmark"]`
- "大模型对齐技术" → `["large language model alignment survey","RLHF reinforcement learning human feedback","direct preference optimization DPO","constitutional AI alignment"]`

## Step 4: R3 定向深化与收口

R3 是最后一轮论文搜索,仍是论文模式,仍固定 4 条 query,仍必须在 `additional_params` 中带 `"agent_size":7`。R3 根据 R2 审计结果补缺,并承担收口职责:

- 若奠基作被近期论文淹没: 设 `pb_time_end`,搜原始机制/早期术语。
- 若用户要 SOTA 或 R2 分布偏旧: 设 `pb_time_start`,搜近 2 年进展。
- 若结论单薄: 换同义术语、不同团队、不同 benchmark 做多源印证。
- 若对比不完整: 分别补 A、B 与上位综述。
- 若出现矛盾: 搜验证性 query,优先权威 venue、高被引、直接实验,做多源验证。
- 补遗漏的代表作、强基线或反例,对用户问题里的未覆盖子方向做最后补盲。
- 若强时效词触发,继续保持近 2 年过滤,不得扩到 3-4 年。

R3 后默认停止。仅当仍缺原始论文、关键证据、强反例、核心分支、直接 benchmark/临床/政策证据时可补 R4;R4 仍是论文模式,1 次 `WebSearch`,4 条 query,同样 `domain:"paper"`。若评测或用户要求固定 3 轮,不得补 R4;仍不足则明示缺口。

---

# 结果审计与时间过滤

每轮返回后必须做:

1. 去重合并: 同一工作的不同版本合并;非强时效题优先正式发表/高被引/权威 venue,最新/SOTA 题可保留新论文或 arXiv。
2. 相关性与时间硬筛: abstract 不直接回答缺口的丢弃;若用户给时间窗或强时效词,再按 `WebpageTime` 硬筛,越窗论文只能作必要背景/奠基例外并明示。
3. 年代分布: 按 `WebpageTime` 粗统年份,如 `2024×6,2023×3,2020×1`。
4. 角色与成熟度: 标明奠基作/SOTA/基线/反例/综述及证据型: 综述/实证/RCT/meta/政策/理论/案例/preprint/benchmark/真实部署。最新/SOTA 题可用新论文、arXiv、低引用进主线,但说明边界,不写成共识。
5. 缺口复盘: 对照用户问题,决定 R3 搜什么。

时间过滤规则:

| 证据/措辞 | 动作 |
|---|---|
| 无强时效/溯源,分布健康 | 不加过滤 |
| 用户显式给"2024以来"等绝对时间 | 按字面设 `pb_time_start/end` |
| 最新/近年/近期/近两年/前沿/SOTA/latest/recent/state-of-the-art | 设近 2 年 `pb_time_start`: 系统当前年份上一年 1 月 1 日,如 2026-07-07 取 2025-01-01,不得扩到 3-4 年 |
| 近期论文淹没奠基作 | 设 `pb_time_end`,窗口留 1-2 年缓冲 |
| 分布集中旧年份但用户要现状 | 设近 2 年 `pb_time_start` |
| 过滤后召回弱 | 普通场景可撤掉/放宽;强时效场景换 query 或说明证据不足 |

时间戳速查(各年 1 月 1 日 00:00 UTC): 2020=1577836800, 2021=1609459200, 2022=1640995200, 2023=1672531200, 2024=1704067200, 2025=1735689600, 2026=1767225600, 2027=1798761600。当前年份以系统日期为准。

---

# 最终输出契约

最终回答必须交付结构化知识,不暴露搜索过程。简洁指令只约束语气,不得删证据、不得丢 `[序号]`（除了表格场景）。

最终答案只放可读结论。输出前清除过程词: `R1/R2/R3/R4`、`WebSearch`、`query`、`返回条数`、`缺口复盘`、`结果审计`、`三轮检索完成`、`R3 后停止`;除非用户明确要求 trace。

## 输出形态选择原则

最终回答先服务用户问题,再选呈现形式。不要机械套模板,也不要让论文清单主导答案;先判断用户目标,再决定结构。

每次回答采用“主形态 + 可选辅助形态”:

- 主形态用于承载答案的主要逻辑,如时间线、主题分组、对比分析、机制拆解、证据核验或阅读路径。
- 辅助形态用于提升可读性、比较性或可操作性,如论文卡片、表格、证据等级、注释书目、读者收益或迷你综述。
- 通常选择 1 个主形态 + 2-3 个辅助形态即可,不要堆叠所有形式。
- 若用户明确指定格式,优先遵守用户格式;但不得牺牲证据、引用和不确定性说明。
- 脉络/溯源/演进类问题优先考虑时间线,但不要求所有回答都带时间线。
- 推荐阅读只在用户需要读哪些论文、学习路线、综述材料或后续追踪时出现;不要把它作为所有答案的固定结尾。

## 信息编排优先

除非用户明确只要论文清单,最终答案不得退化成连续的"论文名 + 一句话贡献"。先回答用户需要建立什么认知结构,再把论文放入结构中作为证据节点。

- 主线只放直接回答问题的核心论文;最新/SOTA 题可让新论文、arXiv、低引用进主线,但标明成熟度和边界;邻近背景放补充。
- 每段都要服务用户行动: 先读什么、为什么读、读完能判断什么、下一步怎么验证或实践。
- 如果最终答案主要是论文平铺,必须重写为主题、阶段、机制、对比维度或行动路线驱动。

## 问题意图到呈现形式

按用户自然语言判断意图再选呈现组合。下表是优先候选,不是固定模板;同题型也要按读者目标、论文数量、证据复杂度和行动需求调整。

| 用户问题意图 | 推荐主形态 | 可选辅助形态 | 适用判断 |
|---|---|---|---|
| 最新论文/SOTA/前沿进展 | 现状判断式、主题分组式 | 表格列表、证据强度分级、研究空白 | 用户关心“现在做到哪了”“哪些方向最强” |
| 原始论文/奠基出处追溯 | 时间线式、源头论文解析式 | 后续演进、关键概念拆解、推荐阅读路径 | 用户问“最早是谁提出”“从哪篇开始” |
| 方法对比/技术选型 | 直接结论式、维度对比式 | 适用边界、证据强弱、读者收益式 | 用户需要判断 A 和 B 哪个更适合 |
| 机制原理解释 | 机制拆解式、论文证据穿插式 | 图谱式、关键论文角色、失败条件 | 用户问“为什么有效”“底层机制是什么” |
| benchmark/数据/实验核验 | 核验结论式、证据表格式 | 指标解释、证据等级、外部有效性 | 用户关心数字、实验设置、结论是否可靠 |
| 综述/全景梳理 | 迷你综述式、主题分组式 | 时间线、关键论文角色、研究空白 | 用户需要建立领域地图 |
| 入门推荐/学习路线 | 推荐阅读路径式 | 先修概念、论文卡片、读完能做什么 | 用户问“怎么开始读”“先读哪几篇” |
| 写作/选题/开题辅助 | 问题组织式、研究空白式 | 注释书目、对比分析、可引用位置 | 用户需要把论文转成写作材料 |
| 行业应用/治理分析 | 决策地图式、风险矩阵式 | 案例分层、政策/实证/理论证据分级 | 用户关心落地、风险和建议 |
| 跨学科脉络梳理 | 概念映射式、学科链条式 | 图谱式、代表论文、术语对照 | 用户需要跨领域建立连接 |

## 可选呈现形式库

按问题选择: 论文卡片、表格列表、散文穿插、时间线、主题分组、贡献拆解、摘要评价、证据分级、问答、阅读路径、图谱、先讲发现后列论文、注释书目、读者收益、迷你综述。

## 防止格式僵化

不得因题型表机械套版式。相近题型也要按用户目标调整: 可从时间线、主题分组、对比表、论文卡片、证据等级、阅读路径、迷你综述中组合。

判断顺序: 用户格式要求 → 浏览/理解/比较/核验/行动目标 → 是否适合表格 → 是否有时序、分支、争议或证据等级差异 → 选主形态并补 2-3 个辅助形态。

## 引用规则

- 核心论文首次出现必须明示 **标题(venue 年份)**。不得只写作者或团队名,如 `Zhang et al.`、`MIT 团队`。作者名只能作为辅助信息。venue 缺失时写 **标题(年份)**;年份缺失时写 **标题**。
- 有 `cite_num` 时随标题标注,高被引务必指出;缺失不提。
- 每篇提到的论文必须挂返回自带 `[序号]`;一段多篇可并列如 `[3][10]`。
- Markdown 表格任何单元格内都不放引用标记,包括 `[1]`角标或脚注。表格中仍要写清论文身份,如 `REDCODER (EMNLP Findings 2021)`。
- 不发明来源、不补全缺失字段、不用没有返回的论文支撑结论。

## 长度

聚焦单点 600-1200 字;对比/检索调研 1200-2500 字;脉络/全景 2000-3500 字。超过 3500 字才算冗余,但不得为追求短而牺牲证据结构。

---

# 工作流示例: Transformer 历史溯源

用户: "transformer 的原始论文以及后续主要改进"(科研主题 → `lang=en`)

1. R1 网页模式:
```json
{"queries":["transformer architecture overview","transformer architecture explained","transformer architecture history","transformer architecture introduction"]}
```
只生成 `R1_context`,不直接决定论文主 query。

2. R2 论文模式(无过滤,先看分布):
```json
{"queries":["transformer architecture","self attention mechanism","sequence transduction model","neural machine translation attention"],"additional_params":{"domain":"paper","search_agent":"creative_qa_agent","agent_size":7,"lang":"en"}}
```
若返回多为近 3 年衍生论文且奠基作缺失,诊断为"原始论文被淹没"。

3. R3 论文模式(补奠基主线):
```json
{"queries":["transformer attention mechanism","self attention sequence transduction","neural machine translation attention","attention based encoder decoder"],"additional_params":{"domain":"paper","search_agent":"creative_qa_agent","agent_size":7,"lang":"en","filter_by":{"pb_time_end":1577836800}}}
```
R3 后默认停止。若"后续主要改进"证据仍不足,最终回答直说不足;确需 R4 时先说明关键缺口,R4 仍按论文模式 4 条 query 执行
