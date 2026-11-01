from pathlib import Path
import hashlib
import os
import tempfile

class ObjectStore:
    """Content-addressed local volume; keys are content hashes, never user paths."""
    def __init__(self, root):
        self.root=Path(root).resolve(); self.root.mkdir(parents=True,exist_ok=True)

    def put_file(self, source):
        h=hashlib.sha256()
        with tempfile.NamedTemporaryFile(dir=self.root,delete=False) as dst:
            temporary=Path(dst.name)
            with open(source,'rb') as src:
                for chunk in iter(lambda:src.read(1024*1024),b''): h.update(chunk); dst.write(chunk)
            dst.flush(); os.fsync(dst.fileno())
        key=h.hexdigest(); target=self.root/key[:2]/key
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            temporary.unlink()
            if self.hash_file(target)!=key: raise ValueError('Stored object checksum mismatch')
        else:
            os.replace(temporary,target)
            target.chmod(0o440)
        return key

    def put_bytes(self, data):
        with tempfile.NamedTemporaryFile(dir=self.root,delete=False) as f:
            f.write(data); name=Path(f.name)
        try: return self.put_file(name)
        finally: name.unlink(missing_ok=True)

    def path(self,key,verify=False):
        if len(key)!=64 or any(c not in '0123456789abcdef' for c in key): raise ValueError('Invalid object key')
        p=self.root/key[:2]/key
        if not p.is_file(): raise FileNotFoundError(key)
        if verify and self.hash_file(p)!=key: raise ValueError('Object checksum mismatch')
        return p

    @staticmethod
    def hash_file(path):
        h=hashlib.sha256()
        with open(path,'rb') as f:
            for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
        return h.hexdigest()
