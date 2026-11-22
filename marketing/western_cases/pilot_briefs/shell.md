# AMFE — Pilot brief: Shell

**One page · first pilot · 2026-11-22 · RESEARCH_ONLY · admission=false · not_client=true**

## 1. Goal

Shell prices freight and crude in more than one contour: the trading view and the budget view do not
reconcile, and the same scenarios reach internal dashboards through different exports. The goal is
one contour and one feed — a price path with the premium stack for crude quality, freight, insurance
and rerouting, its monthly budget effect, and a versioned API so the number in the meeting is the
number in the system. Two bounded cases, one decision each.

## 2. Two pilot cases

### wc-005 · Freight and price in one budget contour

**Question.** What does closing the strait cost the trading contour if freight and war risk rise together with price?

**You get.** PDF 10 pages with the price path, freight and the premium stack, XLSX with stress parameters, JSON with SHA-256, one workshop.

**Modules.** Hormuz / budget.

### wc-006 · One versioned API for internal scenario feeds

**Question.** Can scenarios be delivered to internal systems through one API with a version and a calculation hash?

**You get.** OpenAPI spec v1 and a sandbox, a delivery pack (PDF, XLSX, JSON, HTML), a smoke set and an integration runbook.

**Modules.** API integration.

## 3. Public facts and sources

| Fact | Value | Source |
|---|---|---|
| Production available for sale, 2025 | 2 800 kboe/d | SRC-SHELL-AR-2025 |
| LNG liquefaction, 2025 | 28,4 Mt | SRC-SHELL-AR-2025 |
| LNG sales, 2025 | 72,9 Mt | SRC-SHELL-AR-2025 |
| Proved reserves, end-2025 | 8 123 MBOE | SRC-SHELL-AR-2025 |
| Countries of operation | >70 | SRC-SHELL-AR-2025 |

Figures as published in the 2025 annual report, retrieved 2026-11-21, evidence A. Liquefaction and
sales are different metrics; production is the volume available for sale, not equity output.

## 4. Price and timing

| Scope | Time | Price, USD |
|---|---:|---:|
| wc-005 | 8 weeks | 22 000 |
| wc-006 | 8 weeks | 36 000 |
| Both cases, run one after the other | 16 weeks | 58 000 |

USD, excluding VAT. One fixed figure per case after a scoping call (free diagnosis, once per
company). The API case includes one integration; further systems are scoped separately.

## 5. What we need from you

- The freight and war-risk assumptions used by the trading contour, next to the current budget-contour version, so the gap can be shown.
- A price deck for Brent, diesel and jet at the monthly granularity you report on.
- The contract and route list for the flows that matter; anonymised IDs are enough.
- For the API case: the systems that consume scenarios, their auth model and refresh cadence.
- Two reviewers, trading and finance, plus one IT owner, for a 90-minute workshop.

## 6. Limits and status

RESEARCH_ONLY · admission=false · not_client=true. The model is research-stage and not admitted for
production decisions. We promise ranges with stated assumptions and a reproducible calculation —
versions, SHA-256 — not a validated model, not a market forecast, not an investment recommendation.
Company figures above are public and sourced; the pain described here is a hypothesis until it is
confirmed on your data. This page carries no company contacts and nothing has been sent.

**Contact.** [flumos@gmail.com](mailto:flumos@gmail.com) — the case catalog and module passport go out on request.
