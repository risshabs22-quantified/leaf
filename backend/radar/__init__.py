"""Leaf Radar: what pests and diseases university extension services say are being seen now.

Once a week, for each US state that has a garden, Leaf searches that state's extension sites
(Tavily), reads the pages it finds, and keeps only specific, dated reports of a pest or
disease being seen. Each report is a verbatim quote from the page, checked in code, with the
page it came from. Nothing is seeded, summarised from memory or written without a quote. The
reports feed a capped regional prior in diagnosis (`prior`) and the /radar page.

- `regions`: states, their extension sites, and a cell's state from the NWS point lookup.
- `collect`: search, read, screen (Nano), extract (Lightning), check in code.
- `prior`: match reports to diagnosis candidates; at most +5 points, never the reason for a
  Confident verdict.
"""
