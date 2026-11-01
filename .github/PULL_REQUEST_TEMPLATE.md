## Summary

<!-- What changes and why. Link the issue if there is one. -->

## Checklist

- [ ] `pytest -q` passes against a real PostgreSQL (`TEST_DATABASE_URL`) and `pdflatex`.
- [ ] `node --check` passes for changed files in `src/invoice_optimizer/static/`.
- [ ] `pre-commit run --all-files` is clean (gitleaks, whitespace, large files).
- [ ] Invariants kept: immutable versions, append-only audit, idempotent commands,
      Decimal money, NULL score is not zero, human approval before dispatch.
- [ ] Schema changes use an idempotent migration in `db.py` with an upgrade test.
- [ ] Docs and `CHANGELOG.md` updated where behaviour changes.
- [ ] No secrets, `.env`, `var/`, real email addresses, absolute local paths or
      `output/verification/` files are included.

## How it was verified

<!-- Commands run, tests added, screenshots of UI changes (synthetic data only). -->

By submitting this pull request I agree that my contribution is licensed to the
copyright holder under the terms in `LICENSE`.
