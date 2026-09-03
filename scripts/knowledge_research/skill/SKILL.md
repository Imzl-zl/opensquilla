---
name: knowledge-local-research
description: Research the local Knowledge corpus and deliver a source-grounded HTML/PDF report with original PDF table crops and machine provenance. Use for local knowledge-base investigation and multi-document synthesis, never internet research.
---

# Local Knowledge Research

Use only Knowledge MCP material as factual evidence, not web sources, model
memory, shell output, or workspace documents. The sidecar owns evidence state,
citations, table media, rendering, and provenance; do not recreate these jobs.

## Research Loop

1. Call `mcp_researchBegin` and wait for its `researchId` before dependent calls.
   Copy that ID unchanged into subsequent Knowledge and research calls.
2. Use materially different `mcp_search` queries for discovery. Full-corpus
   searches omit `collectionIds`. Search assigns a `scopeRef`; keep returned
   references instead of reconstructing identifiers or copying long file arrays.
3. Use `mcp_searchByIds` with exactly one selection: `scopeRefs`, `fileRefs`, or
   legacy `fileIds`. It searches at most 20 files per call. If a larger selection
   returns explicit group references, choose relevant groups or a smaller subset;
   do not assume the original selection was searched or combine scores as one ranking.
4. Use `mcp_researchNavigate` to inspect bounded results, references, coverage,
   and continuation state. Use `mcp_researchReadEvidence` to obtain needed canonical
   evidence text, following returned continuation ranges when it is truncated.
   Full text stored in the ledger does not mean it was returned or read.
5. Evaluate support for each important assertion, including units, dates,
   horizons, scenarios, independent corroboration, and contradictions. Split
   distinct topics into focused queries. Follow newly discovered questions back
   to search, scoped search, or evidence reading as appropriate.

Continue while material gaps or useful new evidence remain, including new evidence
inside an already known file. Stop based on question coverage and evidence gain,
not a file quota or a fixed number of iterations. Disclose unresolved limitations;
exhausting a user budget is not proof of research completeness. Never pad citations
or count duplicate formats and repeated excerpts as independent corroboration.

## Tables

For useful PDFs, inspect `mcp_getFileDetails`; follow returned continuations where
needed. Complete enumeration concerns extracted table entries, not proof that every
table in the PDF was detected. Use `mcp_getTable` for selected tables and inspect
text/truncation and quality metadata. Never request or copy screenshot base64.
Original images are limited to available crops of extracted tables, not full PDF
pages or a complete visual inventory. Missing crops do not establish completeness.

An original crop with a valid hash proves artifact identity, not OCR completeness
or visual review. Without an actual image-review path, explicitly retain the
unreviewed status; unknown visual review alone does not block the whole report.
Do not infer missing cells or call conditional scenarios a
guaranteed floor. Corroborate important uncertain values with available evidence
or disclose the gap before using them in a conclusion.

## Assembly and Delivery

- Submit substantive paragraphs in reading order with `mcp_researchAddClaims`,
  preferably 3-5 paragraphs per batch. Give each paragraph a stable `claimKey`
  and each submission a `batchKey`. Bind only evidence registered in this research,
  using exact returned `evidenceRefs` or canonical `evidenceIds` as the tool allows.
- If a paragraph submission times out or its commit status is unknown, retry the
  original batchKey with the original payload. Do not assume it failed or rename
  paragraphs to retry. A committed batch key cannot carry
  different content; revise an accepted paragraph with a new batch key, its same
  claim key, and the returned current `expectedClaimHash`.
- For rejection, inspect the error location and the assigned reference mapping.
  Fix only the rejected batch; never guess a nearby ID. Retrieve more evidence
  when support is missing, not merely because an identifier was mistyped.
- Add selected, artifact-verified tables with `mcp_researchAddTable`, which has no
  batchKey. The same table with identical section/caption replays its original
  item; different content conflicts. On recovery, inspect already-added tables
  using the report view of `mcp_researchNavigate` if exposed by its schema. If that
  view is unavailable, retry the original AddTable arguments, not a changed caption.
- Call `mcp_researchFinalize` when submitted content is ready, then inspect coverage
  and warnings against the evidence. For material evidence gaps, unsupported
  assertions, or known incomplete key tables affecting a conclusion, return to
  search/reading as needed. Revise submitted paragraphs with the same `claimKey`,
  current `expectedClaimHash`, and a new `batchKey`, removing unsupported assertions
  from their text while retaining non-empty, evidence-grounded prose. There is no
  paragraph-deletion API; do not submit empty text. Finalize again before publishing.
  Do not merely acknowledge the warning
  and publish the unsupported conclusion. Unknown visual review alone is not a
  blanket blocker. Optional expected-item lists check submitted items only, not
  unsubmitted paragraphs or research completeness.
- Publish exactly the manifest's `report.html`, `report.pdf`, and `provenance.json`,
  each using `bundle="none"`. Preserve successful publish receipts; on recovery,
  retry only files explicitly reported as failed, not ones already successful.
  If publication status is unknown, query it only when the available tools support
  that operation. Without a query capability, stop and disclose unknown delivery;
  do not claim success or repeatedly resend. The three publish calls are not a
  three-file transaction. Never publish a directory or private ledger.
- Keep internal IDs, short references, and local paths out of all human-facing
  report text and the final chat. Do not write custom reports or patch generated
  files. The final chat confirms only artifacts with successful publish receipts
  and discloses failed or unknown delivery. Do not claim unobserved visual
  verification or complete model usage/cost accounting.
