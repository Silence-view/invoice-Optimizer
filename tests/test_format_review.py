"""Review regressions for format profiles: optional seller keys, logo growth, CLI errors."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from pypdf import PdfReader

from invoice_optimizer import formats
from invoice_optimizer.documents import render_invoice
from invoice_optimizer.formats import ProfileError, resolve_profile
from test_documents import compiler, sample_document
from test_format_profiles import PROFILES, _png, _profile_dir, _raw, _with_presentation


def test_profile_without_iban_or_bic_renders(tmp_path, compiler):
    raw = _raw()
    del raw['seller']['iban'], raw['seller']['bic']
    document = _with_presentation(sample_document(), _profile_dir(tmp_path, raw), 'company')
    assert 'iban' not in document['seller'] and 'bic' not in document['seller']
    result = render_invoice(document, tmp_path / 'out', compiler)
    text = ''.join(page.extract_text() for page in PdfReader(result['pdf_path']).pages)
    assert 'IBAN' not in text and 'BIC' not in text and 'Amount due' in text
    # The render-only defaults never leak into the frozen, hashed document.
    with open(result['json_path'], 'rb') as stored:
        assert json.loads(stored.read()) == document


def test_logo_that_grows_past_the_limit_after_stat_is_refused(tmp_path, monkeypatch):
    assets = tmp_path / 'assets'
    assets.mkdir()
    (assets / 'logo.png').write_bytes(_png())
    real = type(assets).read_bytes
    monkeypatch.setattr(type(assets), 'read_bytes',
                        lambda self: real(self) + b'\0' * formats.MAX_LOGO_BYTES if self.name == 'logo.png' else real(self))
    raw = _raw()
    raw['logo'] = 'logo.png'
    with pytest.raises(ProfileError, match='at most'):
        resolve_profile(raw, assets_dir=assets)


def test_cli_preview_of_an_unrenderable_profile_exits_1_with_json(tmp_path):
    raw = _raw()
    raw['seller']['vat_number'] = ''  # valid profile, but the sample is a VAT invoice
    env = {**os.environ, 'INVOICE_DATA_DIR': str(tmp_path / 'data'),
           'INVOICE_PROFILE_DIR': str(_profile_dir(tmp_path, raw, 'novat'))}
    failed = subprocess.run([sys.executable, '-m', 'invoice_optimizer', 'format', 'preview', '--profile', 'novat',
                             '--out', str(tmp_path / 'preview')], env=env, capture_output=True, text=True, timeout=120)
    assert failed.returncode == 1 and 'Traceback' not in failed.stderr
    assert json.loads(failed.stdout)['valid'] is False
    assert PROFILES.is_dir()
