# Knowledge research Skill split

This release separates research guidance by job while keeping
`knowledge-local-research` as the stable entry point:

| Skill | Responsibility | When to load |
| --- | --- | --- |
| `knowledge-local-research` | source boundary, investigation order, drafting and review hand-offs | every local research task |
| `knowledge-research-finance` | entities, units, periods, actual/estimate status, valuation and scenarios | financial or market analysis |
| `knowledge-research-pdf-tables` | PDF inventory, table metadata, footnotes, text/crop fidelity | PDF/table/chart evidence |
| `knowledge-research-report` | claims, exhibits, review, finalization and three-artifact publication | reports or artifact delivery |

The coordinator asks the agent to load companions with `skill_view`. Skills add
procedural context; they do not create tools or bypass the Gateway/Bridge
guards. Research state, evidence binding, idempotency, review gates, rendering,
and publication remain in the existing research sidecar.

## Test prompts

Use the deployed `knowledge-full-v11` test route with these prompts:

1. **Core only:** `只做一个快速事实核查：知识库里关于[主题]最新一份材料的发布日期和标题是什么？不要写报告。`
2. **Finance:** `请研究[公司/指数]在[期间]的表现，区分价格、盈利和估值，并说明至少一个相反解释。数字必须带实体、单位、期间和实际/预测标签。`
3. **PDF/table:** `请根据知识库中的核心 PDF 检查[指标]，先列出相关表格的单位、期间、脚注和可用性，再说明这些表格能支持什么结论。`
4. **Report:** `请对[主题]做一份深度研究报告，要求 HTML、PDF、provenance 三个产物，包含一个真正有用的原始表格展品，并在发布前复核证据绑定。`
5. **Cross-domain:** `请分析[金融主题]并交付报告。先加载金融、PDF/表格和报告规则；遇到缺失证据时缩小结论，不要补数字。`

Expected behavior can be checked in the run trace: the coordinator is read
first, then only the companions relevant to the prompt. The current Full-v10
deployment remains unchanged for rollback and comparison.
