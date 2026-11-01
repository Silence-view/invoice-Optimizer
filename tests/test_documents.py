from __future__ import annotations

import copy
import hashlib
import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest
from pypdf import PdfReader

from invoice_optimizer.documents import (
    InvoiceCompilationError,
    InvoiceValidationError,
    render_invoice,
    tex_escape,
)


def sample_document() -> dict:
    """Synthetic seller identifiers are deliberately not plausible bank details."""
    return {
        "invoice_number": "DEMO-2026-000001", "invoice_date": "2026-10-01",
        "tax_point": "2026-09-30", "due_date": "2026-10-31", "period": "2026-09",
        "currency": "GBP", "synthetic": True, "vat_enabled": True,
        "seller": {"legal_name": "Example Procurement Services Ltd (DEMO)",
                   "address": "1 Demonstration Square\nLondon\nDEMO UK ADDRESS",
                   "vat_number": "GB-DEMO-NOT-VALID", "company_number": "DEMO-NOT-REGISTERED",
                   "email": "billing@example.test", "bank_name": "DEMO BANK - DO NOT PAY",
                   "account_name": "Example Procurement (DEMO)", "sort_code": "XX-XX-XX",
                   "account_number": "XXXXXXXX"},
        "customer": {"id": "DEMO-C001", "legal_name": "North & West Trading Ltd (DEMO)",
                     "address": "12 Test Street\nManchester\nDEMO UK ADDRESS",
                     "email": "accounts@north-west.example.test", "vat_number": "GB-DEMO-CUSTOMER"},
        "lines": [
            {"description": "Procurement administration - September 2026",
             "quantity": "1200", "unit_price": "0.25", "net_amount": "300.00",
             "vat_rate": "0.20", "vat_amount": "60.00", "gross_amount": "360.00"},
            {"description": "Monthly reconciliation and account statement service",
             "quantity": "1", "unit_price": "85.00", "net_amount": "85.00",
             "vat_rate": "0.20", "vat_amount": "17.00", "gross_amount": "102.00"},
        ],
        "net_total": "385.00", "vat_total": "77.00", "gross_total": "462.00",
        "payment_terms": "Payment due within 30 days of invoice date. Demonstration only - do not pay.",
        "reference": "DEMO-PO-2026-0042", "cash_discount_rate": "0",
    }


@pytest.fixture(scope="module")
def compiler() -> str:
    executable = shutil.which("pdflatex")
    if not executable and Path("/Library/TeX/texbin/pdflatex").is_file():
        executable = "/Library/TeX/texbin/pdflatex"
    if not executable:
        pytest.fail("Real pdflatex is required for the document integration tests")
    return executable


def test_vat_pdf_reconciles_and_same_document_reuses_exact_bytes(tmp_path, compiler):
    document = sample_document()
    original = copy.deepcopy(document)
    result = render_invoice(document, tmp_path, compiler)
    pdf_path = Path(result["pdf_path"])
    pdf = PdfReader(pdf_path)
    assert len(pdf.pages) == 1
    text = "\n".join(page.extract_text() for page in pdf.pages)
    for expected in ("SYNTHETIC DEMO / NOT A TAX INVOICE", "DEMO-2026-000001",
                     "462.00", "77.00", "385.00", "VAT SUMMARY"):
        assert expected in text
    # PDF text extractors can insert spaces at font-kerning boundaries.
    assert "GB-DEMO-NOT-VALID" in "".join(text.split())
    assert len(pdf.pages[0].mediabox) == 4
    assert float(pdf.pages[0].mediabox.width) == pytest.approx(595.28, abs=1)
    assert float(pdf.pages[0].mediabox.height) == pytest.approx(841.89, abs=1)
    assert result["pdf_sha256"] == hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    assert document == original
    assert json.loads(Path(result["json_path"]).read_text()) == original
    mtime = pdf_path.stat().st_mtime_ns
    assert render_invoice(document, tmp_path, compiler) == result
    assert pdf_path.stat().st_mtime_ns == mtime
    assert (pdf_path.parent / "compiler.log").is_file()
    receipt = json.loads((pdf_path.parent / "render.json").read_text())
    assert receipt["shell_escape"] is False
    assert receipt["passes"] == 2


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(net_total="385.01"),
    lambda d: d.update(vat_total="77.01"),
    lambda d: d.update(gross_total="462.01"),
    lambda d: d["lines"][0].update(net_amount="300.01"),
    lambda d: d["lines"][0].update(vat_amount="60.01"),
    lambda d: d["lines"][0].update(gross_amount="360.01"),
    lambda d: d["lines"][0].update(unit_price=0.25),
    lambda d: d["lines"][0].update(unit_price="0.250"),
    lambda d: d["lines"][0].update(quantity="0"),
    lambda d: d["lines"][0].update(vat_rate="NaN"),
    lambda d: d.update(invoice_date="2026-13-01"),
    lambda d: d.update(due_date="2026-09-01"),
    lambda d: d.update(currency="EUR"),
    lambda d: d.update(synthetic="true"),
    lambda d: d.update(cash_discount_rate="0.02"),
    lambda d: d["seller"].update(vat_number=""),
    lambda d: d.update(lines=[]),
])
def test_invalid_document_fails_before_any_compile(tmp_path, mutation):
    document = sample_document()
    mutation(document)
    with pytest.raises(InvoiceValidationError):
        render_invoice(document, tmp_path / "uncreated", "compiler-must-not-run")
    assert not (tmp_path / "uncreated").exists()


def test_non_vat_pdf_and_vat_contradiction_rejection(tmp_path, compiler):
    document = sample_document()
    document["vat_enabled"] = False
    document["seller"]["vat_number"] = ""
    document["vat_total"] = "0.00"
    document["gross_total"] = document["net_total"]
    for line in document["lines"]:
        line.update(vat_rate="0", vat_amount="0.00", gross_amount=line["net_amount"])
    result = render_invoice(document, tmp_path, compiler)
    text = " ".join(page.extract_text() for page in PdfReader(result["pdf_path"]).pages)
    assert "VAT INVOICE" not in text
    assert "Supplier is not VAT registered" in text
    assert "385.00" in text
    document["seller"]["vat_number"] = "GB-DEMO-INVALID"
    with pytest.raises(InvoiceValidationError, match="empty seller.vat_number"):
        render_invoice(document, tmp_path, compiler)


def test_special_characters_are_literal_and_long_invoice_paginates(tmp_path, compiler):
    document = sample_document()
    special = r"R&D 50% _discount_ #42 $5 {braces} ~tilde ^caret \input{secret.txt}"
    document["customer"]["legal_name"] = "O'Neill & Sons (DEMO)"
    document["reference"] = "PO_" + "ABCDEFGHIJ" * 12
    document["lines"][0]["description"] = special + " £1 - " + ("Long service description. " * 70)
    document["lines"] += [copy.deepcopy(document["lines"][1]) for _ in range(45)]
    for total in ("net", "vat", "gross"):
        document[f"{total}_total"] = str(sum(Decimal(line[f"{total}_amount"]) for line in document["lines"]))
    result = render_invoice(document, tmp_path, compiler)
    pdf = PdfReader(result["pdf_path"])
    assert len(pdf.pages) >= 3
    for page in pdf.pages:
        text = page.extract_text()
        assert "SYNTHETIC DEMO / NOT A TAX INVOICE" in text
        assert "DEMO-2026-000001" in text
    text = " ".join(page.extract_text() for page in pdf.pages)
    assert "R&D" in text and "50%" in text and "_discount_" in text
    assert "secret.txt" in text
    assert "Long service description" in text
    assert "Amount due" in text
    tex = Path(result["tex_path"]).read_text()
    assert r"\input{secret.txt}" not in tex
    assert r"\textbackslash{}input\{secret.txt\}" in tex
    assert "Overfull \\hbox" not in (Path(result["pdf_path"]).parent / "invoice.log").read_text()
    assert "Overfull \\vbox" not in (Path(result["pdf_path"]).parent / "invoice.log").read_text()


def test_tampered_object_is_never_reused(tmp_path, compiler):
    document = sample_document()
    result = render_invoice(document, tmp_path, compiler)
    Path(result["pdf_path"]).write_bytes(b"tampered")
    with pytest.raises(InvoiceCompilationError, match="integrity"):
        render_invoice(document, tmp_path, compiler)


def test_compiler_failure_retains_frozen_source_and_diagnostics(tmp_path):
    with pytest.raises(InvoiceCompilationError) as caught:
        render_invoice(sample_document(), tmp_path, "/no/such/compiler")
    failed = caught.value.diagnostic_dir
    assert failed.is_dir()
    assert (failed / "invoice.tex").is_file()
    assert (failed / "invoice.json").is_file()
    assert (failed / "compiler.log").is_file()
    assert not (failed / "render.json").exists()


def test_line_rounding_is_explicit_not_aggregate_rate_rounding(tmp_path, compiler):
    document = sample_document()
    document["lines"] = [dict(description="Synthetic fractional-penny VAT example", quantity="1",
                              unit_price="0.03", net_amount="0.03", vat_rate="0.20",
                              vat_amount="0.01", gross_amount="0.04") for _ in range(2)]
    document.update(net_total="0.06", vat_total="0.02", gross_total="0.08")
    result = render_invoice(document, tmp_path, compiler)
    text = " ".join(page.extract_text() for page in PdfReader(result["pdf_path"]).pages)
    assert "0.08" in text


def test_tex_escape_covers_all_command_characters():
    assert tex_escape(r"\ & % $ # _ { } ~ ^") == (
        r"\textbackslash{} \& \% \$ \# \_ \{ \} \textasciitilde{} \textasciicircum{}"
    )
