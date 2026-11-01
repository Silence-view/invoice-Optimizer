#!/usr/bin/env python3
"""One explicitly requested Gmail API test; no impersonation or automatic retry.

The credential stays in memory. A durable pre-send receipt prevents a second
submission after an ambiguous network failure or process interruption.
"""
import argparse
import base64
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
import json
import os
from pathlib import Path
import subprocess
import time
import httpx


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--credential',type=Path,required=True)
    parser.add_argument('--recipient',required=True)
    parser.add_argument('--receipt',type=Path,required=True)
    args=parser.parse_args()
    if any(c in args.recipient for c in '\r\n ,;<>') or args.recipient.count('@')!=1:
        raise SystemExit('Supply one plain recipient address')
    credential=json.loads(args.credential.read_text())
    if credential.get('type')!='service_account':raise SystemExit('This diagnostic accepts a service-account credential only')
    identity=credential['client_email']
    stamp=int(time.time())
    header={'alg':'RS256','typ':'JWT','kid':credential['private_key_id']}
    claims={'iss':identity,'scope':'https://www.googleapis.com/auth/gmail.send',
            'aud':'https://oauth2.googleapis.com/token','iat':stamp,'exp':stamp+300}
    signing_input=(b64(json.dumps(header,separators=(',',':')).encode())+'.'+b64(json.dumps(claims,separators=(',',':')).encode())).encode()
    read_fd,write_fd=os.pipe()
    try:
        os.write(write_fd,credential['private_key'].encode());os.close(write_fd);write_fd=None
        signed=subprocess.run(['/usr/bin/openssl','dgst','-sha256','-sign',f'/dev/fd/{read_fd}'],
            input=signing_input,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,pass_fds=(read_fd,),check=True)
    finally:
        os.close(read_fd)
        if write_fd is not None:os.close(write_fd)
    assertion=signing_input.decode()+'.'+b64(signed.stdout)
    record={'checked_at':datetime.now(timezone.utc).isoformat(),'recipient':args.recipient,
            'authentication':'service_account_without_delegation','subject':'Invoice Optimizer — Gmail API test',
            'submission_attempted':False,'accepted_by_gmail':False,'delivery':'UNKNOWN'}
    args.receipt.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive creation makes a rerun refuse rather than duplicate a test email.
    with args.receipt.open('x') as f:json.dump({**record,'status':'AUTHENTICATING'},f,indent=2)
    def save():
        args.receipt.write_text(json.dumps(record,indent=2)+'\n')
        print(json.dumps(record,indent=2))
    secrets_to_remove=[assertion,credential['private_key']]
    try:
        with httpx.Client(timeout=httpx.Timeout(25,connect=10),follow_redirects=False,trust_env=False) as client:
            token_response=client.post('https://oauth2.googleapis.com/token',data={
                'grant_type':'urn:ietf:params:oauth:grant-type:jwt-bearer','assertion':assertion})
            record['token_http_status']=token_response.status_code
            if token_response.status_code!=200:
                record['status']='AUTHENTICATION_REJECTED'
                record['google_error']=str(token_response.json().get('error','unknown'))[:100]
                save();return
            access_token=token_response.json()['access_token'];secrets_to_remove.append(access_token)
            message=EmailMessage()
            message['From']=identity;message['To']=args.recipient;message['Subject']=record['subject']
            message['Date']=format_datetime(datetime.now(timezone.utc))
            message['Message-ID']=make_msgid(domain='invoice-optimizer.local')
            message.set_content('This is a test email from Invoice Optimizer to verify Gmail API submission.\n\nNo invoice or payment request is attached.\n')
            record.update(status='SUBMITTING',submission_attempted=True)
            with args.receipt.open('w') as f:
                json.dump(record,f,indent=2);f.flush();os.fsync(f.fileno())
            response=client.post('https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
                headers={'Authorization':'Bearer '+access_token},json={'raw':b64(message.as_bytes())})
            record['send_http_status']=response.status_code
            if response.status_code==200:
                record.update(status='ACCEPTED',accepted_by_gmail=True,gmail_message_id=response.json().get('id'))
            else:
                error=response.json().get('error',{})
                description=str(error.get('message',''))
                for value in secrets_to_remove:description=description.replace(value,'[redacted]')
                record.update(status='REJECTED' if 400<=response.status_code<500 else 'UNKNOWN',
                    google_status=error.get('status'),google_message=description[:600],
                    google_reasons=[x.get('reason') for x in error.get('errors',[]) if isinstance(x,dict)])
    except Exception as exc:
        # Never expose request headers, JWTs, OAuth responses or credential data.
        record.update(status='UNKNOWN' if record['submission_attempted'] else 'AUTHENTICATION_UNAVAILABLE',
                      error_type=type(exc).__name__)
    save()


if __name__=='__main__':main()
