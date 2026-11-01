"""Bounded image previews of exact retained PDFs; never generates invoices.

Poppler concurrency is capped at two operations per Python process. Cache
publication/eviction uses a cross-process file lock. Open descriptors pin image
bytes while another request evicts their cache directory entries.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import struct
import subprocess
import tempfile
import time
from threading import BoundedSemaphore
import zlib

from .storage import ObjectStore

MAX_PDF_BYTES = 32 * 1024 * 1024
MAX_PNG_BYTES = 16 * 1024 * 1024
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_CACHE_FILES = 64
MAX_DIMENSION = 1600
TIMEOUT_SECONDS = 12
_SLOTS = BoundedSemaphore(2)
_NAME = re.compile(r'[a-f0-9]{64}(?:\.json|-p[1-9][0-9]*\.png)\Z')
_PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


class PreviewUnavailable(RuntimeError):
    """The exact retained document cannot currently be previewed."""


class PreviewBusy(PreviewUnavailable):
    """A bounded renderer/cache resource is occupied; retry later."""


def _source(settings, key):
    if not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{64}', key):
        raise ValueError('Invalid PDF SHA-256')
    store = ObjectStore(settings.data_dir / 'objects')
    try:
        path = store.path(key)
        if not 0 < path.stat().st_size <= MAX_PDF_BYTES:
            raise PreviewUnavailable('PDF preview supports retained files up to 32 MiB')
        checksum = hashlib.sha256()
        total = 0
        with path.open('rb') as handle:
            while chunk := handle.read(65536):
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    raise PreviewUnavailable('PDF preview supports retained files up to 32 MiB')
                checksum.update(chunk)
        if checksum.hexdigest() != key:
            raise PreviewUnavailable('Retained PDF checksum verification failed')
        return path
    except OSError as exc:
        raise PreviewUnavailable('The retained PDF is unavailable') from exc


def _root(settings):
    root = settings.data_dir / 'preview-cache'
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def _cache_guard(root):
    import fcntl
    with (root / '.cache.lock').open('a+b') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PreviewBusy('Preview cache is busy; retry shortly') from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _slot():
    if not _SLOTS.acquire(blocking=False):
        raise PreviewBusy('Both preview renderers are occupied; retry shortly')
    try:
        yield
    finally:
        _SLOTS.release()


def _tool(name):
    candidate = shutil.which(name)
    if candidate:
        return candidate
    for directory in ('/opt/homebrew/bin', '/usr/local/bin', '/usr/bin'):
        path = Path(directory) / name
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise PreviewUnavailable(f'PDF preview requires the Poppler {name} executable')


def _run(args, stdout):
    environment = {**os.environ, 'LANG': 'C', 'LC_ALL': 'C'}
    if stdout != subprocess.DEVNULL:
        # Metadata can contain arbitrarily long strings. Drain a bounded pipe
        # instead of buffering all output or allowing an unbounded temp file.
        process = None
        try:
            deadline = time.monotonic() + TIMEOUT_SECONDS
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=environment)
            count = 0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise PreviewUnavailable('PDF preview renderer exceeded its time limit')
                    data = os.read(process.stdout.fileno(), 65536)
                    if not data:
                        break
                    count += len(data)
                    if count > 65536:
                        raise PreviewUnavailable('PDF metadata exceeds the preview parsing limit')
                    stdout.write(data)
            result = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            if result != 0:
                raise PreviewUnavailable('The retained PDF could not be processed by the preview renderer')
        except subprocess.TimeoutExpired as exc:
            raise PreviewUnavailable('PDF preview renderer exceeded its time limit') from exc
        except OSError as exc:
            raise PreviewUnavailable('PDF preview renderer could not be started') from exc
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdout is not None:
                    process.stdout.close()
        return
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=stdout,
                                stderr=subprocess.DEVNULL, timeout=TIMEOUT_SECONDS,
                                check=False, env=environment)
    except subprocess.TimeoutExpired as exc:
        raise PreviewUnavailable('PDF preview renderer exceeded its time limit') from exc
    except OSError as exc:
        raise PreviewUnavailable('PDF preview renderer could not be started') from exc
    if result.returncode != 0:
        raise PreviewUnavailable('The retained PDF could not be processed by the preview renderer')


def _metadata(root, key):
    path = root / (key + '.json')
    try:
        with path.open('rb') as handle:
            data = handle.read(1025)
        if len(data) > 1024:
            return None
        value = json.loads(data)
        if (value.get('pdf_sha256') == key and type(value.get('page_count')) is int
                and 1 <= value['page_count'] <= 2147483647):
            os.utime(path, None)
            return {'pdf_sha256': key, 'page_count': value['page_count']}
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _valid_png(path):
    """Validate bounded PNG structure/CRCs without allocating image pixels."""
    try:
        with path.open('rb') as handle:
            size = os.fstat(handle.fileno()).st_size
            if not 0 < size <= MAX_PNG_BYTES:
                return False
            if handle.read(8) != _PNG_SIGNATURE:
                return False
            first = True
            while True:
                header = handle.read(8)
                if len(header) != 8:
                    return False
                length, kind = struct.unpack('>I4s', header)
                if length > MAX_PNG_BYTES or handle.tell() + length + 4 > size:
                    return False
                checksum = zlib.crc32(kind)
                remaining = length
                if first:
                    if kind != b'IHDR' or length != 13:
                        return False
                    data = handle.read(13)
                    if len(data) != 13:
                        return False
                    width, height = struct.unpack('>II', data[:8])
                    if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION):
                        return False
                    checksum = zlib.crc32(data, checksum)
                    remaining = 0
                    first = False
                while remaining:
                    data = handle.read(min(65536, remaining))
                    if not data:
                        return False
                    checksum = zlib.crc32(data, checksum)
                    remaining -= len(data)
                crc = handle.read(4)
                if len(crc) != 4 or struct.unpack('>I', crc)[0] != checksum & 0xffffffff:
                    return False
                if kind == b'IEND':
                    return length == 0 and handle.read(1) == b''
    except (OSError, ValueError, struct.error):
        return False


def _publish(root, staged, name):
    size = staged.stat().st_size
    if size > MAX_CACHE_BYTES:
        raise PreviewUnavailable('Preview exceeds the disk cache size limit')
    with _cache_guard(root):
        records = []
        for index, path in enumerate(root.iterdir()):
            if index >= 4096:
                raise PreviewUnavailable('Preview cache inventory exceeds its safety limit')
            if _NAME.fullmatch(path.name) and path.name != name:
                try:
                    stat = path.stat()
                    records.append((stat.st_mtime_ns, path, stat.st_size))
                except FileNotFoundError:
                    pass
        records.sort(key=lambda row: row[0])
        total = sum(row[2] for row in records) + size
        # Include the permanent lock file in the 64-file cache budget.
        while records and (len(records) + 2 > MAX_CACHE_FILES or total > MAX_CACHE_BYTES):
            _, oldest, old_size = records.pop(0)
            oldest.unlink(missing_ok=True)
            total -= old_size
        if len(records) + 2 > MAX_CACHE_FILES or total > MAX_CACHE_BYTES:
            raise PreviewUnavailable('Preview cannot fit inside the bounded disk cache')
        target = root / name
        os.replace(staged, target)
        target.chmod(0o440)
        return target


def _info(source, root, key, temporary):
    cached = _metadata(root, key)
    if cached:
        return cached
    with (temporary / 'pdfinfo.txt').open('w+b') as output:
        _run([_tool('pdfinfo'), str(source)], output)
        output.seek(0)
        data = output.read(65537)
    if len(data) > 65536:
        raise PreviewUnavailable('PDF metadata exceeds the preview parsing limit')
    match = re.search(rb'^Pages:\s*([0-9]{1,10})\s*$', data, re.MULTILINE)
    if not match or not 1 <= int(match.group(1)) <= 2147483647:
        raise PreviewUnavailable('The PDF page count could not be determined')
    result = {'pdf_sha256': key, 'page_count': int(match.group(1))}
    staged = temporary / 'metadata.json'
    staged.write_text(json.dumps(result), encoding='ascii')
    _publish(root, staged, key + '.json')
    return result


def _temporary(settings):
    directory = settings.data_dir / 'preview-tmp'
    directory.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix='render-', dir=directory)


def preview_info(settings, pdf_sha256):
    source = _source(settings, pdf_sha256)
    root = _root(settings)
    cached = _metadata(root, pdf_sha256)
    if cached:
        return cached
    with _slot(), _temporary(settings) as directory:
        return _info(source, root, pdf_sha256, Path(directory))


def render_page(settings, pdf_sha256, page):
    if type(page) is not int or page < 1:
        raise ValueError('Preview page must be a positive integer')
    source = _source(settings, pdf_sha256)
    root = _root(settings)
    target = root / f'{pdf_sha256}-p{page}.png'
    cached = _metadata(root, pdf_sha256)
    if cached and page > cached['page_count']:
        raise ValueError('Preview page is outside the retained PDF')
    if cached and _valid_png(target):
        try:
            os.utime(target, None)
            return target
        except FileNotFoundError:
            pass
    with _slot(), _temporary(settings) as directory:
        temporary = Path(directory)
        info = _info(source, root, pdf_sha256, temporary)
        if page > info['page_count']:
            raise ValueError('Preview page is outside the retained PDF')
        prefix = temporary / 'page'
        _run([_tool('pdftoppm'), '-f', str(page), '-l', str(page), '-singlefile',
              '-scale-to', str(MAX_DIMENSION), '-png', str(source), str(prefix)], subprocess.DEVNULL)
        staged = prefix.with_suffix('.png')
        if not _valid_png(staged):
            raise PreviewUnavailable('The preview renderer did not produce a valid bounded PNG')
        # The object is immutable by contract; reject concurrent disk corruption.
        _source(settings, pdf_sha256)
        return _publish(root, staged, target.name)


def open_page(settings, pdf_sha256, page):
    """Return an open binary handle; callers must close it after streaming.

    Opening under the cross-process eviction lock pins the file descriptor.
    A directory-entry eviction between rendering and locking gets one retry.
    """
    for attempt in range(2):
        path = render_page(settings, pdf_sha256, page)
        with _cache_guard(path.parent):
            try:
                return path.open('rb')
            except FileNotFoundError:
                if attempt:
                    raise PreviewBusy('Preview was evicted during access; retry shortly')
    raise PreviewBusy('Preview cache is busy; retry shortly')
