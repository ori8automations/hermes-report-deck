---
id: deploy-summary-2026-06-18
title: Deploy summary — release 2026.6.2
generated_at: 2026-06-18T16:40:00Z
source: ci
lane: ops
tags: [deploy, release]
summary: Release 2026.6.2 shipped to production; one migration, zero rollbacks.
---

# Deploy summary — release 2026.6.2

Deployment finished at **16:40 UTC**. All health checks green.

## Changes

- Added the report-deck read API
- Tightened input validation on list filters
- One database migration (`add_reports_index`)

## Verification

- Smoke tests: `24 passed`
- Canary traffic held for 10 minutes with no error-rate change
- Rollback plan: `revert release 2026.6.2` (not needed)

See the [changelog](https://example.com/changelog) for the full list.
