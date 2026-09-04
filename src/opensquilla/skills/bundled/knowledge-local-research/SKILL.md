---
name: knowledge-local-research
description: Research local Knowledge and deliver source-grounded HTML/PDF reports with table evidence and provenance. Use for local investigation, deep research, and multi-document synthesis, not internet research.
---

# Local Knowledge Research 1.1

Use Knowledge MCP as the only factual source. The Agent researches and writes;
Knowledge returns Evidence; the research sidecar manages evidence state, citations,
table media, rendering, and provenance.

## Required Result

Produce a useful answer to the user's question, not a retrieval log. For deep work,
connect evidence into conclusions, driver chains, disagreements, and conditional
outlooks when relevant. Support shared conclusions with independent sources when
available, and state material unresolved gaps without padding the report.

Every factual paragraph and key number must have nearby human-readable citations.
The renderer creates the deduplicated bibliography and its coverage percentages from
sources actually used by claims or tables; never write or inflate that list manually.

Deliver only the finalized manifest's three artifacts:

- `report.html`: self-contained prose, citations, original PDF table screenshots,
  and complete parsed tables.
- `report.pdf`: A4 prose and citations; table sections show only original PDF
  screenshots, not redrawn tables.
- `provenance.json`: exact machine provenance, internal IDs, bindings, and hashes.

Human-facing chat, HTML, and PDF must contain no internal IDs, refs, private paths,
or raw provenance. Do not hand-edit sidecar-generated reports.

## Recommended Tool Flow

1. **Begin.** Call `mcp_researchBegin`; use `mode="deep"` for deep research and
   match the user's language. Keep its `researchId` unchanged. Start a new research
   for a new report; resume only the current unfinished one.
2. **Discover iteratively.** Call `mcp_search` without `collectionIds` for full-corpus
   discovery. Read the returned chunks, then search again using newly learned terms
   and material gaps. Do not stop after one search or chase quotas for files, calls,
   citations, words, or tables.
3. **Deepen inside candidates.** Use `mcp_searchByIds` for context, exact numbers,
   definitions, opposing evidence, and missing links. Pass exactly one of `scopeRefs`
   or `fileRefs`, at most 20 files per call; explicitly select groups when returned.
4. **Read and verify.** Use `mcp_researchReadEvidence` for saved passages and their
   continuations. Search again when needed context is absent. Verify source, date,
   metric definition, unit, currency, horizon, and conditions before relying on a
   claim. Do not treat a file listing or an unread result as evidence.
5. **Inspect useful PDF tables.** Use `mcp_getFileDetails` and follow relevant
   `nextCursor` pages. For tables that materially help the analysis, use
   `mcp_getTable` and read the complete text, headings, units, and footnotes. Use
   table values only when the extraction is adequate or corroborated. Markdown is
   researched through text chunks; table inventories do not prove PDF completeness.
6. **Build the report.** Submit substantive paragraphs in reading order with
   `mcp_researchAddClaims`, preferably 3-5 at a time. Give them stable `claimKey` and
   `batchKey` values and bind only exact supporting `evidenceRefs`. Add each selected
   table with `mcp_researchAddTable` using its `tableRef`, section, and caption.
7. **Review and revise.** Before deep finalize, start a fresh
   `mcp_researchNavigate` review and follow every `nextCursor`. Compare each pending
   claim and table with its bound sources, then correct attribution, numbers, dates,
   units, comparisons, captions, or evidence. Start a fresh review after changes;
   unchanged completed groups need not be reread.
8. **Finalize and publish.** Call `mcp_researchFinalize` only after material gaps
   have been investigated and the current review is complete. Resolve
   `needs_review` checks instead of retrying blindly. Only `finalized` may be
   published; publish exactly `report.html`, `report.pdf`, and `provenance.json`
   from that result, each with `bundle="none"`.

## Operating Rules

- Copy returned refs and namespaces exactly, follow relevant cursors, and remember
  that tool receipts or review projection do not prove comprehension or correctness.
- Retry an unknown write with the same payload and idempotency key. Revise accepted
  claims or tables with their current hash. Use `TOOLS.md` for exact cursor, hash,
  grouping, progress, and recovery semantics when those cases arise.
- Cite only evidence that supports the nearby assertion. Separate incompatible
  dates, definitions, forecasts, and source positions; never guess flattened table
  columns or turn scenarios into facts.
- Do not add sources merely to increase bibliography size. Duplicate formats of one
  origin are not independent support.
- Do not insert generic process warnings into the report. Disclose a limitation only
  when it materially affects a conclusion or leaves requested work unfinished.
- If source metadata is missing, retry `mcp_getFileDetails` before finalize. Do not
  knowingly publish a blank bibliography title.
- If evidence cannot safely support a submitted assertion, revise it. If no grounded
  revision exists, stop unfinished rather than finalizing unsupported content.
- Preserve successful publication receipts and retry only explicitly failed files;
  never publish drafts, directories, screenshot assets, or private research state.
- Never use web results, model memory, shell output, or workspace documents as
  factual evidence, and never claim unavailable token or cost totals.
