# Multi-model fusion monthly evaluation

Use this workflow to decide whether an OpenSquilla proposer/aggregator lineup
should replace the current baseline. A monthly cycle may keep the baseline; a
calendar change is not evidence that a model change is beneficial.

## Decision sequence

1. Freeze the currently deployed lineup, OpenSquilla revision, AEF revision,
   dataset, prompts, tool policy, timeouts, retries, cache policy, judge protocol,
   and pricing snapshot.
2. Build a short list from current model availability, role suitability, and
   target-channel pricing. Treat vendor benchmark claims as screening evidence.
3. Change one role or one declared lineup dimension per candidate when causal
   attribution matters. Keep the full agent loop for the final comparison.
4. Run baseline and candidates on the same complete task set. Preserve failures,
   retries, tool calls, and costs instead of filtering to successful tasks.
5. Report quality, cost, token use, request/tool counts, latency, score coverage,
   uncertainty, and representative cases. Separate same-run comparisons from a
   historical baseline.
6. Promote a lineup only after an independent confirmation run satisfies the
   predeclared quality and cost gates.

## Required configuration record

For every group record proposer order, aggregator, fallback model, provider and
model IDs, thinking level, output/context limits, candidate length, proposer and
aggregator timeouts, minimum successful proposers, retries, shuffle policy,
tool permissions, and cache behavior. Store credentials only as environment
variable names.

The reusable profile format is documented in
`experiments/monthly-fusion/lineups.example.json`. Materialized profiles are
immutable within one campaign.

## Metrics

Quality uses the complete planned task denominator. A task without a complete
native judge score contributes operational zero to AvgQ and AvgPass, while the
report separately exposes scored coverage and the reason for missing scores.

Generation cost includes proposer, aggregator, tool-continuation, failed, and
retried physical requests. Prefer recorded provider cost. When it is absent,
estimate from input, output, cache-read, and cache-write tokens using the frozen
price snapshot. Keep recorded and estimated subtotals separate.

Always report at least:

- AvgQ, AvgPass, score coverage, generation failures, and exhausted judge units.
- Average and total generation cost, judge cost, and exact-cost coverage.
- Input, output, reasoning, cache-read, visible, and total tokens.
- Tool calls, tasks using tools, LLM requests, total steps, p50, and p95 latency.
- AvgQ, average generation cost, p50, and p95 percentage change from the named
  baseline, calculated from unrounded values.

AvgQ percentage change is distinct from an AvgQ point difference. A negative
cost or latency percentage means the candidate is cheaper or faster.

## Evidence limits

Use paired task analysis and confidence intervals for quality claims. A positive
point estimate whose interval crosses zero does not establish superiority or
non-inferiority. A historical baseline is useful for exploration but cannot
isolate model effects from time, service load, search results, or cache state.

Case studies must come from the evaluated task set and retain counterexamples,
judge ambiguity, attachment coverage, and any exposure to existing answers.
Do not use a favorable case to replace the aggregate result.

## Repository boundary

Reusable harness code, documentation, and offline tests belong under the
existing `experiments/monthly-fusion` directory. Raw runs, catalog snapshots,
receipts, reports, and campaign-specific reviews belong under ignored
`reports/`. Temporary diagnostics, bytecode, logs, and superseded patch copies
should not enter the public source tree.
