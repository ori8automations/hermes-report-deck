---
id: nightly-crawl-2026-06-01
title: Nightly crawl summary
generated_at: 2026-06-01T02:14:00Z
source: scheduler
lane: automation
tags: [crawl, nightly, health]
summary: Overnight crawl finished clean; 3 endpoints slow but within budget.
related:
  - deploy-summary-2026-06-18
---

# Nightly crawl summary

The scheduled crawl completed at **02:14 UTC** with no hard failures.

## Highlights

- 1,284 pages fetched, 0 fatal errors
- 3 endpoints exceeded the 800ms soft threshold
- Sitemap diff: 12 new URLs, 4 removed

## Slow endpoints

- `/api/search` — 910ms p95
- `/api/reports` — 845ms p95
- `/api/profile` — 820ms p95

## Next step

No action required. Re-check the slow endpoints in the next run; if p95 stays
above budget for three nights, open a performance ticket.
