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

Copy returned refs intact, including their research namespace. Page snapshots and
directories by relevance using `nextCursor`; do not claim unvisited entries were
read. Stop on question coverage and evidence gain, not quotas for files, citations,
iterations, or length. Disclose gaps and budget limits. If scoped search fails without a
useful recovery action, report unfinished research; never loop or downgrade deep.

## Check Sources and Compare

For important assertions, check the original speaker: a quoted institution's view
is not the publisher's. Verify publication/observation dates, metric definition,
units/currency, population/market, horizon, and conditions. Preserve old/new direction
in "to ... from ..." revisions; distinguish daily averages from totals and facts from
forecasts/targets/scenarios. Metadata and filenames do not validate claims or dates.
For each numeric assertion, locate the exact original value, unit, year/horizon,
and originating institution before writing. If flattened chart/table text leaves
column alignment ambiguous, fetch the table or search for prose; do not guess
which year a value belongs to. Keep different institutions' factual assertions in
separate paragraphs unless explicitly comparing them with individual attribution.

Bind the passage supporting the assertion at its source/page, not a related excerpt
elsewhere. Resolve mismatches by reading/search, never guessed pages or nearby refs.
Keep factual paragraphs as single-attribution as practical: separate institutions'
assertions and citations, including under institution-specific headings. Avoid
paragraph-end citation bundles for unrelated statements. Comparisons cite verified
inputs and distinguish each source's position from the researcher's inference.

Deep synthesis explains agreements/disagreements, scenarios, drivers and their links
to outcomes, and what would change the conclusion. Compare compatible dates,
definitions, and horizons; explain incompatibilities. Seek independent support and
counterevidence for pivotal conclusions; disclose single-source limits. Duplicate
formats, excerpts, or citations of one origin are not independent support.
A bibliography or target list is not substantive comparison.

## Tables

Inspect useful PDFs with `mcp_getFileDetails`; page relevant extracted-table entries.
Enumeration does not prove every PDF table was detected. Use `mcp_getTable` for
selected tables; read needed full-text continuations, headings, units, and footnotes.
The sidecar supplies available canonical text and original PDF crops to the report,
not just previews. Do not request/copy base64.

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
  and `expectedClaimHash` equal to the current review's `claimHash`.
- Remove unsupported assertions by revising the paragraph to non-empty,
  evidence-grounded prose. There is no paragraph-deletion API; do not submit empty
  text or leave an unsupported conclusion intact merely with a warning.
- `mcp_researchAddTable` takes `researchId`, `section`, `caption`, and `tableRef`;
  only `tableRef` selects the table, with no `fileRef` or `batchKey`. Identical
  payloads replay the result. To correct caption/section, keep `tableRef` and set
  `expectedTableHash` to report/review's current `tableHash`; otherwise it conflicts.
  Retry uncertain submissions with original arguments.

## Review, Finalize, Publish

1. Before deep finalize, call
   `mcp_researchNavigate({researchId, view: "review", limit: 20})` and follow every
   `nextCursor` to exhaustion. Compare submitted paragraphs against their exact
   bound originals by `evidenceRefs`, not entry order. Its full sources follow each
   paragraph, linked by `forClaimItem`, including reused sources. Check each paragraph
   as its sources arrive and collect concrete mismatches before continuing, rather
   than treating completed pagination as a successful review. Verify the exact
   number/year/unit and attribution for each numerical assertion. Read all 2000-character
   fragments; check sources, comparisons, captions, and question coverage.
   Use scoped search for missing context.
2. Collect corrections across the review, apply them in small batches using the
   revision rules above, then start a fresh review without the old cursor and read
   the complete current report's materials, not after each individual edit.
   Some repeated material remains necessary.
3. Deep mode requires a successful scoped search in this research and complete
   current review-material projection. A zero-hit success meets the action gate,
   not an evidence need. These gates prove neither comprehension nor correctness;
   standard mode does not impose them, but still requires source checks.
   Do not try finalize before review or downgrade deep to bypass the gates.
4. Call `mcp_researchFinalize`. `status="needs_review"` returns actionable `checks`,
   no manifest. After its initial gate, filling missing bibliography metadata may
   invalidate prior review: check updated source attribution and review current
   materials before retrying. Stop on repeated unchanged failures, not blind loops.
   Inspect coverage and warnings; resolve material gaps and incomplete key tables
   via the steps above. Never substitute an older manifest. Expected-item lists check
   submitted items, not research completeness.
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
