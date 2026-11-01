# Acceptance evidence — updated 28 October 2026

This is a local synthetic release. The evidence below establishes the recorded
behaviour, not production readiness or a two-million-movement capacity guarantee.

> **Evidence receipts are not published.** Files referenced below under
> `output/verification/` and `output/pdf/*/receipt.json` were written by local
> verification runs and are excluded from this repository because they record
> machine-specific paths; some historical receipts are no longer retained.
> They are cited by path only.

| Check | Recorded result | Evidence |
|---|---|---|
| Inline exact PDF in Swipe | 7 dedicated browser fixture groups plus 10 existing swipe regressions passed. Live: eight page-image byte/hash checks, one image maximum, no underlayer PDF requests, 8 object URLs created/revoked and zero retained after navigation. Next/Previous, Later/Return, enlarged viewer, stale/late responses and desktop/mobile checked. Live only signed in; 23 business-table hashes and retained files unchanged. | Acceptance manifest (`output/verification/swipe-pdf-acceptance.json`), fixture (`output/verification/swipe-pdf-ui-smoke.json`), live (`output/verification/swipe-pdf-live-smoke.json`) |
| Live swipe and model parameter bars | Shared read-only session: 21 browser requests and 8 direct GETs, only sign-in POST; six real frozen-model bars/threshold, mouse hover, desktop/mobile bounds, local Later/return and no extra list/driver request on layout switches. All case/invoice projections unchanged; 23 business-table hashes and retained objects match the pre-upgrade baseline. | Live check (`output/verification/swipe-live-smoke.json`), business hashes (`output/verification/swipe-business-after.json`), final evidence manifest (`output/verification/swipe-acceptance.json`) |
| Swipe/model-bar release regression | 627 tests passed; zero failed or skipped; disposable PostgreSQL schemas. Includes 64 mathematical driver tests and 15 triage/rejection tests. Existing 520-test receipt remains historical. | Full suite (`output/verification/swipe-full-suite.xml`), driver tests (`output/verification/review-card-drivers-final.xml`) |
| Exact case value audit | All 197 open cases checked; 125 scored feature vectors and frozen scores reproduced; 18 explicitly unavailable, 54 quarantined. No available amount/currency/source mismatch; maximum score reconstruction error 8.67e-18. This audits the retained list/detail/explanation contract, separately from the new batched driver endpoint. | Read-only value audit (`output/verification/values-audit.json`) |
| Swipe and parameter-bar browser fixture | 10 check groups, 188 intercepted requests and 12 explicitly gated synthetic POSTs; zero browser errors, external or unexpected requests. Exercises drag/cancel/keyboard/reduced motion, three-layer bound, six features/hover, explicit personal decisions, Later/resume, stale SSE, wrong version/fingerprint, incomplete receipts, same-key retry, layout navigation and late-session replies. | Fixture receipt and source hashes (`output/verification/card-deck-ui-smoke.json`) |
| Review Queue card projection | 28 focused tests plus 36 existing operations, bulk-release and live-API regressions passed. Exact decimal amounts, case-version evidence, separate source currencies, zero/unavailable distinction, receipt-only thresholds, constant query count for 1 vs 50 rows, no model loading or mutation | Card API tests (`output/verification/review-cards-api.xml`), Regression tests (`output/verification/review-cards-regression-pytest.xml`) |
| Live Review Queue cards | 50 cards, 27 after customer filtering; displayed amount/model/score match retained case detail; Cards/List adds zero GETs and preserves inspection selection; desktop/mobile checked; shared access cannot release; case projection unchanged | Read-only live receipt (`output/verification/review-cards-live-smoke.json`), Desktop (`output/verification/review-cards-live-desktop.png`), Mobile (`output/verification/review-cards-live-mobile.png`) |
| Review cards browser fixture | 29 intercepted GETs and no writes; exact large amounts, null/zero distinction, historical evidence, selection preservation, filters/pagination, eight-case inspection cap, lazy details, permission gates and mobile layout. Existing operations, workbench and simple-workspace fixtures also passed | Dedicated fixture (`output/verification/review-cards-ui-smoke.json`), Simple workspace (`output/verification/ui-simple-workspace-fixture-smoke.json`) |
| Browser-independent PDF preview | 23 renderer/cache tests; 9 preview/cache API tests. Exact PDF-derived images, page bounds/hash checks, unavailable/busy states, bounded rendering and cache eviction verified. The real embedded browser displayed pages 1 and 2 of the 15-page invoice | `tests/test_invoice_preview.py`, API receipt (`output/verification/preview-api-tests.xml`), live browser receipt (`output/verification/live-ui-simple-smoke.json`) |
| Real browser during streaming | 15.081 s observation while ingestion/workers ran; 8 actual SSE states, movements 0 to 17,000; 29 browser requests and 5 direct GETs, no errors. Four retained versions, frozen historical reasons/sources and unchanged current review target | Browser receipt (`output/verification/live-ui-streaming-smoke.json`) |
| Existing business state preserved | 23 full-table streamed hashes and all referenced source/snapshot/PDF objects unchanged across additive schema 6 to 7 migration and UI/backend upgrade | Before (`output/verification/console-business-before.json`), after (`output/verification/console-business-after.json`) |
| Final console/backend regression | 520 passed in 99.49 s, zero failures/errors/skips, including final cache hints/revalidation and PDF page preview. The preceding 494-test result is retained | Final regression (`output/verification/pytest.xml`), preceding candidate (`output/verification/pytest-before-page-preview.xml`) |
| Concurrent 100k streaming | PASS: 100,000 movements/200 customers/largest 50,000; 2 workers, 2 HTTP readers+SSE. 141.026 s measured interval, 2,035 HTTP requests/zero errors, 69 SSE updates and final drained queue. All current versions have both assessments; counters match SQL; stale review rejected and attributed. Disposable schema removed; source candidate unchanged | Full receipt (`output/verification/streaming-workload.json`), [method and limits](STREAMING_VERIFICATION.md) |
| Assessment batch cost and fault recovery | Reconcile 20 and mixed-availability model 13 DBAPI cursor executions at both 100 and 200 movements; rollback across 600 rule cases/two alert batches, exact retry and actual-model evidence pass | Passing receipt (`output/verification/assessment-batching-final.log`), preserved failure that led to nullable-insert fix (`output/verification/assessment-batching-before-null-fix.json`) |
| Automation controls | 60 isolated PostgreSQL tests: personal admin/revision/idempotency/audit, pause, fair claims, 32-row recovery, admission retry/replay and concurrent reservations | `tests/test_automation_operations.py`, `tests/test_scheduler.py`, `tests/test_worker_runtime.py`, `tests/test_audit_integrity.py` |
| Version carousel evidence | 17 tests: frozen model identity, missing artifacts, threshold mismatch, source binding, pagination while versions arrive, stale review rejection and bounded queries | Version tests (`output/verification/review-versions.xml`) |
| Identical-input backend comparison | 20,000 logical movements/200 customers: assessment 40.972 s to 12.998 s; total 52.563 s to 23.694 s. Identical source/model hashes and business outcomes. One run per candidate; no capacity extrapolation | Comparison (`output/verification/backend-comparison-20k.json`), candidate (`output/verification/backend-optimized-final-20k.json`) |
| Pre-review-tools regression baseline including Gmail setup | 188 passed; zero failures, errors or skips; 21.81 s wall time | Preserved JUnit baseline (`output/verification/pytest-before-review-tools.xml`) |
| Customer filters, source provenance and bulk review baseline | 217 passed; zero failures, errors or skips; 25.64 s wall time | Preserved JUnit baseline (`output/verification/pytest-before-personal-review.xml`) |
| Personal accounts and complete review-history baseline | 254 passed; zero failures, errors or skips; 31.19 s wall time | Preserved JUnit receipt (`output/verification/pytest-before-model-workbench.xml`) |
| Model training, explanations and workbench baseline | 297 passed; zero failures, errors or skips; 38.66 s test runtime | Preserved JUnit baseline (`output/verification/pytest-before-invoice-inputs-and-reasons.xml`) |
| Source-only examples and readable model narratives baseline | 341 passed; zero failures, errors or skips; 43.94 s test runtime | Preserved JUnit baseline (`output/verification/pytest-before-itemized.xml`) |
| Itemised invoice regression baseline | 384 passed; zero failures, errors or skips; 66.88 s test runtime; final additive PDF-readiness API field also verified by 4 focused tests | Preserved JUnit receipt (`output/verification/pytest-before-line-vat.xml`), API tests (`output/verification/itemized-api-tests.xml`) |
| Per-movement VAT regression | 440 passed; zero failures, errors or skips; 79.719 s JUnit suite time. Final wording-only template changes also passed all 76 renderer tests in 15.336 s JUnit suite time | Preserved JUnit receipt (`output/verification/before-console-pytest.xml`), final renderer receipt (`output/verification/line-vat-renderer-final.xml`) |
| VAT arithmetic and revision integrity | Independent integer-penny reference, positive/negative/zero adjustments, 10,000-row arithmetic bound, precision isolation, legacy/current command replay and policy-bound audit: 42 focused tests. Personal HTTP upgrade and bounded fields: 5 tests | Math/billing receipt (`output/verification/movement-vat-billing.xml`), API receipt (`output/verification/line-vat-api-tests.xml`) |
| Actual per-movement VAT flow | Four normal worker-generated replacement drafts; every one of 322 movements checked against frozen source versions and independently reconciled. C0001 has an explicit +GBP 0.03 adjustment; other three have zero. Totals, source snapshots, old PDF/package bytes and 13 protected business-table hashes preserved | Frozen baseline (`output/verification/line-vat-business-before.json`), preparation (`output/verification/line-vat-preparation.json`), flow proof (`output/verification/line-vat-flow-proof.json`), PDF receipt (`output/pdf/line-vat-flow-invoices/receipt.json`) |
| VAT live UI and PDF validation | 12 browser requests plus 30 read-only requests; all 322 rows checked using integer cents, 4 PDF/source hashes matched, no invoice or review changes. Four PDFs contain 15/2/3/4 pages; all 24 pages rasterised and visually reviewed with no overfull boxes in compiler logs | Live UI receipt (`output/verification/live-ui-line-vat-smoke.json`), PDF visual receipt (`output/verification/line-vat-pdf-visual-check.json`) |
| Normal-flow invoice examples baseline | 72 new movements / 144 source rows / 3 customers; actual scheduler and worker created three DRAFTs with gross totals GBP 131.70 / 516.60 / 1,316.70; exact source replay reused batches and PDFs before presentation regeneration | Ingestion (`output/verification/invoice-examples-ingest.json`), flow/audit proof (`output/verification/invoice-examples-flow-proof.json`), historical PDF receipt (`output/pdf/flow-generated-examples/receipt.json`) |
| Existing records preserved during source expansion | Identical streamed hashes for all columns across 17 existing business tables/scopes, including 2,000 movements, 4,000 assessments, 198 cases, original draft, outbox and delivery records; subsequent presentation revisions are tracked separately below | Before (`output/verification/invoice-examples-business-before.json`), after (`output/verification/invoice-examples-business-after.json`) |
| Actual itemised invoice flow | Four unapproved drafts replaced through normal billing checks and worker rendering: 250/12/24/36 movements across 11/2/2/3 PDF pages. Exact membership, source fields, net/VAT/gross, original PDF/package bytes and snapshot hashes preserved; original revisions retained as STALE | Frozen baseline (`output/verification/itemized-business-before.json`), preparation (`output/verification/itemized-preparation.json`), flow proof (`output/verification/itemized-flow-proof.json`), current PDF receipt (`output/pdf/itemized-flow-invoices/receipt.json`) |
| Itemised UI fixture and actual read-only browser | Fixture 42 requests; live 12 browser requests plus 30 direct GETs. Four complete detail lists, exact cent totals, four PDF hashes and four original-source hashes verified; pagination and personal confirmation exercised; live case/invoice snapshots unchanged | Fixture receipt (`output/verification/ui-itemized-fixture-smoke.json`), live receipt (`output/verification/live-ui-itemized-smoke.json`) |
| Narrative evidence boundaries | 33 narrative tests plus 3 HTTP integration tests: exact signed contributions, threshold equality, source-feature binding, missing evidence, frozen historical versions and separate non-persisted hypothetical descriptions | `tests/test_risk_explanations.py`, `tests/test_case_narratives_api.py` |
| Actual queued synthetic training | SUCCEEDED; 30,000 rows; seed 20261027; active `lr-12065a722769792dde2f`; checksum-verified artifact, dataset and manifest | Model runtime receipt (`output/verification/ml-workbench-after.json`) |
| Chronological synthetic partitions | 17,746 fit / 5,746 validation / 6,000 test; 508 label-availability exclusions; validation F2 threshold 0.9795379071976752 | Same model runtime receipt |
| Synthetic held-out metrics | AP, precision and recall 1.0 on 6,000 examples / 300 positives; deliberately separable generated patterns, no real-world effectiveness claim | Same model runtime receipt |
| Actual read-only inference using new active model | 2,000 current movements checked in batches of at most 500: 1,982 SCORED / 18 UNAVAILABLE; 18 synthetic flags; no scores written back | Same model runtime receipt |
| Exact mathematical explanations | Maximum reconstruction error 1.735e-18; largest compact explanation 682 bytes across scored demo movements; historical model also reproduces its original score | Same model runtime receipt |
| Additive model migration and activation | Schema 6; identical hashes for 198 cases, 2,000 movements, 2,000 versions, 4,000 assessments and the unapproved invoice's protected fields | Before (`output/verification/ml-business-before.json`), After (`output/verification/ml-workbench-after.json`) |
| Recovery and preview integration | Actual training, post-pointer SQL commit failure, process loss, exact metadata drift, automatic retry bound, same-target admin recovery, CLI admission guard, read-only preview and filters | `tests/test_model_ops_api.py` — 16 focused tests |
| Real inference persistence and gates | Actual CSV intake and model scoring persist compact explanations; deterministic mismatch remains blocked; missing features stay unavailable; new activation preserves frozen scores | `tests/test_model_worker_evidence.py` |
| Workbench browser fixtures | 89 intercepted requests; eight-case bound, per-case draft isolation, comparison, filters, late-response protection, automatic active-case narrative, pure shared-session preview, training/recovery and independent heartbeat refresh | Fixture receipt (`output/verification/ui-workbench-fixture-smoke.json`) |
| Live readable reasons and flow-generated invoices | Automatic explanations for model risk, source mismatch and unavailable scores; three new DRAFT PDFs and both August source files downloaded with matching retained hashes; case and invoice snapshots unchanged | Live read-only receipt (`output/verification/live-ui-narrative-smoke.json`) |
| Live model/workbench browser | 23 browser requests, 10 direct read-only requests and one hypothetical preview; three real pinned cases, active/frozen model distinction, RUNNING heartbeat, source download/hash verification, unchanged inspected case and DRAFT invoice | Live receipt (`output/verification/live-ui-workbench-smoke.json`) |
| Additive identity/history migration | Schema 5; all 198 cases unchanged, including 197 open cases; same unapproved invoice and PDF/package hashes; historical attribution unchanged | Before (`output/verification/review-identity-before.json`), After (`output/verification/review-identity-after.json`) |
| Named reviewer and audit boundaries | Two-account attribution, actor-bound replay, private signing key, forgery rejection, deactivation, append-only SQL protection, CSV retention, rejection history and pagination | `tests/test_operators_api.py`, `tests/test_review_history.py`, `tests/test_audit_integrity.py` |
| Explicit live Gmail API diagnostic | Service-account token HTTP 200; send HTTP 400 FAILED_PRECONDITION; no message accepted | Redacted provider receipt (`output/verification/gmail-api-service-account-test.json`) |
| Gmail OAuth module | 68 offline tests passed: state, PKCE, browser binding, scopes, identity, expiry, refresh and private storage | `tests/test_gmail.py` |
| Gmail HTTP integration | 7 tests passed: personal login/CSRF, upload cap, signed Lax callback binding, initiating operator attribution, inactive-operator rejection, fixed redirect, no email side effects | `tests/test_gmail_routes.py` |
| Actual seeded state | 2,072 movements; SQL state counts equal transactional counters; four retained source batches and twelve invoice packages including retained history verified | Local acceptance (`output/verification/local-acceptance.json`) |
| Human gate | Four unapproved VAT-itemised DRAFTs and eight retained STALE revisions (four summaries and four net-only itemised documents); zero invoice allocations in the seeded demo | Same acceptance receipt |
| Live browser | Five views, pagination, actual PDF, session recovery, stale-version rejection and SSE | Initial live check (`output/verification/live-ui-smoke.json`) |
| Final UI integration | Absolute case links, real CSV round trip with zero proposed changes, preserved unsaved review, automatic-dispatch policy visible; SSE observed at 2.307 s | Final live check (`output/verification/live-ui-final-smoke.json`) |
| Processing monitor | UI job counts match API: zero ready/running/failed; seven blocked customer scopes reported, zero scheduler errors | Processing check (`output/verification/live-ui-processing-smoke.json`) |
| XLSX input parity | All 4,090 source rows checked; typed money and timestamps preserved; cross-format replay adds no source records or movements and no changed-payload quarantine | Workbook validation (`output/verification/workbook-validation.json`) |
| Actual UK-style TeX/PDF | VAT, non-VAT and multi-page examples compiled; exact arithmetic, escaping, integrity and failure paths tested; rendered pages inspected | [Document receipt](../output/pdf/demo-invoices/verification.json) |
| Final measured load | 20,000 movements / 200 customers; 80.987 s; 236.69 MiB Python peak RSS excluding PostgreSQL; counts reconciled, no emails/invoices | Final performance receipt (`output/verification/performance.json`) |
| Analytical export | SQL snapshot = streamed Decimal totals = DuckDB Parquet totals; 2,000 current movements, 18 NULL scores | `var/exports/period=2026-09/export-dc93a1aa2f3e43189161f31253900b7a/manifest.json` |
| Deployment definition | Compose configuration and Python syntax validated; native API/worker/PostgreSQL actually run | Docker image/container execution remains untested because the daemon was unavailable |

The final benchmark's Parquet export took longer than the initial run. Both
receipts are retained, and no cause is asserted without profiling. See
[performance methodology and limits](PERFORMANCE.md).

The current suite emits Starlette/httpx and SQLAlchemy 2.1 DISTINCT ON deprecation
warnings. The DISTINCT ON form remains compatible with supported SQLAlchemy 2.0;
these warnings are not failed or skipped tests.

The VAT UI fixture's earlier permission-review timeout remains historical. During
this console update it was rerun successfully with 29 intercepted requests,
alongside the itemised fixture 42, operations 68, workbench 89 and identity 56.
These are browser fixtures, distinct from live application evidence.

The subsequent Review Queue card change was validated with the focused tests
and live checks above. The earlier 520-test full-suite receipt remains evidence
for its recorded source version; it is not represented as a full-suite rerun
after this card change. Live business-row and retained-object hashes were
checked again and remained equal to the existing pre-console baseline.

The first itemisation integration run exposed a narrow-column text overflow and
a legacy-count query in list polling. Both were fixed and the final suite passed;
the failed receipt remains in `pytest-itemized-initial-failures.xml`. All 18 live
output pages were rendered and visually inspected. The explicit 10,000-movement
detail cap is validated as a rejection boundary, not a measured PDF throughput
claim; the largest actual PDF in this run contains 250 movements.

The pre-Gmail acceptance and 114-test baseline are retained as
`local-acceptance-before-gmail.json` and `pytest-before-gmail.xml` respectively.
The source-bound acceptance receipt contains SHA-256 hashes of implementation,
tests, scripts and deployment files. UI fixture tests are kept separate from
live integration evidence. Synthetic model test metrics are separate from
performance on real customer data, which has not been measured.

Not verified: actual source semantics, production tax treatment, successful real-email
provider submission/webhooks, real Google OAuth user consent, recipient acknowledgement, two-million-movement
stress, multi-operator production authentication, cloud durability or full
backup/restore recovery. The runtime rejects activation of a live accounting
contract in this release.

The new August examples are generated exclusively by the normal source-processing
and monthly-preparation flow. The older VAT/non-VAT/multi-page files under
`output/pdf/demo-invoices/` are retained renderer/layout fixtures; they are not
new operational invoices or evidence of scheduler-created documents.

The local personal-account workflow has been exercised with separate test users
in disposable PostgreSQL schemas. This is not enterprise SSO, independent proof
of a person's legal identity or a production authentication certification. The
existing workspace has no automatically invented operator: its owner completes
first-account setup in the UI. Old shared-login decisions retain their recorded
label and remain individually unattributed.
