import logging
import re
import time

from django.http import HttpResponsePermanentRedirect, JsonResponse
from django.shortcuts import render


class CanonicalHostMiddleware:
    """Consolidate the bare domain onto the canonical www hostname."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.get_host().partition(':')[0].lower() == 'edutrellis.in':
            return HttpResponsePermanentRedirect(
                f'https://www.edutrellis.in{request.get_full_path()}'
            )
        return self.get_response(request)


class HideAdminFromNonStaffMiddleware:
    """Django's built-in admin login view tells an authenticated non-staff
    user "You are authenticated as X, but are not authorized to access this
    page" — which both confirms /admin/ exists and echoes their email back
    to them. Anyone logged in but not staff/superuser (e.g. an ordinary
    store customer) should see the same 404 as any unknown URL instead,
    same as an anonymous visitor poking at random paths."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith('/admin/') and request.user.is_authenticated and not (request.user.is_staff or request.user.is_superuser):
            from myapp.views import custom_404
            return custom_404(request)
        return self.get_response(request)


class AdminLoginBrakeMiddleware:
    """Too many wrong passwords on /admin/login/ (from one IP address or for
    one account) pause admin logins for 15 minutes — see myapp.ratelimit."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method != 'POST' or request.path != '/admin/login/':
            return self.get_response(request)
        from django.http import HttpResponse
        from . import ratelimit
        account = request.POST.get('username', '')
        if ratelimit.login_blocked(request, account):
            return HttpResponse(ratelimit.LOGIN_BLOCKED_MESSAGE, status=429, content_type='text/plain')
        response = self.get_response(request)
        if response.status_code == 302:   # the admin login redirects only on success
            ratelimit.login_succeeded(request, account)
        else:
            ratelimit.login_failed(request, account)
        return response


class PublicAssetCacheMiddleware:
    """Give version-stable static/media files a useful browser cache.

    This middleware sits before WhiteNoise so it also wraps files served
    directly by WhiteNoise rather than only normal Django view responses.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.status_code == 200:
            if request.path.startswith('/static/'):
                response['Cache-Control'] = 'public, max-age=86400, stale-while-revalidate=604800'
        return response


class SiteSwitchMiddleware:
    """Dashboard → Disable Site. A switched-off product answers every one of
    its URLs with a "Coming soon" page (503, so search engines come back
    later) — at the address the visitor typed, no redirect. API calls get a
    short JSON error instead. Runs before sessions and login, so a switched-
    off product costs one cached flag check per request and nothing else."""

    AI_PATH = re.compile(r'^/ai(/|$)')
    EDUCHATS_PATH = re.compile(r'^/(educhats|chats?)(/|$)')
    CACHE_SECONDS = 5

    _flags = (0.0, (False, False))

    def __init__(self, get_response):
        self.get_response = get_response

    @classmethod
    def flags(cls):
        stamp, flags = cls._flags
        if time.monotonic() - stamp > cls.CACHE_SECONDS:
            from .models import SiteStatus
            try:
                row = SiteStatus.objects.filter(pk=1).values_list('ai_disabled', 'educhats_disabled').first()
            except Exception:
                row = None   # e.g. before migrate has run: keep everything on
            flags = tuple(row) if row else (False, False)
            cls._flags = (time.monotonic(), flags)
        return flags

    @classmethod
    def refresh(cls):
        cls._flags = (0.0, (False, False))

    def __call__(self, request):
        path = request.path
        product = None
        if self.AI_PATH.match(path) and not path.startswith('/ai/chats'):
            product = 'ai'
        elif self.EDUCHATS_PATH.match(path) or path.startswith('/ai/chats'):
            product = 'educhats'
        if product:
            ai_off, educhats_off = self.flags()
            if (ai_off if product == 'ai' else educhats_off):
                return self.coming_soon(request, product)
        return self.get_response(request)

    @staticmethod
    def coming_soon(request, product):
        name = 'EduTrellis AI' if product == 'ai' else 'EduChats'
        wants_json = ('/api/' in request.path or request.method not in ('GET', 'HEAD')
                      or 'application/json' in request.headers.get('Accept', ''))
        if wants_json:
            detail = f'{name} is coming soon. Please check back later.'
            response = JsonResponse({'ok': False, 'status': 'error', 'error': detail, 'detail': detail}, status=503)
        else:
            response = render(request, 'coming_soon.html', {'product_name': name, 'product': product}, status=503)
        response['Retry-After'] = '3600'
        response['Cache-Control'] = 'no-store'
        response['X-Robots-Tag'] = 'noindex'
        response._has_been_logged = True   # expected, not an error worth a log line per visit
        return response


class FriendlyErrorPageMiddleware:
    """Where DEBUG is on (this computer), only staff see Django's yellow
    debug page with the code and traceback. Everyone else gets the site's
    own pages: "Something went wrong" (500.html) when a view crashes, and
    the normal 404 page for a missing product/page — or a short JSON error
    for API calls. The crash is still logged in full for us."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        from django.core.exceptions import PermissionDenied, SuspiciousOperation, BadRequest
        from django.http import Http404, JsonResponse
        from django.shortcuts import render

        from django.conf import settings
        user = getattr(request, 'user', None)
        if settings.DEBUG and getattr(user, 'is_staff', False):
            return None   # staff on a debug server: Django's full debug page
        if isinstance(exception, (PermissionDenied, SuspiciousOperation, BadRequest)):
            return None   # Django already answers these with a plain 403/400
        wants_json = '/api/' in request.path or 'application/json' in request.headers.get('Accept', '') \
            or request.content_type == 'application/json'
        if isinstance(exception, Http404):
            if wants_json:
                return JsonResponse({'ok': False, 'status': 'error', 'error': 'Not found.', 'detail': 'Not found.'}, status=404)
            return render(request, '404.html', status=404)
        logging.getLogger('myapp').exception('Unhandled error on %s %s', request.method, request.path)
        msg = 'Something went wrong on our side. Please try again in a moment.'
        if wants_json:
            return JsonResponse({'ok': False, 'status': 'error', 'error': msg, 'detail': msg}, status=500)
        return render(request, '500.html', status=500)
