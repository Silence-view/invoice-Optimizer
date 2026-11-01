"""Format profiles and template packs: legacy identity, modern layout, strict validation."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tomllib
import zlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pypdf import PdfReader

from invoice_optimizer import formats
from invoice_optimizer.documents import (InvoiceCompilationError, InvoiceValidationError,
                                         RENDERER_VERSION, TEMPLATE_PATH, render_invoice)
from invoice_optimizer.formats import (PackIntegrityError, ProfileError, build_presentation, canonical_bytes,
                                       format_date, load_pack, resolve_profile, verify_presentation)
from invoice_optimizer.storage import ObjectStore
from test_documents import compiler, sample_document
from test_itemized_documents import itemized_document, with_movement_vat
from test_workflow import prepared, rows, write_csv

PROFILES = Path(__file__).resolve().parents[1] / 'config' / 'invoice_profiles'
LEGACY_SHA256 = '03cf4192053482465e0f0b9d03ee3c199a91835e1fbcbf8daf33fe1eea6c5cf0'


def _toml(value: dict, prefix: str = '') -> str:
    scalars, tables = [], []
    for key, item in value.items():
        if isinstance(item, dict):
            tables.append(f'\n[{prefix}{key}]\n' + _toml(item, f'{prefix}{key}.'))
        else:
            scalars.append(f'{key} = {json.dumps(item)}')
    return '\n'.join(scalars) + ''.join(tables) + '\n'


def _raw(name: str = 'default') -> dict:
    return tomllib.loads((PROFILES / f'{name}.toml').read_text())


def _profile_dir(tmp_path: Path, raw: dict, name: str = 'company') -> Path:
    directory = tmp_path / 'profiles'
    (directory / 'assets').mkdir(parents=True, exist_ok=True)
    (directory / f'{name}.toml').write_text(_toml(raw))
    return directory


def _png(width: int = 60, height: int = 20) -> bytes:
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    pixels = b''.join(b'\x00' + b'\x1f\x3a\x5f' * width for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


def _with_presentation(document: dict, profile_dir: Path = PROFILES, name: str = 'default') -> dict:
    presentation, _ = build_presentation(profile_dir, name)
    document = copy.deepcopy(document)
    seller = presentation['profile']['seller']
    if not document.get('vat_enabled', True):
        assert seller['vat_number'] == ''
    document['seller'] = seller
    document['presentation'] = presentation
    return document


def _pages(result: dict) -> list[str]:
    return [page.extract_text() for page in PdfReader(result['pdf_path']).pages]


def _squash(text: str) -> str:
    return ''.join(text.split())


def _assert_clean_log(result: dict) -> None:
    log = (Path(result['pdf_path']).parent / 'invoice.log').read_text(errors='replace')
    assert 'Overfull \\hbox' not in log and 'Overfull \\vbox' not in log
    assert 'undefined references' not in log and 'Rerun to get' not in log


def _assert_every_page_marked(pages: list[str], number: str) -> None:
    for index, text in enumerate(pages, 1):
        assert 'SYNTHETIC DEMO / NOT A TAX INVOICE' in text
        assert number in text
        assert f'Page {index} of {len(pages)}' in text


# --- legacy pack ------------------------------------------------------------

def test_legacy_template_is_byte_identical_and_pinned():
    assert hashlib.sha256(TEMPLATE_PATH.read_bytes()).hexdigest() == LEGACY_SHA256
    pack = load_pack('uk-classic-v3')
    assert pack['template'] == TEMPLATE_PATH and pack['renderer_version'] == RENDERER_VERSION == 'uk-a4-v3-movement-vat'
    assert formats.PINNED_PACKS['uk-classic-v3']['template_sha256'] == LEGACY_SHA256


def test_document_without_presentation_keeps_exact_legacy_identity(tmp_path, compiler):
    document = sample_document()
    result = render_invoice(document, tmp_path, compiler)
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    expected = hashlib.sha256(f'uk-a4-v3-movement-vat:{hashlib.sha256(canonical).hexdigest()}:{LEGACY_SHA256}'.encode()).hexdigest()
    assert Path(result['pdf_path']).parent.name == expected
    receipt = json.loads((Path(result['pdf_path']).parent / 'render.json').read_text())
    assert receipt['renderer_version'] == 'uk-a4-v3-movement-vat' and receipt['template_sha256'] == LEGACY_SHA256
    assert 'pack_id' not in receipt and 'identity_formula' not in receipt
    assert 'VAT SUMMARY' in ' '.join(_pages(result))


def test_old_receipt_without_pack_keys_is_still_verified(tmp_path, compiler):
    result = render_invoice(sample_document(), tmp_path, compiler)
    receipt_path = Path(result['pdf_path']).parent / 'render.json'
    receipt = json.loads(receipt_path.read_text())
    assert set(receipt) == {'renderer_version', 'document_sha256', 'template_sha256', 'tex_sha256', 'pdf_sha256',
                            'compiler', 'passes', 'shell_escape', 'timeout_seconds_per_pass'}
    assert render_invoice(sample_document(), tmp_path, 'compiler-must-not-run') == result


def test_legacy_template_drift_is_refused_before_compiling(tmp_path, monkeypatch):
    monkeypatch.setitem(formats.PINNED_PACKS, 'uk-classic-v3',
                        {**formats.PINNED_PACKS['uk-classic-v3'], 'template_sha256': '0' * 64})
    with pytest.raises(InvoiceCompilationError, match='drifted'):
        render_invoice(sample_document(), tmp_path / 'out', 'compiler-must-not-run')
    assert not (tmp_path / 'out').exists()


def test_classic_profile_renders_the_legacy_layout_under_a_bound_identity(tmp_path, compiler):
    document = _with_presentation(sample_document(), PROFILES, 'classic')
    result = render_invoice(document, tmp_path, compiler)
    receipt = json.loads((Path(result['pdf_path']).parent / 'render.json').read_text())
    assert receipt['pack_id'] == 'uk-classic-v3' and receipt['template_sha256'] == LEGACY_SHA256
    assert receipt['renderer_version'] == 'uk-a4-v3-movement-vat' and receipt['identity_formula'] == 'v2'
    text = ' '.join(_pages(result))
    assert 'VAT SUMMARY' in text and 'Example Procurement Services Ltd (Synthetic)' in text


def test_modern_pack_drift_is_refused(tmp_path, monkeypatch):
    document = _with_presentation(sample_document())
    packs = tmp_path / 'packs'
    shutil.copytree(formats.PACK_ROOT, packs)
    with (packs / 'uk-modern-v1' / 'invoice.tex.j2').open('a') as stream:
        stream.write('% an unreviewed edit\n')
    monkeypatch.setattr(formats, 'PACK_ROOT', packs)
    with pytest.raises(PackIntegrityError, match='drifted'):
        load_pack('uk-modern-v1')
    with pytest.raises(InvoiceCompilationError, match='drifted'):
        render_invoice(document, tmp_path / 'out', 'compiler-must-not-run')


def test_frozen_document_naming_another_pack_hash_is_not_restyled(tmp_path):
    document = _with_presentation(sample_document())
    document['presentation']['pack_sha256'] = '1' * 64
    with pytest.raises(InvoiceCompilationError, match='silently restyled'):
        render_invoice(document, tmp_path / 'out', 'compiler-must-not-run')


# --- modern pack ------------------------------------------------------------

def test_modern_vat_invoice_compiles_with_legal_content_and_new_identity(tmp_path, compiler):
    document = _with_presentation(sample_document())
    result = render_invoice(document, tmp_path / 'a', compiler)
    pdf = PdfReader(result['pdf_path'])
    assert len(pdf.pages) == 1
    assert float(pdf.pages[0].mediabox.width) == pytest.approx(595.28, abs=1)
    pages = _pages(result)
    text = ' '.join(pages)
    _assert_every_page_marked(pages, 'DEMO-2026-000001')
    for expected in ('VAT INVOICE', 'Bill to'.upper(), 'Invoice date', 'Supply / tax point', 'Payment due',
                     'DEMO-PO-2026-0042', 'Example Procurement Services Ltd (Synthetic)', 'Company no. DEMO ONLY',
                     'VAT no. DEMO - NOT A VAT REGISTRATION', 'North & West Trading Ltd (DEMO)', 'GB-DEMO-CUSTOMER',
                     'Qty', 'Unit price', 'VAT rate', '20%', '300.00', '60.00', '360.00', 'Subtotal (net)',
                     'GBP 385.00', 'VAT at 20%', 'GBP 77.00', 'GBP 462.00', 'Sort code', '00-00-00',
                     'IBAN', 'GB00 DEMO 0000 0000 0000 00', 'DEMOGB00XXX', 'Cash discount: none offered'):
        # Extractors may insert spaces at kerning boundaries of tabular figures.
        assert ''.join(expected.split()) in ''.join(text.split()), expected
    assert text.count('Amount due') == 1
    assert 'VAT SUMMARY' not in text.upper()
    _assert_clean_log(result)
    receipt = json.loads((Path(result['pdf_path']).parent / 'render.json').read_text())
    presentation = document['presentation']
    assert receipt['pack_id'] == 'uk-modern-v1' and receipt['identity_formula'] == 'v2'
    assert receipt['pack_sha256'] == presentation['pack_sha256'] == load_pack('uk-modern-v1')['sha256']
    assert receipt['profile_sha256'] == presentation['profile_sha256'] and receipt['asset_sha256s'] == {}
    assert receipt['compiler_banner'].startswith('This is pdfTeX')
    doc_hash = hashlib.sha256(canonical_bytes(document)).hexdigest()
    assets_hash = hashlib.sha256(canonical_bytes({})).hexdigest()
    assert Path(result['pdf_path']).parent.name == hashlib.sha256(
        f"v2:uk-modern-v1:{doc_hash}:{presentation['pack_sha256']}:{assets_hash}".encode()).hexdigest()
    # Deterministic bytes: an independent build directory gives the same PDF.
    assert render_invoice(document, tmp_path / 'b', compiler)['pdf_sha256'] == result['pdf_sha256']
    assert render_invoice(document, tmp_path / 'a', 'compiler-must-not-run') == result
    info = PdfReader(result['pdf_path']).metadata
    assert info.title == 'VAT invoice DEMO-2026-000001' and 'CreationDate' not in str(info)


def test_modern_non_vat_invoice(tmp_path, compiler):
    raw = _raw()
    raw['seller']['vat_number'] = ''
    directory = _profile_dir(tmp_path, raw)
    document = sample_document()
    document.update(vat_enabled=False, vat_total='0.00', gross_total=document['net_total'])
    for line in document['lines']:
        line.update(vat_rate='0', vat_amount='0.00', gross_amount=line['net_amount'])
    result = render_invoice(_with_presentation(document, directory, 'company'), tmp_path / 'out', compiler)
    text = ' '.join(_pages(result))
    assert 'VAT INVOICE' not in text and 'INVOICE' in text
    assert 'Supplier is not VAT registered' in text and 'No VAT is charged' in text
    assert 'VAT rate' not in text and 'NOT A VAT REGISTRATION' not in text
    assert text.count('Amount due') == 1 and 'GBP385.00' in _squash(text)
    _assert_clean_log(result)


def test_modern_multi_page_invoice_marks_every_page_and_escapes_text(tmp_path, compiler):
    document = sample_document()
    special = r"R&D 50% _discount_ #42 $5 {braces} ~tilde ^caret \input{secret.txt}"
    document['reference'] = 'PO_' + 'ABCDEFGHIJ' * 12
    document['lines'][0]['description'] = special + ' £1 - ' + ('Long service description. ' * 70)
    document['lines'] += [copy.deepcopy(document['lines'][1]) for _ in range(45)]
    for total in ('net', 'vat', 'gross'):
        document[f'{total}_total'] = str(sum(Decimal(line[f'{total}_amount']) for line in document['lines']))
    result = render_invoice(_with_presentation(document), tmp_path, compiler)
    pages = _pages(result)
    assert len(pages) >= 3
    _assert_every_page_marked(pages, 'DEMO-2026-000001')
    text = ' '.join(pages)
    assert 'R&D' in text and '50%' in text and '_discount_' in text and 'secret.txt' in text
    assert text.count('Amount due') == 1 and 'continued' in text
    tex = Path(result['tex_path']).read_text()
    assert r'\input{secret.txt}' not in tex and r'\textbackslash{}input\{secret.txt\}' in tex
    _assert_clean_log(result)


def test_extreme_amounts_stay_inside_their_columns(tmp_path, compiler):
    document = sample_document()
    document['lines'] = [{'description': 'Largest supported amount', 'quantity': '1', 'unit_price': '833333333333333.32',
                          'net_amount': '833333333333333.32', 'vat_rate': '0.20', 'vat_amount': '166666666666666.66',
                          'gross_amount': '999999999999999.98'}]
    document.update(net_total='833333333333333.32', vat_total='166666666666666.66', gross_total='999999999999999.98')
    result = render_invoice(_with_presentation(document), tmp_path, compiler)
    assert '999,999,999,999,999.98' in _squash(' '.join(_pages(result)))
    _assert_clean_log(result)


@pytest.mark.parametrize('amounts,adjustment', [(['0.03'] * 3, '-0.01'), (['1.00'] * 3, None)])
def test_modern_itemised_detail_with_movement_vat_reconciles(tmp_path, compiler, amounts, adjustment):
    document = with_movement_vat(itemized_document(3, amounts))
    result = render_invoice(_with_presentation(document), tmp_path, compiler)
    pages = _pages(result)
    assert len(pages) == 2
    _assert_every_page_marked(pages, 'DEMO-2026-000001')
    text = ' '.join(pages)
    assert 'Itemised movement detail: 3 movements, listed on the following pages.' in text
    assert 'Itemised movement detail' in pages[1] and 'Reconciled invoice totals' in pages[1]
    assert 'Movement subtotal' in pages[1] and 'breakdown of the same monthly charge' in pages[1]
    assert text.count('Amount due') == 1
    for index in (1, 2, 3):
        assert ''.join(text.split()).count(f'SRC-MOV-0000{index}') == 1
    assert ('Invoice VAT rounding adjustment' in text) == (adjustment is not None)
    if adjustment:
        assert _squash(text).count(adjustment) == 2
    _assert_clean_log(result)


def test_modern_net_only_itemised_detail_paginates(tmp_path, compiler):
    document = itemized_document(120)
    result = render_invoice(_with_presentation(document), tmp_path, compiler)
    pages = _pages(result)
    assert len(pages) >= 3
    _assert_every_page_marked(pages, 'DEMO-2026-000001')
    text = ' '.join(pages)
    assert 'Movement reference' in text and 'VAT rate' in pages[0] and 'VAT rate' not in ' '.join(pages[1:])
    assert text.count('Amount due') == 1
    _assert_clean_log(result)


def test_vat_summary_policy_and_letter_paper(tmp_path, compiler):
    document = sample_document()
    document['lines'][1].update(vat_rate='0.05', vat_amount='4.25', gross_amount='89.25')
    document.update(vat_total='64.25', gross_total='449.25')
    text = ' '.join(_pages(render_invoice(_with_presentation(document), tmp_path / 'multi', compiler)))
    assert 'VAT SUMMARY' in text and 'VAT total' in text and '5%' in text
    raw = _raw()
    raw.update(vat_summary='always', paper='letter', margins_mm=20, font='helvet', date_format='dd/MM/yyyy')
    raw['labels'] = {'amount_due': 'Balance to pay'}
    directory = _profile_dir(tmp_path, raw)
    result = render_invoice(_with_presentation(sample_document(), directory, 'company'), tmp_path / 'letter', compiler)
    pdf = PdfReader(result['pdf_path'])
    assert float(pdf.pages[0].mediabox.width) == pytest.approx(612, abs=1)
    text = ' '.join(_pages(result))
    assert 'VAT SUMMARY' in text and '01/10/2026' in text and 'Balance to pay' in text and 'Amount due' not in text
    _assert_clean_log(result)


def test_profile_change_changes_identity_and_bytes(tmp_path, compiler):
    raw = _raw()
    raw['colors']['accent'] = '7A1F3D'
    directory = _profile_dir(tmp_path, raw)
    first = _with_presentation(sample_document())
    second = _with_presentation(sample_document(), directory, 'company')
    assert first['presentation']['profile_sha256'] != second['presentation']['profile_sha256']
    a = render_invoice(first, tmp_path / 'out', compiler)
    b = render_invoice(second, tmp_path / 'out', compiler)
    assert Path(a['pdf_path']).parent != Path(b['pdf_path']).parent and a['pdf_sha256'] != b['pdf_sha256']


def test_logo_is_frozen_by_hash_and_copied_from_the_object_store(tmp_path, compiler):
    raw = _raw()
    raw['logo'] = 'brand/logo.png'
    directory = _profile_dir(tmp_path, raw)
    (directory / 'assets' / 'brand').mkdir()
    logo = directory / 'assets' / 'brand' / 'logo.png'
    logo.write_bytes(_png())
    presentation, paths = build_presentation(directory, 'company')
    digest = hashlib.sha256(logo.read_bytes()).hexdigest()
    assert presentation['assets'] == {'logo.png': digest} and paths == {'logo.png': logo.resolve()}
    document = sample_document()
    document.update(seller=presentation['profile']['seller'], presentation=presentation)
    with pytest.raises(InvoiceValidationError, match='object store'):
        render_invoice(document, tmp_path / 'out', 'compiler-must-not-run')
    store = ObjectStore(tmp_path / 'objects')
    assert store.put_file(logo) == digest
    logo.write_bytes(_png(10, 10))  # a later profile edit cannot restyle the frozen document
    result = render_invoice(document, tmp_path / 'out', compiler, assets=store)
    directory_out = Path(result['pdf_path']).parent
    assert hashlib.sha256((directory_out / 'logo.png').read_bytes()).hexdigest() == digest
    receipt = json.loads((directory_out / 'render.json').read_text())
    assert receipt['asset_sha256s'] == {'logo.png': digest}
    (directory_out / 'logo.png').write_bytes(b'tampered')
    with pytest.raises(InvoiceCompilationError, match='integrity'):
        render_invoice(document, tmp_path / 'out', compiler, assets=store)


# --- validation -------------------------------------------------------------

def _rejects(mutation, match, assets_dir=None):
    raw = _raw()
    mutation(raw)
    with pytest.raises(ProfileError, match=match):
        resolve_profile(raw, assets_dir=assets_dir)


@pytest.mark.parametrize('colour', ['red', '#12345', '12345G', 'FFFF00', 'FFFFFF', '1F3A5F;\\input{x}', 0x1F3A5F])
def test_rejects_bad_or_low_contrast_colours(colour):
    _rejects(lambda raw: raw['colors'].update(accent=colour), 'hex colour|contrast')


@pytest.mark.parametrize('font', ['comic', 'Inter', 'inter}\\input{/etc/passwd}', 'montserrat', ''])
def test_rejects_unknown_fonts(font):
    _rejects(lambda raw: raw.update(font=font), 'font must be one of')


@pytest.mark.parametrize('mutation,match', [
    (lambda raw: raw['labels'].update(unknown='x'), 'unknown keys'),
    (lambda raw: raw['labels'].update(bill_to='x' * 81), 'at most 80'),
    (lambda raw: raw['labels'].update(bill_to='Bill\nto'), 'control characters'),
    (lambda raw: raw['labels'].update(bill_to='  '), 'required'),
    (lambda raw: raw.update(footer_text='x' * 301), 'at most 300'),
    (lambda raw: raw['columns'].update(vat_rate=False), 'Notice 700'),
    (lambda raw: raw['columns'].update(quantity=False), 'Notice 700'),
    (lambda raw: raw['columns'].update(line_vat='yes'), 'true or false'),
    (lambda raw: raw.update(margins_mm=11), 'margins_mm'),
    (lambda raw: raw.update(margins_mm=26), 'margins_mm'),
    (lambda raw: raw.update(margins_mm=16.5), 'margins_mm'),
    (lambda raw: raw.update(margins_mm=True), 'margins_mm'),
    (lambda raw: raw.update(paper='a3'), 'paper must be'),
    (lambda raw: raw.update(date_format='%d %b %Y'), 'date_format'),
    (lambda raw: raw.update(vat_summary='never'), 'vat_summary'),
    (lambda raw: raw.update(tex_preamble='\\input{x}'), 'unknown keys'),
    (lambda raw: raw.update(pack='../uk-modern-v1'), 'Unknown template pack'),
    (lambda raw: raw['seller'].update(email='not-an-email'), 'email'),
    (lambda raw: raw['seller'].update(iban='NOT AN IBAN'), 'IBAN'),
    (lambda raw: raw['seller'].pop('sort_code'), 'sort_code'),
    (lambda raw: raw['seller'].update(admin='x'), 'unknown keys'),
    (lambda raw: raw['payment'].update(show_qr=True), 'unknown keys'),
    (lambda raw: raw.pop('seller'), 'seller'),
])
def test_rejects_invalid_profile_fields(mutation, match):
    _rejects(mutation, match)


def test_classic_pack_accepts_only_a_seller_block():
    assert set(resolve_profile(_raw('classic'))[0]) == {'pack', 'description', 'seller'}
    raw = _raw('classic')
    raw['colors'] = {'accent': '1F3A5F'}
    with pytest.raises(ProfileError, match='does not support'):
        resolve_profile(raw)


@pytest.mark.parametrize('path', ['../secret.png', '/etc/passwd', 'brand/../../secret.png', '~/logo.png',
                                  'brand\\..\\..\\secret.png', 'link.png', 'logo.svg', 'fake.png', 'huge.png',
                                  'missing.png', ''])
def test_logo_paths_are_confined_and_checked(tmp_path, path):
    assets = tmp_path / 'assets'
    (assets / 'brand').mkdir(parents=True)
    (tmp_path / 'secret.png').write_bytes(_png())
    (assets / 'link.png').symlink_to(tmp_path / 'secret.png')
    (assets / 'logo.svg').write_text('<svg/>')
    (assets / 'fake.png').write_text('not a png')
    (assets / 'huge.png').write_bytes(_png() + b'\0' * formats.MAX_LOGO_BYTES)
    raw = _raw()
    raw['logo'] = path
    if path == '':
        assert resolve_profile(raw, assets_dir=assets)[0]['logo'] is None
        return
    with pytest.raises(ProfileError, match='logo'):
        resolve_profile(raw, assets_dir=assets)


def test_profile_file_cannot_name_a_stored_object_as_logo(tmp_path):
    raw = _raw()
    raw['logo'] = {'name': 'logo.pdf', 'sha256': '0' * 64, 'bytes': 10}
    with pytest.raises(ProfileError, match='logo must be a relative path'):
        resolve_profile(raw, assets_dir=tmp_path)


@pytest.mark.parametrize('font', sorted(formats.FONTS))
def test_every_font_choice_compiles_cleanly(tmp_path, compiler, font):
    raw = _raw()
    raw['font'] = font
    directory = _profile_dir(tmp_path, raw)
    result = render_invoice(_with_presentation(sample_document(), directory, 'company'), tmp_path / 'out', compiler)
    assert len(_pages(result)) == 1
    _assert_clean_log(result)


@pytest.mark.parametrize('profile_id', ['../default', 'Default', 'a/b', '', 'x' * 65, '.hidden'])
def test_profile_ids_cannot_traverse(profile_id):
    with pytest.raises(ProfileError, match='Profile id'):
        build_presentation(PROFILES, profile_id)


def test_label_tex_injection_is_escaped(tmp_path, compiler):
    raw = _raw()
    raw['labels'] = {'bill_to': r'\input{secret.txt} & 100% $x$', 'payment_reference': r'}\end{document}'}
    raw['footer_text'] = r'Registered office ~ \write18{rm -rf} #1'
    directory = _profile_dir(tmp_path, raw)
    result = render_invoice(_with_presentation(sample_document(), directory, 'company'), tmp_path / 'out', compiler)
    tex = Path(result['tex_path']).read_text()
    assert r'\input{secret.txt}' not in tex and r'\write18{' not in tex and r'}\end{document}' not in tex
    assert r'\textbackslash{}input\{secret.txt\} \& 100\% \$x\$' in tex
    text = ' '.join(_pages(result))
    assert 'SECRET.TXT' in text.upper() and 'write18' in text


@pytest.mark.parametrize('tamper,match', [
    (lambda p: p['profile']['colors'].update(accent='7A1F3D'), 'profile_sha256'),
    (lambda p: p['profile']['colors'].update(accent='1f3a5f'), 'canonical'),
    (lambda p: p['profile'].update(font='comic'), 'font'),
    (lambda p: p.update(assets={'logo.png': '0' * 64}), 'assets'),
    (lambda p: p.update(renderer_version='uk-modern-v2'), 'renderer_version'),
    (lambda p: p.update(profile_id='../x'), 'profile_id'),
    (lambda p: p.update(extra=True), 'exactly'),
])
def test_tampered_presentation_is_rejected_before_compiling(tmp_path, tamper, match):
    document = _with_presentation(sample_document())
    tamper(document['presentation'])
    with pytest.raises(InvoiceValidationError, match=match):
        render_invoice(document, tmp_path / 'out', 'compiler-must-not-run')
    assert not (tmp_path / 'out').exists()


def test_seller_must_match_the_frozen_profile(tmp_path):
    document = _with_presentation(sample_document())
    document['seller'] = {**document['seller'], 'account_number': '12345678'}
    with pytest.raises(InvoiceValidationError, match='seller'):
        render_invoice(document, tmp_path / 'out', 'compiler-must-not-run')


def test_resolution_is_idempotent_and_example_profiles_validate():
    for name in ('default', 'classic'):
        presentation, _ = build_presentation(PROFILES, name)
        assert resolve_profile(presentation['profile'], frozen=True)[0] == presentation['profile']
        assert verify_presentation({'seller': presentation['profile']['seller'], 'presentation': presentation})
    assert build_presentation(PROFILES, 'default')[0]['pack'] == 'uk-modern-v1'


def test_dates_use_a_locale_free_month_table():
    value = date(2026, 9, 5)
    assert [format_date(value, pattern) for pattern in formats.DATE_FORMATS] == [
        '5 Sep 2026', '05 Sep 2026', '5 September 2026', '05/09/2026', '2026-09-05']
    assert format_date(date(2026, 10, 1), 'dd MMM yyyy') == date(2026, 10, 1).strftime('%d %b %Y')


def test_cli_validate_and_preview(tmp_path):
    env = {**os.environ, 'INVOICE_DATA_DIR': str(tmp_path / 'data'), 'INVOICE_PROFILE_DIR': str(PROFILES)}
    command = [sys.executable, '-m', 'invoice_optimizer', 'format']
    listed = json.loads(subprocess.run(command + ['list'], env=env, capture_output=True, check=True, text=True).stdout)
    assert {pack['id']: pack['status'] for pack in listed['packs']} == {'uk-classic-v3': 'OK', 'uk-modern-v1': 'OK'}
    assert {profile['id']: profile['status'] for profile in listed['profiles']} == {'classic': 'VALID', 'default': 'VALID'}
    valid = json.loads(subprocess.run(command + ['validate', 'default'], env=env, capture_output=True,
                                      check=True, text=True).stdout)
    assert valid['valid'] and valid['pack'] == 'uk-modern-v1' and valid['resolved_profile']['font'] == 'inter'
    raw = _raw()
    raw['font'] = 'comic'
    env['INVOICE_PROFILE_DIR'] = str(_profile_dir(tmp_path, raw, 'broken'))
    failed = subprocess.run(command + ['validate', 'broken'], env=env, capture_output=True, text=True)
    assert failed.returncode == 1 and json.loads(failed.stdout)['valid'] is False
    env['INVOICE_PROFILE_DIR'] = str(PROFILES)
    preview = json.loads(subprocess.run(command + ['preview', '--profile', 'default', '--out', str(tmp_path / 'preview')],
                                        env=env, capture_output=True, check=True, text=True, timeout=180).stdout)
    assert Path(preview['pdf_path']).is_file() and preview['synthetic'] is True
    if shutil.which('pdftoppm'):
        assert Path(preview['png_path']).read_bytes().startswith(b'\x89PNG')


# --- billing integration -----------------------------------------------------

def test_freeze_embeds_profile_and_regeneration_applies_a_new_one(prepared, tmp_path):
    import dataclasses
    from sqlalchemy import select
    from invoice_optimizer import billing
    from invoice_optimizer.billing import create_invoice, regenerate_invoice_format
    from invoice_optimizer.db import Blocked, Conflict, digest
    from invoice_optimizer.models import Audit, Invoice
    from invoice_optimizer.worker import run_job

    def work():
        while run_job(db, kinds=('ASSESS', 'SCORE', 'RENDER')):
            pass

    db = prepared
    default_settings = db.settings
    db.settings = dataclasses.replace(default_settings, format_profile='missing')
    with pytest.raises(Blocked, match='format profile is not usable'):
        create_invoice(db, 'C1', '2026-09', 'format-close-missing', 'reviewer', line_format='SUMMARY')
    db.settings = default_settings
    created = create_invoice(db, 'C1', '2026-09', 'format-close', 'reviewer', line_format='SUMMARY')
    work()
    with db.transaction() as session:
        old = session.get(Invoice, created['id'])
        presentation = old.document['presentation']
        assert old.status == 'DRAFT' and presentation['pack'] == 'uk-modern-v1' and presentation['profile_id'] == 'default'
        assert old.document['seller'] == presentation['profile']['seller']
        assert old.package['presentation'] == {key: presentation[key] for key in billing.PACKAGE_PRESENTATION_KEYS}
        assert old.fingerprint == digest(old.package) and old.package['message']['from'] == 'billing@example.test'
        old_id, old_fingerprint, old_pdf = old.id, old.fingerprint, old.pdf_sha256
    with pytest.raises(Conflict, match='already uses'):
        regenerate_invoice_format(db, old_id, old_fingerprint, 'SUMMARY', 'restyle-unchanged', 'reviewer')

    raw = _raw()
    raw['logo'] = 'logo.png'
    raw['colors']['accent'] = '7A1F3D'
    directory = _profile_dir(tmp_path, raw, 'brand')
    (directory / 'assets' / 'logo.png').write_bytes(_png())
    logo_sha = hashlib.sha256(_png()).hexdigest()
    db.settings = dataclasses.replace(default_settings, profile_dir=directory, format_profile='brand')
    changed = regenerate_invoice_format(db, old_id, old_fingerprint, 'SUMMARY', 'restyle-1', 'reviewer')
    assert changed['approval_required'] and changed['old_invoice_id'] == old_id
    assert regenerate_invoice_format(db, old_id, old_fingerprint, 'SUMMARY', 'restyle-1', 'reviewer') == changed
    (directory / 'assets' / 'logo.png').unlink()  # rendering reads only the frozen object copy
    work()
    store = ObjectStore(db.settings.data_dir / 'objects')
    with db.transaction() as session:
        previous, new = session.get(Invoice, old_id), session.get(Invoice, changed['id'])
        assert previous.status == 'STALE' and previous.pdf_sha256 == old_pdf
        assert new.status == 'DRAFT' and new.approved_at is None and new.pdf_sha256 != old_pdf
        assert (new.net_total, new.vat_total, new.gross_total, new.snapshot_hash) == (
            previous.net_total, previous.vat_total, previous.gross_total, previous.snapshot_hash)
        assert new.document['presentation']['profile_id'] == 'brand'
        assert new.document['presentation']['assets'] == {'logo.png': logo_sha}
        assert new.package['presentation']['profile_sha256'] == new.document['presentation']['profile_sha256']
        assert store.path(logo_sha, verify=True).read_bytes() == _png()
        relation = session.scalar(select(Audit).where(Audit.action == 'invoice_format_regenerated',
                                                      Audit.entity_id == new.id)).details
        assert relation['from_presentation']['profile_id'] == 'default'
        assert relation['to_presentation']['profile_id'] == 'brand' and relation['human_approval_required']
        assert ' '.join(_pages({'pdf_path': store.path(new.pdf_key, verify=True)})).count('Amount due') == 1
    db.settings = default_settings
