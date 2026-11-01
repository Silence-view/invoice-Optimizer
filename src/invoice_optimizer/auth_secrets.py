"""Private server signing key, independent of the user-visible setup token."""
import os
from pathlib import Path
import secrets
import stat
import tempfile


def session_signing_secret(settings):
    directory=Path(settings.data_dir)/'private'
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError('The private session-key directory must be a real directory')
    directory.chmod(0o700)
    destination=directory/'session-signing.key'
    if not destination.exists():
        # Publish a complete key with an exclusive hard link. A second process
        # either wins publication or reads the same already-complete key; it can
        # never observe a half-written file or replace another process's key.
        fd,name=tempfile.mkstemp(prefix='.session-key-',dir=directory)
        try:
            with os.fdopen(fd,'wb') as stream:
                os.fchmod(stream.fileno(),0o600)
                stream.write(secrets.token_bytes(64));stream.flush();os.fsync(stream.fileno())
            try:os.link(name,destination)
            except FileExistsError:pass
        finally:Path(name).unlink(missing_ok=True)
    fd=os.open(destination,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size!=64:
            raise RuntimeError('The private session-signing key must be a 64-byte regular file with mode 0600')
        with os.fdopen(fd,'rb',closefd=False) as stream:value=stream.read(65)
        if len(value)!=64:raise RuntimeError('The private session-signing key is invalid')
        return value
    finally:os.close(fd)
