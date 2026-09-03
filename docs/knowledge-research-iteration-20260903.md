# Iterative Knowledge Research Improvements

## Scope and Baseline

User approval: implement the audited report-workflow fixes, using subagents with at least two produce/evaluate/improve rounds in each phase. This is a forward-looking change for future research, not an edit to existing conversations or reports.

- Development branch: `codex/knowledge-evidence-ledger-v1`, starting commit `26991ed2462d437a572f89c29e8f17350156faf9`, no configured upstream; `origin` fetched before edits.
- Knowledge baseline: `9e73f6a326601b626aa57a1a11aa88dcebea4834`. No ingestion, embedding, ranking, vector database, or PDF-processing changes are planned in this workstream.
- Existing focused baseline: `tests/test_scripts/test_knowledge_research_sidecar.py`, **11 passed in 1.41s** on 2026-09-03, isolated temporary root.
- Never modify live immutable releases, historic reports, Preview/g-fleet data, or running Chunk/PDF/Embedding pipelines. A release must identify an exact commit and retain rollback.
- Knowledge supplies evidence, not analytical answers. Integrity verification must not imply semantic correctness or visual review.

## Defects and Intended Remedies

| Defect | Intended remedy | Acceptance evidence |
|---|---|---|
| Research executed as a one-way sequence | Skill loops discovery, scoped reading, gap/conflict checks, and further discovery without a fixed total-file quota | Independent forward evaluation can discover a new topic after reading and return to search; stopping depends on evidence, not a source-count target |
| Repeated long file arrays and copying mistakes | Research-namespaced immutable scope/file references with exact expansion and resumable mappings | Cross-research rejection, deterministic retry, subset selection, explicit behavior beyond the upstream 20-file bound |
| Missing research context bypasses compact response and overflows MCP stdio | Require research context before upstream research calls; retain bounded responses | Missing/invalid context makes zero upstream requests; UTF-8 response sizes stay within transport bounds |
| Model sees truncated chunk text but ledger holds full content | Explicit bounded continuation for full evidence, with truthful delivered/read progress | Reassemble full original text with its hash; reject stale/mismatched references; no silent loss |
| Table preview cuts raw HTML mid-header | Structured HTML/Markdown table preview with useful header/sample information and bounds | Rowspan/colspan, malformed markup, long tables, numbers, and empty cells covered |
| Screenshot presence is conflated with visual verification | Distinguish artifact integrity, text extraction quality, and visual-review capability/state | Text-only path never reports visual review; original screenshots remain available and embedded |
| One bad claim causes a whole-report rewrite | Small atomic claim batches, stable keys, idempotent replay, precise error positions | Retry does not duplicate claims; conflicting replay rejected; failed batch does not partially persist |
| Bibliography uses conversion comments as titles | Deterministic source-title cleanup with conservative identity grouping | Converter comments excluded; dated issues preserved; verified PDF/Markdown grouping retained |
| Many citations mistaken for independent corroboration | Skill checks claim support, independent sources, horizons, units, and conditional scenarios | Evaluation distinguishes repeated evidence, duplicate formats, and conditional forecasts |
| Trace/usage gaps misreported as complete measurements | Preserve explicit limits in diagnostics; assess minimal observability additions | No fabricated per-tool durations, true billed totals, or automated factual verification |

Known OCR column omissions require a separate extraction-quality remedy or actual visual/human review. This report-adapter change must not relabel old extraction as repaired or automatically validated.

## Subagent Ownership

| Role | Design/implementation responsibility | Independent evaluation |
|---|---|---|
| Godel | Search scopes, short references, continuation, bridge integration | Parfit and main integration review |
| Hubble | Table projections, bibliography labels, rendering honesty | Parfit and focused artifact checks |
| Dalton | Atomic/idempotent claims, state transactions, concise iterative Skill | Parfit and independent workflow exercise |
| Parfit | Adversarial design/code review and isolated regression gates | Authors must address findings before second-round acceptance |

Write sets will be frozen after design review. Shared files have one owner at a time. Agent results are integrated in the same approved developer worktree, never in a live release.

## Round Ledger

| Phase | Round | Produce | Evaluate | Improve / outcome |
|---|---|---|---|---|
| Design | 1 | Godel, Hubble, Dalton produced slice proposals; baseline defect inventory produced | Parfit completed independent review: six high-priority contract gaps; constructed 92,521-byte legacy-style response; main reviewed excessive new tools and mandatory plan proposal | R2 must address full-wire bytes, truthful delivery levels, snapshot coverage, source revisions, process-level transactions, and extraction-quality negative fixture |
| Design | 2 | Authors revised and froze interfaces: original search names retained, maximum two new read/navigation tools; one state transaction callback; no mandatory paragraph plan; no fixed iteration count added to future Skill | Main evaluated revisions; Parfit second independent review **passed**, no remaining design blockers | Frozen 60KiB wire bound, explicit >20-file groups, namespace/version-bound references, honest projection coverage, and immutable report generations |
| Implementation | 1 | A/B/C produced separate navigation, table, transaction/Skill slices; main versioned private media adapter | Independent media review: 13/19 passed, six failures; A review: 21/25 passed, four failures; A independently reproduced loss of a legacy finalized manifest in C | Findings: redirect credential forwarding, inconsistent extraction metadata across pages, candidate Skill isolation, legacy artifact-history preservation |
| Implementation | 2 | Authors rejected redirects/proxies, checked full page contracts on both paths, isolated candidate rules, added report recovery navigation, replaced manual Markdown splitting with MarkdownIt, preserved legacy manifests | Independent media/candidate **35 passed**; A independent **25 passed**; C legacy regression **4 passed**; B's four added findings independently closed after corrections | Independent review complete; old assertions retained or explicitly updated only for the intentional full-text projection contract |
| Verification | 1 | Full isolated suite plus actual stock MCP stdio, restart/continuation, claim/table replay and WeasyPrint output; independent Skill forward exercise | Combined suite **178 passed / 4 failed / 1 skipped** (four A contract-drift failures subsequently fixed). Opt-in real renderer run **2 passed in 7.40s**. Skill R3 confirms fixes with one wording ambiguity | Add direct legacy-finalize regression, clarify revision rather than nonexistent paragraph deletion, re-run combined suite and actual runtime |
| Verification | 2 | Pre-final **199 passed** exposed remaining coverage gaps in independent review; corrective final run with real renderer: **213 passed in 11.26s** | C independently re-ran all four original table/bibliography reproductions against unchanged corrected files; all passed. Main final Ruff, format and mypy (17 files) passed | Code gates complete. No claim of full LLM behavior or OCR repair; deployment identity and switch receipt are recorded separately |

## Release Gates

- Exact commit, focused regression and changed-path static checks.
- Independent fixture roots; never mutate production search data as tests.
- Preserve original screenshot-only PDF, self-contained HTML, readable citations, and private machine identifiers.
- Existing task/report compatibility and rollback must be assessed explicitly.
- No claim of repaired OCR, actual image understanding, or full-service behavioral performance without corresponding evidence.
- Deployment, existing-service impact, and any unrun paid/live model tests must be reported honestly.

## Design R1 Review Applied to R2

The following are required constraints for the second design round, not claims that implementation or validation has already passed:

1. Outer MCP JSON-RPC lines must remain at or below 60KiB including envelope, string escaping, duplicated structures if any, and newline. Pagination must preserve the remainder. Errors and metadata-only responses also need bounds. A guard error alone must not discard a successful search with no recoverable continuation.
2. Storage integrity, returned projection coverage, and model-request delivery are separate concepts. The bridge can report the first two; actual model delivery remains unknown absent a trusted Gateway receipt. None proves understanding.
3. Snapshots retain ordered members, content revisions/hashes, and returned intervals. Retry must not inflate unique counts. More than 20 selected files must require explicit selection or stable sub-scope references, never silently truncate or merge incomparable shard Top20 results.
4. Scope references are research-namespaced capabilities within the existing trusted Gateway boundary, not replacement authorization. The implementation must not claim new multi-user isolation without a trusted caller identity.
5. Claim text, stable keys, and successful replay receipts commit together under cross-process serialization. Invalid batches leave prior report state intact. Finalize/write failure and append/finalize races require explicit tests.
6. Table previews must propagate upstream truncation, including truncation done by the current inner media adapter before the research bridge. Table SHA verification remains independent of extraction completeness and visual review.

## Frozen Shared Interfaces

- Existing `search`, `searchByIds`, `getFileDetails`, `getTable` remain. Search creates stable references automatically. Scoped search accepts exactly one of scope references, file references, or legacy file IDs; more than 20 files returns explicit groups without executing a hidden global or sharded query.
- At most two additional model tools: `researchNavigate` for bounded snapshot/coverage/resume and `researchReadEvidence` for canonical ledger text continuation.
- `atomic_update(research_id, mutator)` and `record_knowledge_call/error(..., on_commit(state, call))` hold the same per-research cross-process lock. The callback runs before save, can roll back the whole transaction, and cannot perform network I/O or nested transactions.
- Claim batches optionally use `batchKey`; paragraphs optionally use `claimKey` and `expectedClaimHash`. Legacy keyless callers retain legacy append behavior, not a new idempotency promise.
- Finalize may check an expected submitted-item list but does not require a predetermined research plan. New artifact generations preserve previously published files; failed attempts cannot delete them.
- Table projections use `page: {start, end, total, hasMore}`, half-open Unicode code-point intervals for text and item intervals for inventories. `projectionComplete` describes this response, not the last page or model understanding. `extractedInventoryComplete` describes enumeration of detected tables only.
- Private media hop: source text preserved, maximum 16MiB serialized response and 8MiB collected inventory. The outer model hop independently enforces 60KiB; source truncation is never repaired by changing a label.

## Independent Skill Forward Evaluation

Copernicus evaluated the three original live instruction files without the audit conclusions and without making service calls. R1 independently identified the file-discovery stopping condition, no explicit coverage-failure recovery, ambiguous resubmission scope, and absence of actual image viewing. R2 assessed the new canonical Skill plus workspace AGENTS/TOOLS against the same natural task and schemas; R3 confirmed fixes. Its remaining deletion ambiguity was corrected to same-key paragraph revision, not a nonexistent deletion API. This is instruction-level behavioral evaluation, not a claim that a live model run has passed.

## Isolated Runtime Receipts

- Candidate: `/mnt/data/opensquilla-dev/tmp/research-gateway-candidate-20260903`, loopback port 19637. New home/state/workspace/private roots; no old sessions or reports copied. Same Gateway core and model routing; separate sidecar source. Live Full-v10 and ingestion service PIDs remained unchanged during this stage.
- Stock MCP client test includes a large CJK/escaped-content reply, explicit full continuation, child process restart, snapshot replay, scoped search, table retrieval, idempotent submission, real PDF output, embedded CJK fonts, image objects and screenshot-only PDF table representation.
- Actual local Knowledge smoke, one bounded sample: search (5 hits) 1.271s, scoped search (5 hits) 0.149s, details (17 extracted entries) 0.067s, table 0.037s, finalize 5.169s. Receipt: candidate `private/live-tools-1788418753.json`. These are individual tool measurements, not end-to-end Agent latency or a performance benchmark.
- Generated sample PDF was inspected as rendered pixels: readable Chinese heading, original table crop, readable bibliography, A4 pages. The source excerpt was deliberately verbatim test material, not an authored research report.
- Gateway tool registration and full Skill retrieval were checked using authenticated loopback RPC. This does not prove live-model tool selection, comprehension, publication or completion quality.
- No live LLM research task or broad core/full-repository regression has been run in this change. OCR omissions and complete failed-request cost accounting remain outside this sidecar fix.
- Second bounded sample: search 0.243s, scoped search 0.089s, details 0.060s, table 0.034s, finalize 5.165s. Receipt: candidate `private/live-tools-1788419045.json`; warm-cache observations must not be presented as a general speedup.
- Pre-final combined JUnit receipt SHA256: `c726f279c7b39775e4d12b21d020e182232511a0bfc33597f9fdaa65571c2741`. Later corrective checks are required before this becomes a release receipt.
- Final combined JUnit: `/mnt/data/opensquilla-dev/tmp/research-release-final-20260903.xml`, **213 passed**, SHA256 `b9d881e7fa66158f4f1d98cf8953b983891143baaa6f9e50680470d61c299d45`.
- Existing MCP stdio/artifact-validation boundary tests: **126 passed, 1 skipped** (optional external MCP SDK unavailable). No broad repository-wide regression was run.

## Release and Recovery

The reviewed rollout exports only the exact commit's research sidecar to a new immutable release. It changes the Full-v10 research-current pointer, the MCP launch arguments, two tool allow entries and the three canonical instruction files. Gateway core, routing, Knowledge service, proxy and old reports remain unchanged.

Two static rollout reviews were performed. R1 required durable recovery metadata, protection around the first service-stop attempt and restoration of file mode/ownership. R2 confirmed those corrections and authenticated tool/Skill checks in addition to health checks. This was not a fault-injected live rollback drill.

The switch requires an idle Full-v10 Gateway and a short maintenance window; it is not zero-downtime and does not eliminate the final idle-check/stop race. Backup state is retained for explicit recovery only, never automatically restored over valid new work. Configuration/instruction bytes, hashes, ownership, previous pointer and service identity are saved before stopping. Failed activation attempts restore the prior configuration and pointer. A durable recovery manifest supports an interrupted rollout.

No schema migration or reindex is required: changes are additive research-sidecar state and tool projections. Old artifacts are not rewritten; repeated finalization creates immutable output generations and preserves prior manifests. A rollback does not promise old sidecar code understands the new short-reference workflow; start a fresh research task after a downgrade rather than replaying new-only tools against old code.
