---
name: knowledge-local-research
description: Research local Knowledge and deliver source-grounded HTML/PDF reports with table crops and provenance. Use for local investigation, deep research, and multi-document synthesis, not internet research.
---

# Local Knowledge Research

Use only Knowledge MCP evidence, not web, model memory, shell output, or workspace
documents. The researcher analyzes; Knowledge returns Evidence, not an Answer.
The sidecar owns evidence state, citations, table media, and rendering.

## Research Loop

1. Call `mcp_researchBegin` with `mode="deep"` for a deep-research request;
   short questions use `mode="standard"` (default). Match the user's report language:
   Chinese `language="zh-CN"`, English `language="en"`. Wait for `researchId` and
   copy it unchanged into dependent calls. New reports need new research; resume
   only current unfinished work, not historical reports.
2. Discover evidence for the user's questions with `mcp_search`, omitting
   `collectionIds` for full-corpus search. Deep research follows discoveries with
   `mcp_searchByIds` for context, definitions, assumptions, and opposing evidence.
   Supply exactly one of `scopeRefs` or `fileRefs`, at most 20 files. Larger
   selections return groups without searching: explicitly select relevant groups
   or subsets. Scores across calls are not one ranking.
3. Read canonical passages and needed continuations with `mcp_researchReadEvidence`.
   It reads saved excerpts, not whole documents or new context. Use scoped search
   for absent passages, broader search for missing viewpoints, independent support,
   and contradictions. Follow these gaps inside known files too; repeated global
   discovery alone is not deep reading.

Deep research follows question-specific gaps in source viewpoints, disagreements,
driver-to-outcome chains, future scenarios and conditions, and useful tables.
Institutions are a financial example; adapt coverage to other topics rather than
forcing a financial structure. A gap is material if resolving it could change a main
conclusion, the explanation of source disagreements, a driver chain, or scenario
conditions. Distinguish unverified gaps from evidence still unavailable after
targeted search. Unread relevant candidates are unverified, not unavailable; inspect
them or disclose access/budget limits and unfinished checks.

Copy refs and namespaces intact. Page relevant snapshots/directories with `nextCursor`;
do not claim unvisited entries were read. Stop on question coverage and evidence
gain, not completed paragraph review or quotas for files, words, citations, figures,
or search rounds. Disclose remaining gaps. If scoped search fails without a useful
recovery action, report unfinished research; never loop or downgrade deep.

## Check Sources and Compare

Identify the original source or evidence producer; distinguish quoted views from
the publisher's own. Verify publication/observation dates, metric definitions,
units/currency, population/market, horizon, and conditions. Preserve old/new direction
in "to ... from ..." revisions; distinguish daily averages from totals and facts from
forecasts/targets/scenarios. Metadata and filenames do not validate claims or dates.
Before each numeric assertion, locate the exact original value, unit, year/horizon,
and evidence producer. For ambiguous flattened chart/table columns, fetch the table
or search for prose; never guess a value's year.

Bind each assertion to its exact supporting passage and source/page. Resolve
mismatches by reading/search, never guessed pages or nearby refs. Keep factual
paragraphs single-attribution where practical; separate sources' assertions and
citations, even under source-specific headings. Avoid unrelated paragraph-end
citation bundles. Comparisons cite verified inputs and distinguish source positions
from researcher inference.

Synthesize by question or viewpoint. For shared viewpoints, seek substantive support
from multiple independent sources where available; explain each source's support or
qualifications. Compare compatible dates, definitions, and horizons; explain
incompatibilities, disagreements, causal links, and what would change the conclusion.
Seek counterevidence for pivotal conclusions; disclose single-source limits.
Duplicate formats, excerpts, or quotations of one origin are not independent support.
A bibliography or target list is not comparison.

## Tables

Inspect useful PDFs with `mcp_getFileDetails`; page relevant extracted-table entries.
Select available tables by analytical contribution: comparisons, assumptions,
drivers, scenarios, or evidence that prose alone would obscure. Do not stop at a
representative figure or add tables to fill a count. Enumeration does not prove
every PDF table was detected. Use `mcp_getTable` for selected tables; read full-text
continuations, headings, units, and footnotes.

The sidecar renders self-contained reports. HTML includes original table screenshots
plus full tables, not previews. PDF retains research prose and citations; only its
table sections use original screenshots without redrawing HTML tables. Keep captions
and source attribution with each table. Do not request/copy base64 or manually
rebuild reports.

Crops cover extracted tables, not full pages or a complete visual inventory. Artifact
integrity proves neither extraction completeness nor visual review. Without actual
vision inspection retain unreviewed status; that alone need not block a report.
Missing cells, truncation, or uncertain key values need corroboration or removal from
conclusions. Never invent values or turn conditional scenarios into guaranteed floors.

## Write and Revise

- Submit substantive paragraphs in reading order with `mcp_researchAddClaims`,
  preferably 3-5 per batch, with stable `claimKey` and `batchKey`. Bind only
  exact `evidenceRefs` registered in this research.
- Do not write a References/bibliography section or a paragraph listing document
  titles: the renderer builds the only bibliography from substantive cited claims
  and tables. Listing a source is not using its evidence. Each citation must
  support an actual assertion, not enlarge the reference count.
- On timeout/unknown claim commit, retry the original batch key and payload.
  Never rename paragraphs or change committed content to retry. Fix rejected
  batches using error locations and exact mappings, not guessed refs.
  Revise accepted text or bindings with the same `claimKey`, a new `batchKey`,
  and `expectedClaimHash` equal to report/review's current `claimHash`.
- Revise unsupported assertions into non-empty, evidence-grounded prose. There is
  no paragraph-deletion API: do not submit empty text, invent filler to satisfy the
  non-empty constraint, or leave unsupported conclusions intact with a warning.
  If no safe revision exists, stop and report the work unfinished; do not finalize
  or publish the unsupported report.
- `mcp_researchAddTable` takes `researchId`, `section`, `caption`, and `tableRef`;
  only `tableRef` selects the table, with no `fileRef` or `batchKey`. Identical
  payloads replay the result. To correct caption/section, keep `tableRef` and set
  `expectedTableHash` to report/review's current `tableHash`; otherwise it conflicts.
  Retry uncertain submissions with original arguments.

## Review, Finalize, Publish

1. Before deep finalize, start a fresh
   `mcp_researchNavigate({researchId, view: "review", limit: 20})` without an old
   cursor. Before the first fresh review, the service attempts to fill missing
   source metadata (best-effort). Metadata warnings mean an incomplete attempt,
   not source verification. Explicitly retry `mcp_getFileDetails` if needed; unknown
   metadata alone implies neither bad OCR nor a need to reread the whole report.
   Fresh review returns only unfinished or changed item/source groups, tracked by
   item and source hashes. Follow pending pages via `nextCursor` to exhaustion;
   read all fragments and exact bound sources for each returned group. Match by
   `evidenceRefs` and `forClaimItem`, not entry order. Check attribution, comparisons,
   captions, and exact numbers/years/units; collect concrete mismatches as sources
   arrive. Use scoped search for missing context. Projection is not semantic review.
2. Apply collected corrections in small batches using the revision rules above,
   then start a fresh review without the old cursor. Review only pending groups:
   new, unfinished, or changed items and items affected by changed sources. Completed
   unchanged groups carry forward; do not reread the whole report after edits.
   Empty pending pages do not establish research coverage; address material gaps
   through search/reading and additions, then review the affected groups.
3. Deep mode requires a successful scoped search in this research and complete
   projection of current pending review materials. A zero-hit success meets the
   action gate, not an evidence need. These gates prove neither sufficient depth,
   comprehension, nor correctness; standard mode still requires source checks.
   Do not try finalize before review or downgrade deep to bypass the gates.
4. Call `mcp_researchFinalize`. `status="needs_review"` returns actionable `checks`,
   no manifest. Resolve those checks and follow fresh pending review pages for
   affected groups before retrying; do not repeat whole-report review or blind
   finalize calls. Stop on repeated unchanged failures. Inspect coverage and warnings;
   resolve material gaps and incomplete key tables via the steps above. Never
   substitute an older manifest. Expected-item lists check submitted items, not
   research completeness.
5. Only `status="finalized"` permits publication; it is not semantic verification.
   Publish exactly that result's `publicArtifactManifest` entries: `report.html`,
   `report.pdf`, and `provenance.json`, each with `bundle="none"`. Never publish
   directories, drafts, screenshot assets, or private state. Never hand-edit
   generated or historical reports.
6. Preserve successful publish receipts; retry only explicitly failed files.
   For unknown publication status use a supported status query; without one, stop
   and disclose uncertainty, not success or resend. Publishing is not an atomic
   three-file transaction. Final chat confirms only successful artifacts
   and discloses failed/unknown delivery.

Keep internal IDs, refs, paths, and raw provenance out of report prose/final chat.
Never claim unavailable model usage/cost as complete totals. See workspace
`TOOLS.md` for metadata and progress meanings, not measures of comprehension.
