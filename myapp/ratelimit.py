"""Who is asking (client_ip) and brakes for password guessing (login_*).

Login brake: after too many wrong passwords from one IP address, or against
one account, password logins pause for LOGIN_WINDOW. A correct login clears
the account's count. Counts live in Django's cache (one process on Railway).
"""
import hashlib

from django.core.cache import cache

LOGIN_WINDOW = 15 * 60
LOGIN_MAX_PER_IP = 20
LOGIN_MAX_PER_ACCOUNT = 8
LOGIN_BLOCKED_MESSAGE = 'Too many login attempts. Please wait 15 minutes and try again.'


def client_ip(request):
    """The visitor's IP address. Railway's edge proxy adds the address it saw
    to the end of X-Forwarded-For; anything before that came from the visitor
    and can be made up, so only the last entry is trusted."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    last = forwarded.split(',')[-1].strip() if forwarded else ''
    return (last or request.META.get('REMOTE_ADDR', '') or 'unknown')[:64]


def _keys(request, account):
    keys = [f'login-ip:{client_ip(request)}']
    if account:
        digest = hashlib.sha256(account.strip().lower().encode('utf-8')).hexdigest()[:32]
        keys.append(f'login-account:{digest}')
    return keys


def login_blocked(request, account=''):
    ip_key, *account_key = _keys(request, account)
    if cache.get(ip_key, 0) >= LOGIN_MAX_PER_IP:
        return True
    return bool(account_key) and cache.get(account_key[0], 0) >= LOGIN_MAX_PER_ACCOUNT


def login_failed(request, account=''):
    for key in _keys(request, account):
        if not cache.add(key, 1, LOGIN_WINDOW):
            try:
                cache.incr(key)
            except ValueError:
                cache.set(key, 1, LOGIN_WINDOW)


def login_succeeded(request, account=''):
    if account:
        cache.delete(_keys(request, account)[1])
