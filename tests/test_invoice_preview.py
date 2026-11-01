"""Real Poppler rendering of retained PDFs, plus bounded-cache/fault checks.

Tests reuse an existing repository PDF; they never author or regenerate invoices.
"""
from dataclasses import replace
from pathlib import Path
import hashlib
import json
import os
import struct
import sys
import subprocess

import pytest

from invoice_optimizer import invoice_preview as preview
from invoice_optimizer.config import Settings
from invoice_optimizer.storage import ObjectStore


@pytest.fixture
def retained(tmp_path):
    settings=replace(Settings(),data_dir=tmp_path/'data').prepare()
    original=Path(__file__).resolve().parents[1]/'output/pdf/synthetic-uk-vat-invoice.pdf'
    assert original.is_file(),'The retained repository PDF fixture is required'
    data=original.read_bytes()
    key=ObjectStore(settings.data_dir/'objects').put_bytes(data)
    return settings,key,data


def test_real_png_is_bounded_exact_source_unchanged_and_cache_needs_no_renderer(retained,monkeypatch):
    settings,key,data=retained
    info=preview.preview_info(settings,key)
    assert info=={'pdf_sha256':key,'page_count':1}
    path=preview.render_page(settings,key,1)
    content=path.read_bytes()
    assert content.startswith(b'\x89PNG\r\n\x1a\n') and len(content)<=preview.MAX_PNG_BYTES
    width,height=struct.unpack('>II',content[16:24])
    assert max(width,height)==1600
    assert ObjectStore(settings.data_dir/'objects').path(key).read_bytes()==data
    assert hashlib.sha256(data).hexdigest()==key
    monkeypatch.setattr(preview,'_run',lambda *args,**kwargs:pytest.fail('Cache hit invoked a renderer'))
    assert preview.preview_info(settings,key)==info
    assert preview.render_page(settings,key,1).read_bytes()==content
    with preview.open_page(settings,key,1) as handle:assert handle.read()==content
    assert list((settings.data_dir/'preview-tmp').iterdir())==[]


@pytest.mark.parametrize('key',['../outside','A'*64,'0'*63,None,'f'*65])
def test_invalid_keys_never_resolve_user_paths(retained,key):
    settings,_,_=retained
    with pytest.raises(ValueError,match='SHA-256'):preview.preview_info(settings,key)
    with pytest.raises(ValueError,match='SHA-256'):preview.render_page(settings,key,1)


@pytest.mark.parametrize('page',[0,-1,True,1.5,'1',2])
def test_invalid_pages_are_explicit(retained,page):
    settings,key,_=retained
    with pytest.raises(ValueError,match='page'):preview.render_page(settings,key,page)


def test_tampered_source_is_rejected_even_when_both_caches_exist(retained):
    settings,key,_=retained
    preview.render_page(settings,key,1)
    source=ObjectStore(settings.data_dir/'objects').path(key)
    source.chmod(0o600);source.write_bytes(b'changed retained object')
    with pytest.raises(preview.PreviewUnavailable,match='checksum'):preview.preview_info(settings,key)
    with pytest.raises(preview.PreviewUnavailable,match='checksum'):preview.render_page(settings,key,1)


def test_missing_object_and_oversized_pdf_are_explicit_and_do_not_render(retained,monkeypatch):
    settings,key,_=retained
    with pytest.raises(preview.PreviewUnavailable,match='unavailable'):preview.preview_info(settings,'0'*64)
    monkeypatch.setattr(preview,'MAX_PDF_BYTES',10)
    monkeypatch.setattr(preview,'_run',lambda *args:pytest.fail('Oversized source reached renderer'))
    with pytest.raises(preview.PreviewUnavailable,match='32 MiB'):preview.render_page(settings,key,1)


def test_timeout_releases_slot_and_removes_temporary_files(retained,monkeypatch):
    settings,key,_=retained
    real_timeout=preview.TIMEOUT_SECONDS
    monkeypatch.setattr(preview,'TIMEOUT_SECONDS',0.000001)
    with pytest.raises(preview.PreviewUnavailable,match='time limit'):preview.preview_info(settings,key)
    assert list((settings.data_dir/'preview-tmp').iterdir())==[]
    monkeypatch.setattr(preview,'TIMEOUT_SECONDS',real_timeout)
    assert preview.preview_info(settings,key)['page_count']==1


def test_poppler_commands_are_shell_free_bounded_and_use_C_locale(retained,monkeypatch):
    settings,key,_=retained
    original=preview.subprocess.Popen;calls=[]
    def recording(args,**kwargs):
        calls.append((args,kwargs))
        return original(args,**kwargs)
    monkeypatch.setattr(preview.subprocess,'Popen',recording)
    preview.render_page(settings,key,1)
    assert len(calls)==2
    for args,kwargs in calls:
        assert isinstance(args,list) and kwargs.get('shell',False) is False
        assert kwargs['env']['LC_ALL']==kwargs['env']['LANG']=='C'
    assert preview.TIMEOUT_SECONDS==12
    assert '-singlefile' in calls[1][0]
    assert calls[1][0][calls[1][0].index('-scale-to')+1]=='1600'


def test_missing_renderer_reports_unavailable(retained,monkeypatch):
    settings,key,_=retained
    def missing(name):raise preview.PreviewUnavailable('Missing Poppler '+name)
    monkeypatch.setattr(preview,'_tool',missing)
    with pytest.raises(preview.PreviewUnavailable,match='Missing Poppler'):preview.preview_info(settings,key)
    assert list((settings.data_dir/'preview-tmp').iterdir())==[]


def test_nonblocking_renderer_backpressure_does_not_wait(retained):
    settings,key,_=retained
    assert preview._SLOTS.acquire(blocking=False)
    assert preview._SLOTS.acquire(blocking=False)
    try:
        with pytest.raises(preview.PreviewBusy,match='occupied'):preview.preview_info(settings,key)
    finally:
        preview._SLOTS.release();preview._SLOTS.release()
    assert preview.preview_info(settings,key)['page_count']==1


def test_bad_render_output_and_nonzero_exit_are_explicit(retained,monkeypatch):
    settings,key,_=retained
    preview.preview_info(settings,key)
    real=preview.subprocess.run
    monkeypatch.setattr(preview.subprocess,'run',lambda args,**kwargs:subprocess.CompletedProcess(args,2))
    with pytest.raises(preview.PreviewUnavailable,match='processed'):preview.render_page(settings,key,1)
    monkeypatch.setattr(preview.subprocess,'run',lambda args,**kwargs:subprocess.CompletedProcess(args,0))
    with pytest.raises(preview.PreviewUnavailable,match='valid bounded PNG'):preview.render_page(settings,key,1)
    assert list((settings.data_dir/'preview-tmp').iterdir())==[]
    monkeypatch.setattr(preview.subprocess,'run',real)
    assert preview.render_page(settings,key,1).is_file()


def test_corrupt_cache_png_is_rebuilt_and_metadata_is_revalidated(retained):
    settings,key,_=retained
    path=preview.render_page(settings,key,1);original=path.read_bytes()
    path.chmod(0o600);path.write_bytes(original[:-8]+b'tampered')
    assert preview.render_page(settings,key,1).read_bytes()==original
    metadata=settings.data_dir/'preview-cache'/(key+'.json')
    metadata.chmod(0o600);metadata.write_text('invalid metadata')
    assert preview.preview_info(settings,key)['page_count']==1


def test_eviction_obeys_file_and_byte_budgets_and_open_handle_survives(retained,monkeypatch):
    settings,key,_=retained
    handle=preview.open_page(settings,key,1)
    original=handle.read();handle.seek(0)
    root=settings.data_dir/'preview-cache'
    monkeypatch.setattr(preview,'MAX_CACHE_FILES',3)
    monkeypatch.setattr(preview,'MAX_CACHE_BYTES',1024)
    for index in range(5):
        staged=settings.data_dir/f'staged-{index}'
        staged.write_bytes(b'x'*700)
        preview._publish(root,staged,f'{index:064x}.json')
    files=[path for path in root.iterdir() if path.is_file()]
    assert len(files)<=3 and sum(path.stat().st_size for path in files)<=1024
    assert not (root/f'{key}-p1.png').exists()
    try:assert handle.read()==original
    finally:handle.close()


def test_open_page_retries_eviction_between_path_and_pin(retained,monkeypatch):
    settings,key,_=retained
    real=preview.render_page;calls=[]
    def evict_once(*args):
        path=real(*args);calls.append(path)
        if len(calls)==1:path.unlink()
        return path
    monkeypatch.setattr(preview,'render_page',evict_once)
    with preview.open_page(settings,key,1) as handle:assert handle.read(8)==preview._PNG_SIGNATURE
    assert len(calls)==2


def test_metadata_pipe_rejects_excess_before_growing_a_large_file(tmp_path):
    output=tmp_path/'metadata.txt'
    with output.open('w+b') as handle:
        with pytest.raises(preview.PreviewUnavailable,match='metadata exceeds'):
            preview._run([sys.executable,'-c',"import sys; sys.stdout.write('x'*1000000)"],handle)
    assert output.stat().st_size<=65536
