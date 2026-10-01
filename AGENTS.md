# AGENTS.md --- complete-pullback-fubon-pipeline

## Repository purpose

This repository runs the production pullback-selection workflow that combines current Fubon brokerage state, FinLab market data, market-breadth recording, exit/MFE monitoring, the daily selector, strategy ledger, and idempotent order intents.

It is a live-state pipeline. Current Fubon inventory is not a historical inventory snapshot.

## General engineering rules

GitHub is the code source of truth. Preserve existing uncommitted work when operating in a checked-out repository. Read this file, the workflow files, dependency definitions, and the relevant runner/module before editing.

Prefer minimal changes. Do not mix research-rule changes with infrastructure, diagnostics, storage, or UI fixes.

Do not hard-code or commit API keys, passwords, certificates, service-account JSON, OAuth tokens, or other credentials.

Do not change the Python/runtime version as part of an unrelated task. Runtime-version reconciliation must be handled explicitly and tested separately.

## Production-state invariants

The production pipeline uses one resolved complete FinLab as-of date across broker reconciliation, market breadth, and selector execution.

A manually requested historical `AS_OF_DATE` must not be treated as a valid historical Fubon inventory state. The broker inventory API represents current inventory unless a separate historical reconstruction is explicitly implemented.

Preserve:

- Fubon current-inventory semantics
- fill-history and FIFO reconstruction semantics
- strategy-ledger continuity
- inventory reconciliation diagnostics
- selector exclusion of actual current holdings
- deterministic/idempotent order-intent identity
- validated Day-35 / stop-loss strategy parameters unless the task explicitly changes research rules
- FinLab-only production market-breadth source
- immutable timestamped run archives
- completion-marker semantics used by GitHub Actions

Do not silently reconstruct missing live state or overwrite historical run archives.

## Observability and diagnostics

Every production or long-running pipeline must be observable from GitHub Actions/Colab logs without attaching a debugger.

Every major stage must emit bounded progress with `START`, `DONE` or `FAIL`, elapsed time, and useful non-secret counts/dates. Existing low-level broker steps should report completion timing and bounded result metadata.

For a long loop, emit bounded `x/y` progress periodically. Do not print one line per row merely to simulate progress.

Diagnostics must be designed from invariants at implementation time. Before adding or changing a long-running stage, explicitly consider:

`Stage → Input contract → Invariant → Observable → Failure diagnostic`

For this repository, logs should make it possible to localize at least:

- FinLab as-of-date resolution and source coverage
- Fubon authentication
- current inventory retrieval
- filled-history retrieval
- target-date trade reconstruction
- market-breadth fetch and Google Sheet write
- FIFO/open-lot reconstruction
- MFE/exit-watch calculation
- strategy-ledger update and inventory reconciliation
- selector execution and candidate count
- order-intent persistence
- output/archive completion

Diagnostics may expose bounded metadata such as row counts, shapes, dates, counts of reconciliation differences, and data-source status. They must not expose Fubon credentials, certificate material, account secrets, FinLab tokens, Google credentials, or unnecessarily large raw account/trade records.

A green GitHub Actions job is not sufficient evidence of business completion. Preserve the distinction between process execution, data readiness, broker/state consistency, output creation, remote archive upload, and completion-marker publication.

## Error semantics

Required stages fail loudly.

Optional stages may continue only where the existing contract explicitly permits continuation. An optional failure must remain visible in logs/output diagnostics; do not convert it into silent success.

Do not change failure policy merely to make CI green.

## Data and identifier boundaries

Treat broker/API/CSV/parquet/Google Sheet/Drive data as typed external boundaries.

Security identifiers must remain strings. Missing data must not be silently converted to zero. Do not forward-fill unavailable market or execution data unless an explicit research contract requires it.

Persistent ledger/schema changes require backward-compatibility review and realistic regression tests.

## Strategy and research integrity

Engineering fixes must not silently change signal definitions, entry/exit rules, holding-day rules, stop-loss rules, position sizing, or outcome definitions.

Any research-rule change must state the market mechanism, hypothesis, expected metric impact, possible side effects, and required validation. Prefer one core research variable per experiment.

Check look-ahead bias, survivorship bias, data snooping, selection bias, costs, slippage, liquidity, sample size, and concentration before interpreting strategy performance.

## Validation

After a change, run the narrowest relevant tests first, then broader tests when feasible. Do not change tests merely to make a failure disappear.

Observability changes should verify at minimum:

- START/DONE timing is emitted
- exceptions emit FAIL and are reraised for required stages
- optional-stage failures remain explicitly visible
- no credential value is logged
- result diagnostics are bounded
- business outputs and state semantics are unchanged

## Git discipline

Do not commit or push unless explicitly authorized.

Before a requested commit/push, verify the target branch and remote/head, review the diff, and ensure no credentials or generated private data are included. Never force-push unless explicitly requested.

## Completion report

Report separately:

- files modified
- tests executed and results
- validation not performed
- commit hash
- branch
- push result
- remaining risks
