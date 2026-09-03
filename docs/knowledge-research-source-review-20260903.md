# Research Source Review: 2026-09-03

## Scope

This change updates only the research MCP sidecar, report renderer and local
research Skill. Knowledge retrieval, chunk/table/embedding pipelines, Gateway
core, model routing and historical report artifacts are unchanged. Release is
restricted to the Full-v10 Gateway after its task queue is idle, with an exact
Git release and recoverable configuration/sidecar pointer.

## Observed Defects

The latest user trial completed in 211.786 seconds but performed no file-scoped
search. Six discovery calls projected 118 distinct excerpts from 105 files.
Those counts were not evidence that 105 complete files had been read. Three
table directories contributed substantial redundant cell/layout metadata.
Four table submissions failed because model-visible selector options encouraged
both the legacy and short selector to be supplied. Several financial statements
misread attribution, target direction or the meaning of average daily turnover.
An English implementation-oriented table disclaimer led a Chinese report.

The Skill was delivered in full; the shorter transcript preview was a UI
projection. No fix should treat that preview as proof of missing model input.

## Iteration 1

- Compact search metadata while retaining evidence content, locator and private
  source hashes. Add readable file metadata and conservative duplicate-passage
  hints; do not merge whole documents based on equal text or series titles.
- Report successful scoped calls and prepared excerpt coverage explicitly,
  without labeling them as full-document reading or comprehension.
- Expose one table selector to the model; retain strictly validated legacy
  compatibility at the sidecar runtime boundary.
- Compact table inventories into text rows plus sparse span metadata, retaining
  original canonical table text, empty cells and source truncation indicators.
- Add explicit standard/deep research mode and a source-comparison view for
  submitted paragraphs, exact cited excerpts and used table text/captions.
- Teach an evidence-driven discovery/scoped-reading loop, institution-specific
  attribution, units/horizons/direction checks, revision and precise publication.
- Localize report labels and place a concise table limitation note after the
  references. Keep known material defects beside the affected table.

## Iteration 2

Independent review identified and prompted corrections to the first iteration:

- Check deep prerequisites before bibliography metadata calls, then recheck
  inside finalization. A pending result has no artifact manifest.
- Support caption/section correction by compare-and-swap using the current
  table item hash. Identical replay remains compatible; stale edits conflict.
- Bind current review preparation to bibliographic identity as well as report
  and evidence content. Corrected source metadata invalidates an old review.
- Preserve nested page ranges and section locations, including a bounded
  continuation path for oversized metadata instead of a permanently broken cursor.
- Remove legacy selector guidance from model-facing instructions and reduce
  duplicated workflow prose. Offline forward evaluation checks tool sequencing
  and financial interpretation separately from schema/unit checks.

## Behavioral Contract

`researchBegin(mode="deep", language="zh-CN")` starts a deep Chinese report.
Mode is immutable; old states and omitted mode retain standard behavior.
`researchNavigate(view="review")` returns paginated comparison material. Follow
all pages, compare each paragraph with its own sources, group corrections, then
read a fresh comparison after editing. `researchFinalize` returns `needs_review`
when deep prerequisites remain; only `finalized` includes a publishable manifest.
Metadata completion can itself require a fresh comparison.

A completed empty scoped search satisfies only the action gate, not research
sufficiency. Prepared comparison ranges are not semantic verification. The
service does not inspect screenshots or claim that OCR/table detection is
complete. The Agent still decides when useful evidence gains have ended and
must disclose material gaps, rather than meet fixed citation/word quotas.

Current review preparation is report-wide, not incremental by edited paragraph;
it therefore adds some repeated context and tool calls. This tradeoff must be
reported honestly, not presented as a guaranteed end-to-end speedup.

## Actual-Run Follow-up

A fresh isolated Gateway trial exercised the actual model-visible schemas and
complete Skill. It ran 46 tool calls in 349.262 seconds: six discovery searches,
two scoped searches, four table inventories, six table reads, seven paragraph
batches, five table additions, nine navigation calls, two finalize calls and
three successful publications, plus Skill load and research begin. All tool
results were successful; the first finalize correctly returned a pending review
after bibliography metadata changed. The final report had 18 paragraphs, five
original table crops and 10 A4 pages with embedded Noto CJK fonts.

This run exposed another quality issue despite successful tool execution: one
paragraph manually listed literature, duplicating the automatic bibliography
and inflating apparent source usage. The run's 41 references must therefore not
be presented as 41 substantively validated sources. The follow-up adds explicit
Skill/tool instructions and an atomic rejection for newly authored reserved
bibliography sections in deep mode. Existing committed replays remain valid;
historical reports are not rewritten. An arbitrary source list under a different
heading still requires Agent judgment; this is not a semantic classifier.

The same actual-run review also found incorrect attribution, forecast year and
target price in a semiconductor paragraph despite complete review projection.
The follow-up therefore changes comparisons to paragraph/source groups, repeats
shared excerpts beside each relevant paragraph, and links them with `forClaimItem`.
The Skill requires concrete numeric/year/unit/attribution checks as each complete
group arrives, rather than a generic assertion that all review pages were read.
This adds repeated context deliberately; it does not turn the sidecar into an
LLM or certify interpretation. The broad trial remains a preserved failure sample,
not a semantically approved report.

On the exact same 73 stored table inventory entries, summary JSON decreased
from 171,838 to 58,631 UTF-8 bytes (65.9%). This is a projection-size measurement,
not an end-to-end speed claim. The broader actual trial was slower than the
211.786-second prior user trial because it performed more reading and comparison.

A second, narrower buyback task completed in 443.847 seconds with 35 tool calls,
13 paragraphs, eight sources and three original table crops. It made two scoped
searches and two actual claim revisions after comparison, with no tool errors or
manual bibliography section. The unchanged router selected a different model
for this task, so it is not a controlled performance or model-quality A/B.

That trial still missed an explicit 1,000x amount-equivalence contradiction.
A final deterministic guard now returns `NUMBER_SCALE_MISMATCH` for nonzero,
same-currency Chinese amount expressions joined directly by "namely/equal to"
whose normalized magnitudes differ by at least 10x. It is intentionally narrow:
keyed deep-report claims only, complete single amounts, no range endpoints,
cross-currency comparisons, percent denominators or general semantic checking.
Known error examples are excluded within their current sentence. The guard does
not choose a correct value. CAS correction and fresh review are required; simply
reading the comparison again does not remove an unresolved contradiction.

The unchanged real candidate was replayed read-only against this guard and
blocked without publishing a manifest. Independent fixtures exercise correction,
fresh review and successful finalization. No additional full model run was made
after the arithmetic guard; neither actual trial is certified free of semantic
errors. Institution, forecast-year and conditional-source interpretation still
require Agent judgment and human acceptance.

## Verification and Rollout

Focused sidecar tests, independent regression checks, actual Gateway schema and
Skill projection, two fresh isolated research tasks, and HTML/PDF visual checks are
recorded in the execution receipts. No tests use Preview/g-fleet data or ports.
No historical user conversation or report is rewritten. Release and rollback
receipts live in the private Full-v10 deployment recovery directory.

Final isolated sidecar suite including the stock stdio restart and real PDF gate:
327 passed in 12.76 seconds. Ruff check and format check pass. Directed mypy passes with explicit
package bases; the expanded test-file invocation uses `--ignore-missing-imports`
because the installed Gateway dependency has no `py.typed` marker. No whole-repo
test/static gate was run because core code and core contracts are unchanged.

The final independent arithmetic-boundary replay passed all five previously
failing probes: four non-equivalences are skipped and the cross-sentence genuine
contradiction is still flagged. Rollout recovery probes also passed for a busy
Gateway, a corrupt backup and a failed restoration write, without real service
commands. Idle observations are not atomic admission fencing; rollout must occur
in a quiet window and preserve before/after task inventories.

## Breadth and Incremental Review Follow-up

The next real user task completed in 522.626 seconds, with 20 paragraphs (5,360
body characters), 31 references and three table crops. It discovered 83 files
but scoped-search selected only five files in one call. It projected 91 comparison
fragments three times: after drafting, after one claim correction, and after
finalize filled bibliography metadata. One limit=51 navigation call was rejected.
Compared with the earlier 7,347-character/eight-table report, research breadth
was not restored by adding heavier review. Reference count is not coverage.

The follow-up makes two separately testable changes:

- Deep fresh review prepares cited-source metadata before creating its snapshot.
  Metadata I/O is outside the state transaction. Explicit cursors, snapshot
  resumes and committed request replays do not re-fetch metadata. Failed metadata
  attempts remain failures, carry warnings and are not immediately retried at
  finalize for the same report/source hash. An explicit getFileDetails can retry;
  successful metadata corrections still invalidate affected source comparisons.
- Review protocol `source-comparison/4` tracks complete comparison groups by item
  and exact bound source hashes. New, edited or source-affected groups require
  fresh full fragment coverage; unchanged complete groups carry forward. Missing
  fragment ranges, old cursors or unmarked legacy snapshots cannot certify a new
  group. These records mean prepared comparison material, not semantic verification.

The Skill separates question coverage from item review. It follows substantive
gaps in viewpoints, mechanisms, scenarios and table evidence through discovery and
scoped reading. It does not enforce word, file, citation, image or iteration quotas,
nor treat a successful scoped call as enough depth. HTML retains parsed tables
and original crops; PDF retains prose/citations but uses only crops for tables.

In a read-only in-memory replay of the same latest user report, changing one
paragraph reduced the next comparison from 91 fragments to five, reusing 22
unchanged groups. The original state bytes were unchanged. This is a projection
measurement, not an observed end-to-end speed or semantic-quality claim.

Round one implemented the dependency hashes, metadata preparation and coverage
guidance. Round two independently reviewed fragment/cursor isolation, metadata
recovery and realistic Skill decisions. Focused fixtures exercise partial groups,
dependency invalidation, new interpretations of shared evidence, caption updates,
legacy snapshots, failed metadata attempts, crash recovery and standard-mode
compatibility. Exact final verification and deployment receipts accompany the
immutable release; historical user artifacts remain untouched.

Independent review found two recovery defects and confirmed their fixes. Metadata
attempts now merge with current committed state under the transaction lock; a
stale failed request cannot replace another request's attempts or poison a newer
report version. Reusing already rendered artifacts returns a copy of the receipt
with current review preparation, without rewriting historical artifact bytes or
the stored old receipt. Seven deterministic metadata concurrency cases and the
cached-receipt regression cover these paths.

The final isolated sidecar suite passed 382 tests in 14.18 seconds, including the
stock stdio restart and real PDF gates. Ruff check, format check (25 files),
directed mypy (25 files, explicit package bases and ignored missing dependency
stubs), and git diff whitespace checks passed. No whole-repository gates were run.

### Fresh Same-Question Trial

An isolated Gateway used the same user question, unchanged core/router and
read-only Full-v10 Knowledge service. Both this trial and the prior user task
recorded `z-ai/glm-5.2`. The trial workspace is
`/mnt/data/opensquilla-dev/tmp/research-breadth-candidate-20260903`.

| Observed measure | Prior user task | New isolated trial |
| --- | ---: | ---: |
| Task duration, seconds | 522.626 | 407.893 |
| Body characters | 5,360 | 6,595 |
| Paragraphs | 20 | 20 |
| Bibliography entries | 31 | 39 |
| Original table crops | 3 | 3 |
| PDF pages | 8 | 10 |
| Discovery files in ledger | 83 | 88 |
| Scoped selected files / calls | 5 / 1 | 4 / 1 |
| Comparison fragment projections | 91 + 91 + 91 | 116 + 10 |
| Finalize calls | 3 | 1 |

The second comparison followed two claim revisions: two groups were pending,
21 unchanged groups were reused. Seven review-page calls completed both snapshots
and finalization succeeded on its first call. There were no tool-result errors.
The 34 actual tool-use events include six discovery searches, one scoped search,
two explicit file-details reads and three table reads. Automatic bibliography
metadata requests are not counted as Agent reading of additional files/tables.

All three manifest SHA256 values match actual artifact bytes. HTML has three
parsed tables and three inline original images. The A4 PDF is ten pages with
embedded Noto CJK fonts; the first page was rendered and visually inspected.
Neither HTML nor extracted PDF text exposes internal evidence/table/research IDs.
The latest and earlier user report state SHA256 values remained unchanged.

This is one real trial, not a stable performance estimate or proof of broader
research quality. Body length increased by 23.0% and observed duration fell by
22.0%, but useful table coverage did not grow and scoped reading remained narrow.
The trial still contains inconsistent same-currency parenthetical amounts in a
semiconductor earnings paragraph; the existing narrow direct-equivalence guard
does not cover general approximate parentheticals. It is not a semantically
approved report. The incremental protocol proves comparison material was prepared,
not that the Agent interpreted source numbers, units or forecasts correctly.

The serial model trial did not exercise the late concurrency-recovery fixes;
those were validated separately by the final focused suite and independent
closing probes. No historical user task or report was repaired in place.
