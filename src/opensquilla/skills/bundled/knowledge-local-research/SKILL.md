---
name: knowledge-local-research
description: Research local Knowledge and deliver source-grounded HTML/PDF reports with table evidence and provenance. Use for local investigation, deep research, and multi-document synthesis, not internet research.
---

# Local Knowledge Research 1.2

## Mission

Use Knowledge MCP as the only factual source. Knowledge returns Evidence; the Agent
researches, compares sources, and writes; the sidecar owns evidence state, citations,
table media, rendering, and provenance. Quality takes priority over speed.

Default to `mode="deep"` for research, analysis, or report requests. Use
`mode="standard"` only when the user explicitly wants a quick, narrow fact lookup.

## Required Outcome

A deep report must:

- Answer the user's question with substantive conclusions, not a retrieval log or
  stitched document summaries.
- Connect evidence into key facts, driver chains, disagreements, and conditional
  outlooks when relevant. Distinguish source statements from Agent synthesis.
- Ground every factual paragraph and key number in exact nearby evidence after
  checking source, date, definition, unit, currency, horizon, and conditions.
- Seek independent support and counterevidence for major conclusions. Explain what
  each source adds; duplicate formats of one origin are not independent evidence.
- Use as many relevant sources as substantively improve the answer, without padding.
  Important discovered candidates must be searched, not merely listed.
- Read by importance: examine core files from multiple angles and inspect their PDF
  tables; read complete relevant passages from supporting, qualifying, and opposing
  sources; use auxiliary sources only for evidence they add.
- State material unresolved gaps honestly, while keeping internal process details and
  nonmaterial technical warnings out of the report.

Research is iterative. Continue while another targeted query or unread important
candidate could materially change a conclusion, disagreement, causal chain, or
scenario. Do not optimize for fixed counts of searches, files, words, citations, or
tables. Stop on material evidence saturation, not the first plausible answer.

## Deliverables

Use human-readable inline citations. The sidecar builds the only bibliography from
sources actually used by claims or tables, deduplicates it, and adds honest reading
coverage percentages. Never author, inflate, or explain that bibliography manually;
ensure each entry has a readable title rather than a bare file type.

Publish only the finalized manifest's three artifacts:

- `report.html`: self-contained prose, citations, complete parsed tables, and
  original PDF table screenshots.
- `report.pdf`: A4 prose and citations; table sections show only original PDF
  screenshots, not redrawn tables.
- `provenance.json`: exact machine provenance, internal IDs, bindings, and hashes.

Every published table must have usable complete text and its original PDF crop. Omit
a table if either is missing or too ambiguous for its intended claim. Continue with
reliable prose evidence when possible; qualify or remove a core conclusion if that
table was its only support.

Apply a core-strict, local-best-effort gate. Do not publish when the core question
lacks reliable evidence, a key assertion remains unsupported, or artifact integrity
fails. Do not block a sound report for a secondary gap, missing nonessential table,
or noncritical metadata issue. Disclose only limitations that affect interpretation.

Chat, HTML, and PDF must contain no internal IDs, refs, private paths, raw provenance,
tool narration, or review state. Final chat gives a concise conclusion, material
limitations, and links to successfully published artifacts.

## Recommended Tool Flow

1. **Begin:** Call `mcp_researchBegin`, match the user's language, and preserve its
   `researchId`. Start a new research for a new report; resume only current
   unfinished work.
2. **Discover:** Use `mcp_search` without `collectionIds`. Read returned chunks,
   then vary queries using learned concepts, entities, dates, terminology, viewpoints,
   and material gaps. Its five-chunks-per-file limit is not a total article limit.
3. **Deepen:** Use `mcp_searchByIds` for context, numbers, definitions, assumptions,
   corroboration, conflicts, and outlook. Pass exactly one of `scopeRefs` or
   `fileRefs`, at most 20 files per call. Explicitly select returned groups and
   cover important candidates in successive calls. Reformulate zero-hit queries;
   never downgrade retrieval or change factual sources.
4. **Read:** Use `mcp_researchReadEvidence` for saved passages and continuations.
   It neither reads whole documents nor discovers absent context; search again when
   needed evidence is missing.
5. **Inspect tables:** Call `mcp_getFileDetails` for every core PDF and other PDFs
   likely to contain useful numerical evidence; follow relevant `nextCursor` pages.
   Call `mcp_getTable` for analytically useful tables and read complete headings,
   units, footnotes, and text. Research Markdown through text chunks.
6. **Write:** Add only supported paragraphs with `mcp_researchAddClaims`, preferably
   3-5 at a time in reading order, using stable `claimKey` and `batchKey` values
   and exact `evidenceRefs`. Add selected tables with `mcp_researchAddTable` using
   their `tableRef`, section, and informative caption. Do not write references.
7. **Close gaps:** Review the emerging argument and return to discovery, scoped
   search, and reading for missing support, unread candidates, disagreements, weak
   causal links, or unsupported scenarios. Revise as evidence changes.
8. **Review and deliver:** Start a fresh `mcp_researchNavigate` review, exhaust every
   `nextCursor`, and compare pending claims and tables with bound sources. Correct
   mismatches and review changed groups again. Then call `mcp_researchFinalize`;
   resolve `needs_review` by correcting or narrowing affected content. Publish only
   `status="finalized"`, and exactly its three manifest entries with
   `bundle="none"`.

## Finalize Gate

Before finalize, confirm that material search gaps and unread important candidates
have been addressed; every factual paragraph has exact evidence; major conclusions
have independent support or an explicit single-source limit; relevant conflicts and
future conditions are represented; selected tables are usable and have screenshots;
source titles are readable; review pages are exhausted; and no internal identifiers
or process commentary can enter human-facing output.

Copy refs and namespaces exactly. Preserve successful publish receipts and retry only
explicit failures. Consult `TOOLS.md` only when cursor snapshots, grouping, current
hashes, idempotent retries, progress meanings, or publication recovery arise. Never
hand-edit generated reports, use web or model memory as evidence, or claim unavailable
token and cost totals.
