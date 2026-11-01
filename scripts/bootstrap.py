#!/usr/bin/env python3
"""Create local secrets only when absent; never overwrite a user's environment."""
from pathlib import Path
from urllib.parse import quote
import os, secrets
root=Path(__file__).resolve().parents[1]
p=root/'.env'
if not p.exists():
    # One generated password serves compose (POSTGRES_PASSWORD), scripts/local_postgres.sh
    # (initdb) and the application DSN, so no shared default password exists anywhere.
    password=secrets.token_urlsafe(24)
    content=('DATABASE_URL=postgresql+psycopg://invoice:'+quote(password,safe='')+'@127.0.0.1:55432/invoice_optimizer\n'
        +'POSTGRES_PASSWORD='+password+'\n'
        +'INVOICE_API_TOKEN='+secrets.token_urlsafe(36)+'\n'
        +'INVOICE_SETUP_TOKEN='+secrets.token_urlsafe(36)+'\n'
        +'INVOICE_DATA_DIR=var\nINVOICE_SYNTHETIC=1\nINVOICE_PROVIDER=simulated\n'
        +'OMP_NUM_THREADS=1\nOPENBLAS_NUM_THREADS=1\nMKL_NUM_THREADS=1\n')
    # Owner-only from creation; O_EXCL never clobbers a concurrently created file.
    with os.fdopen(os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:f.write(content)
    print('Created .env with a generated console token and a one-time setup token. Open .env locally to sign in; do not share it.')
else:print('Existing .env preserved.')
