---
name: knowledge-research-finance
description: Apply financial and market reasoning controls to local Knowledge research, including prices, earnings, valuation, forecasts, and scenario analysis. Load with knowledge-local-research when financial numbers or market attribution matter.
---

# Financial research companion

Use this companion only with `knowledge-local-research`. Knowledge MCP remains
the only factual source. These rules control how numbers and market arguments
are verified; they do not supply data.

## Verify every material number

Bind each important number to all of the following before using it:

`entity -> metric -> original value -> unit/currency -> data period -> actual or estimate -> source`

Preserve original units when they are clearer. Check scale before converting:
`1 KRW bn = 10 亿韩元`; `1,000 KRW bn = 1 万亿韩元`. Do not turn aggregate
profit into EPS, turnover into net flow, percentage points into percent, or an
adjacent statistic into the requested metric. Verify the definition in source
text, headings, or footnotes rather than inheriting it from a filename or report
date.

For prices and returns, carry the exact benchmark or entity and observation
date into the sentence or caption. Separate price performance, earnings change,
and valuation change. Compare like with like: index versus company, forward
versus trailing earnings, and full-year versus quarterly data are different
measurements. Without comparable inputs, explain the mechanism qualitatively
instead of inventing a numerical decomposition.

## Forecasts and disagreement

Attribute every forecast to its author, publication date, and horizon. Keep
forecast vintages and definitions separate. Do not average incompatible target
prices, present different horizons as one consensus range, or describe a
conditional downside case as a guaranteed floor. Explain disagreements through
assumptions, timing, sample, or metric definitions.

## Scenarios and causal claims

A scenario must have this chain:

`condition -> mechanism -> possible outcome -> observable signal`

State what supports the proposed mechanism, the strongest alternative
explanation, what weakens the judgment, and what would change it. Do not invent
probabilities, current quotes, target prices, or precise triggers. Mark a
statement as a source fact, synthesis, or unresolved question when the boundary
could otherwise be unclear.

Before `mcp_researchAddClaims`, check that every financial claim has evidence
containing the same entity, number, period, and qualification. If a value is
only visible in a table, use the table exhibit and its fully sourced caption;
do not repeat an unsupported value in prose.
