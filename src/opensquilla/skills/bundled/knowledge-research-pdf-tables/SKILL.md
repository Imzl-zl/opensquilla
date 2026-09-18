---
name: knowledge-research-pdf-tables
description: Inspect PDFs, tables, charts, exhibits, page metadata, units, and footnotes for local Knowledge research. Load with knowledge-local-research when document structure or tabular evidence affects the conclusion.
---

# PDF and table companion

Use this companion when a PDF supports a summary, key number, judgment,
disagreement, scenario, or report exhibit. A review metadata lookup is not table
inspection, and a discovered PDF is not evidence until its relevant content is
read.

## Required inspection

Before writing claims:

1. Call `mcp_getFileDetails` for every core PDF and follow relevant
   `nextCursor` pages until the file inventory is complete enough for the
   argument.
2. Call `mcp_getTable` for useful exhibits. Read the full title, row and column
   headings, units, periods, actual/forecast markers, and footnotes. Record
   which cells support which claim.
3. Distinguish parsed text from the original image crop. Claim visual
   inspection only when the crop was actually delivered to vision input. An
   available but unseen crop may accompany verified text, but its visual status
   remains unknown.

Zero usable tables is a valid result only after actual inspection finds no
relevant table. Do not drop a useful exhibit just to avoid inspection.

## Fidelity rules

Preserve the source's units, scale, date, footnotes, and forecast markers.
Never redraw a source table, invent cells, infer hidden totals, or silently
repair OCR. If parsed text is incomplete or contradictory, qualify or remove
the conclusion that depends on it and seek another source. Use Markdown chunks
for text evidence and an original crop for a published table exhibit.

Every exhibit needs a caption stating the comparison, period/units, and why it
matters. Explain what the table cannot establish. Table-only numbers belong in
that sourced caption unless text evidence separately supports the prose claim.

Keep every nonredundant exhibit that materially improves understanding; omit
decorative or unusable tables.

The report companion owns `mcp_researchAddTable`, review, and publication. This
skill owns the inspection record that makes those later steps defensible.
