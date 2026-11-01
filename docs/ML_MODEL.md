# Synthetic data and Logistic Regression

This implementation is a runnable **synthetic simulation**. Neither generated
labels nor the reported metrics establish how well the system would detect real
errors, fraud or compliance breaches. The two original sources are still unknown.
Their real identity, matching, completeness and accounting contracts must be
defined before any production ingestion.

## Position in the processing flow

The intake service normalises a movement version and performs deterministic
controls. It separately calls `score_batch` with the movement payload. The model
does not approve a payment or override reconciliation. The application persists
the returned status, nullable score, flag, model version, policy version, feature
schema version and input feature values in the assessment record. Database
persistence, case creation and alert outbox insertion belong to the application
transaction, not to the ML module.

`SCORED` means the frozen model produced a finite score. `UNAVAILABLE` means the
model or its required evidence was unavailable; its score is **null**, never zero.
Its `flagged=false` value is not a valid/clear decision. The monthly-close policy
must inspect status, deterministic results and review state separately.

The scoring function performs batch numerical inference. The application worker
decides how frequently new movements are assessed. There is no online fitting or
automatic self-training while invoices are being sent.

## Synthetic files

`generate_dataset` writes two equivalent representations of each source:
`source_a.csv` / `source_b.csv` for interchange and ZSTD-compressed Parquet for
columnar processing. CSV rows and Parquet row groups are written incrementally;
each Parquet buffer is limited to 8,192 rows. No full serving dataset is assembled
in memory. The manifest records file sizes, SHA256 hashes, exact physical source
row counts, logical movement counts, customer counts and scenarios. It is
published only after all files are complete.

All source monetary values are exact two-decimal strings. Binary float is used
only for transformed model features. Datetimes include an offset and the hour is
derived in UTC. The fictional customer addresses, tax identifiers and reserved
`example.test` email addresses are unusable for actual billing or delivery.

Source A and B represent one-to-one observations of the same purchased service.
Source A is authoritative for the amount only under this explicit simulation
contract. Customer `C0001` is clean by construction, so a complete billing demo
can run without resolving every deliberately broken record. Remaining customers
contain normal movements, model-risk examples, mismatched amounts, missing
counterparts, invalid values, missing model features, duplicate retries,
conflicting identities, ambiguous records and later source revisions.

`scenario_labels.csv` stores the scenario and generated risk label separately.
Neither source contains scenario labels, review outcomes or future information.
`training.parquet` contains supervised labels from the preceding 120-day interval,
ending more than one day before the serving month. Label availability is recorded
24 hours after the synthetic event. This timing is a test convention, not an
assumption about real investigator turnaround time.

`movements` is the **total** logical row count. The optional
`largest_customer_movements` argument assigns an explicit number to `C0001` and
distributes the remaining rows among other customers. A two-million-movement
largest customer does not imply two million movements for each customer.

## Features and unavailable evidence

| Feature | Row-local transformation |
|---|---|
| `log_amount` | Natural log of one plus the exact amount, converted at the model boundary |
| `amount_to_expected` | Exact amount / positive expected amount, then float conversion |
| `log_customer_age_days` | Natural log of one plus nonnegative customer age |
| `log_supplier_age_days` | Natural log of one plus nonnegative supplier age |
| `night_hour` | 1 when the event hour in UTC is 00–05 |
| `cross_border` | Explicit 0/1 source attribute |

Expected amount and customer/supplier ages must have been known at the event's
assessment time. The demo provides these values directly. A real implementation
must document their provenance and availability; computing them from future
records would invalidate this design. IDs, labels, scenario names and other rows
are excluded. Schema v1 has no silent imputation. Missing, nonfinite, negative,
precision-invalid or conflicting inputs return an unavailable assessment.

## Training, selection and test boundary

The baseline uses `StandardScaler` followed by scikit-learn
`LogisticRegression`, fitted only on the earliest approximately 60% of events.
The next approximately 20% is validation and the final approximately 20% is the
held-out synthetic test. Identical timestamps remain on the same side of each
boundary. Training labels that become available at or after the validation
boundary are excluded; validation labels unavailable before the test boundary
are excluded. These counts and timestamps are retained in model metadata.

The alert threshold maximises F2 on validation only, with ties resolved in favour
of the highest threshold. F2 weights recall more strongly than precision; it is a
demonstration choice, not a negotiated production cost function. The test labels
cannot affect feature scaling, model coefficients or threshold selection. Test
metrics are computed only after selection and are explicitly marked synthetic.

The synthetic patterns are deliberately separable. High scores on this test are
expected and are **not** evidence of real-world generalisation or calibrated
probabilities. Moving to production requires representative, reviewed labels,
agreed error costs, fresh temporal validation, operational-volume measurements
and explicit model approval.

Training accepts 100–100,000 rows and fails above that bound. The generator creates
3,000–30,000 historical rows regardless of the serving volume. This keeps the
baseline's in-memory training bounded. Scaling the historical training corpus
requires a separately validated sampling or incremental-training implementation;
increasing this limit without memory planning is not the intended workflow.

## Frozen artifacts

Each trained pipeline is stored under `versions/<model_version>/model.joblib`
with version metadata. An atomic update of `metadata.json` selects the active
version after its artifact is complete. Metadata includes source SHA256, artifact
SHA256, scikit-learn version, feature names, threshold, split boundaries and
evaluation scope. Workers cache by metadata and artifact filesystem identity.
The pipeline is loaded once per cached version, not once per movement.

Only locally generated, trusted artifacts may enter the model directory. Joblib
is not an untrusted-file interchange format: checksum validation does not make a
malicious artifact safe if its accompanying metadata is also malicious. The
loader restricts paths to that directory, validates schema/library version and
checksum before loading, and refuses artifacts not approved for synthetic
simulation. The model directory therefore requires operator-controlled access.

Inference is unavailable for an event at or before the model's information
cutoff, the latest label availability used for training or threshold selection.
This guards against applying a model fitted on future evidence during historical
replay. It does not replace a production registry's deployment-time approvals.

## Primary references

- [scikit-learn LogisticRegression API](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html): estimator and prediction interface.
- [scikit-learn threshold selection](https://scikit-learn.org/stable/modules/classification_threshold.html): separating score prediction from a decision threshold and selecting thresholds on separate data.
- [scikit-learn model persistence](https://scikit-learn.org/stable/model_persistence.html): trusted artifacts and version compatibility restrictions.

The executable environment records its installed scikit-learn version in every
model artifact. The linked stable documentation can advance independently.
