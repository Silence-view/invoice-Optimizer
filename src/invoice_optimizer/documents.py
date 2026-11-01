"""Exact-decimal, immutable UK-style invoice rendering through real LaTeX.

This module renders an already frozen document; it never assigns invoice numbers,
changes financial values, decides tax treatment, or marks an invoice as issued.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .formats import (FONTS, LABELS, LEGACY_PACK, MAX_LOGO_BYTES, PackIntegrityError, ProfileError,
                      canonical_bytes, format_date, load_pack, verify_presentation)


PENNY = Decimal("0.01")
TEMPLATE_PATH = Path(__file__).parent / "templates" / "uk_invoice.tex.j2"
# The legacy identity: documents without a presentation block keep this exact
# formula and the pinned uk-classic-v3 template bytes.
RENDERER_VERSION = "uk-a4-v3-movement-vat"
COMPILE_TIMEOUT_SECONDS = 45
MAX_LINES = 10_000
MAX_MOVEMENT_DETAILS = 10_000
MOVEMENT_VAT_POLICY = "line_half_up_reconciled_v1"


class InvoiceValidationError(ValueError):
    """A frozen document is incomplete or does not reconcile exactly."""


class InvoiceCompilationError(RuntimeError):
    """The compiler failed; diagnostic files are retained at diagnostic_dir."""

    def __init__(self, message: str, diagnostic_dir: Path):
        self.diagnostic_dir = diagnostic_dir
        super().__init__(f"{message}. Diagnostics: {diagnostic_dir}")


def _decimal(value: Any, field: str, *, money: bool = False) -> Decimal:
    # Floats must never enter a financial document, even if str(0.1) looks tidy.
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(r"\d+(?:\.\d+)?", value):
        raise InvoiceValidationError(f"{field} must be a nonnegative decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise InvoiceValidationError(f"{field} is not a decimal") from exc
    if not result.is_finite() or result >= Decimal("1e15"):
        raise InvoiceValidationError(f"{field} is outside the supported numeric range")
    if money and result.as_tuple().exponent < -2:
        raise InvoiceValidationError(f"{field} must have at most two decimal places")
    return result


def _text(value: Any, field: str, *, required: bool = True, maximum: int = 4000) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise InvoiceValidationError(f"{field} must be text of at most {maximum} characters")
    if required and not value.strip():
        raise InvoiceValidationError(f"{field} is required")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise InvoiceValidationError(f"{field} contains unsupported control characters")
    return value


def _iso_date(value: Any, field: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise InvoiceValidationError(f"{field} must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvoiceValidationError(f"{field} is not a valid date") from exc


def tex_escape(value: Any, *, token_limit: int = 30, break_every: int = 16) -> str:
    """Escape literal text without granting data any TeX command capability.

    Break opportunities inside very long tokens prevent reference IDs/URLs from
    exceeding their column. Known typographic dashes/quotes are normalised only
    for display; immutable JSON retains the exact original text.
    """
    translation = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-",
                                 "\u2013": "-", "\u2014": "-", "\u2212": "-",
                                 "\u2018": "'", "\u2019": "'", "\u201c": '"',
                                 "\u201d": '"', "\u00a0": " "})
    text = str(value).translate(translation).replace("\r\n", "\n").replace("\r", "\n")
    escapes = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
               "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
               "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
               "£": r"\pounds{}", "€": r"\texteuro{}", "\n": r"\newline{}", "\t": " "}
    rendered = []
    for token in re.split(r"(\s+)", text):
        for index, char in enumerate(token):
            rendered.append(escapes.get(char, char))
            if len(token) > token_limit and not token.isspace() and (index + 1) % break_every == 0:
                rendered.append(r"\hspace{0pt}")
    return "".join(rendered)


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _quantity(value: Decimal) -> str:
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else str(value)


def _description_chunks(text: str, length: int = 320) -> list[str]:
    # longtable cannot split a single cell over pages. Continued rows keep even a
    # long description bounded, without repeating its financial amounts.
    chunks = []
    while len(text) > length:
        split = text.rfind(" ", 0, length)
        if split < length // 2:
            split = length
        chunks.append(text[:split])
        text = text[split:].lstrip(" ")
    chunks.append(text)
    return chunks


def _detail_chunks(value: str, maximum: int) -> list[str]:
    """Bound both character count and explicit line breaks in a table cell."""
    remaining = value.replace("\r\n", "\n").replace("\r", "\n")
    chunks = []
    while remaining:
        end = min(maximum, len(remaining))
        breaks = [index for index, char in enumerate(remaining[:end]) if char == "\n"]
        if len(breaks) >= 5:
            end = breaks[4] + 1
        elif end < len(remaining):
            space = remaining.rfind(" ", 0, end)
            if space >= end // 2:
                end = space + 1
        chunks.append(remaining[:end])
        remaining = remaining[end:]
    return chunks or [""]


def _movement_vat_context(document: dict, details: list, totals: dict, groups: dict) -> dict:
    """Validate explicit row VAT and its visible invoice-level reconciliation."""
    fields = {"vat_rate", "vat_amount", "gross_amount"}
    if "movement_vat" not in document:
        if any(isinstance(detail, dict) and fields.intersection(detail) for detail in details):
            raise InvoiceValidationError("Movement VAT fields require a complete movement_vat policy")
        return {"movement_vat_enabled": False}
    policy = document["movement_vat"]
    if not isinstance(policy, dict) or policy.get("policy") != MOVEMENT_VAT_POLICY:
        raise InvoiceValidationError("movement_vat must declare the supported explicit rounding policy")
    rate = _decimal(policy.get("rate"), "movement_vat.rate")
    if len(groups) != 1 or rate not in groups:
        raise InvoiceValidationError("Movement VAT rate must match the single invoice accounting rate")
    with localcontext() as context:
        context.prec = 64
        if totals["vat"] != (totals["net"] * rate).quantize(PENNY, rounding=ROUND_HALF_UP):
            raise InvoiceValidationError("Movement VAT policy requires invoice-level HALF_UP rounding of total net at its declared rate")
        line_vat, line_gross = Decimal("0.00"), Decimal("0.00")
        for number, detail in enumerate(details):
            label = f"movement_detail[{number}]"
            if not isinstance(detail, dict):
                raise InvoiceValidationError(f"{label} must be a dictionary")
            row_rate = _decimal(detail.get("vat_rate"), f"{label}.vat_rate")
            amount = _decimal(detail.get("amount"), f"{label}.amount", money=True)
            vat = _decimal(detail.get("vat_amount"), f"{label}.vat_amount", money=True)
            gross = _decimal(detail.get("gross_amount"), f"{label}.gross_amount", money=True)
            if row_rate != rate or vat != (amount * rate).quantize(PENNY, rounding=ROUND_HALF_UP):
                raise InvoiceValidationError(f"{label}: movement VAT must use the declared rate and HALF_UP penny rounding")
            if gross != amount + vat:
                raise InvoiceValidationError(f"{label}: movement gross must equal net plus VAT")
            line_vat += vat
            line_gross += gross
        if _decimal(policy.get("line_vat_total"), "movement_vat.line_vat_total", money=True) != line_vat:
            raise InvoiceValidationError("Movement VAT subtotal does not match every row")
        if _decimal(policy.get("line_gross_total"), "movement_vat.line_gross_total", money=True) != line_gross:
            raise InvoiceValidationError("Movement gross subtotal does not match every row")
        adjustment_text = policy.get("rounding_adjustment")
        if not isinstance(adjustment_text, str) or not re.fullmatch(r"-?\d+\.\d{2}", adjustment_text):
            raise InvoiceValidationError("movement_vat.rounding_adjustment must be a signed exact-penny decimal string")
        adjustment = _decimal(adjustment_text.lstrip("-"), "movement_vat.rounding_adjustment", money=True)
        if adjustment_text.startswith("-"):
            adjustment = -adjustment
        if adjustment != totals["vat"] - line_vat or line_gross + adjustment != totals["gross"]:
            raise InvoiceValidationError("Movement VAT adjustment must reconcile both VAT and gross to the unchanged invoice totals")
    return {"movement_vat_enabled": True, "movement_vat_rate": _quantity(rate * 100),
            "movement_vat_subtotals": {"net": _money(totals["net"]), "vat": _money(line_vat), "gross": _money(line_gross)},
            "movement_vat_adjustment": f"{adjustment:+,.2f}", "movement_vat_has_adjustment": adjustment != 0}


def _movement_context(document: dict, totals: dict, groups: dict) -> dict:
    """Validate the complete frozen detail without changing accounting lines."""
    line_format = document.get("line_format", "SUMMARY")
    if line_format not in ("SUMMARY", "ITEMIZED"):
        raise InvoiceValidationError("line_format must be SUMMARY or ITEMIZED")
    count = document.get("movement_count")
    if count is not None and (type(count) is not int or count < 1):
        raise InvoiceValidationError("movement_count must be a positive integer")
    if line_format == "SUMMARY":
        if "movement_detail" in document or "movement_vat" in document:
            raise InvoiceValidationError("SUMMARY format must not contain silently omitted movement_detail or movement_vat")
        return {"line_format": line_format, "movement_count": count, "movement_rows": [], "movement_vat_enabled": False}
    details = document.get("movement_detail")
    if not isinstance(details, list) or not 1 <= len(details) <= MAX_MOVEMENT_DETAILS:
        raise InvoiceValidationError(f"ITEMIZED movement_detail must contain 1 to {MAX_MOVEMENT_DETAILS} complete entries; truncation is not permitted")
    if type(count) is not int or count != len(details):
        raise InvoiceValidationError("movement_count must match every frozen movement_detail entry")
    vat_context = _movement_vat_context(document, details, totals, groups)
    movements, versions = set(), set()
    rows = []
    total = Decimal("0.00")
    with localcontext() as context:
        context.prec = 64
        for number, detail in enumerate(details, start=1):
            label = f"movement_detail[{number - 1}]"
            if not isinstance(detail, dict):
                raise InvoiceValidationError(f"{label} must be a dictionary")
            if type(detail.get("row_no")) is not int or detail["row_no"] != number:
                raise InvoiceValidationError("Movement detail row numbers must be contiguous from 1")
            movement_id = _text(detail.get("movement_id"), f"{label}.movement_id", maximum=64)
            version_id = _text(detail.get("version_id"), f"{label}.version_id", maximum=64)
            if movement_id in movements or version_id in versions:
                raise InvoiceValidationError("Movement detail must contain unique movement and version identifiers")
            movements.add(movement_id); versions.add(version_id)
            external_id = _text(detail.get("external_id"), f"{label}.external_id", maximum=160)
            supplier_id = _text(detail.get("supplier_id"), f"{label}.supplier_id", maximum=160)
            description = _text(detail.get("description"), f"{label}.description", maximum=1000)
            timestamp = _text(detail.get("event_at"), f"{label}.event_at", maximum=64)
            try:
                event = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError as exc:
                raise InvoiceValidationError(f"{label}.event_at must be an ISO timestamp") from exc
            if event.tzinfo is None or event.utcoffset() is None:
                raise InvoiceValidationError(f"{label}.event_at must include a timezone")
            event = event.astimezone(timezone.utc)
            if event.strftime("%Y-%m") != document["period"]:
                raise InvoiceValidationError(f"{label}.event_at must belong to the invoice period in UTC")
            if detail.get("currency") != document["currency"]:
                raise InvoiceValidationError("Movement detail currency must match the GBP invoice")
            amount = _decimal(detail.get("amount"), f"{label}.amount", money=True)
            total += amount
            fragments = {"description": _detail_chunks(description, 240),
                         "external_id": _detail_chunks(external_id, 80),
                         "supplier_id": _detail_chunks(supplier_id, 80)}
            fragment_count = max(len(values) for values in fragments.values())
            for index in range(fragment_count):
                rows.append({"row_no": number, "continuation": index > 0, "last": index == fragment_count - 1,
                    "event_date": event.date().isoformat(), "event_time": event.time().isoformat(),
                    "amount": _money(amount),
                    **({"vat_rate": vat_context["movement_vat_rate"],
                        "vat_amount": _money(Decimal(detail["vat_amount"])),
                        "gross_amount": _money(Decimal(detail["gross_amount"]))} if vat_context["movement_vat_enabled"] else {}),
                    **{key: values[index] if index < len(values) else ""
                                                 for key, values in fragments.items()}})
    if total != totals["net"]:
        raise InvoiceValidationError("Movement detail net sum must match the unchanged invoice net total")
    return {"line_format": line_format, "movement_count": count, "movement_rows": rows, **vat_context}


def _validated_context(document: dict) -> dict:
    if not isinstance(document, dict):
        raise InvoiceValidationError("document must be a dictionary")
    if document.get("currency") != "GBP":
        raise InvoiceValidationError("Only GBP invoices are supported in v1")
    if not isinstance(document.get("synthetic"), bool):
        raise InvoiceValidationError("synthetic must be an explicit boolean")
    vat_enabled = document.get("vat_enabled", True)
    if not isinstance(vat_enabled, bool):
        raise InvoiceValidationError("vat_enabled must be a boolean")
    for field in ("invoice_number", "period", "payment_terms", "reference"):
        _text(document.get(field), field)
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", document["period"]):
        raise InvoiceValidationError("period must be YYYY-MM")
    invoice_date = _iso_date(document.get("invoice_date"), "invoice_date")
    tax_point = _iso_date(document.get("tax_point"), "tax_point")
    due_date = _iso_date(document.get("due_date"), "due_date")
    if due_date < invoice_date:
        raise InvoiceValidationError("due_date must not precede invoice_date")
    for party in ("seller", "customer"):
        if not isinstance(document.get(party), dict):
            raise InvoiceValidationError(f"{party} must be a dictionary")
        for field in ("legal_name", "address", "email"):
            _text(document[party].get(field), f"{party}.{field}")
        _text(document[party].get("vat_number", ""), f"{party}.vat_number", required=False)
    _text(document["customer"].get("id"), "customer.id")
    for field in ("company_number", "bank_name", "account_name", "sort_code", "account_number"):
        _text(document["seller"].get(field), f"seller.{field}")
    if vat_enabled and not document["seller"].get("vat_number", "").strip():
        raise InvoiceValidationError("A VAT-enabled invoice requires seller.vat_number")
    # A non-registered seller and zero-rated taxable supplies are different cases.
    if not vat_enabled and document["seller"].get("vat_number", "").strip():
        raise InvoiceValidationError("Non-VAT mode requires an empty seller.vat_number")
    cash_discount = _decimal(document.get("cash_discount_rate", "0"), "cash_discount_rate")
    if cash_discount != 0:
        raise InvoiceValidationError("Cash-discount accounting is not supported in v1")
    lines = document.get("lines")
    if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES:
        raise InvoiceValidationError(f"lines must contain 1 to {MAX_LINES} entries")
    groups: dict[Decimal, dict[str, Decimal]] = {}
    totals = {key: Decimal("0.00") for key in ("net", "vat", "gross")}
    rows = []
    with localcontext() as context:
        context.prec = 64
        for index, line in enumerate(lines):
            if not isinstance(line, dict):
                raise InvoiceValidationError(f"lines[{index}] must be a dictionary")
            label = f"lines[{index}]"
            description = _text(line.get("description"), f"{label}.description", maximum=12000)
            quantity = _decimal(line.get("quantity"), f"{label}.quantity")
            unit_price = _decimal(line.get("unit_price"), f"{label}.unit_price", money=True)
            rate = _decimal(line.get("vat_rate"), f"{label}.vat_rate")
            if quantity <= 0 or rate > 1:
                raise InvoiceValidationError(f"{label}: quantity must be positive and VAT rate at most 1")
            net = _decimal(line.get("net_amount"), f"{label}.net_amount", money=True)
            vat = _decimal(line.get("vat_amount"), f"{label}.vat_amount", money=True)
            gross = _decimal(line.get("gross_amount"), f"{label}.gross_amount", money=True)
            if (quantity * unit_price).quantize(PENNY, rounding=ROUND_HALF_UP) != net:
                raise InvoiceValidationError(f"{label}: quantity times unit_price does not match net_amount")
            if (net * rate).quantize(PENNY, rounding=ROUND_HALF_UP) != vat:
                raise InvoiceValidationError(f"{label}: line VAT does not match HALF_UP penny rounding")
            if net + vat != gross:
                raise InvoiceValidationError(f"{label}: net plus VAT does not match gross_amount")
            if not vat_enabled and (rate != 0 or vat != 0):
                raise InvoiceValidationError("Non-VAT mode must have zero VAT rates and amounts")
            group = groups.setdefault(rate, {key: Decimal("0.00") for key in totals})
            for key, value in (("net", net), ("vat", vat), ("gross", gross)):
                totals[key] += value
                group[key] += value
            fragments = _description_chunks(description)
            for fragment_index, fragment in enumerate(fragments):
                rows.append({"description": fragment, "continuation": fragment_index > 0,
                             "last": fragment_index == len(fragments) - 1,
                             "quantity": _quantity(quantity), "unit_price": _money(unit_price),
                             "rate": _quantity(rate * 100), "net": _money(net),
                             "vat": _money(vat), "gross": _money(gross)})
        for key, value in totals.items():
            if _decimal(document.get(f"{key}_total"), f"{key}_total", money=True) != value:
                raise InvoiceValidationError(f"{key}_total does not match the exact sum of line amounts")
    detail_context = _movement_context(document, totals, groups)
    return {"document": document, "vat_enabled": vat_enabled, "rows": rows, **detail_context,
            # Same text as strftime("%d %b %Y") in the C locale, without the locale.
            "invoice_date": format_date(invoice_date, "dd MMM yyyy"),
            "tax_point": format_date(tax_point, "dd MMM yyyy"), "due_date": format_date(due_date, "dd MMM yyyy"),
            "totals": {key: _money(value) for key, value in totals.items()},
            "vat_groups": [{"rate": _quantity(rate * 100),
                            **{key: _money(value) for key, value in values.items()}}
                           for rate, values in sorted(groups.items())]}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _pdf_text(value: str) -> str:
    # PDF metadata goes through hyperref's string parser, not tex_escape.
    return re.sub(r"[^A-Za-z0-9 .,/()-]", "", value)[:120].strip()


def _presentation_context(document: dict, profile: dict, base: dict) -> dict:
    """Template inputs for a profile pack; every value is validated or escaped later."""
    seller = document["seller"]
    for field in ("iban", "bic"):
        _text(seller.get(field, ""), f"seller.{field}", required=False, maximum=42)
    dates = {key: format_date(_iso_date(document[key], key), profile["date_format"])
             for key in ("invoice_date", "tax_point", "due_date")}
    logo = profile.get("logo")
    # IBAN/BIC are optional profile keys; the template's StrictUndefined needs them present.
    return {**dates, "document": {**document, "seller": {"iban": "", "bic": "", **seller}}, "fmt": {
        "font_tex": FONTS[profile["font"]], "paper": profile["paper"], "margin": profile["margins_mm"],
        "colors": profile["colors"], "labels": {**LABELS, **profile["labels"]}, "columns": profile["columns"],
        "show_vat_summary": profile["vat_summary"] == "always" or len(base["vat_groups"]) > 1,
        "footer_text": profile["footer_text"], "logo": logo["name"] if logo else None,
        "show_iban": profile["payment"]["show_iban_bic"], "extra_note": profile["payment"]["extra_note"],
        "pdf_title": _pdf_text(("VAT invoice " if base["vat_enabled"] else "Invoice ") + document["invoice_number"]),
        "pdf_author": _pdf_text(seller["legal_name"]),
        "pdf_subject": _pdf_text("Billing period " + document["period"])}}


def _result(directory: Path, document_hash: str, expected: dict) -> dict:
    """Verify retained bytes; receipts written before presentation packs lack the new keys."""
    try:
        receipt = json.loads((directory / "render.json").read_text())
        for filename, key in (("invoice.pdf", "pdf_sha256"), ("invoice.tex", "tex_sha256"),
                              ("invoice.json", "document_sha256")):
            if _sha256(directory / filename) != receipt[key]:
                raise ValueError(f"Stored {filename} hash mismatch")
        for name, digest in receipt.get("asset_sha256s", {}).items():
            if _sha256(directory / name) != digest:
                raise ValueError(f"Stored {name} hash mismatch")
        if receipt["document_sha256"] != document_hash or any(receipt.get(key) != value for key, value in expected.items()):
            raise ValueError("Stored rendering identity mismatch")
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        raise InvoiceCompilationError("Existing document failed integrity verification", directory) from exc
    return {"pdf_path": str(directory / "invoice.pdf"), "tex_path": str(directory / "invoice.tex"),
            "json_path": str(directory / "invoice.json"), "pdf_sha256": receipt["pdf_sha256"]}


def render_invoice(document: dict, output_dir: Path, compiler: str = "pdflatex", assets: Any = None) -> dict:
    """Validate and compile a frozen document, returning content-addressed paths.

    Financial policy is deliberately explicit: nonnegative GBP line amounts,
    HALF_UP rounding per line, no credit notes or cash discounts. The caller is
    responsible for authentic seller details, tax classification, sequential
    numbering and legal issuance. Same payload/template reuses verified bytes.
    A document's optional presentation block selects a pinned template pack;
    its assets are read from ``assets`` (an ObjectStore) and re-verified.
    """
    # Round-trip detaches the caller's mutable object and rejects non-JSON data.
    try:
        canonical = json.dumps(document, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
        frozen = json.loads(canonical)
    except (TypeError, ValueError) as exc:
        raise InvoiceValidationError("document must contain only finite JSON values") from exc
    context = _validated_context(frozen)
    document_hash = hashlib.sha256(canonical).hexdigest()
    presentation = frozen.get("presentation")
    try:
        if presentation is None:
            pack, profile = load_pack(LEGACY_PACK), None
        else:
            pack, profile = verify_presentation(frozen)
            if not pack["legacy"]:
                context.update(_presentation_context(frozen, profile, context))
    except ProfileError as exc:
        raise InvoiceValidationError(f"presentation: {exc}") from exc
    except PackIntegrityError as exc:
        raise InvoiceCompilationError(str(exc), Path(output_dir)) from exc
    template_path = pack["template"]
    template_hash = _sha256(template_path)
    if presentation is None:
        identity = hashlib.sha256(f"{RENDERER_VERSION}:{document_hash}:{template_hash}".encode()).hexdigest()
        expected = {"template_sha256": template_hash}
        asset_names = {}
    else:
        asset_names = presentation["assets"]
        assets_hash = hashlib.sha256(canonical_bytes(asset_names)).hexdigest()
        identity = hashlib.sha256(f"v2:{pack['renderer_version']}:{document_hash}:{pack['sha256']}:{assets_hash}"
                                  .encode()).hexdigest()
        expected = {"template_sha256": template_hash, "pack_id": pack["id"], "pack_sha256": pack["sha256"],
                    "profile_sha256": presentation["profile_sha256"], "asset_sha256s": asset_names}
        if asset_names and assets is None:
            raise InvoiceValidationError("presentation assets require an object store")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / identity
    if destination.exists():
        return _result(destination, document_hash, expected)
    environment = Environment(loader=FileSystemLoader(str(template_path.parent)),
                              undefined=StrictUndefined, autoescape=False,
                              block_start_string="<%", block_end_string="%>",
                              variable_start_string="<<", variable_end_string=">>",
                              comment_start_string="<#", comment_end_string="#>",
                              # Legacy bytes depend on the original whitespace handling.
                              trim_blocks=not pack["legacy"], lstrip_blocks=not pack["legacy"])
    environment.filters["tex"] = tex_escape
    environment.filters["texdetail"] = lambda value: tex_escape(value, token_limit=7, break_every=7)
    tex = environment.get_template(template_path.name).render(**context)
    build_dir = Path(tempfile.mkdtemp(prefix=".invoice-build-", dir=output_dir))
    (build_dir / "invoice.json").write_bytes(canonical)
    (build_dir / "invoice.tex").write_text(tex, encoding="utf-8")
    command = [str(compiler), "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error",
               "-file-line-error", "-jobname=invoice", "invoice.tex"]
    compiler_env = dict(os.environ, openin_any="p", openout_any="p", shell_escape="f",
                        TEXMFOUTPUT=str(build_dir))
    try:
        for name, digest in asset_names.items():
            # A profile edited after freezing cannot change this copy.
            source = assets.path(digest, verify=True)
            if source.stat().st_size > MAX_LOGO_BYTES:
                raise RuntimeError(f"Asset {name} exceeds the size limit")
            shutil.copyfile(source, build_dir / name)
            if _sha256(build_dir / name) != digest:
                raise RuntimeError(f"Asset {name} changed while copying")
        with (build_dir / "compiler.log").open("wb") as log:
            for pass_number in (1, 2):
                log.write(f"\n--- Compilation pass {pass_number} ---\n".encode())
                log.flush()
                completed = subprocess.run(command, cwd=build_dir, env=compiler_env,
                                           stdout=log, stderr=subprocess.STDOUT,
                                           timeout=COMPILE_TIMEOUT_SECONDS, check=False)
                if completed.returncode != 0:
                    raise RuntimeError(f"LaTeX exited with status {completed.returncode}")
        pdf_path = build_dir / "invoice.pdf"
        if not pdf_path.is_file():
            raise RuntimeError("Compiler did not produce a PDF")
        with pdf_path.open("rb") as pdf_stream:
            if not pdf_stream.read(8).startswith(b"%PDF-"):
                raise RuntimeError("Compiler output is not a PDF")
        receipt = {"renderer_version": pack["renderer_version"], "document_sha256": document_hash,
                   "template_sha256": template_hash, "tex_sha256": _sha256(build_dir / "invoice.tex"),
                   "pdf_sha256": _sha256(pdf_path), "compiler": str(compiler), "passes": 2,
                   "shell_escape": False, "timeout_seconds_per_pass": COMPILE_TIMEOUT_SECONDS}
        if presentation is not None:
            banner = re.search(rb"This is pdfTeX, Version [^\r\n]{1,120}", (build_dir / "compiler.log").read_bytes())
            receipt.update({key: value for key, value in expected.items() if key != "template_sha256"},
                           identity_formula="v2", profile_id=presentation["profile_id"],
                           compiler_banner=banner.group().decode("ascii", "replace") if banner else None)
        (build_dir / "render.json").write_text(json.dumps(receipt, indent=2) + "\n")
        try:
            build_dir.rename(destination)
        except OSError:
            if not destination.exists():
                raise
            # Concurrent identical render: use only an intact winning document.
            result = _result(destination, document_hash, expected)
            shutil.rmtree(build_dir)
            return result
        return _result(destination, document_hash, expected)
    except Exception as exc:
        if build_dir.exists():
            failed_dir = output_dir / f"failed-{uuid.uuid4().hex}"
            build_dir.rename(failed_dir)
        else:
            failed_dir = destination
        raise InvoiceCompilationError(f"Invoice rendering failed ({type(exc).__name__}: {exc})", failed_dir) from exc
