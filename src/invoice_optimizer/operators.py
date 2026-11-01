"""Personal local accounts: passwords are never persisted in plain text.

The shared workspace token is a setup/read-only credential, not evidence of
which person performed a review. Every operator has an immutable account ID.
"""
import hashlib
import re
import secrets
from sqlalchemy import String, Integer, Boolean, DateTime, CheckConstraint, select, func, text
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime
from .models import Base, uid, now
from .db import audit, Conflict

ITERATIONS = 600_000
ACCOUNT_LOCK = 681209731


class Operator(Base):
    __tablename__ = 'operators'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(256))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    role: Mapped[str] = mapped_column(String(20), default='reviewer')
    credential_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (CheckConstraint("role IN ('admin', 'reviewer')"),)


def normalize_username(value):
    username = value.strip().lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9._@-]{2,79}', username):
        raise ValueError('Username must be 3–80 characters: letters, numbers, dot, underscore, @ or hyphen')
    return username


def hash_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('Personal passwords must contain 12–256 characters')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f'pbkdf2_sha256${ITERATIONS}${salt}${digest.hex()}'


def verify_password(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or not 100_000 <= int(rounds) <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError):
        return False


# An absent account still pays the password-hash cost; it never authenticates.
DUMMY_HASH = f'pbkdf2_sha256${ITERATIONS}$' + '00' * 16 + '$' + '00' * 32


def operator_identity(operator):
    return {'actor': 'user:' + operator.id, 'user_id': operator.id,
            'username': operator.username, 'display_name': operator.display_name,
            'role': operator.role, 'identity_type': 'operator', 'can_review': True}


def public_operator(operator):
    return {**operator_identity(operator), 'id': operator.id, 'active': operator.active, 'can_review':operator.active,
            'created_at': operator.created_at.isoformat()}


def account_lock(session):
    session.execute(text('SELECT pg_advisory_xact_lock(:lock)'), {'lock': ACCOUNT_LOCK})


def count_operators(session):
    return session.scalar(select(func.count()).select_from(Operator))


def create_operator(session, *, username, display_name, password, role='reviewer', actor):
    """Create under the account lock; callers authorize setup/admin access."""
    username = normalize_username(username)
    display_name = display_name.strip()
    if not display_name or len(display_name) > 120:
        raise ValueError('Display name must contain 1–120 characters')
    if role not in ('admin', 'reviewer'):
        raise ValueError('Choose the admin or reviewer role')
    encoded = hash_password(password)
    account_lock(session)
    if session.scalar(select(Operator.id).where(Operator.username == username)):
        raise Conflict('That username already exists')
    if count_operators(session) >= 1000:
        raise ValueError('This workspace supports at most 1,000 personal operators')
    operator = Operator(username=username, display_name=display_name,
                        password_hash=encoded, role=role)
    session.add(operator)
    session.flush()
    audit(session, actor, 'operator_created', operator.id,
          {'username': username, 'display_name': display_name, 'role': role})
    return operator


def actor_attributions(session, actors):
    """Resolve display labels without pretending a historical shared actor is a person."""
    actors = set(a for a in actors if a)
    ids = [a[5:] for a in actors if a.startswith('user:')]
    operators = {op.id: op for op in session.scalars(select(Operator).where(Operator.id.in_(ids)))} if ids else {}
    result = {}
    for actor in actors:
        op = operators.get(actor[5:]) if actor.startswith('user:') else None
        result[actor] = ({**operator_identity(op), 'active': op.active} if op else
                         {'actor': actor, 'user_id': None, 'username': None,
                          'display_name': actor, 'identity_type': 'legacy_or_automation',
                          'can_review': False, 'person_verified': False})
    return result
