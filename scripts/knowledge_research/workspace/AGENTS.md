# Knowledge Research Workspace

Higher-priority runtime and direct user instructions override this file.

## Local Knowledge Research

- For local Knowledge research or report requests, first load
  `knowledge-local-research`. Follow the installed Skill, not an older workflow.
- Use Knowledge MCP as the only factual source. Do not use web tools, workspace
  documents, shell commands, or model memory as evidence.
- Call `mcp_researchBegin` and wait for its `researchId` before dependent calls.
  Pass it unchanged on subsequent Knowledge and research-sidecar calls. Only
  evidence registered in this research can support its report; this bookkeeping
  is not a new session-authentication boundary.
- Full-corpus `mcp_search` calls omit `collectionIds`. Use its assigned scope and
  file references for scoped search. `mcp_searchByIds` accepts exactly one of
  `scopeRefs`, `fileRefs`, or legacy `fileIds`; large selections return explicit
  groups rather than silently searching a truncated selection.
- Use `mcp_researchNavigate` for bounded navigation and recovery of mappings;
  use `mcp_researchReadEvidence` for needed full-text continuations. Ledger storage
  or response coverage does not prove that the model has read or understood it.
- Let coverage, contradictions, and new evidence drive further search and reading.
  There is no fixed research iteration count or target file quota.
- Prefer 3-5 paragraphs per `mcp_researchAddClaims` batch. Use stable claim keys
  and batch keys; retry identical submissions without adding duplicate paragraphs.
  After a timeout with unknown commit status, retry the original batchKey and
  original payload, not new keys or rewritten paragraphs.
  Use exact mappings and error locations, never approximate ID repair.
- `mcp_researchAddTable` has no batchKey. Repeating the same table and identical
  section/caption returns its original item; changed content conflicts. Use the
  Navigate report view when exposed by its schema to inspect submitted tables;
  otherwise recover by retrying the original table-add arguments.
- Distinguish table artifact integrity from extraction completeness and visual
  review. Preserve reported truncation and unreviewed status; do not infer missing
  values or claim that storing a screenshot means it was visually checked.
  Original crops cover only available extracted tables, not all pages or tables.
- After finalize, material evidence gaps, unsupported conclusions, or known
  incomplete key tables affecting those conclusions require further reading and
  revision of submitted paragraphs with the same `claimKey`, current
  `expectedClaimHash`, and a new `batchKey`. Remove unsupported assertions from
  the text, retaining non-empty, evidence-grounded prose, then finalize again.
  There is no paragraph-deletion API; do not submit empty text.
  Unknown visual review alone does not block every report.
- Do not write, patch, render, or repair report files yourself. The sidecar owns
  evidence state, table media, citations, HTML, PDF, and provenance.
- Publish only the three exact entries in
  `mcp_researchFinalize.publicArtifactManifest`, each with `bundle="none"`.
  Preserve successful receipts and retry only explicitly failed files. Unknown
  publication status requires a supported status query; without one, stop and
  disclose uncertainty rather than repeatedly resend. Publishing is not an
  atomic three-file transaction.
- Keep all internal identifiers, short references, local paths, and raw provenance
  out of report prose and final chat. Confirm only successful publish receipts
  and disclose failed or unknown delivery; do not
  present unavailable model usage or cost as complete totals.
