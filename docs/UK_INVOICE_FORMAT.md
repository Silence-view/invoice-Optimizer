# UK invoice document contract

Verified against official guidance on 27 October 2026. The application produces a conventional A4 UK business invoice layout. There is no substantiated claim that this is the UK's most-used visual template. The layout is original, with a clear seller/customer block, dated invoice reference, item table, VAT summary, amount due and payment instructions. The visual design is selected by a format profile and a pinned template pack; see [INVOICE_FORMATS.md](INVOICE_FORMATS.md). New drafts use `uk-modern-v1` by default. Documents frozen before profiles existed keep the legacy `uk-classic-v3` layout described here.

## Authoritative references

- [GOV.UK: required invoice information](https://www.gov.uk/invoicing-and-taking-payment-from-customers/invoices-what-they-must-include): identifies the parties, document identifier, dates, descriptions and payable amounts. A limited company's full registered name belongs on the invoice.
- [HMRC VAT Notice 700, section 16.3](https://www.gov.uk/guidance/vat-guide-notice-700#vat-invoices): the full VAT invoice includes its sequential identifier, supplier VAT registration, supply/tax-point date, quantities or service extent, prices and applicable VAT information. It includes cash-discount details where offered. This renderer uses GBP and displays totals per VAT rate as well as overall totals.
- [HMRC VAT Notice 700, section 17.5](https://www.gov.uk/guidance/vat-guide-notice-700#calculation-of-vat-on-invoices--rounding-of-amounts): describes invoice and line rounding and requires a consistent chosen method. The demonstration policy below is explicit; it does not establish the tax treatment of the user's still-unknown input contracts.
- [Office of the Small Business Commissioner: effective invoicing](https://www.smallbusinesscommissioner.gov.uk/help-and-guidance/all-advice/a-guide-for-effective-invoicing/): supports a simple layout with a purchase-order reference when relevant, agreed payment terms, due date and bank instructions.
- [GOV.UK: invoicing overview](https://www.gov.uk/invoicing-and-taking-payment-from-customers): an invoice requests payment; a receipt acknowledges a payment. The generated document is an invoice, not evidence that the customer has paid.

## Implemented fields

| Area | Frozen document fields | Visible result |
|---|---|---|
| Identity | `invoice_number`, `invoice_date`, `period`, `reference` | Invoice number, date, billing period, reference / PO |
| Supply and payment | `tax_point`, `due_date`, `payment_terms` | Supply/tax-point date, due date and contractual terms |
| Seller | `legal_name`, `address`, `email`, `company_number`, `vat_number` | Seller details; VAT registration only in VAT mode. Billing copies them from the active format profile's `[seller]` block at freeze time |
| Customer | `id`, `legal_name`, `address`, `email`, `vat_number` | Customer identification and bill-to details |
| Services | `description`, `quantity`, `unit_price`, `net_amount` | Item description, extent/quantity, unit price and net charge |
| Tax and totals | `vat_rate`, `vat_amount`, `gross_amount`; document totals | Line VAT, rate-group breakdown, net/VAT/amount due |
| Payment instructions | `bank_name`, `account_name`, `sort_code`, `account_number`; optional `iban`, `bic` | Bank information and invoice-number payment reference (IBAN/BIC only in profile packs) |
| Presentation | optional `presentation` (pack, pack hash, resolved profile, profile hash, asset hashes) | Selects the template pack; absent means the legacy `uk-classic-v3` pack |
| Simulation | Explicit `synthetic` boolean | Every demo page states `SYNTHETIC DEMO / NOT A TAX INVOICE` |

`vat_enabled` defaults to `true`; the seller VAT number is then required. A valid zero VAT rate is distinct from a non-registered seller. With `vat_enabled=false`, seller VAT number must be empty and every line's VAT rate and amount must be zero. The document is titled `INVOICE` and explicitly explains that the supplier is not VAT registered. Tax classification is a business input, never inferred from the customer's identifier.

## Exact arithmetic and supported scope

- Amounts are decimal strings with at most two decimal places; floating-point values, negatives and non-finite numbers are rejected. Quantities must be positive decimal strings.
- Each net line must equal `quantity × unit_price`, rounded to pence using `ROUND_HALF_UP`. Each line's VAT must equal its net amount multiplied by its VAT rate, using the same rounding rule. Gross must equal net plus VAT.
- Document totals and VAT-rate groups sum the validated line amounts exactly. VAT groups do not recompute tax from the group's net amount: line rounding can make those results differ.
- This is one explicit accounting policy, not a universal tax calculation engine. It supports GBP, ordinary positive invoice lines, no cash discounts (`cash_discount_rate="0"`), and no credit notes, reverse charge, margin schemes, self-billing, FX, exemptions or mixed tax jurisdictions. A zero percentage represents zero-rated supplies; exemption classification is not modelled. Production tax eligibility and source-specific treatment require a separate agreed contract.
- The renderer checks the supplied fields and arithmetic. It does not authenticate tax registrations, select rates, determine the correct tax point, prove supply, assign sequential numbers or perform legal issuance. Those decisions belong to the controlled business workflow.

## Movement VAT breakdown

The monthly billing adapter retains one financial service-package line. Its
VAT remains the HALF_UP penny rounding of the total net multiplied by the
configured synthetic 20% rate. The itemised section is a breakdown of that same
charge and does not add a second amount payable.

Under `movement_vat.policy = "line_half_up_reconciled_v1"`, every movement stores
`vat_rate`, `vat_amount` and `gross_amount` as decimal strings. VAT is independently
rounded HALF_UP per movement; gross is net plus VAT. The policy object stores
`rate`, `line_vat_total`, `line_gross_total`, and `rounding_adjustment`.
The adjustment is exactly invoice VAT minus line VAT subtotal. It can be
positive or negative and is displayed separately when nonzero, in both VAT and
gross columns. The final detail totals equal the invoice totals exactly.

For example, the retained 250-movement synthetic scope has line VAT GBP
62,820.90 and invoice VAT GBP 62,820.93. The additional GBP 0.03 is explicitly
labelled **Invoice VAT rounding adjustment**, with zero net. It is not hidden
inside an arbitrary movement. This is a presentation reconciliation under the
existing monthly-charge contract, not an assertion that every underlying
movement is a separate taxable supply. Mixed rates are not supported by this
movement-breakdown policy; the standalone invoice renderer has separate
rate-group support for its financial lines.

The renderer validates rates, each row's tax/gross calculation, both subtotals,
the signed adjustment and final totals. Missing or contradictory VAT fields
fail validation. Historical net-only itemised documents continue to render
without fabricating tax values. A VAT upgrade creates a new unapproved invoice
revision through normal billing and worker rendering; old PDF bytes remain
retained.

## Rendering and audit properties

`render_invoice(document, output_dir, compiler="pdflatex", assets=None)` is the public function. `assets` is the object store that holds a frozen profile's logo. It returns `pdf_path`, `tex_path`, `json_path`, and `pdf_sha256`. The caller supplies the final immutable invoice number, dates, content and financial values.

The JSON is copied and canonicalised before validation; no caller data is changed. The renderer saves exact JSON, escaped TeX, the actual PDF, compiler logs and a hash receipt in a content-addressed directory. The identity binds the document, template and renderer version. Documents with a `presentation` block also bind the pack hash and asset hashes (identity formula `v2`, see [INVOICE_FORMATS.md](INVOICE_FORMATS.md#integrity-and-render-identity)). Legacy documents keep the original formula. A retry returns the same verified PDF bytes; a different document or template creates a different object. Corrupt existing objects fail integrity verification. Filesystem permissions and retention/backups remain deployment responsibilities.

Only the trusted, sha256-pinned template packs can issue TeX commands; format profiles are validated data. User fields are escaped, including backslashes, braces and special characters. Compilation is isolated in a temporary directory, runs twice for page tables, disables shell escape and has a 45-second timeout per pass. Failed compiles retain the frozen source and logs; no substitute or fabricated PDF is generated. The default pdfLaTeX path supports the Latin-script demo; unsupported Unicode must fail and be resolved through a reviewed font/compiler change, not silently dropped.

The line table repeats its headings across pages. Very long descriptions continue in bounded rows with monetary amounts printed only once. Very long reference tokens have line-break opportunities. A 10,000-line ceiling bounds document generation; monthly output should aggregate billable service categories and link to separately retained movement evidence, rather than print millions of movements.

## Verification

`pytest tests/test_documents.py tests/test_format_profiles.py` runs actual pdfLaTeX compilation for both packs for VAT, non-VAT, multiple pages, literal special characters, line rounding and retry integrity. It also checks mismatched totals, malformed inputs, unsupported modes and retained failure diagnostics. Representative output must additionally be rasterised with Poppler and visually reviewed; text extraction alone does not establish correct layout.
