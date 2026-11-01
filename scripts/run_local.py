#!/usr/bin/env python3
"""Run both local application processes; terminate only our own children."""
import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--skip-postgres-start',action='store_true',help='Use an already running configured PostgreSQL service')
    args=parser.parse_args()
    os.chdir(ROOT)
    # Never stop an unrelated process to take its port.
    with socket.socket() as check:
        # A closed API may leave TCP connections in TIME_WAIT. Permit a clean
        # restart while still rejecting a port with an active listener.
        check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:check.bind(('127.0.0.1',args.port))
        except OSError:raise SystemExit(f'Port {args.port} is occupied. Use the existing console or choose --port.')
    subprocess.run([sys.executable,'scripts/bootstrap.py'],check=True)
    if not args.skip_postgres_start:subprocess.run(['bash','scripts/local_postgres.sh'],check=True)
    subprocess.run([sys.executable,'-m','invoice_optimizer','init-db'],check=True)
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env',override=False)
    # Local runs refresh data automatically: the worker polls var/inbox every
    # 30 s and, in synthetic mode, a demo feed adds new rows every 15 minutes.
    # Set either variable to 0 in .env to switch it off.
    os.environ.setdefault('INVOICE_INBOX_SECONDS','30')
    if os.environ.get('INVOICE_SYNTHETIC','1')=='1':os.environ.setdefault('INVOICE_DEMO_FEED_SECONDS','900')
    children=[];logs=[]
    def stop(signum,frame):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    try:
        for name,command in (
            ('api',['serve','--host','127.0.0.1','--port',str(args.port)]),
            ('worker',['worker']),
        ):
            log=(ROOT/'var'/f'{name}.log').open('a',buffering=1)
            logs.append(log)
            children.append(subprocess.Popen([sys.executable,'-m','invoice_optimizer',*command],stdout=log,stderr=subprocess.STDOUT,cwd=ROOT))
        for _ in range(100):
            if any(p.poll() is not None for p in children):raise RuntimeError('A service exited; inspect var/api.log and var/worker.log')
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/health',timeout=.5):break
            except OSError:time.sleep(.1)
        else:raise RuntimeError('API readiness timeout; inspect var/api.log')
        print(f'Console: http://127.0.0.1:{args.port}',flush=True)
        print('API and worker running. Sign in with a personal operator account; use the private .env setup token only to create the first account.',flush=True)
        provider=os.environ.get('INVOICE_PROVIDER','simulated')
        if provider=='simulated':print('Email: simulator only; no message leaves this computer.',flush=True)
        else:print(f'Email: {provider}, test mode. Every message is redirected to the connected Gmail account; invoices are sent only after human approval. Connect Gmail in the Email tab first.',flush=True)
        print('Automation: data inbox every '+os.environ['INVOICE_INBOX_SECONDS']+' s, demo feed every '+os.environ.get('INVOICE_DEMO_FEED_SECONDS','0')+' s, month-end drafts from 00:00 Europe/London on the 1st.',flush=True)
        print('Ctrl-C stops these two processes; PostgreSQL remains available.',flush=True)
        while True:
            for child in children:
                if child.poll() is not None:raise RuntimeError(f'Child {child.pid} exited; inspect var logs')
            time.sleep(1)
    except KeyboardInterrupt:print('Stopping local API and worker.',flush=True)
    finally:
        for child in children:
            if child.poll() is None:child.terminate()
        for child in children:
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        for log in logs:log.close()


if __name__=='__main__':main()
