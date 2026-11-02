from django.contrib.auth import logout
from django.contrib.sessions.models import Session
from django.utils import timezone

from .models import ActiveUserSession
from .single_device import register_active_session


class AISingleDeviceSessionMiddleware:
    """Log out a browser when a newer login has claimed the same account's
    single AI-login slot — but only enforced while the request is actually
    for /ai/, so a customer who is merely browsing the storefront on two
    devices at once is never affected. The claim itself (ActiveUserSession)
    is still written on every login site-wide, via ai_app.signals — this
    middleware only decides when to act on it."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and request.path.startswith('/ai/'):
            current_key = request.session.session_key
            active = ActiveUserSession.objects.filter(user=request.user).first()

            if not current_key:
                request.session.save()
                register_active_session(request.user, request.session.session_key)
            elif active is None:
                # Covers accounts that were already logged in when this
                # feature was deployed: their current browser gets the slot.
                register_active_session(request.user, current_key)
            elif active.session_key != current_key:
                active_session_still_valid = Session.objects.filter(
                    session_key=active.session_key,
                    expire_date__gt=timezone.now(),
                ).exists()
                if active_session_still_valid:
                    # The active key belongs to the newer device. Django's
                    # logout also clears this browser's stale auth cookie —
                    # note this ends the whole shared session (storefront
                    # included), since there's only one account system.
                    logout(request)
                else:
                    # A normal session-key rotation (for example after a
                    # password change) or an expired record may leave the
                    # database pointer stale; safely let the only live
                    # authenticated session reclaim it.
                    register_active_session(request.user, current_key)

        return self.get_response(request)


class AICustomModelsMiddleware:
    """Keeps models added on the dashboard (EduTrellis AI > API Settings > Add
    a new model) in ai_chat.MODELS for the pages and APIs that read it.
    Usually just a cache read — see ai_app.custom_models.sync. Scoped to /ai/
    and the dashboard so storefront requests never touch it."""

    PREFIXES = ('/ai/', '/store/dashboard/')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(self.PREFIXES):
            from . import ai_chat, custom_models
            custom_models.sync()
            ai_chat.apply_model_id_overrides()
        return self.get_response(request)
