from dataclasses import dataclass, field
from pathlib import Path
import os
from urllib.parse import urlsplit
from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    # No password default: DATABASE_URL (scripts/bootstrap.py), PGPASSWORD or ~/.pgpass supplies it.
    database_url: str = 'postgresql+psycopg://invoice@127.0.0.1:55432/invoice_optimizer'
    data_dir: Path = field(default_factory=lambda: Path('var').resolve())
    api_token: str = ''
    # One-time first-administrator secret; the shared token stands in only when unset.
    setup_token: str = ''
    actor: str = 'local-reviewer'
    batch_size: int = 1000
    worker_batch_size: int = 500
    session_secure: bool = False
    synthetic: bool = True
    compiler: str = 'pdflatex'
    # Format profile for newly frozen drafts; frozen documents keep their own.
    format_profile: str = 'default'
    profile_dir: Path = field(default_factory=lambda: Path('config/invoice_profiles').resolve())
    provider: str = 'simulated'
    smtp_host: str = ''
    smtp_port: int = 587
    smtp_user: str = ''
    smtp_password: str = ''
    allowed_email_domains: tuple[str, ...] = ('example.test',)
    poll_seconds: float = 2.0
    auto_close: bool = True
    auto_dispatch: bool = True
    public_base_url: str = 'http://127.0.0.1:8765'
    gmail_expected_email: str = ''
    # Test-recipient redirect for real providers: 'connected' (the connected
    # Gmail account), one explicit address, or 'off' (refused in synthetic mode).
    mail_redirect: str = 'connected'
    # Outbox kinds a real provider submits; other kinds stay on the simulator.
    mail_send_kinds: tuple[str, ...] = ('INVOICE', 'DIGEST', 'TEST')
    operator_email: str = 'operations@example.test'
    approval_digest: bool = True
    # Automatic data refresh: inbox polling (0 disables) and the synthetic feed.
    inbox_seconds: int = 30
    inbox_max_files: int = 4
    demo_feed_seconds: int = 0
    demo_feed_rows: int = 300
    # Per process (api, worker and cli run with their own environment).
    database_read_url: str = ''
    db_pool_size: int = 4
    db_max_overflow: int = 2
    db_pool_recycle_seconds: int = 1800
    db_statement_timeout_ms: int = 0
    db_lock_timeout_ms: int = 0
    db_idle_tx_timeout_ms: int = 0

    @classmethod
    def from_env(cls):
        load_dotenv(override=False)
        return cls(
            database_url=os.getenv('DATABASE_URL', cls.database_url),
            data_dir=Path(os.getenv('INVOICE_DATA_DIR','var')).resolve(),
            api_token=os.getenv('INVOICE_API_TOKEN',''),
            setup_token=os.getenv('INVOICE_SETUP_TOKEN','').strip(),
            actor=os.getenv('INVOICE_ACTOR','local-reviewer'),
            batch_size=int(os.getenv('INVOICE_BATCH_SIZE','1000')),
            worker_batch_size=int(os.getenv('INVOICE_WORKER_BATCH_SIZE','500')),
            session_secure=os.getenv('INVOICE_SECURE_COOKIES','0')=='1',
            synthetic=os.getenv('INVOICE_SYNTHETIC','1')=='1',
            compiler=os.getenv('INVOICE_TEX_COMPILER','pdflatex'),
            format_profile=os.getenv('INVOICE_FORMAT_PROFILE','default').strip(),
            profile_dir=Path(os.getenv('INVOICE_PROFILE_DIR','config/invoice_profiles')).resolve(),
            provider=os.getenv('INVOICE_PROVIDER','simulated'),
            smtp_host=os.getenv('SMTP_HOST',''), smtp_port=int(os.getenv('SMTP_PORT','587')),
            smtp_user=os.getenv('SMTP_USER',''),smtp_password=os.getenv('SMTP_PASSWORD',''),
            allowed_email_domains=tuple(x.strip() for x in os.getenv('INVOICE_ALLOWED_EMAIL_DOMAINS','example.test').split(',') if x.strip()),
            poll_seconds=float(os.getenv('INVOICE_POLL_SECONDS','2')),
            auto_close=os.getenv('INVOICE_AUTO_CLOSE','1')=='1',
            auto_dispatch=os.getenv('INVOICE_AUTO_DISPATCH','1')=='1',
            public_base_url=os.getenv('INVOICE_PUBLIC_BASE_URL','http://127.0.0.1:8765').rstrip('/'),
            gmail_expected_email=os.getenv('INVOICE_GMAIL_EXPECTED_EMAIL','').strip().lower(),
            mail_redirect=os.getenv('INVOICE_MAIL_REDIRECT','').strip().lower() or ('connected' if os.getenv('INVOICE_SYNTHETIC','1')=='1' else 'off'),
            mail_send_kinds=tuple(x.strip().upper() for x in os.getenv('INVOICE_MAIL_SEND_KINDS','INVOICE,DIGEST,TEST').split(',') if x.strip()),
            operator_email=os.getenv('INVOICE_OPERATOR_EMAIL','operations@example.test').strip().lower(),
            approval_digest=os.getenv('INVOICE_APPROVAL_DIGEST','1')=='1',
            inbox_seconds=int(os.getenv('INVOICE_INBOX_SECONDS','30')),
            inbox_max_files=int(os.getenv('INVOICE_INBOX_MAX_FILES','4')),
            demo_feed_seconds=int(os.getenv('INVOICE_DEMO_FEED_SECONDS','0')),
            demo_feed_rows=int(os.getenv('INVOICE_DEMO_FEED_ROWS','300')),
            database_read_url=os.getenv('DATABASE_READ_URL','').strip(),
            db_pool_size=int(os.getenv('INVOICE_DB_POOL_SIZE','4')),
            db_max_overflow=int(os.getenv('INVOICE_DB_MAX_OVERFLOW','2')),
            db_pool_recycle_seconds=int(os.getenv('INVOICE_DB_POOL_RECYCLE_SECONDS','1800')),
            db_statement_timeout_ms=int(os.getenv('INVOICE_DB_STATEMENT_TIMEOUT_MS','0')),
            db_lock_timeout_ms=int(os.getenv('INVOICE_DB_LOCK_TIMEOUT_MS','0')),
            db_idle_tx_timeout_ms=int(os.getenv('INVOICE_DB_IDLE_TX_TIMEOUT_MS','0')),
        )

    @property
    def model_dir(self): return self.data_dir / 'model'
    @property
    def mode(self): return 'SIMULATION' if self.synthetic else 'LIVE'
    @property
    def https(self): return urlsplit(self.public_base_url).scheme=='https'
    @property
    def cookie_secure(self):
        # One rule for every cookie: an HTTPS public origin always means Secure.
        return self.https or self.session_secure
    @property
    def bootstrap_secret(self): return self.setup_token or self.api_token

    def _check_mail_and_refresh(self):
        import re
        address=r'[^\s@]+@[^\s@]+\.[^\s@]+'
        if not re.fullmatch(address,self.operator_email):raise ValueError('INVOICE_OPERATOR_EMAIL must be one email address')
        if self.mail_redirect not in ('connected','off'):
            if not re.fullmatch(address,self.mail_redirect):
                raise ValueError("INVOICE_MAIL_REDIRECT must be 'connected', 'off' or one email address")
            domain=self.mail_redirect.rsplit('@',1)[1]
            # A test redirect may target only the owner's own expected account
            # or an explicitly allow-listed domain, never an arbitrary inbox.
            if self.mail_redirect!=self.gmail_expected_email and domain not in self.allowed_email_domains:
                raise ValueError('INVOICE_MAIL_REDIRECT must be the expected Gmail address or in INVOICE_ALLOWED_EMAIL_DOMAINS')
        if self.provider!='simulated' and self.synthetic and self.mail_redirect=='off':
            raise ValueError('Synthetic mode never sends real email without a test redirect; set INVOICE_MAIL_REDIRECT=connected')
        if not self.mail_send_kinds or set(self.mail_send_kinds)-{'INVOICE','DIGEST','TEST','ALERT'}:
            raise ValueError('INVOICE_MAIL_SEND_KINDS accepts INVOICE, DIGEST, TEST and ALERT')
        if not (self.inbox_seconds==0 or 5<=self.inbox_seconds<=3600):raise ValueError('INVOICE_INBOX_SECONDS must be 0 or 5..3600')
        if not 1<=self.inbox_max_files<=50:raise ValueError('INVOICE_INBOX_MAX_FILES must be 1..50')
        if not (self.demo_feed_seconds==0 or 60<=self.demo_feed_seconds<=86400):raise ValueError('INVOICE_DEMO_FEED_SECONDS must be 0 or 60..86400')
        if self.demo_feed_seconds and not self.inbox_seconds:raise ValueError('INVOICE_DEMO_FEED_SECONDS requires INVOICE_INBOX_SECONDS > 0')
        if self.demo_feed_seconds and not self.synthetic:raise ValueError('The synthetic demo feed requires INVOICE_SYNTHETIC=1')
        if not 8<=self.demo_feed_rows<=20000:raise ValueError('INVOICE_DEMO_FEED_ROWS must be 8..20000')

    def prepare(self):
        for name in ('objects','documents','mail','exports','reports','incoming','model','inbox'):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)
        if not 1 <= self.batch_size <= 10000: raise ValueError('Batch size must be 1..10000')
        if type(self.worker_batch_size) is not int or not 1 <= self.worker_batch_size <= 10000:
            raise ValueError('Worker batch size must be 1..10000')
        if not 0.1 <= self.poll_seconds <= 60:
            raise ValueError('Worker polling interval must be 0.1..60 seconds')
        if not self.synthetic and not self.https:
            raise ValueError('Non-synthetic mode requires an HTTPS INVOICE_PUBLIC_BASE_URL (Secure cookies, HSTS)')
        if not self.synthetic: raise ValueError('This release implements the synthetic source and tax contract only. Production activation requires a reviewed source/tax adapter.')
        if self.setup_token and (len(self.setup_token)<32 or self.setup_token==self.api_token):
            raise ValueError('INVOICE_SETUP_TOKEN must be a separate generated secret of at least 32 characters')
        if self.provider not in ('simulated','smtp','gmail'): raise ValueError('Unknown email provider')
        self._check_mail_and_refresh()
        from .formats import PROFILE_ID
        if not PROFILE_ID.fullmatch(self.format_profile):
            raise ValueError('INVOICE_FORMAT_PROFILE must be 1-64 lowercase letters, digits, - or _')
        if self.provider == 'smtp' and not self.smtp_host: raise ValueError('SMTP_HOST is required')
        parsed=urlsplit(self.public_base_url)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('','/') or parsed.scheme not in ('http','https')
                or (parsed.scheme=='http' and parsed.hostname not in ('127.0.0.1','localhost','::1'))):
            raise ValueError('INVOICE_PUBLIC_BASE_URL must be an HTTPS origin or an HTTP loopback origin')
        if not 1 <= self.db_pool_size <= 200 or not 0 <= self.db_max_overflow <= 200:
            raise ValueError('INVOICE_DB_POOL_SIZE must be 1..200 and INVOICE_DB_MAX_OVERFLOW 0..200')
        if not -1 <= self.db_pool_recycle_seconds <= 86400: raise ValueError('INVOICE_DB_POOL_RECYCLE_SECONDS must be -1..86400')
        for name in ('db_statement_timeout_ms','db_lock_timeout_ms','db_idle_tx_timeout_ms'):
            if not 0 <= getattr(self,name) <= 86_400_000: raise ValueError('INVOICE_'+name.upper()+' must be 0..86400000 (0 disables)')
        if self.database_read_url and not self.database_read_url.startswith('postgresql'):
            raise ValueError('DATABASE_READ_URL must be a PostgreSQL URL')
        if self.gmail_expected_email:
            import re
            if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',self.gmail_expected_email):raise ValueError('Invalid expected Gmail address')
        return self
