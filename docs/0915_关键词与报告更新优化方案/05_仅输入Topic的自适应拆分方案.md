# 仅输入 Topic 的自适应拆分方案

## 1 输入事实

当前拆分接口只有一个用户输入：`query/topic`。系统拿它执行 Web Search，再根据检索到的文献生成子问题。没有额外的领导问题，因此实现不能依赖用户先选择“技术演进、长鑫机会、内部落地”等意图。

这种输入条件下，自适应的含义应是：

```text
Topic 的语义类型
  + Topic 文字中是否显式包含对象或目的
  + 长鑫默认管理关注点
  + 本次检索材料真实出现的议题
  → 本次子问题结构
```

如果 Topic 只是“深度学习”，系统应设置 `analysis_mode=overview`，生成综合认知结构，不能虚构“领导正在决定投入”之类的具体意图。

## 2 第一步：结构化理解 Topic

建议增加一个 `profile_topic` 模型任务，输入只有 Topic 和组织背景，输出固定 Schema：

```json
{
  "normalized_topic": "深度学习",
  "topic_type": "新技术或能力",
  "analysis_mode": "overview",
  "explicit_intent": null,
  "explicit_objects": [],
  "time_scope": null,
  "intent_source": "default_overview",
  "assumptions": ["用户未指定具体决策问题，采用综合认知视角"]
}
```

程序只允许有限枚举：

- `topic_type`：新技术或能力、行业市场变化、政策变化、竞争对象、经营问题。
- `analysis_mode`：`focused` 或 `overview`。
- `intent_source`：`query_explicit` 或 `default_overview`。

Topic 本身包含明确表达时才进入 `focused`。例如：

| Topic/Query | 解析结果 |
| --- | --- |
| 深度学习 | 综合认知，不推测具体决策 |
| 深度学习对 DRAM 的影响 | 聚焦影响，显式对象为 DRAM |
| 是否应该在质检中使用深度学习 | 聚焦内部落地和方案选择 |
| 某竞争对手的深度学习芯片进展 | 聚焦竞争对象和能力进展 |

## 3 第二步：根据 Topic 生成检索策略

如果 Web Search 支持多个 Query，建议先由 `profile_topic` 生成 3—5 个检索意图，再分别搜索。对于宽泛的“深度学习”，可以使用：

```text
深度学习 技术架构 最新进展 一手来源
深度学习 主要应用 落地数据 局限
深度学习 算力 内存带宽 DRAM 需求
深度学习 产业链 主要厂商 竞争格局
深度学习 风险 成本 失败案例
```

这些是检索方向，不是最终模块名称，也不是固定模板。具体查询应随 `topic_type` 变化。

如果现有 Web Search 只能接收原始 Topic 并返回一次结果，也可以继续：系统直接对返回文献做议题提取和聚类，只是材料覆盖可能更偏向搜索引擎已有排序。此时应在结果中返回 `search_gaps`，说明缺少哪些方向，而不能用模型常识补齐正式模块。

## 4 第三步：从检索材料产生议题簇

对文献去重、评估和排序后，提取以下结构：

```json
{
  "issue_id": "issue_memory",
  "issue_name": "模型规模增长带来的内存容量和带宽需求",
  "topic_relevance": 2,
  "cxmt_relevance": 2,
  "management_value": 2,
  "evidence_ids": ["E12", "E19"],
  "counter_evidence_ids": ["E25"],
  "qualifiers": ["主要针对训练场景"]
}
```

此时先保留完整议题，不要急着压缩成六个字。文献聚类可以使用 Embedding 做初步分组，再让大模型合并语义相近的议题；第一版也可直接让模型结构化输出，但必须校验证据引用。

## 5 第四步：用 Topic 类型和长鑫视角转换为管理问题

Topic 类型只提供候选维度库：

| Topic 类型 | 可参考的管理维度 |
| --- | --- |
| 新技术或能力 | 技术演进、成熟度、应用、需求影响、投入条件、风险信号 |
| 行业市场变化 | 需求、价格、库存、竞争、客户、经营传导 |
| 政策变化 | 适用范围、影响链、响应时点、资源要求、未明确事项 |
| 竞争对象 | 战略动作、真实能力、产品客户、产能投入、竞争影响 |
| 经营问题 | 问题规模、根因、选项、成本约束、验证指标 |

维度库是软约束。一个维度只有在以下情况下才能成为正式模块：

1. 本次材料中存在对应议题；或者
2. 虽无材料，但它是会显著影响综合判断的关键缺口，此时标记为 `gap`。

长鑫组织背景负责计算公司相关性，例如“深度学习”材料中的模型结构、应用落地、算力变化很多，但对长鑫领导应优先解释其对内存容量、带宽、产品路线、客户需求和产业竞争的影响。不能反过来强行让所有 Topic 都生成“良率、产能、客户”模块。

## 6 第五步：候选评分、收敛和短标题

每个候选模块至少保存：完整核心问题、公司相关性、管理价值、证据、反证、范围和标题。推荐沿用可解释评分：

```text
候选分 = 2×Topic相关性 + 3×管理判断价值 + 证据适用性 + 模块独立性
```

筛选规则：

- 正式模块通常 4—6 个，不强行凑数。
- 同义议题合并，颗粒度不一致时提升或下沉为二级问题。
- 相关性或管理价值为 0 的候选淘汰。
- 有证据的主题为主体，关键 `gap` 通常不超过两个。
- 最后单独把完整问题压缩成 2—6 字名词短语。

对于宽泛 Topic“深度学习”，假设材料确实覆盖相关内容，可能得到：

| 短标题 | 完整核心问题 |
| --- | --- |
| 模型架构 | 主流架构怎样变化，哪些变化会影响算力和内存特征？ |
| 落地场景 | 哪些应用已经形成可验证价值，哪些仍停留在试验阶段？ |
| 算力需求 | 训练和推理对计算、互连和系统资源提出什么要求？ |
| 存储影响 | 容量、带宽和内存层级变化会怎样影响 DRAM 产品需求？ |
| 产业格局 | 主要参与者的技术、产品和生态位置如何变化？ |
| 风险信号 | 哪些成本、可靠性、合规或需求信号会改变当前判断？ |

这只是结果示例。如果检索材料完全没有“产业格局”，系统不应为了目录完整强行保留该模块。

## 7 推荐 Prompt

```text
输入只有用户 Topic、长鑫组织背景和本次检索文献，不存在额外的领导问题。

第一步判断 Topic 类型，并检查 Topic 原文是否显式包含影响对象、行动目的、比较对象或时间范围。
只有原文明确表达时才能填写 explicit_intent；否则使用 analysis_mode=overview，
不得推测用户正在做投资、立项或技术路线选择。

先根据文献形成完整议题，再把议题转换成对长鑫领导有管理价值的问题。
材料决定本次有哪些正式模块，公司背景只影响管理解释和优先级。
不得为了覆盖预设维度生成没有材料的正式模块。

最终输出 4—6 个模块，不强行凑数；标题为 2—6 个字符，
完整 core_question 必须保留，且每个正式模块都引用真实文献片段。
```

## 8 代码流程

```python
def decompose_from_single_topic(topic: str):
    profile = profile_topic(topic, organization_context)
    queries = build_search_queries(profile)       # 如果搜索服务支持扩展
    documents = web_search(queries or [topic])
    ranked = rank_documents(topic, documents, organization_context)
    issues = extract_and_cluster_issues(ranked)
    candidates = build_management_modules(profile, issues, organization_context)
    reviewed = review_structure(candidates)
    return compress_module_titles(reviewed, max_chars=6)
```

如果 Web Search 已由上游完成，拆分接口直接从 `documents` 开始执行，最好同时接收原始 `query` 和 Query—文献映射。

## 9 当前代码需要怎样调整

当前 `/decompose` 已允许 `leadership_question=None`，并把 Topic、组织配置和文献传给候选生成 Prompt，因此可以处理只有 Topic 的请求。但当前“意图识别”和“模块生成”在同一个模型阶段，难以确认模型有没有擅自补出决策目的。

建议增加：

- 独立 `TopicProfile` Schema 和 `profile_topic` 任务。
- `analysis_mode=overview|focused` 与 `intent_source` 字段。
- Query—文献对应关系和 `search_gaps`。
- 议题卡及候选模块保留/合并/淘汰记录。
- 针对宽泛 Topic、带显式影响对象 Topic、带行动目的 Topic 的回归测试。

