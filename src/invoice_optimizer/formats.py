"""Invoice presentation: code-reviewed template packs and validated company profiles.

A pack is trusted TeX, pinned by sha256 in this module, so any edit needs a
code review and a new pack id. A profile is company data (TOML) and never
becomes raw TeX: colours are validated hex, fonts and date formats are enums
mapped here, free text is escaped by the template, and a logo is copied into
the build under a fixed name. Freezing embeds the fully resolved profile in the
invoice document, so later profile edits never restyle a frozen draft.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

PACK_ROOT = Path(__file__).parent / 'templates' / 'packs'
LEGACY_TEMPLATE = Path(__file__).parent / 'templates' / 'uk_invoice.tex.j2'
LEGACY_PACK = 'uk-classic-v3'
DEFAULT_PACK = 'uk-modern-v1'
# Changing a pinned file must ship as a new pack id and renderer version;
# frozen documents name the pack hash they were reviewed with.
PINNED_PACKS = {
    'uk-classic-v3': {'renderer_version': 'uk-a4-v3-movement-vat',
                      'template_sha256': '03cf4192053482465e0f0b9d03ee3c199a91835e1fbcbf8daf33fe1eea6c5cf0',
                      'pack_sha256': 'f96b1bf1d50ebebe887a6f11955d4a5cd1dea9e1b6539bb429a56a3ad3ec94e0'},
    'uk-modern-v1': {'renderer_version': 'uk-modern-v1',
                     'pack_sha256': 'ce0d1d3af880dbc4178761c4029828c10e75338840a47365d2824c5212c49f82'},
}
PROFILE_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')
MAX_PROFILE_BYTES = 64 * 1024
MAX_LOGO_BYTES = 512 * 1024
LOGO_TYPES = {'.png': ('png', b'\x89PNG\r\n\x1a\n'), '.jpg': ('jpg', b'\xff\xd8\xff'),
              '.jpeg': ('jpg', b'\xff\xd8\xff'), '.pdf': ('pdf', b'%PDF-')}
# Each line was compiled twice under pdflatex -no-shell-escape and produced
# byte-identical PDFs (TeX Live 2025). Montserrat failed and is excluded.
FONTS = {
    'inter': '\\usepackage[sfdefault,type1,proportional]{inter}',
    'helvet': '\\usepackage{helvet}\n\\renewcommand{\\familydefault}{\\sfdefault}',
    'sourcesanspro': '\\usepackage[default,type1]{sourcesanspro}',
    'FiraSans': '\\usepackage[sfdefault,type1]{FiraSans}',
    'roboto': '\\usepackage[sfdefault,type1]{roboto}',
    'tgheros': '\\usepackage{tgheros}\n\\renewcommand{\\familydefault}{\\sfdefault}',
    'lato': '\\usepackage[default]{lato}',
}
MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
          'August', 'September', 'October', 'November', 'December')
DATE_FORMATS = ('d MMM yyyy', 'dd MMM yyyy', 'd MMMM yyyy', 'dd/MM/yyyy', 'yyyy-MM-dd')
PAPERS = ('a4', 'letter')
LABELS = {
    'bill_to': 'Bill to', 'invoice_details': 'Invoice details', 'invoice_number': 'Invoice number',
    'invoice_date': 'Invoice date', 'tax_point': 'Supply / tax point', 'due_date': 'Payment due',
    'reference': 'Reference / PO', 'period': 'Billing period', 'customer_id': 'Customer ID',
    'currency': 'Currency', 'description': 'Description', 'quantity': 'Qty', 'unit_price': 'Unit price',
    'net': 'Net', 'vat_rate': 'VAT rate', 'vat': 'VAT', 'total': 'Total', 'vat_summary': 'VAT summary',
    'subtotal': 'Subtotal (net)', 'vat_total': 'VAT total', 'amount_due': 'Amount due',
    'payment': 'Payment details', 'terms': 'Terms', 'bank': 'Bank', 'account_name': 'Account name',
    'sort_code': 'Sort code', 'account_number': 'Account number', 'iban': 'IBAN', 'bic': 'BIC / SWIFT',
    'payment_reference': 'Payment reference',
}
# HMRC VAT Notice 700 s16.3: every full VAT invoice line states quantity or
# extent, unit price and VAT rate. Only redundant per-line amounts may hide.
REQUIRED_COLUMNS = ('quantity', 'unit_price', 'vat_rate')
COLUMNS = {'quantity': True, 'unit_price': True, 'vat_rate': True, 'line_vat': True, 'line_total': True}
SELLER_FIELDS = {'legal_name': 200, 'address': 400, 'email': 200, 'company_number': 80, 'vat_number': 80,
                 'bank_name': 120, 'account_name': 120, 'sort_code': 40, 'account_number': 40}
SELLER_OPTIONAL = {'iban': 42, 'bic': 11}
DEFAULTS = {
    'description': '', 'paper': 'a4', 'margins_mm': 16, 'font': 'inter', 'date_format': 'd MMM yyyy',
    'vat_summary': 'multi_rate_only', 'footer_text': '', 'logo': None,
    'colors': {'accent': '1F3A5F', 'ink': '1B2430', 'muted': '5B6675'},
    'labels': {}, 'columns': dict(COLUMNS), 'payment': {'show_iban_bic': True, 'extra_note': ''},
}
TOP_LEVEL = {'pack', 'seller', *DEFAULTS}


class ProfileError(ValueError):
    """A format profile is malformed, unsafe or unsupported by its pack."""


class PackIntegrityError(RuntimeError):
    """An installed template pack differs from its code-reviewed pin."""


def canonical_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def format_date(value: date, pattern: str) -> str:
    """Locale-free; strftime('%b') would follow the process locale."""
    if pattern not in DATE_FORMATS:
        raise ProfileError('Unsupported date_format')
    month = MONTHS[value.month - 1]
    return {'d MMM yyyy': f'{value.day} {month[:3]} {value.year:04d}',
            'dd MMM yyyy': f'{value.day:02d} {month[:3]} {value.year:04d}',
            'd MMMM yyyy': f'{value.day} {month} {value.year:04d}',
            'dd/MM/yyyy': f'{value.day:02d}/{value.month:02d}/{value.year:04d}',
            'yyyy-MM-dd': value.isoformat()}[pattern]


# --- packs ---------------------------------------------------------------

def _pack_files(pack_id: str) -> dict[str, Path]:
    directory = PACK_ROOT / pack_id
    files = {path.name: path for path in sorted(directory.iterdir())
             if path.is_file() and not path.name.startswith('.')}
    if pack_id == LEGACY_PACK:
        files[LEGACY_TEMPLATE.name] = LEGACY_TEMPLATE
    return files


def pack_digest(pack_id: str) -> str:
    files = _pack_files(pack_id)
    return sha256_hex(canonical_bytes(sorted((name, sha256_hex(path.read_bytes())) for name, path in files.items())))


def load_pack(pack_id: str, *, verify: bool = True) -> dict:
    """Return a pinned pack; a drifted pack is refused, never silently re-keyed."""
    if not isinstance(pack_id, str) or pack_id not in PINNED_PACKS:
        raise ProfileError('Unknown template pack')
    pin = PINNED_PACKS[pack_id]
    directory = PACK_ROOT / pack_id
    try:
        manifest = tomllib.loads((directory / 'pack.toml').read_text(encoding='utf-8'))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PackIntegrityError(f'Template pack {pack_id} is not installed correctly') from exc
    template = LEGACY_TEMPLATE if pack_id == LEGACY_PACK else directory / 'invoice.tex.j2'
    if manifest.get('id') != pack_id or manifest.get('renderer_version') != pin['renderer_version']:
        raise PackIntegrityError(f'Template pack {pack_id} manifest does not match its pin')
    digest = pack_digest(pack_id)
    if verify:
        if 'template_sha256' in pin and sha256_hex(template.read_bytes()) != pin['template_sha256']:
            raise PackIntegrityError(f'Template pack {pack_id} template drifted from its pinned sha256')
        if digest != pin['pack_sha256']:
            raise PackIntegrityError(f'Template pack {pack_id} drifted from its pinned sha256')
    papers = manifest.get('papers', ['a4'])
    fields = manifest.get('profile_fields', [])
    if not set(papers) <= set(PAPERS) or not set(fields) <= TOP_LEVEL - {'pack'}:
        raise PackIntegrityError(f'Template pack {pack_id} declares unsupported options')
    return {'id': pack_id, 'renderer_version': pin['renderer_version'], 'sha256': digest,
            'template': template, 'papers': tuple(papers), 'fields': tuple(fields),
            'title': str(manifest.get('title', pack_id)), 'legacy': pack_id == LEGACY_PACK}


def list_packs() -> list[dict]:
    result = []
    for pack_id in sorted(PINNED_PACKS):
        try:
            pack = load_pack(pack_id)
            result.append({'id': pack_id, 'title': pack['title'], 'renderer_version': pack['renderer_version'],
                           'pack_sha256': pack['sha256'], 'papers': list(pack['papers']), 'status': 'OK'})
        except (PackIntegrityError, ProfileError) as exc:
            result.append({'id': pack_id, 'status': 'REFUSED', 'error': str(exc)})
    return result


# --- profiles ------------------------------------------------------------

def _text(value, field: str, maximum: int, *, required: bool = False, multiline: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ProfileError(f'{field} must be text of at most {maximum} characters')
    allowed = '\n' if multiline else ''
    if any((ord(char) < 32 and char not in allowed) or 0x7f <= ord(char) < 0xa0 for char in value):
        raise ProfileError(f'{field} contains unsupported control characters')
    if required and not value.strip():
        raise ProfileError(f'{field} is required')
    return value


def _hex(value, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'#?[0-9A-Fa-f]{6}', value):
        raise ProfileError(f'{field} must be a six-digit hex colour such as 1F3A5F')
    return value.lstrip('#').upper()


def _luminance(colour: str) -> float:
    channels = []
    for index in (0, 2, 4):
        c = int(colour[index:index + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast_with_white(colour: str) -> float:
    return 1.05 / (_luminance(colour) + 0.05)


def _table(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise ProfileError(f'{field} must be a table')
    return value


def _only(value: dict, allowed, field: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ProfileError(f'{field} has unknown keys: {", ".join(unknown)}')


def _seller(value) -> dict:
    seller = _table(value, 'seller')
    _only(seller, {*SELLER_FIELDS, *SELLER_OPTIONAL}, 'seller')
    result = {}
    for key, maximum in SELLER_FIELDS.items():
        # An empty VAT number is a non-registered seller; the billing adapter decides if that is allowed.
        result[key] = _text(seller.get(key, '' if key == 'vat_number' else None), f'seller.{key}', maximum,
                            required=key != 'vat_number', multiline=key == 'address')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', result['email']):
        raise ProfileError('seller.email must be an email address')
    for key, maximum in SELLER_OPTIONAL.items():
        text = _text(seller.get(key, ''), f'seller.{key}', maximum)
        if text:
            pattern = r'[A-Z]{2}[0-9]{2}[0-9A-Z ]{8,36}' if key == 'iban' else r'[A-Z0-9]{8}(?:[A-Z0-9]{3})?'
            if not re.fullmatch(pattern, text):
                raise ProfileError(f'seller.{key} is not a well-formed {key.upper()}')
            result[key] = text
    return result


def _logo(value, assets_dir: Path | None, frozen: bool) -> tuple[dict | None, Path | None]:
    if value is None or value == '':
        return None, None
    if frozen:
        # Already resolved inside a frozen document: only the hash is trusted.
        # A profile file may never name an object hash directly.
        _only(_table(value, 'logo'), {'name', 'sha256', 'bytes'}, 'logo')
        if (value.get('name') not in ('logo.png', 'logo.jpg', 'logo.pdf')
                or not isinstance(value.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', value['sha256'])
                or type(value.get('bytes')) is not int or not 0 < value['bytes'] <= MAX_LOGO_BYTES):
            raise ProfileError('logo must reference a verified asset')
        return dict(value), None
    if not isinstance(value, str) or len(value) > 200 or assets_dir is None:
        raise ProfileError('logo must be a relative path inside the profile assets directory')
    relative = Path(value)
    if relative.is_absolute() or '..' in relative.parts or value.startswith(('~', '/', '\\')) or '\\' in value:
        raise ProfileError('logo must be a relative path inside the profile assets directory')
    root = assets_dir.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ProfileError('logo resolves outside the profile assets directory')
    kind = LOGO_TYPES.get(path.suffix.lower())
    if kind is None:
        raise ProfileError('logo must be a .png, .jpg or .pdf file')
    if not path.is_file():
        raise ProfileError('logo file does not exist')
    size = path.stat().st_size
    if not 0 < size <= MAX_LOGO_BYTES:
        raise ProfileError(f'logo must be at most {MAX_LOGO_BYTES // 1024} KB')
    data = path.read_bytes()
    if len(data) > MAX_LOGO_BYTES:  # grew after stat; frozen validation would refuse it at render time
        raise ProfileError(f'logo must be at most {MAX_LOGO_BYTES // 1024} KB')
    if not data.startswith(kind[1]):
        raise ProfileError('logo content does not match its file type')
    return {'name': 'logo.' + kind[0], 'sha256': sha256_hex(data), 'bytes': len(data)}, path


def resolve_profile(raw, *, assets_dir: Path | None = None, frozen: bool = False,
                    verify_pack: bool = True) -> tuple[dict, Path | None]:
    """Validate a raw profile (from TOML) or a frozen one into canonical form.

    Resolving a frozen profile returns it unchanged, which lets the renderer
    re-validate exactly what a document froze. Only the logo differs: a raw
    profile names a file under assets_dir, a frozen one its verified hash.
    """
    data = _table(raw, 'profile')
    _only(data, TOP_LEVEL, 'profile')
    pack = load_pack(data.get('pack', DEFAULT_PACK), verify=verify_pack)
    fields = set(pack['fields'])
    unsupported = sorted(set(data) - fields - {'pack', 'description'})
    if unsupported:
        raise ProfileError(f'Pack {pack["id"]} does not support profile fields: {", ".join(unsupported)}')
    if 'seller' not in data:
        raise ProfileError('profile requires a [seller] table')
    result = {'pack': pack['id'], 'description': _text(data.get('description', ''), 'description', 200),
              'seller': _seller(data['seller'])}
    logo_path = None
    for key in sorted(fields - {'seller'}):
        value = data.get(key, DEFAULTS[key])
        if key == 'paper':
            if value not in pack['papers']:
                raise ProfileError(f'paper must be one of {", ".join(pack["papers"])} for pack {pack["id"]}')
        elif key == 'margins_mm':
            if type(value) is not int or not 12 <= value <= 25:
                raise ProfileError('margins_mm must be a whole number from 12 to 25')
        elif key == 'font':
            if value not in FONTS:
                raise ProfileError(f'font must be one of {", ".join(FONTS)}')
        elif key == 'date_format':
            if value not in DATE_FORMATS:
                raise ProfileError(f'date_format must be one of {", ".join(DATE_FORMATS)}')
        elif key == 'vat_summary':
            if value not in ('always', 'multi_rate_only'):
                raise ProfileError('vat_summary must be always or multi_rate_only')
        elif key == 'footer_text':
            value = _text(value, 'footer_text', 300)
        elif key == 'logo':
            value, logo_path = _logo(value, assets_dir, frozen)
        elif key == 'colors':
            colours = _table(value, 'colors')
            _only(colours, DEFAULTS['colors'], 'colors')
            value = {name: _hex(colours.get(name, default), f'colors.{name}')
                     for name, default in DEFAULTS['colors'].items()}
            for name, colour in value.items():
                if contrast_with_white(colour) < 4.5:
                    raise ProfileError(f'colors.{name} has contrast {contrast_with_white(colour):.2f}:1 against '
                                       'white; WCAG AA requires at least 4.5:1')
        elif key == 'labels':
            labels = _table(value, 'labels')
            _only(labels, LABELS, 'labels')
            value = {name: _text(text, f'labels.{name}', 80, required=True).strip() for name, text in sorted(labels.items())}
        elif key == 'columns':
            columns = _table(value, 'columns')
            _only(columns, COLUMNS, 'columns')
            value = {name: columns.get(name, default) for name, default in COLUMNS.items()}
            if any(type(flag) is not bool for flag in value.values()):
                raise ProfileError('columns values must be true or false')
            hidden = [name for name in REQUIRED_COLUMNS if not value[name]]
            if hidden:
                raise ProfileError('HMRC VAT Notice 700 section 16.3 requires these columns on a VAT invoice: '
                                   + ', '.join(hidden))
        elif key == 'payment':
            payment = _table(value, 'payment')
            _only(payment, DEFAULTS['payment'], 'payment')
            value = {'show_iban_bic': payment.get('show_iban_bic', True),
                     'extra_note': _text(payment.get('extra_note', ''), 'payment.extra_note', 300)}
            if type(value['show_iban_bic']) is not bool:
                raise ProfileError('payment.show_iban_bic must be true or false')
        result[key] = value
    return result, logo_path


def profile_path(profile_dir: Path, profile_id: str) -> Path:
    if not isinstance(profile_id, str) or not PROFILE_ID.fullmatch(profile_id):
        raise ProfileError('Profile id must be 1-64 lowercase letters, digits, - or _')
    return Path(profile_dir).resolve() / f'{profile_id}.toml'


def read_profile(profile_dir: Path, profile_id: str) -> dict:
    path = profile_path(profile_dir, profile_id)
    try:
        if path.stat().st_size > MAX_PROFILE_BYTES:
            raise ProfileError('Profile file is too large')
        return tomllib.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise ProfileError(f'Format profile {profile_id!r} was not found in {path.parent}') from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProfileError(f'Format profile {profile_id!r} is not valid TOML: {exc}') from None


def build_presentation(profile_dir: Path, profile_id: str) -> tuple[dict, dict[str, Path]]:
    """Resolve a profile into the presentation block that freezing embeds."""
    profile, logo_path = resolve_profile(read_profile(profile_dir, profile_id),
                                         assets_dir=Path(profile_dir) / 'assets')
    pack = load_pack(profile['pack'])
    logo = profile.get('logo')
    assets = {logo['name']: logo['sha256']} if logo else {}
    presentation = {'pack': pack['id'], 'renderer_version': pack['renderer_version'], 'pack_sha256': pack['sha256'],
                    'profile_id': profile_id, 'profile': profile,
                    'profile_sha256': sha256_hex(canonical_bytes(profile)), 'assets': assets}
    return presentation, ({logo['name']: logo_path} if logo else {})


def verify_presentation(document: dict) -> tuple[dict, dict]:
    """Re-validate a frozen presentation block against the installed packs."""
    presentation = document.get('presentation')
    if not isinstance(presentation, dict) or set(presentation) != {
            'pack', 'renderer_version', 'pack_sha256', 'profile_id', 'profile', 'profile_sha256', 'assets'}:
        raise ProfileError('presentation must contain exactly the frozen pack, profile and asset identities')
    pack = load_pack(presentation['pack'])
    if presentation['renderer_version'] != pack['renderer_version']:
        raise ProfileError('presentation renderer_version does not match the installed pack')
    if presentation['pack_sha256'] != pack['sha256']:
        raise PackIntegrityError('Frozen document names a different template pack hash; '
                                 'it will not be silently restyled')
    if not isinstance(presentation['profile_id'], str) or not PROFILE_ID.fullmatch(presentation['profile_id']):
        raise ProfileError('presentation.profile_id is not a valid profile id')
    profile, _ = resolve_profile(presentation['profile'], frozen=True)
    if profile != presentation['profile'] or profile['pack'] != pack['id']:
        raise ProfileError('presentation.profile is not a canonical resolved profile')
    if sha256_hex(canonical_bytes(profile)) != presentation['profile_sha256']:
        raise ProfileError('presentation.profile_sha256 does not match its profile')
    logo = profile.get('logo')
    if presentation['assets'] != ({logo['name']: logo['sha256']} if logo else {}):
        raise ProfileError('presentation.assets must match the profile assets exactly')
    if document.get('seller') != profile['seller']:
        raise ProfileError('Document seller must equal the frozen profile seller')
    return pack, profile


def list_profiles(profile_dir: Path) -> list[dict]:
    result = []
    root = Path(profile_dir)
    for path in sorted(root.glob('*.toml')) if root.is_dir() else []:
        entry = {'id': path.stem, 'path': str(path)}
        try:
            presentation, _ = build_presentation(root, path.stem)
            entry.update(status='VALID', pack=presentation['pack'], profile_sha256=presentation['profile_sha256'],
                         description=presentation['profile']['description'])
        except (ProfileError, PackIntegrityError) as exc:
            entry.update(status='INVALID', error=str(exc))
        result.append(entry)
    return result


# --- preview -------------------------------------------------------------

def sample_document(seller: dict, *, itemized: bool = False) -> dict:
    """A synthetic VAT invoice that exercises every block of a layout."""
    lines = [('Procurement administration - September 2026', '1200', '0.25'),
             ('Monthly reconciliation and account statement service', '1', '85.00'),
             ('Supplier onboarding checks (per supplier)', '14', '12.50')]
    rows, net_total, vat_total = [], Decimal('0.00'), Decimal('0.00')
    for description, quantity, price in lines:
        net = (Decimal(quantity) * Decimal(price)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        vat = (net * Decimal('0.20')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        rows.append({'description': description, 'quantity': quantity, 'unit_price': price, 'net_amount': str(net),
                     'vat_rate': '0.20', 'vat_amount': str(vat), 'gross_amount': str(net + vat)})
        net_total += net
        vat_total += vat
    document = {'invoice_number': 'DEMO-2026-00000042', 'invoice_date': '2026-10-01', 'tax_point': '2026-09-30',
                'due_date': '2026-10-31', 'period': '2026-09', 'currency': 'GBP', 'synthetic': True,
                'vat_enabled': True, 'seller': seller,
                'customer': {'id': 'DEMO-C001', 'legal_name': 'North & West Trading Ltd (Synthetic)',
                             'address': '12 Test Street\nManchester\nZZ2 2ZZ\nUnited Kingdom',
                             'email': 'accounts@north-west.example.test', 'vat_number': 'DEMO CUSTOMER VAT'},
                'lines': rows, 'net_total': str(net_total), 'vat_total': str(vat_total),
                'gross_total': str(net_total + vat_total),
                'payment_terms': '30 days from invoice date. Demonstration only - do not pay.',
                'reference': 'DEMO-PO-2026-0042', 'line_format': 'SUMMARY', 'movement_count': 3}
    if itemized:
        from .movement_vat import calculate_movement_vat
        amounts = ['0.10', '0.15', '0.20']
        net = sum(map(Decimal, amounts), Decimal('0.00'))
        vat = (net * Decimal('0.20')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        document.update(lines=[{'description': 'Monthly processing service package for 2026-09: 3 reconciled movements.',
                                'quantity': '1', 'unit_price': str(net), 'net_amount': str(net), 'vat_rate': '0.20',
                                'vat_amount': str(vat), 'gross_amount': str(net + vat)}],
                        net_total=str(net), vat_total=str(vat), gross_total=str(net + vat), line_format='ITEMIZED')
        row_vat, policy = calculate_movement_vat(amounts, invoice_net_total=net, invoice_vat_total=vat,
                                                 invoice_gross_total=net + vat, rate='0.20')
        document['movement_detail'] = [
            {'row_no': index, 'movement_id': f'movement-{index}', 'version_id': f'version-{index}',
             'external_id': f'SRC-MOV-{index:05d}', 'event_at': f'2026-09-0{index}T10:20:30+00:00',
             'description': f'Synthetic purchased service {index}', 'supplier_id': f'SUPPLIER-{index:03d}',
             'amount': amount, 'currency': 'GBP', **fields}
            for index, (amount, fields) in enumerate(zip(amounts, row_vat), 1)]
        document['movement_vat'] = policy
    return document


def render_preview(profile_dir: Path, profile_id: str, out_dir: Path, compiler: str = 'pdflatex',
                   *, itemized: bool = False, dpi: int = 110) -> dict:
    """Render a synthetic sample with a profile; nothing is stored in the database."""
    import shutil
    import subprocess
    from .documents import render_invoice
    from .storage import ObjectStore
    presentation, asset_paths = build_presentation(profile_dir, profile_id)
    out_dir = Path(out_dir).resolve()
    store = ObjectStore(out_dir / '.preview-objects')
    for name, path in asset_paths.items():
        if store.put_file(path) != presentation['assets'][name]:
            raise ProfileError(f'Asset {name} changed while it was being read')
    document = sample_document(presentation['profile']['seller'], itemized=itemized)
    document['presentation'] = presentation
    result = render_invoice(document, out_dir / '.preview-render', compiler, assets=store)
    stem = f'{profile_id}-preview'
    pdf = out_dir / f'{stem}.pdf'
    shutil.copyfile(result['pdf_path'], pdf)
    png = None
    if shutil.which('pdftoppm'):
        subprocess.run(['pdftoppm', '-png', '-r', str(dpi), '-f', '1', '-l', '1', '-singlefile',
                        str(pdf), str(out_dir / stem)], check=True, timeout=60)
        png = str(out_dir / f'{stem}.png')
    return {'profile_id': profile_id, 'pack': presentation['pack'], 'profile_sha256': presentation['profile_sha256'],
            'pdf_path': str(pdf), 'png_path': png, 'pdf_sha256': result['pdf_sha256'], 'synthetic': True}
