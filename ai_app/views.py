import base64
import binascii
import csv
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import unquote, urlencode, urlsplit

import requests
from PIL import Image, ImageOps

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.models import User
from django.db import OperationalError, ProgrammingError
from django.db.models import Q, F, Count, Sum, Max, Prefetch, Exists, OuterRef
from django.shortcuts import render, redirect, get_object_or_404
from django.template.loader import render_to_string
from django.urls import resolve, Resolver404, reverse
from django.templatetags.static import static as static_url
from django.http import JsonResponse, StreamingHttpResponse, FileResponse, HttpResponse
from django.core.files.base import ContentFile
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.views.decorators.http import require_POST
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.conf import settings
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from datetime import datetime, timedelta, timezone as dt_timezone
from .forms import PhoneVerifyForm, AILoginForm, AIProfileEditForm, AIPasswordChangeForm, GrantAISubscriptionForm, GrantAPIAccessForm, MAX_AMOUNT_PAID, AISiteSettingsForm, AIAddUserForm
from .models import AISiteSettings, AIAccountMessageSettings, AIConversation, AIMessage, AIBlock, AINote, AIReport, AIGeneratedFile, AIUserImage, GitHubConnection, YouTubeDownloadJob, AIAPIAccess, AIAPIKey, ProviderAPICredential, AIVideoJob
from myapp.models import StoreProfile, PhoneVerification
from myapp import dropbox_storage
from myapp import ratelimit
from . import dropbox_images
from . import ai_chat
from . import context_compress
from . import media_models
from . import video_generation
from . import chat_export
from . import ai_calls
from . import coding_api
from . import github_ops
from . import doc_extract
from . import doc_image
from . import image_styles
from . import poster
from . import doc_text
from . import company_knowledge
from . import image_ocr
from . import image_generation
from . import privacy
from . import request_router
from . import file_convert
from . import web_search
from . import deep_research
from . import audio_transcribe
from . import youtube_download
from .geocoding import reverse_geocode_place
from .ai_report_analysis import analyze_report, aggregate_report_issues
from myapp.emailing import send_store_email, get_notify_email
from myapp.sms import send_phone_otp, verify_phone_otp
from . import model_controls
from .single_device import register_active_session

# EduTrellis's own staff guard (it knows the store dashboard login flow).
from myapp.views import dashboard_staff_required

logger = logging.getLogger(__name__)

try:
    import razorpay
except ImportError:  # pragma: no cover - optional dependency until configured
    razorpay = None




def pwa_service_worker(request):
    response = render(request, 'ai_sw.js', content_type='application/javascript')
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response['Service-Worker-Allowed'] = '/ai/'
    return response


def _user_payload(user):
    profile = getattr(user, 'store_profile', None)
    return {
        'name': user.get_full_name().strip() or user.username,
        'email': user.email,
        'phone': profile.phone if profile else '',
        'age': profile.age if profile else None,
        'state': profile.state if profile else '',
        'city': profile.city if profile else '',
        # A superuser is an administrator even if the independently editable
        # is_staff checkbox was accidentally cleared in the backend.
        'is_staff': bool(user.is_staff or user.is_superuser),
        'is_superuser': user.is_superuser,
        'avatar_url': profile.avatar.url if (profile and profile.avatar) else None,
        'wallet_balance': float(profile.wallet_balance) if profile else 0.0,
        'phone_verified': bool(profile and profile.phone_verified),
        'location_consent': profile.location_consent if profile else StoreProfile.LOCATION_UNKNOWN,
        'amount_paid_missing': bool(not profile or profile.manual_amount_paid <= 0),
    }


def _location_prompt_needed(user):
    if not user.is_authenticated or user.is_superuser:
        return False
    profile = getattr(user, 'store_profile', None)
    if not profile or profile.location_consent == StoreProfile.LOCATION_UNKNOWN:
        return True
    # A decline isn't forever — ask again a day later rather than never again,
    # since plenty of "not now" taps are just bad timing, not a lasting no.
    if profile.location_consent == StoreProfile.LOCATION_DENIED:
        if not profile.location_updated_at:
            return True
        return timezone.now() - profile.location_updated_at >= timedelta(hours=24)
    return False


def _profile_wizard_needed(user):
    """Whether to show the mandatory "complete your profile" wizard (see
    includes/profile_wizard.html) on this account's next page load — its
    details/amount/location steps are how every account now supplies name,
    age, state, city, email, contact, and amount paid, since there is no
    public self-signup form to collect them at account-creation time
    anymore (accounts are created manually by staff — dashboard AIAddUserForm
    — which doesn't ask for age/state/city at all and leaves amount paid
    optional). Fires whenever any one of those is still missing, not just
    when all are — unlike the old amount-and-location-only version of this
    check, this is meant to actually be mandatory."""
    if not user.is_authenticated or user.is_superuser:
        return False
    profile = getattr(user, 'store_profile', None)
    if not profile:
        return True
    location_missing = profile.location_consent == StoreProfile.LOCATION_UNKNOWN
    amount_missing = profile.manual_amount_paid <= 0
    details_missing = not (profile.age and profile.state.strip() and profile.city.strip() and profile.phone.strip())
    return location_missing or amount_missing or details_missing


def _merge_session_ai_chats_into_user(user, session_key):
    """Attach a guest's saved AI data to the account they just entered."""
    if not session_key:
        return
    AIConversation.objects.filter(session_key=session_key, user__isnull=True).update(user=user, session_key='')
    AIGeneratedFile.objects.filter(session_key=session_key, user__isnull=True).update(user=user, session_key='')
    # A report submitted before login should not disappear from the account
    # panel afterwards.  Keep its immutable evidence and status, but move its
    # owner from the guest session to the authenticated account just like the
    # conversation itself.
    AIReport.objects.filter(session_key=session_key, user__isnull=True).update(user=user, session_key='')


def _parse_json_body(request):
    try:
        return json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return {}


def _is_ajax(request):
    """True for fetch() calls the dashboard JS makes to update a panel in
    place — every such call sets this header. Used to return JSON/an HTML
    fragment instead of the usual message+redirect, so a CRUD action (add,
    delete, grant, revoke, search) never triggers a full page navigation."""
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


PHONE_VERIFY_OTP_TTL_MINUTES = 10
PHONE_VERIFY_RESEND_COOLDOWN_SECONDS = 45
PHONE_VERIFY_MAX_ATTEMPTS = 5


def _new_phone_verification(user, phone, now):
    """Sends a fresh OTP via 2Factor and records the resulting session so
    a later confirm can check the code against it. The OTP digits
    themselves live at 2Factor, not here."""
    session_id = send_phone_otp(phone)
    pending, _created = PhoneVerification.objects.update_or_create(
        user=user,
        defaults={
            'session_id': session_id, 'phone': phone, 'attempts': 0, 'last_sent_at': now,
            'expires_at': now + timedelta(minutes=PHONE_VERIFY_OTP_TTL_MINUTES),
        },
    )
    if settings.DEBUG:
        # Local/dev visibility — the OTP itself isn't known to us (2Factor
        # generates and checks it), but this confirms the SMS send attempt
        # ran and which phone/session it's tied to.
        print(f"\n{'='*60}\nPhone OTP sent to {phone} (2Factor session {session_id})\n{'='*60}\n")
    return pending


def ai_signup(request):
    """Create an EduTrellis account from the AI page. Runs the store's own
    signup (myapp.views.store_signup — same form, validation, account and
    phone-verification SMS), so one account works on both /ai/ and /store/,
    then carries the guest's AI chats over to the new account exactly like
    ai_login does."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    from myapp import views as store_views

    if not request.session.session_key:
        request.session.create()
    pre_login_session_key = request.session.session_key

    response = store_views.store_signup(request)
    if response.status_code != 200 or not request.user.is_authenticated:
        return response
    _merge_session_ai_chats_into_user(request.user, pre_login_session_key)
    return JsonResponse({'status': 'ok', 'user': _user_payload(request.user)})


def ai_phone_verify_send(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)

    profile, _ = StoreProfile.objects.get_or_create(user=request.user)
    if profile.phone_verified:
        return JsonResponse({'status': 'error', 'detail': 'Your phone number is already verified.'}, status=400)
    if not profile.phone:
        return JsonResponse({'status': 'error', 'detail': 'Add a phone number to your profile first.'}, status=400)

    now = timezone.now()
    existing = PhoneVerification.objects.filter(user=request.user).first()
    if existing and (now - existing.last_sent_at).total_seconds() < PHONE_VERIFY_RESEND_COOLDOWN_SECONDS:
        wait = PHONE_VERIFY_RESEND_COOLDOWN_SECONDS - int((now - existing.last_sent_at).total_seconds())
        return JsonResponse({'status': 'error', 'detail': f'Please wait {wait}s before requesting another code.'}, status=429)

    try:
        _new_phone_verification(request.user, profile.phone, now)
    except Exception as e:
        logger.exception("Phone verification SMS failed for %s: %s", profile.phone, e)
        return JsonResponse({'status': 'error', 'detail': 'Could not send the verification code. Please try again shortly.'}, status=502)

    return JsonResponse({'status': 'ok'})


def ai_phone_verify_confirm(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)

    form = PhoneVerifyForm(_parse_json_body(request))
    if not form.is_valid():
        return JsonResponse({'status': 'validation_error', 'errors': {k: v[0] for k, v in form.errors.items()}}, status=400)

    otp = form.cleaned_data['otp']
    pending = PhoneVerification.objects.filter(user=request.user).first()
    if not pending:
        return JsonResponse({'status': 'error', 'detail': 'No pending verification — send a new code first.'}, status=404)

    if pending.is_expired:
        pending.delete()
        return JsonResponse({'status': 'error', 'detail': 'That code has expired — send a new one.'}, status=400)

    if pending.attempts >= PHONE_VERIFY_MAX_ATTEMPTS:
        pending.delete()
        return JsonResponse({'status': 'error', 'detail': 'Too many incorrect attempts — send a new code.'}, status=400)

    try:
        matched = verify_phone_otp(pending.session_id, otp)
    except Exception as e:
        logger.exception("2Factor verify call failed for %s: %s", pending.phone, e)
        return JsonResponse({'status': 'error', 'detail': 'Could not verify that code right now. Please try again shortly.'}, status=502)

    if not matched:
        pending.attempts += 1
        pending.save(update_fields=['attempts'])
        left = PHONE_VERIFY_MAX_ATTEMPTS - pending.attempts
        return JsonResponse({'status': 'error', 'detail': f'Incorrect code — {left} attempt{"s" if left != 1 else ""} left.'}, status=400)

    profile, _ = StoreProfile.objects.get_or_create(user=request.user)
    profile.phone_verified = True
    profile.save(update_fields=['phone_verified'])
    pending.delete()

    return JsonResponse({'status': 'ok', 'user': _user_payload(request.user)})


def ai_login(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)

    form = AILoginForm(_parse_json_body(request))
    if not form.is_valid():
        return JsonResponse(
            {'status': 'validation_error', 'errors': {k: v[0] for k, v in form.errors.items()}},
            status=400,
        )

    identifier = form.cleaned_data['identifier'].strip()
    password = form.cleaned_data['password']
    if ratelimit.login_blocked(request, identifier):
        return JsonResponse({'status': 'error', 'detail': ratelimit.LOGIN_BLOCKED_MESSAGE}, status=429)

    user_obj = User.objects.filter(email__iexact=identifier).first()
    if not user_obj:
        profile = StoreProfile.objects.filter(phone=identifier).first()
        user_obj = profile.user if profile else None

    if not user_obj:
        ratelimit.login_failed(request, identifier)
        return JsonResponse({'status': 'error', 'detail': 'No account found with that email or phone.'}, status=400)

    auth_user = authenticate(request, username=user_obj.username, password=password)
    if not auth_user:
        ratelimit.login_failed(request, identifier)
        return JsonResponse({'status': 'error', 'detail': 'Incorrect password.'}, status=400)
    ratelimit.login_succeeded(request, identifier)

    if not request.session.session_key:
        request.session.create()
    pre_login_session_key = request.session.session_key

    login(request, auth_user)
    _merge_session_ai_chats_into_user(auth_user, pre_login_session_key)

    return JsonResponse({'status': 'ok', 'user': _user_payload(auth_user)})


def ai_location_update(request):
    """Save one consent choice and, when allowed, one browser location fix."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)

    data = _parse_json_body(request)
    consent = data.get('consent')
    if consent not in (StoreProfile.LOCATION_GRANTED, StoreProfile.LOCATION_DENIED):
        return JsonResponse({'status': 'error', 'detail': 'Choose whether to enable location.'}, status=400)

    profile, _ = StoreProfile.objects.get_or_create(user=request.user)
    profile.location_consent = consent
    profile.location_updated_at = timezone.now()
    update_fields = [
        'location_consent', 'location_updated_at', 'location_latitude',
        'location_longitude', 'location_accuracy_m', 'location_place_name',
    ]

    if consent == StoreProfile.LOCATION_DENIED:
        profile.location_latitude = None
        profile.location_longitude = None
        profile.location_accuracy_m = None
        profile.location_place_name = ''
    else:
        try:
            latitude = Decimal(str(data.get('latitude')))
            longitude = Decimal(str(data.get('longitude')))
            accuracy = Decimal(str(data.get('accuracy', 0)))
            if not latitude.is_finite() or not longitude.is_finite() or not accuracy.is_finite():
                raise InvalidOperation
            if not Decimal('-90') <= latitude <= Decimal('90'):
                raise InvalidOperation
            if not Decimal('-180') <= longitude <= Decimal('180'):
                raise InvalidOperation
            if not Decimal('0') <= accuracy <= Decimal('1000000'):
                raise InvalidOperation
        except (InvalidOperation, TypeError, ValueError):
            return JsonResponse({'status': 'error', 'detail': 'The browser returned an invalid location.'}, status=400)
        profile.location_latitude = latitude
        profile.location_longitude = longitude
        profile.location_accuracy_m = int(accuracy.to_integral_value())
        profile.location_place_name = reverse_geocode_place(latitude, longitude)

    profile.save(update_fields=update_fields)
    return JsonResponse({
        'status': 'ok',
        'location_consent': profile.location_consent,
        'location_captured': bool(
            profile.location_latitude is not None and profile.location_longitude is not None
        ),
    })


def ai_user_map(request):
    """Superuser-only: every account that shared its browser location, for
    the Maps view in the AI account menu."""
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated or not request.user.is_superuser:
        return JsonResponse({'status': 'error', 'detail': 'Maps is only available to the site owner.'}, status=403)

    profiles = (
        StoreProfile.objects
        .filter(location_latitude__isnull=False, location_longitude__isnull=False)
        .select_related('user')
        .order_by('-location_updated_at')
    )
    users = []
    for profile in profiles:
        user = profile.user
        users.append({
            'id': user.pk,
            'name': user.get_full_name().strip() or user.username,
            'email': user.email,
            'phone': profile.phone,
            'place': profile.location_place_name,
            'lat': float(profile.location_latitude),
            'lng': float(profile.location_longitude),
            'accuracy_m': profile.location_accuracy_m,
            'located_at': profile.location_updated_at.isoformat() if profile.location_updated_at else None,
            'last_login': user.last_login.isoformat() if user.last_login else None,
            'avatar_url': profile.avatar.url if profile.avatar else None,
            'is_superuser': user.is_superuser,
        })
    return JsonResponse({
        'status': 'ok',
        'users': users,
        'total_users': User.objects.count(),
    })


AMOUNT_PAID_MAX = Decimal(str(MAX_AMOUNT_PAID))


def ai_amount_paid_update(request):
    """Lets a logged-in user self-report how much they've already paid, for
    older accounts staff hasn't recorded a manual_amount_paid figure for
    yet (see the profile-completion wizard, includes/profile_wizard.html).
    Only accepted while the field is still at its untouched default of 0 —
    once a real value is on file (self-reported here, or entered by staff
    in the dashboard), further self-reports are silently ignored so this
    can't be used to overwrite that figure after the fact from the client."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)

    data = _parse_json_body(request)
    try:
        amount = Decimal(str(data.get('amount')))
        if not amount.is_finite() or amount < 0:
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError):
        return JsonResponse({'status': 'error', 'detail': 'Enter a valid amount.'}, status=400)
    if amount > AMOUNT_PAID_MAX:
        return JsonResponse(
            {'status': 'error', 'detail': f'Amount paid cannot be more than ₹{AMOUNT_PAID_MAX}.'}, status=400,
        )

    profile, _ = StoreProfile.objects.get_or_create(user=request.user)
    if profile.manual_amount_paid <= 0:
        profile.manual_amount_paid = amount
        profile.save(update_fields=['manual_amount_paid'])
    return JsonResponse({'status': 'ok', 'manual_amount_paid': float(profile.manual_amount_paid)})


def ai_logout(request):
    logout(request)
    return JsonResponse({'status': 'ok'})


def ai_profile_update(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)

    form = AIProfileEditForm(request.POST, request.FILES, user=request.user)
    if not form.is_valid():
        return JsonResponse(
            {'status': 'validation_error', 'errors': {k: v[0] for k, v in form.errors.items()}},
            status=400,
        )

    name = form.cleaned_data['name']
    first_name, _, last_name = name.partition(' ')
    request.user.first_name = first_name
    request.user.last_name = last_name
    user_fields = ['first_name', 'last_name']
    email = form.cleaned_data.get('email')
    if email:
        # Login resolves an account by its current email and then authenticates
        # its internal username, so staff/custom usernames do not need changing.
        request.user.email = email
        user_fields.append('email')
    request.user.save(update_fields=user_fields)

    profile, _ = StoreProfile.objects.get_or_create(user=request.user)
    new_phone = form.cleaned_data['phone']
    if profile.phone != new_phone:
        profile.phone_verified = False
    profile.phone = new_phone
    profile.age = form.cleaned_data['age']
    profile.state = form.cleaned_data['state']
    profile.city = form.cleaned_data['city']
    if form.cleaned_data.get('avatar'):
        profile.avatar = form.cleaned_data['avatar']
    profile.save()

    return JsonResponse({'status': 'ok', 'user': _user_payload(request.user)})


def ai_password_change(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)

    form = AIPasswordChangeForm(_parse_json_body(request))
    if not form.is_valid():
        return JsonResponse(
            {'status': 'validation_error', 'errors': {k: v[0] for k, v in form.errors.items()}},
            status=400,
        )

    if not request.user.check_password(form.cleaned_data['current_password']):
        return JsonResponse({'status': 'error', 'detail': 'Current password is incorrect.'}, status=400)

    request.user.set_password(form.cleaned_data['new_password'])
    request.user.save(update_fields=['password'])
    update_session_auth_hash(request, request.user)  # keep the session logged in
    register_active_session(request.user, request.session.session_key)
    return JsonResponse({'status': 'ok'})


def ai_account_details(request):
    """Return the signed-in customer's account data for the AI profile menu.

    Reports are intentionally selected through request.user only; guest
    reports and reports belonging to other accounts must never appear here.
    """
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)

    profile, _ = StoreProfile.objects.get_or_create(user=request.user)
    # Django normally creates superusers with is_staff=True, but the two
    # flags can be edited independently in the backend.  AI Management lists
    # either flag as a full admin account, so the customer-facing entitlement
    # API must honour either one as well.
    is_staff = _ai_has_admin_access(request.user)
    is_subscribed = profile.is_ai_subscribed
    if is_staff:
        plan_name = 'Staff access'
    elif is_subscribed:
        plan_name = 'EduTrellis AI Premium'
    else:
        plan_name = 'Free plan'

    reports = []
    for report in AIReport.objects.filter(user=request.user).select_related('conversation')[:100]:
        reports.append({
            'id': report.pk,
            'status': report.status,
            'status_label': report.get_status_display(),
            'explanation': report.explanation,
            'user_prompt': report.user_prompt,
            'reported_reply': report.reported_reply,
            'model': ai_chat.MODELS.get(report.model_key, {}).get('label', report.model_key or 'AI'),
            'conversation_title': report.conversation.title if report.conversation else 'Deleted conversation',
            'created_at': timezone.localtime(report.created_at).isoformat(),
        })

    # The "My Images" gallery, newest first. Read from AIUserImage, not from
    # the chat transcript: these rows survive their conversation being deleted,
    # which is the whole point of the table. The url column is only ever the
    # real stored media URL written by _ai_flux_response, never model text.
    images = [
        {
            'id': image.pk,
            'url': image.url,
            'prompt': image.prompt,
            'conversation_id': image.conversation_id,
            'created_at': timezone.localtime(image.created_at).isoformat(),
        }
        for image in AIUserImage.objects.filter(
            user=request.user,
        ).exclude(url='').order_by('-created_at')[:120]
    ]

    api_access = AIAPIAccess.objects.filter(user=request.user).exclude(model_keys='').first()
    api_key = getattr(request.user, 'ai_api_key', None)
    granted_model_keys = api_access.model_key_list if api_access else []
    response = JsonResponse({
        'status': 'ok',
        'user': _user_payload(request.user),
        'api_access': {
            'granted': bool(granted_model_keys),
            'models': [
                {'key': key, 'label': ai_chat.MODELS[key]['label']}
                for key in granted_model_keys if key in ai_chat.MODELS
            ],
            'has_key': bool(api_key),
            'key_prefix': api_key.key_prefix if api_key else None,
            'key_created_at': (
                timezone.localtime(api_key.created_at).isoformat() if api_key else None
            ),
        },
        'coding': coding_api.account_summary(
            request.user, allowed=bool(is_staff or is_subscribed), purchase_url=_ai_purchase_url(),
        ),
        'calls': ai_calls.summaries(request.user),
        'subscription': {
            'plan_name': plan_name,
            'active': bool(is_staff or is_subscribed),
            'is_staff': is_staff,
            'started_at': (
                timezone.localtime(profile.ai_subscription_started_at).isoformat()
                if profile.ai_subscription_started_at and is_subscribed else None
            ),
            'expires_at': (
                timezone.localtime(profile.ai_subscription_until).isoformat()
                if profile.ai_subscription_until and is_subscribed else None
            ),
            'free_used': profile.ai_free_messages_used,
            'free_limit': AI_FREE_MESSAGE_LIMIT,
            'free_remaining': max(0, AI_FREE_MESSAGE_LIMIT - profile.ai_free_messages_used),
            'purchase_url': _ai_purchase_url(),
        },
        'reports': reports,
        'images': images,
    })
    response['Cache-Control'] = 'private, no-store'
    return response


def ai_developer_key_generate(request):
    """Generate (or regenerate) the signed-in user's developer API key —
    see AIAPIKey.generate_for. Only usable once staff have granted at least
    one model via AIAPIAccess (dashboard API Management); the raw key is
    returned exactly once here and never retrievable again afterwards, so
    the frontend must show it to the user immediately."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)

    access = AIAPIAccess.objects.filter(user=request.user).exclude(model_keys='').first()
    if not access:
        return JsonResponse({
            'status': 'error',
            'detail': "You don't have API access yet. Contact an administrator to request it.",
        }, status=403)

    raw_key = AIAPIKey.generate_for(request.user)
    return JsonResponse({
        'status': 'ok',
        'api_key': raw_key,
        'models': [
            {'key': key, 'label': ai_chat.MODELS[key]['label']}
            for key in access.model_key_list if key in ai_chat.MODELS
        ],
    })


def _ai_self_serve_model_keys(user):
    """Which ai_chat.MODELS to grant when a user requests developer API
    access themselves — the same chat models the in-app picker already
    shows them, so self-serve just opens a second door to the same access
    instead of handing out anything new."""
    full_access = _ai_has_full_model_access(user)
    return [
        key for key in _api_chat_model_choices()
        if ai_chat.is_model_enabled(key)
        and (full_access or key not in AI_FULL_ACCESS_ONLY_MODEL_KEYS)
        and (full_access or key in AI_FREE_MODEL_KEYS)
    ]


def ai_api_access_request(request):
    """Self-serve developer API access: grants the signed-in user's own
    AIAPIAccess row on the spot instead of making them contact staff (see
    dashboard_api_access_grant for the staff-side equivalent). They still
    generate their own key afterwards via ai_developer_key_generate."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)

    model_keys = _ai_self_serve_model_keys(request.user)
    if not model_keys:
        return JsonResponse({
            'status': 'error',
            'detail': 'No models are available to grant right now. Try again once a model is enabled for your account.',
        }, status=400)

    AIAPIAccess.objects.update_or_create(
        user=request.user,
        defaults={'model_keys': ','.join(model_keys), 'granted_by': request.user},
    )
    return JsonResponse({
        'status': 'ok',
        'models': [{'key': key, 'label': ai_chat.MODELS[key]['label']} for key in model_keys],
    })


# Chat-capable models only — an image-generation-only entry (image_generation:
# True) has no 'chat.completions'-shaped upstream to answer through
# ai_chat.stream_chat, so it's excluded from what the public API can serve
# even if a model_keys grant otherwise includes it.
def _api_chat_model_choices():
    return {
        key: cfg for key, cfg in ai_chat.MODELS.items()
        if key != 'vision' and not cfg.get('image_generation')
    }


@csrf_exempt
def api_chat_completions(request):
    """Public developer API: a granted user's own code calls this directly
    with their AIAPIKey, no browser session involved — see
    AIAPIAccess/AIAPIKey and dashboard_api_management. Deliberately a plain
    JSON in/JSON out endpoint (not streaming): an external HTTP client is
    far more likely to want one complete response than an SSE-style stream.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid request method. Use POST.'}, status=405)

    auth_header = request.META.get('HTTP_AUTHORIZATION', '')
    raw_key = auth_header[7:] if auth_header.lower().startswith('bearer ') else ''
    api_key = AIAPIKey.resolve(raw_key)
    if not api_key:
        return JsonResponse({'error': 'Invalid or missing API key. Send it as "Authorization: Bearer <key>".'}, status=401)

    access = AIAPIAccess.objects.filter(user=api_key.user).exclude(model_keys='').first()
    granted = set(access.model_key_list) if access else set()
    if not granted:
        return JsonResponse({'error': 'This API key has no model access. Contact an administrator.'}, status=403)

    payload = _parse_json_body(request)
    model_key = str(payload.get('model') or '').strip()
    if not model_key:
        return JsonResponse({'error': 'The "model" field is required.'}, status=400)
    if model_key not in granted:
        return JsonResponse({
            'error': f'This API key is not authorized for model "{model_key}". '
                     f'Authorized models: {", ".join(sorted(granted)) or "(none)"}.',
        }, status=403)
    chat_models = _api_chat_model_choices()
    if model_key not in chat_models:
        return JsonResponse({'error': f'Model "{model_key}" does not support the chat API.'}, status=400)
    if not ai_chat.is_model_enabled(model_key):
        return JsonResponse({'error': f'Model "{model_key}" is currently disabled.'}, status=503)

    messages = payload.get('messages')
    if not messages:
        single_message = str(payload.get('message') or '').strip()
        if not single_message:
            return JsonResponse({'error': 'Provide either "message" (a string) or "messages" (a list).'}, status=400)
        messages = [{'role': 'user', 'content': single_message}]
    if not isinstance(messages, list) or not messages:
        return JsonResponse({'error': '"messages" must be a non-empty list of {role, content} objects.'}, status=400)
    if len(messages) > 30:
        return JsonResponse({'error': 'Too many messages — send at most 30 per request.'}, status=400)
    clean_messages = []
    for item in messages:
        if not isinstance(item, dict) or item.get('role') not in ('user', 'assistant') or not str(item.get('content') or '').strip():
            return JsonResponse({'error': 'Each message needs "role" ("user" or "assistant") and non-empty "content".'}, status=400)
        content = str(item['content'])
        if len(content) > 8000:
            return JsonResponse({'error': 'Each message\'s content must be 8000 characters or fewer.'}, status=400)
        clean_messages.append({'role': item['role'], 'content': content})
    if clean_messages[-1]['role'] != 'user':
        return JsonResponse({'error': 'The last message must have role "user".'}, status=400)

    try:
        reply = ''.join(ai_chat.stream_chat(clean_messages, model_key=model_key))
    except Exception as exc:
        logger.exception('Developer API chat request failed for user %s model=%s', api_key.user_id, model_key)
        return JsonResponse({'error': _ai_chat_failure_reply(exc, model_key, is_staff=False)}, status=502)

    # Same public-facing sanitization the in-app chat gets — an external caller
    # leaking the real backend vendor or borrowing a persona name it wasn't
    # given is just as much a trust problem here as it is in the browser UI.
    identity = ai_chat.owner_identity(model_key)
    if identity:
        reply = _owner_identity_public_reply(reply, identity, ai_chat.MODELS[model_key]['label'])
    elif model_key in (ai_chat.CHATGPT_56_MODEL_KEY, ai_chat.SOL_MODEL_KEY, ai_chat.TERRA_MODEL_KEY):
        reply = _chatgpt_public_reply(reply, ai_chat.chatgpt_persona_name(model_key))
    else:
        reply = _vidhyora_public_reply(reply, ai_chat.MODELS[model_key]['label'])

    AIAPIKey.objects.filter(pk=api_key.pk).update(last_used_at=timezone.now(), request_count=F('request_count') + 1)
    return JsonResponse({
        'model': model_key,
        'reply': reply,
        'created_at': timezone.now().isoformat(),
    })




def _dashboard_guard(request):
    """Only authenticated staff/admin users can use the custom dashboard."""
    return request.user.is_authenticated and (
        request.user.is_staff or request.user.is_superuser
    )








USER_DATA_LOCATION_FILTERS = {
    'enabled': StoreProfile.LOCATION_GRANTED,
    'declined': StoreProfile.LOCATION_DENIED,
    'unknown': StoreProfile.LOCATION_UNKNOWN,
}

USER_DATA_SORTS = {
    'newest': ('-date_joined', '-pk'),
    'oldest': ('date_joined', 'pk'),
    'most_chats': ('-chat_count', '-date_joined'),
    'most_logins': ('-store_profile__login_count', '-date_joined'),
}
















@dashboard_staff_required
def dashboard_ai_management(request):
    """Lets staff see who has Vidhyora AI access and for how long, and
    manually grant a customer premium access (see GrantAISubscriptionForm)
    without them having to actually buy the plan — e.g. a comped account.
    Deliberately separate from the Django-admin/dashboard-staff tier: this
    only ever touches StoreProfile.ai_subscription_until, never
    is_staff/is_superuser, so granting someone AI premium here can never
    accidentally hand them dashboard or Django-admin access."""
    now = timezone.now()
    subscribers = (
        StoreProfile.objects.filter(ai_subscription_until__isnull=False)
        .select_related('user').order_by('-ai_subscription_until')
    )
    admins = User.objects.filter(Q(is_staff=True) | Q(is_superuser=True)).select_related('store_profile').order_by('-date_joined')
    add_user_retry = request.session.pop('dashboard_ai_add_user_retry', None)
    add_user_open = bool(add_user_retry)
    add_user_form = AIAddUserForm(add_user_retry) if add_user_open else AIAddUserForm()
    if add_user_open:
        add_user_form.is_valid()  # re-run validation so the same errors render again
    context = {
        'active': 'ai_management',
        'subscribers': subscribers,
        'active_subscriber_count': subscribers.filter(ai_subscription_until__gt=now).count(),
        'admins': admins,
        'now': now,
        'grant_form': GrantAISubscriptionForm(),
        'add_user_form': add_user_form,
        'add_user_open': add_user_open,
        'new_account_whatsapp': request.session.pop('dashboard_new_account_whatsapp', None),
    }
    return render(request, 'dashboard/ai_management.html', context)


@dashboard_staff_required
def dashboard_ai_message_template_save(request):
    """Save edits from a generated message for all subsequently created accounts."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)

    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'detail': 'Invalid message data.'}, status=400)

    message = str(payload.get('message') or '').strip()
    email = str(payload.get('email') or '')
    password = str(payload.get('password') or '')
    days = str(payload.get('days') or '')
    if not message or len(message) > 5000:
        return JsonResponse(
            {'status': 'error', 'detail': 'The message must be between 1 and 5,000 characters.'},
            status=400,
        )

    # Convert this customer's displayed values back into reusable placeholders.
    template = message
    if email:
        template = template.replace(email, '{email}')
    if password:
        template = template.replace(password, '{password}')
    if days:
        template = template.replace(f'{days} days', '{access_days} days')

    required_tokens = ('{email}', '{password}', '{access_days}')
    if any(token not in template for token in required_tokens):
        return JsonResponse({
            'status': 'error',
            'detail': 'Keep the generated email, password, and validity values unchanged while editing.',
        }, status=400)

    settings_obj = AIAccountMessageSettings.get_solo()
    settings_obj.message_template = template
    settings_obj.save(update_fields=['message_template', 'updated_at'])
    return JsonResponse({'status': 'ok', 'message': message})


@dashboard_staff_required
def dashboard_ai_grant(request):
    ajax = _is_ajax(request)
    if request.method == 'POST':
        form = GrantAISubscriptionForm(request.POST)
        if form.is_valid():
            target_user = form.matched_user
            days = form.cleaned_data['days']
            profile, _ = StoreProfile.objects.get_or_create(user=target_user)
            profile.ai_subscription_started_at = timezone.now()
            profile.ai_subscription_until = timezone.now() + timedelta(days=days)
            profile.ai_free_messages_used = 0
            profile.save(update_fields=['ai_subscription_started_at', 'ai_subscription_until', 'ai_free_messages_used'])
            success_message = (
                f"Granted {target_user.email or target_user.username} Vidhyora AI premium access "
                f"until {timezone.localtime(profile.ai_subscription_until):%d %b %Y}."
            )
            if ajax:
                now = timezone.now()
                subscribers = StoreProfile.objects.filter(ai_subscription_until__isnull=False)
                return JsonResponse({
                    'status': 'ok',
                    'message': success_message,
                    # The JS replaces this profile's existing row if one is
                    # already on screen, or prepends it as new — either way
                    # avoids re-fetching/re-sorting the whole table, at the
                    # minor cost of a freshly-granted row not always landing
                    # in exact "-ai_subscription_until" sort order until the
                    # next full page load.
                    'row_html': render_to_string(
                        'dashboard/_subscriber_row.html', {'profile': profile, 'now': now}, request=request,
                    ),
                    'active_subscriber_count': subscribers.filter(ai_subscription_until__gt=now).count(),
                    'ever_granted_count': subscribers.count(),
                })
            messages.success(request, success_message)
        else:
            if ajax:
                errors = {
                    field: [str(e) for e in errs]
                    for field, errs in form.errors.items() if field in ('identifier', 'days')
                }
                return JsonResponse({'status': 'validation_error', 'errors': errors}, status=400)
            for error in form.errors.get('identifier', []):
                messages.error(request, error)
            for error in form.errors.get('days', []):
                messages.error(request, error)
    elif ajax:
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    return redirect('dashboard_ai_management')


@dashboard_staff_required
def dashboard_ai_revoke(request, pk):
    ajax = _is_ajax(request)
    if request.method != 'POST':
        if ajax:
            return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
        return redirect('dashboard_ai_management')

    profile = get_object_or_404(StoreProfile, pk=pk)
    profile.ai_subscription_until = None
    profile.ai_subscription_started_at = None
    profile.save(update_fields=['ai_subscription_until', 'ai_subscription_started_at'])
    success_message = f'Revoked Vidhyora AI premium access for {profile.user.email or profile.user.username}.'
    if ajax:
        now = timezone.now()
        subscribers = StoreProfile.objects.filter(ai_subscription_until__isnull=False)
        return JsonResponse({
            'status': 'ok',
            'message': success_message,
            'active_subscriber_count': subscribers.filter(ai_subscription_until__gt=now).count(),
            'ever_granted_count': subscribers.count(),
        })
    messages.success(request, success_message)
    return redirect('dashboard_ai_management')


@dashboard_staff_required
def dashboard_api_management(request):
    """Lets staff grant a customer's own code direct HTTP access to
    specific ai_chat.MODELS (a developer API key — see AIAPIAccess/
    AIAPIKey), separate from in-app AI chat access. The customer generates
    their own key from the AI page's account menu once granted here."""
    grants = list(
        AIAPIAccess.objects.exclude(model_keys='')
        .select_related('user', 'granted_by', 'user__ai_api_key')
        .order_by('-updated_at')
    )
    model_labels = {key: cfg['label'] for key, cfg in ai_chat.MODELS.items()}
    for grant in grants:
        grant.model_label_list = [model_labels.get(key, key) for key in grant.model_key_list]
    context = {
        'active': 'api_management',
        'grants': grants,
        'grant_form': GrantAPIAccessForm(),
    }
    return render(request, 'dashboard/api_management.html', context)


@dashboard_staff_required
def dashboard_api_access_grant(request):
    ajax = _is_ajax(request)
    if request.method == 'POST':
        form = GrantAPIAccessForm(request.POST)
        if form.is_valid():
            target_user = form.matched_user
            model_keys = ','.join(form.cleaned_data['model_keys'])
            access, _ = AIAPIAccess.objects.update_or_create(
                user=target_user,
                defaults={'model_keys': model_keys, 'granted_by': request.user},
            )
            success_message = (
                f"Updated {target_user.email or target_user.username}'s API access."
                if model_keys else
                f"Revoked {target_user.email or target_user.username}'s API access."
            )
            if ajax:
                if model_keys:
                    model_labels = {key: cfg['label'] for key, cfg in ai_chat.MODELS.items()}
                    access.model_label_list = [model_labels.get(key, key) for key in access.model_key_list]
                row_html = render_to_string(
                    'dashboard/_api_grant_row.html', {'grant': access}, request=request,
                ) if model_keys else ''
                return JsonResponse({
                    'status': 'ok',
                    'message': success_message,
                    'row_html': row_html,
                    'access_id': access.pk,
                    'revoked': not model_keys,
                })
            messages.success(request, success_message)
        else:
            if ajax:
                errors = {field: [str(e) for e in errs] for field, errs in form.errors.items()}
                return JsonResponse({'status': 'validation_error', 'errors': errors}, status=400)
            for field_errors in form.errors.values():
                for error in field_errors:
                    messages.error(request, error)
    elif ajax:
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    return redirect('dashboard_api_management')


@dashboard_staff_required
def dashboard_api_access_revoke(request, pk):
    ajax = _is_ajax(request)
    if request.method != 'POST':
        if ajax:
            return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
        return redirect('dashboard_api_management')

    access = get_object_or_404(AIAPIAccess, pk=pk)
    access.model_keys = ''
    access.granted_by = request.user
    access.save(update_fields=['model_keys', 'granted_by', 'updated_at'])
    success_message = f'Revoked API access for {access.user.email or access.user.username}.'
    if ajax:
        return JsonResponse({'status': 'ok', 'message': success_message})
    messages.success(request, success_message)
    return redirect('dashboard_api_management')


def _ai_activity_redirect(conversation_id):
    if conversation_id:
        try:
            return redirect('dashboard_ai_activity_detail', pk=int(conversation_id))
        except (TypeError, ValueError):
            pass
    return redirect('dashboard_ai_activity')


AI_ACTIVITY_DATE_FILTERS = {
    # Each maps to how far back from "now" to look, keyed off updated_at
    # (last active) rather than created_at — this page exists to spot a
    # spam pattern happening now, so "today" should mean "active today",
    # not just "the thread happened to start today".
    'today': lambda now: now.replace(hour=0, minute=0, second=0, microsecond=0),
    'yesterday': lambda now: (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
    'week': lambda now: now - timedelta(days=7),
    'month': lambda now: now - timedelta(days=30),
}

AI_ACTIVITY_SORTS = {
    'recent': '-updated_at',
    'most_messages': '-message_count',
    'oldest': 'created_at',
}


@dashboard_staff_required
def dashboard_ai_activity(request):
    """Every AI conversation — who sent it (account or guest IP) and how
    many messages — so staff can actually see a spam pattern (same IP or
    account hammering the chat) instead of it sitting invisibly behind the
    live rate limiter. See AIBlock / dashboard_ai_block for the
    accompanying block tools."""
    q = request.GET.get('q', '').strip()
    date_filter = request.GET.get('when', '').strip()
    sort = request.GET.get('sort', '').strip()
    if sort not in AI_ACTIVITY_SORTS:
        sort = 'recent'

    conversations = (
        AIConversation.objects.select_related('user')
        .annotate(message_count=Count('messages'))
        .order_by(AI_ACTIVITY_SORTS[sort])
    )
    if q:
        # Matched by conversation PK against a separate subquery, not a
        # direct messages__content__icontains join on this same queryset —
        # joining here would make the message_count annotation above count
        # only the messages that happen to match the search text instead of
        # the conversation's real total (a classic Django gotcha: filtering
        # and aggregating over the same reverse relation in one queryset).
        content_match_ids = AIMessage.objects.filter(content__icontains=q).values_list('conversation_id', flat=True)
        conversations = conversations.filter(
            Q(user__email__icontains=q) | Q(user__username__icontains=q) |
            Q(ip_address__icontains=q) | Q(title__icontains=q) |
            Q(pk__in=content_match_ids)
        )
    if date_filter == 'yesterday':
        now = timezone.localtime()
        start = AI_ACTIVITY_DATE_FILTERS['yesterday'](now)
        conversations = conversations.filter(updated_at__gte=start, updated_at__lt=start + timedelta(days=1))
    elif date_filter in AI_ACTIVITY_DATE_FILTERS:
        start = AI_ACTIVITY_DATE_FILTERS[date_filter](timezone.localtime())
        conversations = conversations.filter(updated_at__gte=start)
    else:
        date_filter = 'all'

    blocked_ips = set(AIBlock.objects.exclude(ip_address__isnull=True).values_list('ip_address', flat=True))
    blocked_user_ids = set(AIBlock.objects.exclude(user__isnull=True).values_list('user_id', flat=True))
    context = {
        'active': 'ai_activity',
        'conversations': conversations[:200],
        'q': q,
        'date_filter': date_filter,
        'sort': sort,
        'blocks': AIBlock.objects.select_related('user', 'created_by').order_by('-created_at'),
        'blocked_ips': blocked_ips,
        'blocked_user_ids': blocked_user_ids,
    }
    return render(request, 'dashboard/ai_activity.html', context)


@dashboard_staff_required
def dashboard_ai_activity_detail(request, pk):
    conversation = get_object_or_404(AIConversation.objects.select_related('user'), pk=pk)
    context = {
        'active': 'ai_activity',
        'conversation': conversation,
        'ai_messages': conversation.messages.order_by('created_at'),
        'is_ip_blocked': bool(conversation.ip_address) and AIBlock.objects.filter(ip_address=conversation.ip_address).exists(),
        'is_user_blocked': bool(conversation.user_id) and AIBlock.objects.filter(user_id=conversation.user_id).exists(),
    }
    return render(request, 'dashboard/ai_activity_detail.html', context)


@dashboard_staff_required
def dashboard_api_settings(request):
    """Per-feature controls that go beyond a plain key — see ai_app.api_controls:
    each ChatGPT model, image generation and web search get a key, a
    live connection test, an on/off checkbox and request counters."""
    from . import api_controls
    from .models import CustomAIModel
    message = None
    add_result = None
    if request.method == 'POST':
        if request.POST.get('action') == 'add_model':
            add_result = api_controls.add_model(request)
        else:
            panel_id, ok, text = api_controls.handle_post(request)
            if text:
                message = {'panel': panel_id, 'ok': ok, 'text': text}
    panels, removed_models = api_controls.split_removed(api_controls.panel_context())
    return render(request, 'dashboard/api_settings.html', {
        'active': 'api_settings',
        'panels': panels,
        'removed_models': removed_models,
        'message': message,
        'add_result': add_result,
        'api_types': CustomAIModel.API_TYPE_CHOICES,
        'model_kinds': CustomAIModel.KIND_CHOICES,
    })


def _api_settings_model_keys():
    """The models that have a panel on API Settings: the same set API Data lists."""
    from . import api_controls
    keys = set()
    for panel in api_controls.definitions():
        keys.update(panel.get('text_models') or [panel['model_key']])
    return keys & set(ai_chat.MODELS)


@dashboard_staff_required
def dashboard_api_data(request):
    """How many requests each model has served. The models are the ones that
    have a panel on API Settings, where the keys are managed."""
    text_counts = dict(
        AIMessage.objects.filter(role=AIMessage.ROLE_ASSISTANT)
        .exclude(model_key='').values('model_key')
        .annotate(n=Count('id')).values_list('model_key', 'n')
    )
    image_counts = dict(
        AIUserImage.objects.exclude(model_key='').values('model_key')
        .annotate(n=Count('id')).values_list('model_key', 'n')
    )
    request_counts = dict(text_counts)
    for key, count in image_counts.items():
        request_counts[key] = request_counts.get(key, 0) + count

    last_used = dict(
        AIMessage.objects.filter(role=AIMessage.ROLE_ASSISTANT)
        .exclude(model_key='').values('model_key')
        .annotate(last=Max('created_at')).values_list('model_key', 'last')
    )
    for key, when in (
        AIUserImage.objects.exclude(model_key='').values('model_key')
        .annotate(last=Max('created_at')).values_list('model_key', 'last')
    ):
        if key not in last_used or when > last_used[key]:
            last_used[key] = when

    total_requests = sum(request_counts.values())
    managed_model_keys = _api_settings_model_keys()

    model_q = request.GET.get('model_q', '').strip()
    model_scope = request.GET.get('model_scope', '').strip()
    if model_scope not in ('frontend', 'internal'):
        model_scope = 'all'
    model_sort = request.GET.get('model_sort', '').strip()
    if model_sort not in ('most_requests', 'least_requests', 'az', 'recent'):
        model_sort = 'most_requests'

    models = []
    for key, cfg in ai_chat.MODELS.items():
        if key not in managed_model_keys:
            continue
        in_frontend = key != 'vision' and not cfg.get('hidden_from_picker', False)
        if model_scope == 'frontend' and not in_frontend:
            continue
        if model_scope == 'internal' and in_frontend:
            continue
        if model_q and model_q.lower() not in cfg['label'].lower():
            continue
        requests_served = request_counts.get(key, 0)
        models.append({
            'key': key,
            'label': cfg['label'],
            'in_frontend': in_frontend,
            'requests': requests_served,
            'share_pct': round(requests_served * 100 / total_requests, 1) if total_requests else 0,
            'last_used': last_used.get(key),
        })
    model_sort_keys = {
        'most_requests': (lambda m: m['requests'], True),
        'least_requests': (lambda m: m['requests'], False),
        'az': (lambda m: m['label'].lower(), False),
        'recent': (lambda m: m['last_used'] or datetime.min.replace(tzinfo=dt_timezone.utc), True),
    }
    sort_fn, sort_reverse = model_sort_keys[model_sort]
    models.sort(key=sort_fn, reverse=sort_reverse)

    context = {
        'active': 'api_data',
        'models': models,
        'model_q': model_q,
        'model_scope': model_scope,
        'model_sort': model_sort,
        'frontend_model_count': sum(
            1 for key, cfg in ai_chat.MODELS.items()
            if key in managed_model_keys and key != 'vision' and not cfg.get('hidden_from_picker', False)
        ),
        'total_requests': total_requests,
    }
    return render(request, 'dashboard/api_data.html', context)


def _ai_reporter_scope(report):
    """Return the exact account/session identity represented by a report."""
    conversation = report.conversation
    user = report.user or (conversation.user if conversation and conversation.user_id else None)
    session_key = report.session_key or (
        conversation.session_key if conversation and not user else ''
    )
    return user, session_key


def _ai_report_system_health():
    """Scan objective storage/linkage gaps that report text alone cannot show."""
    signals = []
    generated_images = list(
        AIMessage.objects.filter(role=AIMessage.ROLE_ASSISTANT)
        .exclude(image_data='').values_list('image_data', flat=True)
    )
    missing_images = 0
    for image_value in generated_images:
        storage_name = _ai_storage_name_from_url(image_value)
        if not storage_name:
            continue
        try:
            if not default_storage.exists(storage_name):
                missing_images += 1
        except Exception:
            # Storage availability is unknown, not proof that the image is
            # missing. Do not turn a connectivity problem into a false alert.
            pass
    if missing_images:
        signals.append({
            'key': 'missing_generated_media',
            'label': 'Generated image files are missing',
            'severity': 'high',
            'count': missing_images,
            'evidence': (
                f'{missing_images} of {len(generated_images)} stored generated-image '
                'messages point to media files that are no longer available.'
            ),
            'fix': (
                'Move generated media to durable storage and include media in backup/restore. '
                'Keep the database snapshot added to new image reports as review evidence.'
            ),
        })

    unlinked_reports = AIReport.objects.filter(message__isnull=True).count()
    if unlinked_reports:
        signals.append({
            'key': 'unlinked_report_evidence',
            'label': 'Reports are missing a stable response link',
            'severity': 'medium',
            'count': unlinked_reports,
            'evidence': f'{unlinked_reports} reports are not linked to an assistant message.',
            'fix': (
                'Keep submitting the exact message id for saved replies and persist structured '
                'rows for provider errors so every future report has an authoritative target.'
            ),
        })

    return signals


@dashboard_staff_required
def dashboard_ai_reports(request):
    """Every 'report this reply' submission from the AI chat (see the
    Report button under each reply, and ai_report_submit) — who reported
    it (account, with the email/login to follow up with, or guest
    session), what reply they flagged, and why — grouped by open/resolved
    so staff can see what still needs attention."""
    q = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip().lower()
    if status_filter not in {choice for choice, _ in AIReport.STATUS_CHOICES}:
        status_filter = ''

    reports = AIReport.objects.select_related('user', 'conversation', 'message').order_by('-created_at')
    if q:
        reports = reports.filter(
            Q(user__email__icontains=q) | Q(user__username__icontains=q) |
            Q(session_key__icontains=q) | Q(explanation__icontains=q) |
            Q(reported_reply__icontains=q) | Q(user_prompt__icontains=q) |
            Q(user_document_name__icontains=q) | Q(model_key__icontains=q) |
            Q(conversation__title__icontains=q)
        )

    # Totals are counted before the status filter is applied, so clicking
    # "Resolved" doesn't make the other tiles read zero.
    matched = list(reports)
    open_count = sum(1 for r in matched if r.status == AIReport.STATUS_OPEN)
    resolved_count = sum(1 for r in matched if r.status == AIReport.STATUS_RESOLVED)
    total_count = len(matched)

    all_reports = [r for r in matched if not status_filter or r.status == status_filter]
    for report in all_reports:
        report.detected_issues = analyze_report(report)
        report.primary_issue = report.detected_issues[0] if report.detected_issues else None
    groups = [
        {'status': value, 'label': label, 'reports': [r for r in all_reports if r.status == value]}
        for value, label in AIReport.STATUS_CHOICES
        if not status_filter or value == status_filter
    ]
    return render(request, 'dashboard/ai_reports.html', {
        'active': 'ai_reports', 'reports': all_reports, 'groups': groups, 'q': q,
        'status_filter': status_filter,
        'report_stats': {
            'total': total_count,
            'open': open_count,
            'resolved': resolved_count,
            'resolved_percent': round(resolved_count * 100 / total_count) if total_count else 0,
        },
        'failure_insights': aggregate_report_issues(all_reports),
        'system_health': _ai_report_system_health(),
    })


@dashboard_staff_required
def dashboard_ai_report_detail(request, pk):
    report = get_object_or_404(
        AIReport.objects.select_related('user', 'conversation__user', 'message'), pk=pk,
    )
    owner_user, owner_session = _ai_reporter_scope(report)

    if owner_user:
        conversation_filter = Q(user=owner_user)
        report_filter = Q(user=owner_user) | Q(user__isnull=True, conversation__user=owner_user)
        reporter_label = owner_user.get_full_name() or owner_user.email or owner_user.username
    elif owner_session:
        conversation_filter = Q(user__isnull=True, session_key=owner_session)
        report_filter = Q(user__isnull=True, session_key=owner_session)
        reporter_label = f'Guest session {owner_session[:10]}…'
    elif report.conversation_id:
        conversation_filter = Q(pk=report.conversation_id)
        report_filter = Q(pk=report.pk)
        reporter_label = 'Guest (identity unavailable)'
    else:
        conversation_filter = Q(pk__in=[])
        report_filter = Q(pk=report.pk)
        reporter_label = 'Deleted/unknown reporter'

    message_queryset = AIMessage.objects.only(
        'id', 'conversation_id', 'role', 'content', 'image_data',
        'document_name', 'document_text', 'model_key', 'created_at',
    ).order_by('created_at', 'pk')
    conversations = list(
        AIConversation.objects.filter(conversation_filter)
        .prefetch_related(Prefetch('messages', queryset=message_queryset, to_attr='report_messages'))
        .order_by('-updated_at', '-pk')
    )
    reporter_reports = list(
        AIReport.objects.filter(report_filter)
        .select_related('conversation', 'message')
        .order_by('-created_at', '-pk')
    )
    for reporter_report in reporter_reports:
        reporter_report.detected_issues = analyze_report(reporter_report)

    return render(request, 'dashboard/ai_report_detail.html', {
        'active': 'ai_reports',
        'report': report,
        'reporter_label': reporter_label,
        'current_issues': analyze_report(report),
        'reporter_insights': aggregate_report_issues(reporter_reports),
        'reporter_reports': reporter_reports,
        'conversations': conversations,
        'conversation_count': len(conversations),
        'message_count': sum(len(conversation.report_messages) for conversation in conversations),
    })


@dashboard_staff_required
def dashboard_ai_report_status_update(request, pk):
    if request.method == 'POST':
        report = get_object_or_404(AIReport, pk=pk)
        report.status = AIReport.STATUS_RESOLVED if report.status == AIReport.STATUS_OPEN else AIReport.STATUS_OPEN
        report.save(update_fields=['status'])
    return redirect('dashboard_ai_reports')


@dashboard_staff_required
def dashboard_ai_report_delete(request, pk):
    if request.method == 'POST':
        get_object_or_404(AIReport, pk=pk).delete()
    return redirect('dashboard_ai_reports')


def _dashboard_ai_image_response(image_value):
    """Serve private chat/report images without embedding base64 in HTML."""
    if not image_value:
        return HttpResponse('Image not available.', status=404, content_type='text/plain')

    if image_value.startswith('data:image/'):
        match = re.match(r'^data:(image/(?:png|jpe?g|webp|gif));base64,(.+)$', image_value, re.I | re.S)
        if not match:
            return HttpResponse('Image not available.', status=404, content_type='text/plain')
        try:
            raw = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError, TypeError):
            return HttpResponse('Image not available.', status=404, content_type='text/plain')
        if len(raw) > AI_IMAGE_MAX_DATA_URI_CHARS:
            return HttpResponse('Image is too large to preview.', status=413, content_type='text/plain')
        response = HttpResponse(raw, content_type=match.group(1).lower())
    else:
        storage_name = _ai_storage_name_from_url(image_value)
        if storage_name:
            try:
                image_file = default_storage.open(storage_name, 'rb')
            except Exception:
                return HttpResponse('Image file is unavailable.', status=404, content_type='text/plain')
            response = FileResponse(
                image_file,
                content_type=mimetypes.guess_type(storage_name)[0] or 'application/octet-stream',
            )
        else:
            parsed = urlsplit(image_value)
            if parsed.scheme not in ('http', 'https'):
                return HttpResponse('Image not available.', status=404, content_type='text/plain')
            return redirect(image_value)

    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


AI_REPORT_CHAT_LIMIT = 300


@dashboard_staff_required
def dashboard_ai_report_chat(request, pk):
    """The whole conversation a report came from, for the eye button's
    pop-up on AI Reports. The reported reply is marked so it can be
    highlighted; long chats are trimmed to their newest messages."""
    report = get_object_or_404(AIReport.objects.select_related('conversation'), pk=pk)
    messages_out = []
    total = 0
    if report.conversation_id:
        queryset = AIMessage.objects.filter(conversation_id=report.conversation_id).only(
            'id', 'role', 'content', 'image_data', 'document_name', 'model_key', 'created_at', 'superseded',
        ).order_by('created_at', 'pk')
        total = queryset.count()
        rows = list(queryset[max(0, total - AI_REPORT_CHAT_LIMIT):])
        for m in rows:
            messages_out.append({
                'id': m.pk,
                'role': m.role,
                'text': m.content,
                'model': m.model_key,
                'document': m.document_name,
                'image': reverse('dashboard_ai_message_image', args=[m.pk]) if m.image_data else '',
                'time': timezone.localtime(m.created_at).strftime('%d %b %Y, %I:%M %p'),
                'superseded': m.superseded,
                'reported': m.pk == report.message_id,
            })
    conversation = report.conversation
    return JsonResponse({
        'status': 'ok',
        'title': (conversation.title if conversation else '') or 'Conversation',
        'total': total,
        'shown': len(messages_out),
        'messages': messages_out,
        'conversation_url': reverse('dashboard_ai_activity_detail', args=[conversation.pk]) if conversation else '',
        'review_url': reverse('dashboard_ai_report_detail', args=[report.pk]),
    })


@dashboard_staff_required
def dashboard_ai_message_image(request, pk):
    message = get_object_or_404(AIMessage.objects.only('image_data'), pk=pk)
    return _dashboard_ai_image_response(message.image_data)


@dashboard_staff_required
def dashboard_ai_report_image(request, pk, side):
    report = get_object_or_404(AIReport.objects.only('user_image', 'reported_image'), pk=pk)
    if side == 'user':
        image_value = report.user_image
    elif side == 'assistant':
        image_value = report.reported_image
    else:
        return HttpResponse('Image not available.', status=404, content_type='text/plain')
    return _dashboard_ai_image_response(image_value)


@dashboard_staff_required
def dashboard_ai_block(request):
    if request.method == 'POST':
        ip_address = request.POST.get('ip_address', '').strip()
        user_id = request.POST.get('user_id', '').strip()
        conversation_id = request.POST.get('conversation_id', '').strip()
        reason = request.POST.get('reason', '').strip()
        target_user = None
        if user_id:
            target_user = get_object_or_404(User, pk=user_id)
            if target_user.is_staff or target_user.is_superuser:
                messages.error(request, "Staff/admin accounts can't be blocked from here.")
                return _ai_activity_redirect(conversation_id)
        if not ip_address and not target_user:
            messages.error(request, 'Nothing to block — no IP or account given.')
        else:
            block, created = AIBlock.objects.get_or_create(
                ip_address=ip_address or None, user=target_user,
                defaults={'reason': reason, 'created_by': request.user},
            )
            label = (target_user.email or target_user.username) if target_user else ip_address
            if created:
                messages.success(request, f'Blocked {label} from Vidhyora AI.')
            else:
                messages.error(request, f'{label} is already blocked.')
        return _ai_activity_redirect(conversation_id)
    return redirect('dashboard_ai_activity')


@dashboard_staff_required
def dashboard_ai_unblock(request, pk):
    if request.method == 'POST':
        block = get_object_or_404(AIBlock, pk=pk)
        label = (block.user.email or block.user.username) if block.user_id else block.ip_address
        block.delete()
        messages.success(request, f'Unblocked {label}.')
    return _ai_activity_redirect(request.POST.get('conversation_id', '').strip())


















































DROPBOX_FIELD_LIMITS = (('app_key', 200), ('app_secret', 200), ('refresh_token', 400))


def _dropbox_sources(settings_obj):
    """For each credential: saved on this page, or taken from the server settings."""
    return {
        name: 'dashboard' if getattr(settings_obj, name) else ('server' if getattr(settings_obj, 'effective_' + name) else '')
        for name in ('app_key', 'app_secret', 'refresh_token')
    }




DROPBOX_FIELD_LABELS = {'app_key': 'App Key', 'app_secret': 'App Secret', 'refresh_token': 'Dropbox code or Refresh Token'}












# ── /home landing page and its forms ─────────────────────────────────────────
HOME_FORM_LIMIT = 10              # contact + newsletter posts per hour, per IP
HOME_FORM_WINDOW = 60 * 60
HOME_SERVICES = {
    'ai-subscription': 'AI Model Subscription', 'meta-ads': 'Meta Advertising',
    'google-ads': 'Google Advertising', 'youtube-ads': 'YouTube Advertising',
    'seo': 'Search Engine Optimization', 'smo': 'Social Media Optimization',
    'website': 'Custom Web Development', 'school-erp': 'School ERP System',
    'lms': 'Learning Management System', 'edu-website': 'Educational Portal',
}




def _home_form_allowed(request):
    """A light per-IP limit so the public forms can't be used to flood the
    dashboard. Returns the IP (may be None) or False when over the limit."""
    ip = _client_ip(request)
    key = f'home-form-{ip or "unknown"}'
    count = cache.get(key, 0)
    if count >= HOME_FORM_LIMIT:
        return False
    cache.set(key, count + 1, HOME_FORM_WINDOW)
    return ip


def _home_email(value):
    from django.core.validators import validate_email
    value = (value or '').strip()[:254]
    try:
        validate_email(value)
    except ValidationError:
        return ''
    return value












# ── AI Chat (/AI/) ──────────────────────────────────────────────────────────

AI_CHAT_RATE_LIMIT = 30           # messages
AI_CHAT_RATE_WINDOW = 10 * 60     # per 10 minutes, per IP
AI_CHAT_MAX_MESSAGE_CHARS = 16000
AI_CHAT_MAX_HISTORY = 20          # last 10 user+assistant turns — outer cap on how many rows are even fetched
AI_IMAGE_DAILY_LIMIT = 20         # successfully generated images, per account/session and local day
# Starts a streamed reply that is an error message rather than an answer.
# Invisible, and tells the page not to show that turn's web sources.
AI_REPLY_FAILED_MARK = '\u2063'
AI_VIDEO_DAILY_LIMIT = 5          # videos asked for, per account and local day (staff: no limit)
# Accounts exempt from AI_IMAGE_DAILY_LIMIT entirely, by login email —
# a manual allowlist rather than a plan/subscription flag, so it's just
# this one specific account rather than a purchasable tier.
AI_UNLIMITED_IMAGE_EMAILS = frozenset({'rnt@gmail.com'})
# A per-message-count cap alone doesn't bound size: an attached document can
# replay up to 15,000 chars on every later turn, so a handful of document
# turns can approach the model's real context window even within 20
# messages. This is a second, size-based trim applied on top of the count
# cap (see the clean_history loop below) — roughly 4 chars/token, so this
# budget leaves headroom under typical 32k+ context windows once the system
# prompt, late reminders, and reply tokens are also accounted for.
AI_CHAT_HISTORY_CHAR_BUDGET = 120000  # room for one multi-file turn (doc_extract.TOTAL_MAX_CHARS) plus recent chat
AI_CONVERSATION_TITLE_CHARS = 60
AI_CURRENT_CONVERSATION_SESSION_KEY = 'ai_current_conversation_id'
AI_GUEST_MESSAGE_LIMIT = 10       # free messages before a guest must log in/sign up
AI_FREE_MESSAGE_LIMIT = 20        # free messages for a logged-in, non-staff, unsubscribed account before Vidhyora AI requires the paid plan
AI_FREE_MODEL_KEYS = frozenset({'quick', 'code', ai_chat.FLUX_KLEIN_4B_MODEL_KEY})
# Accounts that already have full model access (staff or an active AI
# subscription) keep seeing the ChatGPT-branded picker entries as
# before. New/free accounts and signed-out guests never see them at all (not
# even locked/greyed-out) — they get the Vidhyora-branded lineup only.
AI_FULL_ACCESS_ONLY_MODEL_KEYS = frozenset({
    ai_chat.CHATGPT_56_MODEL_KEY, ai_chat.SOL_MODEL_KEY, ai_chat.TERRA_MODEL_KEY,
})
# ~1.5MB of raw image data as a base64 data: URI (~2M chars) — well under
# Django's default 2.5MB DATA_UPLOAD_MAX_MEMORY_SIZE for the whole request
# body, so an oversized image gets our own clean error instead of Django's
# generic one. The client also resizes/compresses before ever uploading.
AI_IMAGE_MAX_DATA_URI_CHARS = 2_000_000
AI_DOCUMENT_MODES = {'coding', 'details', 'multi'}
AI_DOCUMENT_CODE_MAX_OUTPUT_TOKENS = 6000
AI_GENERATED_FILE_MAX_OUTPUT_TOKENS = 10000


def _ai_voice_call_instruction(payload):
    """Reply style for the phone-call feature: the answer is read aloud, so it
    must be short, plain spoken language. The caller's name is cleaned to
    letters/spaces so it can't carry instructions into the prompt."""
    name = re.sub(r"[^\w .'-]", '', str(payload.get('caller_name') or ''), flags=re.UNICODE).strip()[:40]
    text = (
        "This is a live voice call: your reply will be read aloud by a speech engine. "
        "Answer like a friendly person on the phone, in one to three short spoken sentences. "
        "Use no Markdown, lists, tables, headings, code blocks, emojis or links. Do not read out "
        "URLs. If the answer needs a lot of detail, give the key point and offer to continue. "
        "Spell out symbols and units the way a person would say them."
    )
    if name:
        text += f" The caller's name is {name}; use it naturally now and then, not in every reply."
    return text


def _ai_document_instruction(mode, filename, truncated=False):
    """Return the server-controlled instruction for an attachment action."""
    if mode == 'coding':
        truncation_rule = (
            " The supplied source was truncated, so clearly say that a complete safe rewrite is not possible "
            "from this partial input; do not pretend omitted sections are unchanged."
            if truncated else
            " Return the COMPLETE updated file, including unchanged sections, in one fenced code block; "
            "never return only a patch, diff, excerpt, or isolated snippet."
        )
        return (
            f"The user selected Start coding for the attached file {filename!r}. Apply exactly the change "
            f"requested in their current message.{truncation_rule} Keep explanation brief and put the full "
            "updated file first. For a binary office file, return the complete revised textual content that "
            "can be represented in chat and do not claim to have generated a downloadable binary file."
        )
    if mode == 'multi':
        return (
            f"The user attached several files at once ({filename}). Their extracted text is provided in "
            "order, each under a '=== File n of N: name ===' heading, and long ones may be shortened. Answer the "
            "user's current message using all of them, say which file a point comes from, and compare or combine "
            "them when asked. Do not claim to have produced a downloadable file."
        )
    if mode == 'details':
        return (
            f"The user selected Show details for the attached file {filename!r}. Analyse and explain only: "
            "summarise its content and structure and point out relevant findings. Do not rewrite the file, "
            "do not output an updated version, and do not switch into coding unless the user asks in a later turn."
        )
    return None


AI_GENERATED_FILE_EXTENSIONS = frozenset({
    'txt', 'md', 'docx', 'pdf', 'xlsx', 'pptx',
    'html', 'htm', 'css', 'js', 'mjs', 'cjs', 'ts', 'tsx',
    'jsx', 'py', 'json', 'csv', 'xml', 'yaml', 'yml', 'sql', 'sh', 'ps1',
    'java', 'c', 'cpp', 'h', 'hpp', 'cs', 'php', 'rb', 'go', 'rs', 'swift',
    'kt', 'kts', 'vue', 'svelte', 'toml', 'ini',
})
# Longest label first within each format — 'microsoft excel' must win over the
# bare 'excel' that also appears in it, and _ai_generated_file_spec takes the
# first entry whose label is present.
AI_GENERATED_FILE_TYPE_EXTENSIONS = {
    # "create doc file for me" produced a .txt, because 'doc' was missing and
    # the lookup fell through to the default. Ordering matters here: the loop
    # takes the first label found, so 'word document' has to be tried before
    # the bare 'word', and 'docx' before 'doc'.
    'word document': 'docx', 'microsoft word': 'docx', 'ms word': 'docx',
    'word file': 'docx', 'word doc': 'docx', 'docx': 'docx',
    'word': 'docx', 'doc': 'docx', 'docs': 'docx',
    'pdf': 'pdf',
    'microsoft excel': 'xlsx', 'spreadsheet': 'xlsx', 'excel': 'xlsx',
    'xlsx': 'xlsx', 'xls': 'xlsx',
    'microsoft powerpoint': 'pptx', 'presentation': 'pptx',
    'powerpoint': 'pptx', 'slide deck': 'pptx', 'slides': 'pptx',
    'pptx': 'pptx', 'ppt': 'pptx',
    'markdown': 'md', 'html': 'html', 'css': 'css', 'javascript': 'js',
    'typescript': 'ts', 'python': 'py', 'json': 'json', 'csv': 'csv',
    'xml': 'xml', 'yaml': 'yaml', 'sql': 'sql', 'powershell': 'ps1',
    'java': 'java', 'php': 'php', 'ruby': 'rb', 'golang': 'go', 'rust': 'rs',
    'swift': 'swift', 'kotlin': 'kt', 'vue': 'vue', 'svelte': 'svelte',
}
# "give me a slide deck", "put this in a spreadsheet", "I need a PDF" are all
# the same ask as "create a ..." — the object pattern below is what actually
# keeps this from firing on ordinary chat, so the verb list can afford to
# cover how people really phrase it, including the Hindi/Hinglish imperatives
# this audience types.
_AI_FILE_ACTION_RE = re.compile(
    r"\b(?:create|generate|make|write|produce|prepare|build|export|save|"
    r"give|put|turn|convert|compile|draft|need|want|send|share|"
    r"banao|bana\s?do|bnao|bna\s?do|banaiye|chahiye|de\s?do|do\s?na)\b",
    re.IGNORECASE,
)
# Format names count as the object on their own. AIReport #33 ("... add logo
# I have attached in pdf") never said "file"/"document"/"downloadable", just
# the format — and the same is true of "make me a presentation" or "put this
# in an excel sheet", which named no generic file word at all.
_AI_FILE_OBJECT_RE = re.compile(
    r"\b(?:file|document|downloadable|download\s+link|pdf|"
    r"excel|spreadsheet|xlsx?|sheet|workbook|"
    r"powerpoint|presentation|slide\s?deck|slides?|pptx?)\b",
    re.IGNORECASE,
)
_AI_FILE_FORMAT_RE = re.compile(
    r"\b(?:pdf|docx?|excel|spreadsheet|xlsx?|workbook|powerpoint|presentation|slide\s?deck|pptx?)\b",
    re.IGNORECASE,
)
_AI_EXPLICIT_FILENAME_RE = re.compile(
    r"\b([A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.([A-Za-z0-9]{1,8}))\b",
    re.IGNORECASE,
)


def _ai_generated_file_spec(message):
    """Return a safe filename when the user explicitly asks for a file.

    This intent is handled server-side so every model-picker choice behaves
    consistently and none can pretend a download exists when it does not.
    """
    text = (message or '').strip()
    explicit = None
    for match in _AI_EXPLICIT_FILENAME_RE.finditer(text):
        if match.group(2).lower() in AI_GENERATED_FILE_EXTENSIONS:
            explicit = match.group(1)
            break
    if not _AI_FILE_ACTION_RE.search(text) or not (explicit or _AI_FILE_OBJECT_RE.search(text)):
        return None
    # "Create an image for my brand ... featuring File Upload, Web Search ..." is
    # a picture, not a file: the bare words file/document/sheet inside a picture
    # request only describe what the picture shows. Only a named format
    # (pdf, excel, slides...) or a real filename turns an image request into a file.
    if (
        not explicit
        and (ai_chat.is_image_generation_request(text) or ai_chat.is_design_request(text))
        and not ai_chat.is_image_capability_question(text)
        and not _AI_FILE_FORMAT_RE.search(text)
    ):
        return None

    if explicit:
        candidate = explicit
    else:
        lower_text = text.lower()
        extension = next(
            (ext for label, ext in AI_GENERATED_FILE_TYPE_EXTENSIONS.items() if re.search(rf"\b{re.escape(label)}\b", lower_text)),
            'txt',
        )
        candidate = f'generated.{extension}'

    # Basename-only, conservative characters, and an allow-listed extension:
    # the result can be downloaded but never becomes a filesystem path.
    candidate = Path(candidate.replace('\\', '/')).name
    candidate = re.sub(r'[^A-Za-z0-9_.-]+', '-', candidate).strip('.-')[:120]
    stem, dot, extension = candidate.rpartition('.')
    if not dot or extension.lower() not in AI_GENERATED_FILE_EXTENSIONS:
        candidate = 'generated.txt'
    return {'file_name': candidate}


# Shared by every generated document. The rules are about content (what a
# reader can trust) as much as layout: the writers in doc_pdf / doc_word turn
# this Markdown into headings, tables, callouts and charts, so structure the
# model writes becomes design, and invented figures become a visibly wrong
# document — hence the accuracy rule is spelled out, not implied.
_AI_ACCURACY_RULE = (
    "Use only facts the user gave you, facts present in any retrieved web context, and well-established "
    "general knowledge. Never invent statistics, names, dates, prices, quotes or sources: for a value you do "
    "not have, write 'Not available' instead of guessing."
)
_AI_CHART_FORMAT = (
    "(a line 'type:' of column, hbar, line, pie or donut; optional 'title:' and 'unit:' lines; then CSV "
    "rows: a header row with the label column first and one column per data series, then one row per "
    "category with plain numbers). Only add a chart for numbers the user supplied or the web context "
    "contains."
)
_AI_DOCUMENT_STYLE_GUIDE = (
    "Write it as a finished, professional document in Markdown: begin with one '# ' title line, then (for "
    "anything longer than a page) a one- or two-sentence summary paragraph; use '## ' sections, '### ' "
    "sub-sections, short paragraphs, '-' bullet lists and '1.' numbered steps; put any comparison, schedule, "
    "price list or other data in a Markdown table with a header row (plain numbers such as 15,000 or 12.5%, "
    "with the unit in the column title); use '> **Note:** …', '> **Tip:** …' or '> **Warning:** …' "
    "callouts sparingly for the few points that really matter; use **bold** for key terms. "
    + _AI_ACCURACY_RULE +
    " When facts or figures come from the web search context, finish with a '## Sources' section listing "
    "the real URLs from that context as Markdown links — never a URL that is not in it. If, and only if, "
    "the user asked for a chart or graph, add it under the relevant section as a fenced block tagged chart "
    + _AI_CHART_FORMAT +
    " If the document contains a code sample or a chart block, open the OUTER fence with four backticks so "
    "the inner fence does not end it."
)


def _ai_generated_file_instruction(filename):
    return _ai_generated_file_instruction_body(filename) + _AI_FILE_INSTRUCTION_SUFFIX


def _ai_generated_file_instruction_body(filename):
    if filename.lower().endswith(('.html', '.htm')):
        return (
            f"The user explicitly requested one real downloadable HTML file named {filename!r}. "
            "Return exactly one fenced html code block containing one COMPLETE, self-contained HTML5 "
            "document, starting with <!DOCTYPE html> and ending with </html>. Put ALL CSS inside a "
            "<style> element in that document and ALL JavaScript inside a <script> element before "
            "</body>. Never reference local files such as style.css, script.js, images, or other pages, "
            "and never emit separate CSS or JavaScript fences. External HTTPS font/icon/CDN links are "
            "allowed only when optional; the page must remain usable if they fail. Make the page "
            "responsive and functional, with no placeholders, TODOs, omitted sections, or ellipses. "
            "Keep the implementation concise enough to finish within the response budget: a smaller "
            "complete working website is better than a large unfinished one. Put no explanation outside "
            "the single fence. The application will validate it, create the file, and show the download."
        )
    if filename.lower().endswith('.docx'):
        return (
            f"The user explicitly requested a real downloadable Microsoft Word document named {filename!r}. "
            "Write the complete final document content now in exactly one fenced Markdown block, with no "
            "placeholders or explanatory text outside the block. " + _AI_DOCUMENT_STYLE_GUIDE +
            " Do not output XML, base64, a fake URL, or claim that you attached a file. The application will "
            "convert this content into a genuine DOCX file and attach the real download link."
        )
    if filename.lower().endswith('.pdf'):
        return (
            f"The user explicitly requested a real downloadable PDF named {filename!r}. Write the complete "
            "final document content now in exactly one fenced Markdown block, with no placeholders or "
            "explanatory text outside the block. " + _AI_DOCUMENT_STYLE_GUIDE +
            " Do not output base64, a fake URL, or claim that you attached a file. The application will "
            "render this content into a genuine PDF and attach the real download link."
        )
    if filename.lower().endswith('.xlsx'):
        return (
            f"The user explicitly requested a real downloadable Excel spreadsheet named {filename!r}. "
            "Write the complete final table now as CSV in exactly one fenced block: the first row is the "
            "column headers, every following row is one record, values separated by commas, and any value "
            "containing a comma wrapped in double quotes. Write real values, never placeholders. Write "
            "numbers plainly (15000, 12.5, 8%) with the unit in the column header, for example 'Price (₹)', "
            "so they can be summed and sorted; write dates as YYYY-MM-DD. If the data has totals, add a last "
            "row whose first cell is 'Total' and check every sum before writing it. Put no explanatory text, "
            "notes, or totals commentary outside the block. " + _AI_ACCURACY_RULE + " The application will "
            "convert this into a genuine XLSX workbook and attach the real download link."
        )
    if filename.lower().endswith('.pptx'):
        return (
            f"The user explicitly requested a real downloadable PowerPoint deck named {filename!r}. "
            "Write the complete final deck now in exactly one fenced Markdown block, using this exact "
            "structure: start each slide with '# ' followed by that slide's title on its own line, then "
            "the slide's bullet points as lines starting with '- ' (indent a sub-point by two spaces). The "
            "FIRST slide is the title slide: its '# ' line is the deck's title and its only content is one "
            "short '- ' subtitle line. Use one slide per idea, keep every bullet under about 15 words, and "
            "write real content with no placeholders. Put a comparison, schedule or other data in a Markdown "
            "table with a header row (it becomes a real table on its own slide). If the user asked for a "
            "chart, add one under that slide as a fenced block tagged chart " + _AI_CHART_FORMAT +
            " Add speaker notes only if the user asked for them, as a line starting 'Notes:' under the "
            "slide. " + _AI_ACCURACY_RULE + " Put no explanatory text outside the block. The application "
            "will convert this into a genuine PPTX deck and attach the real download link."
        )
    return (
        f"The user explicitly requested a downloadable file named {filename!r}. Create the complete final "
        "contents now. Return exactly one fenced code block containing the entire file and no placeholders, "
        "patch, diff, download URL, attachment claim, or explanatory text outside that block. Preserve any "
        "exact content the user supplied. The application, not you, will create and attach the real download link."
    )


# Appended to every file instruction above. Both halves are here because both
# failure modes were seen in the same session: a fabricated download link that
# rendered as a second, dead "Download generated.pdf" next to the real one and
# became the file's only contents, and a clarifying question that was written
# into a .txt and handed back to the user as their "document".
_AI_FILE_INSTRUCTION_SUFFIX = (
    " NEVER write a download link, URL, or file path of your own anywhere in this reply — not in Markdown, "
    "not as plain text — and never say the file is attached or ready. The application adds the one real "
    "link by itself, and any link you write is dead and appears as a confusing duplicate. "
    + _AI_ACCURACY_RULE + " "
    "If you genuinely cannot write the document because the user has not said what it should contain, "
    "reply with only your single clarifying question and NO fenced block at all; no file will then be "
    "created, which is correct. Otherwise write the full content and do not ask anything."
)


# The model is told not to invent a download link, and still sometimes writes
# its own Markdown one with a made-up token. Two things went wrong when it did:
# the reply showed two "Download generated.pdf" links (the model's, which is
# dead, and the real one the server appends), and because that link was the
# whole reply, it also became the file's entire contents — producing a PDF
# whose only text was "[Download generated.pdf](http://.../12345678-.../)".
_AI_FAKE_DOWNLOAD_LINK_RE = re.compile(
    # A Markdown link pointing at this app's own download endpoint...
    r'\[[^\]\r\n]{0,160}\]\(\s*[^)\r\n]*/ai/api/files/[^)\r\n]*\)|'
    # ...or any "[Download ...](...)" / "[Click here ...](...)" link at all,
    # since the server is the only thing allowed to offer one.
    r'\[\s*(?:download|click\s+here|get\s+(?:the\s+)?file)[^\]\r\n]{0,160}\]\([^)\r\n]*\)|'
    # ...or a bare URL to the endpoint, pasted without Markdown.
    r'https?://\S*/ai/api/files/\S*',
    re.IGNORECASE,
)

# "Tell me what it should contain and I'll create it" is a legitimate reply —
# but it is not document content, and turning it into a file gave the user a
# .txt containing the question they were just asked.
_AI_CLARIFYING_REPLY_RE = re.compile(
    r'\b(?:what (?:topic|content|information|details|data|kind)|'
    r'which (?:topic|format|details)|'
    r'could you (?:please )?(?:specify|clarify|tell me|provide|share)|'
    r'(?:please )?let me know what|'
    r'please (?:provide|specify|share|tell me|send)|'
    r'would you like (?:me to|the))\b',
    re.IGNORECASE,
)


class _NoGeneratedFileContent(Exception):
    """The model produced no document content, so no file should be attached."""


def _strip_fake_download_links(text):
    """Remove any download link the model invented for itself."""
    cleaned = _AI_FAKE_DOWNLOAD_LINK_RE.sub('', text or '')
    # Collapse the blank lines the removal leaves behind.
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


# Files whose body is a document: the model may write code samples or chart
# blocks *inside* it, so the outer fence has to be matched, not just closed at
# the first ``` that turns up.
_AI_DOCUMENT_SUFFIXES = ('.docx', '.pdf', '.pptx', '.md', '.txt')
_FENCE_LINE_RE = re.compile(r'^[ \t]*(`{3,}|~{3,})(.*)$')


def _first_fenced_block(text):
    """The content of the first fenced block, keeping fences written inside it.

    A four-backtick opener ends only at a bare fence of four or more. With the
    usual three, an inner fence that names a language (```chart, ```python)
    opens a nested block and the bare ``` after it closes that block; the outer
    block ends at the first bare fence left over. An unclosed fence — a reply
    cut off by the token limit — yields what was written so far. None when
    there is no fence at all.
    """
    lines = (text or '').split('\n')
    opener = None
    for start, line in enumerate(lines):
        opener = _FENCE_LINE_RE.match(line)
        if opener:
            break
    if not opener:
        return None
    marker = opener.group(1)
    depth = 0
    body = []
    for line in lines[start + 1:]:
        match = _FENCE_LINE_RE.match(line)
        if match and match.group(1)[0] == marker[0] and len(match.group(1)) >= len(marker):
            named = bool(match.group(2).strip())
            if len(marker) > 3:
                if not named:
                    return '\n'.join(body).rstrip('\r\n')
            elif named:
                depth += 1
            elif depth:
                depth -= 1
            else:
                return '\n'.join(body).rstrip('\r\n')
        body.append(line)
    return '\n'.join(body).rstrip('\r\n')


def _extract_ai_generated_file_content(reply, filename=''):
    """Extract the fenced payload the server prompt asked for.

    Returns '' when the model did not actually produce document content — the
    caller must then skip file creation entirely rather than write a file whose
    contents are a clarifying question or a fabricated link.
    """
    text = _strip_fake_download_links(reply)
    fences = list(re.finditer(r"```([^\r\n]*)\r?\n([\s\S]*?)```", text))
    is_html = str(filename or '').lower().endswith(('.html', '.htm'))
    if fences and is_html:
        html_fence = next(
            (
                match for match in fences
                if re.search(r'<!doctype\s+html|<html\b', match.group(2), re.I)
            ),
            None,
        )
        if not html_fence:
            return ''
        content = html_fence.group(2).rstrip('\r\n')

        # Recover a common model mistake: it writes a complete HTML shell that
        # links style.css/script.js, then emits those files as later fences even
        # though the user asked for one file. Inline those blocks deterministically.
        css_parts = [
            match.group(2).strip() for match in fences
            if match is not html_fence and match.group(1).strip().lower() in ('css', 'stylesheet')
        ]
        js_parts = [
            match.group(2).strip() for match in fences
            if match is not html_fence and match.group(1).strip().lower() in ('js', 'javascript')
        ]
        if css_parts:
            content = re.sub(
                r'<link\b(?=[^>]*\brel=["\']?stylesheet["\']?)(?=[^>]*\bhref=["\'](?!https?:|//|data:)[^"\']*\.css(?:\?[^"\']*)?["\'])[^>]*>',
                '', content, flags=re.I,
            )
            styles = '<style>\n' + '\n\n'.join(css_parts) + '\n</style>\n'
            content = re.sub(r'</head\s*>', styles + '</head>', content, count=1, flags=re.I)
        if js_parts:
            content = re.sub(
                r'<script\b(?=[^>]*\bsrc=["\'](?!https?:|//|data:)[^"\']*\.js(?:\?[^"\']*)?["\'])[^>]*>\s*</script\s*>',
                '', content, flags=re.I,
            )
            scripts = '<script>\n' + '\n\n'.join(js_parts) + '\n</script>\n'
            content = re.sub(r'</body\s*>', scripts + '</body>', content, count=1, flags=re.I)

        # Never advertise a visibly truncated or multi-file-dependent page as
        # a completed download. The model can be retried instead.
        if not (
            re.search(r'<!doctype\s+html', content, re.I)
            and re.search(r'<html\b', content, re.I)
            and re.search(r'</body\s*>', content, re.I)
            and re.search(r'</html\s*>\s*$', content, re.I)
        ):
            return ''
        if re.search(
            r'<link\b[^>]*\bhref=["\'](?!https?:|//|data:)[^"\']*\.css(?:\?[^"\']*)?["\']|'
            r'<script\b[^>]*\bsrc=["\'](?!https?:|//|data:)[^"\']*\.js(?:\?[^"\']*)?["\']',
            content, re.I,
        ):
            return ''
        return _strip_fake_download_links(content)
    if is_html:
        # A complete raw HTML document is still usable if the model omitted
        # Markdown fences. An unclosed fence/document is a truncated attempt.
        content = text.strip()
        if not (
            re.search(r'<!doctype\s+html', content, re.I)
            and re.search(r'<html\b', content, re.I)
            and re.search(r'</body\s*>', content, re.I)
            and re.search(r'</html\s*>\s*$', content, re.I)
        ):
            return ''
        if re.search(
            r'<link\b[^>]*\bhref=["\'](?!https?:|//|data:)[^"\']*\.css(?:\?[^"\']*)?["\']|'
            r'<script\b[^>]*\bsrc=["\'](?!https?:|//|data:)[^"\']*\.js(?:\?[^"\']*)?["\']',
            content, re.I,
        ):
            return ''
        return _strip_fake_download_links(content)
    if str(filename or '').lower().endswith(_AI_DOCUMENT_SUFFIXES):
        nested = _first_fenced_block(text)
        if nested is not None:
            return _strip_fake_download_links(nested)
    if fences:
        return _strip_fake_download_links(fences[0].group(2).rstrip('\r\n'))
    if text.startswith('```') and text.endswith('```'):
        text = text[3:-3]
        text = re.sub(r'^[A-Za-z0-9_+.-]+\r?\n', '', text, count=1)
        return text.strip()

    # No fenced block: the model ignored the instruction. Accept the reply as
    # the document only when it actually reads like one — not when it is a
    # question back to the user, and not when nothing survived link-stripping.
    text = text.strip()
    if not text:
        return ''
    if _AI_CLARIFYING_REPLY_RE.search(text) or text.rstrip().endswith('?'):
        return ''
    return text


def _ai_office_file(build, *args, **meta):
    """Run one document writer; a missing library or a file it cannot build is
    reported as RuntimeError, which the download view turns into "temporarily
    unavailable" rather than a server error."""
    try:
        return build(*args, **meta)
    except file_convert.ConvertError as exc:
        raise RuntimeError(str(exc)) from exc


def _ai_word_document_bytes(content, **meta):
    """Convert model-produced document text into a designed DOCX package
    (headings, lists, tables, callouts, charts — see doc_word)."""
    return _ai_office_file(file_convert.text_to_docx_bytes, content, **meta)


def _ai_pdf_bytes(content, **meta):
    """Convert model-produced document text into a designed, paginated PDF.

    Both this and the file-conversion endpoint render through the same writer
    (doc_pdf), so an AI-generated PDF and a converted one share one typography.
    ConvertError is re-raised as RuntimeError because that is what the download
    view below catches for "this format is temporarily unavailable".
    """
    return _ai_office_file(file_convert.text_to_pdf_bytes, content, **meta)


def _ai_excel_bytes(content, **meta):
    """Convert the model's CSV output into a styled XLSX workbook.

    CSV is the intermediate on purpose (see _ai_generated_file_instruction):
    it is the tabular format models emit most reliably, and csv.reader handles
    the quoting rules so a value containing a comma survives intact.
    """
    rows = list(csv.reader(io.StringIO(content or '')))
    return _ai_office_file(file_convert.rows_to_xlsx_bytes, rows, **meta)


def _ai_powerpoint_bytes(content, **meta):
    """Convert the model's '# title / - bullet' Markdown into a designed 16:9
    PPTX deck (title slide, themed slides, native tables and charts — see
    doc_slides)."""
    try:
        from . import doc_slides
    except ImportError as exc:
        raise RuntimeError('PowerPoint generation is temporarily unavailable.') from exc
    return _ai_office_file(doc_slides.render_pptx, content, **meta)


def _ai_user_context(user):
    """A compact snapshot of the logged-in user's own remembered name/
    location — nothing about the store (no cart, wallet, or order data;
    the AI chat no longer has any access to that) — so the assistant can
    address a returning user naturally instead of asking who they are
    every conversation. Always scoped to `user` (the authenticated
    request.user)."""
    lines = [f"Logged in as: {user.first_name or user.username}."]

    profile = getattr(user, 'store_profile', None)
    if profile:
        # Name/location the user gave the AI chat during onboarding (see
        # ai_chat_send's onboarding block) — surfaced here so a returning
        # user is actually remembered/addressed naturally instead of the
        # assistant asking who they are again every conversation.
        if profile.ai_display_name:
            lines.append(f"This user previously told the assistant their name is {profile.ai_display_name}.")
        if profile.ai_location:
            lines.append(f"This user previously told the assistant they're located in {profile.ai_location}.")

    return '\n'.join(lines)


def _ai_owner_filter(request):
    """Logged-in users own conversations by user FK; guests own them by the
    session_key their browser already carries (same session used for the
    anonymous cart) — that's what lets a guest's chat survive a page reload
    and then get handed to their account the moment they log in. Deliberately
    does NOT create a session for a guest that doesn't have one yet — a plain
    page view or list/read call has no conversation to attach to anyway, and
    forcing a new database-backed session row on every cookie-less visit is
    an easy way to grow django_session unbounded. Only ai_chat_send (which
    actually needs a stable key to save a message under) creates one."""
    if request.user.is_authenticated:
        return Q(user=request.user)
    session_key = request.session.session_key
    if not session_key:
        return Q(pk__in=[])
    return Q(user__isnull=True, session_key=session_key)


def ai_generated_file_download(request, token):
    """Download one AI-created file, only for the account/session that made it."""
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)

    files = AIGeneratedFile.objects.filter(token=token)
    if request.user.is_authenticated:
        files = files.filter(user=request.user)
    else:
        session_key = request.session.session_key
        if not session_key:
            files = files.none()
        else:
            files = files.filter(user__isnull=True, session_key=session_key)
    generated_file = get_object_or_404(files)

    name = generated_file.file_name.lower()
    if doc_image.is_image_file(generated_file.content):
        extension = name.rsplit('.', 1)[-1]
        try:
            payload = doc_image.render(name, generated_file.content)
        except Exception:
            logger.exception('Could not build the image file %s', generated_file.pk)
            return JsonResponse({
                'status': 'error',
                'detail': "We couldn't build this file. Please ask for it again.",
            }, status=503)
        response = HttpResponse(payload, content_type=doc_image.CONTENT_TYPES.get(extension, 'application/octet-stream'))
        response['Content-Disposition'] = f'attachment; filename="{generated_file.file_name}"'
        response['X-Content-Type-Options'] = 'nosniff'
        response['Cache-Control'] = 'private, no-store'
        return response

    # The day the file was made goes into the document itself (title band,
    # footer), so a re-download is identical. Generated files carry no brand
    # name: they are the user's documents.
    meta = {'brand': '', 'created': generated_file.created_at}
    builders = {
        '.docx': (_ai_word_document_bytes,
                  'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
        '.pdf': (_ai_pdf_bytes, 'application/pdf'),
        '.xlsx': (_ai_excel_bytes,
                  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
        '.pptx': (_ai_powerpoint_bytes,
                  'application/vnd.openxmlformats-officedocument.presentationml.presentation'),
    }
    builder = next((entry for suffix, entry in builders.items() if name.endswith(suffix)), None)
    if builder:
        try:
            payload = builder[0](generated_file.content, **meta)
        except RuntimeError as exc:
            return JsonResponse({'status': 'error', 'detail': str(exc)}, status=503)
        except Exception:
            # A document the writers cannot lay out must never be a server
            # error: say so, and the person can ask for the file again.
            logger.exception('Could not build the AI-generated file %s', generated_file.pk)
            return JsonResponse({
                'status': 'error',
                'detail': "We couldn't build this file. Please ask for it again.",
            }, status=503)
        content_type = builder[1]
    else:
        payload = doc_text.finish_text_file(generated_file.file_name, generated_file.content, **meta)
        content_type = f"{mimetypes.guess_type(generated_file.file_name)[0] or 'text/plain'}; charset=utf-8"
    response = HttpResponse(payload, content_type=content_type)
    response['Content-Disposition'] = f'attachment; filename="{generated_file.file_name}"'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, no-store'
    return response


def _ai_notes_snapshot(request):
    """Single database snapshot used by page boot, API responses and chat."""
    notes = list(
        AINote.objects.filter(_ai_owner_filter(request))
        .values('id', 'heading', 'content', 'created_at')
        .order_by('-created_at', '-pk')[:200]
    )
    for note in notes:
        note['created_at'] = timezone.localtime(note['created_at']).isoformat()
    return notes


def ai_manifest(request):
    pwa = AISiteSettings.get_solo()
    version = int(pwa.updated_at.timestamp() * 1_000_000)
    if pwa.icon:
        icon_192 = request.build_absolute_uri(
            f"{reverse('ai_pwa_icon', args=[192])}?v={version}"
        )
        icon_512 = request.build_absolute_uri(
            f"{reverse('ai_pwa_icon', args=[512])}?v={version}"
        )
    else:
        icon_192 = request.build_absolute_uri(static_url('ai-icon-192.png'))
        icon_512 = request.build_absolute_uri(static_url('ai-icon-512.png'))
    manifest = {
        'id': '/ai/',
        'name': pwa.app_name or 'EduTrellis AI',
        'short_name': pwa.short_name or 'EduTrellis AI',
        'description': pwa.description or 'Chat with EduTrellis AI.',
        'start_url': '/ai/',
        'scope': '/ai/',
        'display': 'standalone',
        'background_color': pwa.background_color or '#ffffff',
        'theme_color': pwa.theme_color or '#e8001e',
        'icons': [
            {'src': icon_192, 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': icon_512, 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': icon_512, 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
        ],
    }
    response = JsonResponse(manifest, content_type='application/manifest+json')
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


def ai_pwa_icon(request, size):
    """Serve the uploaded admin icon at real manifest-required dimensions."""
    if size not in (192, 512):
        return JsonResponse({'status': 'error', 'detail': 'Invalid icon size.'}, status=404)
    pwa = AISiteSettings.get_solo()
    if not pwa.icon:
        return JsonResponse({'status': 'error', 'detail': 'No PWA icon configured.'}, status=404)
    try:
        pwa.icon.open('rb')
        with Image.open(pwa.icon) as source:
            source = ImageOps.exif_transpose(source).convert('RGBA')
            rendered = ImageOps.fit(
                source, (size, size), method=Image.Resampling.LANCZOS,
            )
            output = io.BytesIO()
            rendered.save(output, format='PNG', optimize=True)
        pwa.icon.close()
    except Exception:
        logger.exception('Could not render configured PWA icon')
        return JsonResponse({'status': 'error', 'detail': 'Could not load the PWA icon.'}, status=404)
    response = HttpResponse(output.getvalue(), content_type='image/png')
    response['Cache-Control'] = 'public, max-age=3600'
    return response


def ai_page(request):
    # Refreshes MODELS' brand-suffixed labels (Ultra/Quick/Code/Vision) from
    # the dashboard setting before model_labels/models below read them, so a
    # rename shows up on the very next page load with no restart needed.
    ai_chat.get_ai_brand_name()
    pwa = AISiteSettings.get_solo()
    pwa_version = int(pwa.updated_at.timestamp() * 1_000_000)
    conversations = list(
        AIConversation.objects.filter(_ai_owner_filter(request))
        .values('id', 'title', 'updated_at').order_by('-updated_at')[:100]
    )
    for c in conversations:
        c['updated_at'] = timezone.localtime(c['updated_at']).isoformat()

    notes = _ai_notes_snapshot(request)

    # Browser storage can be unavailable or cleared. Remember the last chat
    # the server actually opened/sent to as a safe refresh/login fallback.
    # Always validate it against this request's owner before exposing it.
    conversation_ids = {c['id'] for c in conversations}
    try:
        resume_conversation_id = int(request.session.get(AI_CURRENT_CONVERSATION_SESSION_KEY) or 0)
    except (TypeError, ValueError):
        resume_conversation_id = 0
    if resume_conversation_id not in conversation_ids:
        resume_conversation_id = conversations[0]['id'] if conversations else None
    if resume_conversation_id:
        request.session[AI_CURRENT_CONVERSATION_SESSION_KEY] = resume_conversation_id
    model_labels = {key: cfg['label'] for key, cfg in ai_chat.MODELS.items()}

    ai_is_staff = _ai_has_admin_access(request.user)
    ai_subscribed = False
    ai_free_used = 0
    if request.user.is_authenticated and not ai_is_staff:
        profile, _ = StoreProfile.objects.get_or_create(user=request.user)
        ai_subscribed = profile.is_ai_subscribed
        ai_free_used = profile.ai_free_messages_used
    ai_full_model_access = bool(ai_is_staff or ai_subscribed)
    # Sol is the picker's default selection (a fixed flagship endpoint, not
    # the ChatGPT-56 auto-router) — kept separate from ai_chat.DEFAULT_MODEL_KEY,
    # which is the router model used as the internal MODELS-lookup fallback.
    ai_default_model = (
        ai_chat.SOL_MODEL_KEY
        if ai_full_model_access and ai_chat.is_model_enabled(ai_chat.SOL_MODEL_KEY) else 'quick'
    )
    models = [
        {
            'key': key,
            'label': cfg['label'],
            'description': cfg['description'],
            'locked': not ai_full_model_access and key not in AI_FREE_MODEL_KEYS,
            'power': not cfg.get('image_generation'),
        }
        for key, cfg in ai_chat.MODELS.items()
        if key != 'vision' and not cfg.get('hidden_from_picker', False)
        and (ai_full_model_access or key not in AI_FULL_ACCESS_ONLY_MODEL_KEYS)
        and ai_chat.is_model_enabled(key)
    ]

    return render(request, 'ai_chat.html', {
        'ai_authenticated': request.user.is_authenticated,
        'ai_user': _user_payload(request.user) if request.user.is_authenticated else None,
        'ai_conversations': conversations,
        'ai_notes': notes,
        'ai_resume_conversation_id': resume_conversation_id,
        'ai_guest_limit': AI_GUEST_MESSAGE_LIMIT,
        'ai_guest_used': 0 if request.user.is_authenticated else min(
            AI_GUEST_MESSAGE_LIMIT,
            max(request.session.get('ai_guest_msg_count', 0), _ip_free_messages_used(_client_ip(request))),
        ),
        'ai_is_staff': ai_is_staff,
        'ai_is_superuser': bool(request.user.is_authenticated and request.user.is_superuser),
        'ai_subscribed': ai_subscribed,
        'ai_free_limit': AI_FREE_MESSAGE_LIMIT,
        'ai_free_used': ai_free_used,
        'ai_purchase_url': _ai_purchase_url(),
        'ai_models': models,
        'ai_languages': [
            {
                'code': code, 'label': label, 'short': short, 'speech': speech, 'group': group,
                'search': f'{label} {code}'.lower(),
            }
            for code, label, short, speech, group in ai_chat.LANGUAGE_MENU
        ],
        # Drives the homepage's ChatGPT 5.6 call-out: whether to say "it's
        # already selected" or "here's how to unlock it". Without this the
        # empty state would tell a free user a model is selected that the
        # picker below is actually showing as Vidhyora Quick.
        'ai_full_model_access': ai_full_model_access,
        'ai_image_styles': image_styles.gallery(),
        'ai_default_model': ai_default_model,
        'ai_default_model_label': model_labels[ai_default_model],
        'ai_model_labels': model_labels,
        'ai_github_oauth_available': bool(settings.GITHUB_OAUTH_CLIENT_ID),
        'show_location_prompt': _location_prompt_needed(request.user),
        'show_profile_wizard': _profile_wizard_needed(request.user),
        'ai_pwa': pwa,
        'ai_pwa_ready': pwa.ready,
        'ai_pwa_version': pwa_version,
    })


def _client_ip(request):
    return ratelimit.client_ip(request)


def _format_wait_time(seconds):
    """'42 seconds' under a minute, '3 minutes' under an hour, otherwise
    '6 hours' — each rounded up to the next whole unit, so the number shown
    is never an underestimate of how long is actually left."""
    seconds = max(1, int(seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    if seconds < 3600:
        minutes = (seconds + 59) // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = (seconds + 3599) // 3600
    return f"{hours} hour{'s' if hours != 1 else ''}"


def _ai_has_admin_access(user):
    """Whether an authenticated user has backend-managed unlimited access.

    Keep this in one place so a superuser whose ``is_staff`` flag was not
    also selected cannot be shown as a full admin in AI Management and then
    be treated as a free customer by the chat endpoints.
    """
    return bool(
        user.is_authenticated and (user.is_staff or user.is_superuser)
    )


def _ai_has_full_model_access(user):
    if not user.is_authenticated:
        return False
    if _ai_has_admin_access(user):
        return True
    profile, _ = StoreProfile.objects.get_or_create(user=user)
    return profile.is_ai_subscribed


def _ai_purchase_url():
    # Premium access is managed by staff from the AI Management dashboard.
    return 'mailto:support@edutrellis.in?subject=EduTrellis%20AI%20Premium'


def _ip_free_messages_used(ip):
    """Total free (guest + signed-in-but-unsubscribed) Vidhyora AI
    messages ever sent from this IP, across every guest session and every
    account that's ever chatted from it. Session/account counters alone
    (request.session['ai_guest_msg_count'], StoreProfile.ai_free_messages_
    used) reset the moment someone opens an incognito window or signs up a
    throwaway second account — this is what actually survives that, since
    it's derived from the saved messages themselves, not a counter. Deliber-
    ately counts every non-staff message tied to this IP regardless of
    whether the sender was subscribed at the time — an over-count is the
    safe direction for an anti-abuse cap."""
    if not ip or ip == 'unknown':
        return 0
    return AIMessage.objects.filter(
        role=AIMessage.ROLE_USER, conversation__ip_address=ip,
    ).exclude(
        Q(conversation__user__is_staff=True)
        | Q(conversation__user__is_superuser=True)
    ).count()


def _ai_profile_gate(user, ip=None):
    """Returns None if `user` (already known to be authenticated) has
    unrestricted Vidhyora AI access — staff, or an active paid
    subscription — else the 403 JSON payload to send back once they've used
    their free allotment. Staff never even touch the StoreProfile row here,
    which is what gives every staff account (including admin@gmail.com)
    unlimited messages and every model with no separate per-account
    allowlist to maintain."""
    if _ai_has_admin_access(user):
        return None
    profile, _ = StoreProfile.objects.get_or_create(user=user)
    if profile.is_ai_subscribed:
        return None
    # If this account has already exhausted its allowance, the potentially
    # large IP message-count query cannot change the gate's response.
    if profile.ai_free_messages_used >= AI_FREE_MESSAGE_LIMIT or (
        bool(ip) and _ip_free_messages_used(ip) >= (AI_GUEST_MESSAGE_LIMIT + AI_FREE_MESSAGE_LIMIT)
    ):
        return {
            'status': 'subscription_required',
            'detail': (
                f"You've used all {AI_FREE_MESSAGE_LIMIT} free Vidhyora AI messages. "
                "Subscribe for Rs 99/month for unlimited messages on every model."
            ),
            'purchase_url': _ai_purchase_url(),
        }
    return None


def _ai_note_heading(text):
    """First line of the noted text, trimmed to a short heading — same idea
    as AIConversation's title-from-first-message, just capped shorter since
    this is meant to read like a Google Keep card title."""
    first_line = text.strip().splitlines()[0].strip() if text.strip() else ''
    return (first_line[:60] + '…') if len(first_line) > 60 else first_line


AI_SELECTED_NOTES_SESSION_KEY = 'ai_selected_note_ids'
AI_PENDING_NOTE_EDITS_SESSION_KEY = 'ai_pending_note_edits'


def _ai_safe_note_text(text):
    """Fix only a small set of unambiguous spelling mistakes."""
    corrections = {
        'teh': 'the', 'tomorow': 'tomorrow', 'tommorow': 'tomorrow',
        'meting': 'meeting', 'remeber': 'remember',
    }
    return re.sub(
        r"\b(?:teh|tomorow|tommorow|meting|remeber)\b",
        lambda match: corrections[match.group(0).lower()],
        (text or '').strip(), flags=re.IGNORECASE,
    )


def _ai_set_selected_note(request, conversation, note_id):
    selected = dict(request.session.get(AI_SELECTED_NOTES_SESSION_KEY, {}))
    selected[str(conversation.pk)] = int(note_id)
    request.session[AI_SELECTED_NOTES_SESSION_KEY] = selected


def _ai_get_selected_note(request, conversation):
    selected = request.session.get(AI_SELECTED_NOTES_SESSION_KEY, {})
    try:
        note_id = int(selected.get(str(conversation.pk)))
    except (TypeError, ValueError, AttributeError):
        return None
    return AINote.objects.filter(_ai_owner_filter(request), pk=note_id).first()


def _ai_set_pending_note_edit(request, conversation, payload=None):
    pending = dict(request.session.get(AI_PENDING_NOTE_EDITS_SESSION_KEY, {}))
    key = str(conversation.pk)
    if payload:
        pending[key] = payload
    else:
        pending.pop(key, None)
    request.session[AI_PENDING_NOTE_EDITS_SESSION_KEY] = pending


def _ai_get_pending_note_edit(request, conversation):
    pending = request.session.get(AI_PENDING_NOTE_EDITS_SESSION_KEY, {})
    return pending.get(str(conversation.pk)) if isinstance(pending, dict) else None


def _ai_note_action_reply(request, conversation, confirmation, extra_headers=None):
    """Shared tail for every My Notes action (save/show/delete/edit) —
    saves `confirmation` as the assistant's turn and returns it on the same
    StreamingHttpResponse contract ai_chat_send's real model-call path uses,
    so the frontend's existing send()/pump() handling needs no
    special-casing beyond reading the extra X-Notes-Changed header (see
    refreshNotes() in ai.html). No AI model call, no free-message quota
    spent, for any of these."""
    AIMessage.objects.create(
        conversation=conversation, role=AIMessage.ROLE_ASSISTANT,
        content=confirmation, model_key='note',
    )
    conversation.updated_at = timezone.now()
    conversation.save(update_fields=['updated_at'])

    def event_stream():
        yield confirmation

    response = StreamingHttpResponse(event_stream(), content_type='text/plain; charset=utf-8')
    response['Cache-Control'] = 'private, no-cache, no-store, no-transform'
    response['X-Accel-Buffering'] = 'no'
    response['X-Conversation-Id'] = str(conversation.id)
    response['X-Model-Key'] = 'note'
    response['X-Request-Category'] = 'note'
    response['X-Sumudrika'] = ''
    response['X-Jagu'] = ''
    response['X-Persona-End'] = ''
    for key, value in (extra_headers or {}).items():
        response[key] = value
    return response


def _ai_save_note_response(request, conversation, message):
    """'Take this note' / 'note it down' / 'save details...' (see
    request_router.is_note_intent) — saves a new AINote.

    Notes whatever extra text came with the trigger phrase itself ('note
    down: buy milk' -> 'buy milk') when there is any; otherwise falls back
    to the assistant's last reply in this conversation, since that's what a
    bare 'note it down' naturally refers to — and to the user's own message
    as a last resort, for a brand new conversation with nothing to look
    back at yet.
    """
    remainder = request_router.strip_note_intent(message).strip(' :-—')
    if not remainder:
        return _ai_note_action_reply(request, conversation, 'What would you like the note to say?')
    note_content = _ai_safe_note_text(remainder)
    heading = _ai_note_heading(note_content)

    note = AINote.objects.create(
        user=request.user if request.user.is_authenticated else None,
        session_key='' if request.user.is_authenticated else (request.session.session_key or ''),
        conversation=conversation, heading=heading, content=note_content,
    )

    note = AINote.objects.filter(_ai_owner_filter(request), pk=note.pk).first()
    if not note:
        return _ai_note_action_reply(request, conversation, 'I couldn\'t save that note. Please try again.')
    _ai_notes_snapshot(request)
    _ai_set_selected_note(request, conversation, note.pk)

    confirmation = "Saved to your notes. The saved note is:\n\n" + _ai_note_final_text(note)
    return _ai_note_action_reply(request, conversation, confirmation, {
        'X-Notes-Changed': '1', 'X-Note-Id': str(note.id),
    })


def _ai_show_notes_response(request, conversation):
    """'Show my notes' / 'what notes do I have' (see
    request_router.is_show_notes_intent) — lists the user's saved notes
    right in the chat, newest first."""
    notes = _ai_notes_snapshot(request)
    if not notes:
        confirmation = "You don’t have any saved notes."
    else:
        lines = [
            (f"{index}. **{note['content']}**" if (note['heading'] or '').strip() == note['content'].strip()
             else f"{index}. **{note['heading'] or 'Untitled note'}**\n{note['content']}")
            for index, note in enumerate(notes, 1)
        ]
        confirmation = "Here are your saved notes:\n\n" + '\n\n'.join(lines)
    return _ai_note_action_reply(request, conversation, confirmation)


def _ai_matching_notes(request, target):
    database_id = re.fullmatch(r"id\s*#?\s*(\d+)", (target or '').strip(), re.IGNORECASE)
    if database_id:
        note = AINote.objects.filter(_ai_owner_filter(request), pk=int(database_id.group(1))).first()
        return [note] if note else []
    ordinal = re.fullmatch(r"(?:number\s*|#\s*)?(\d+)(?:st|nd|rd|th)?", (target or '').strip(), re.IGNORECASE)
    if ordinal:
        position = int(ordinal.group(1))
        if position < 1:
            return []
        note = AINote.objects.filter(_ai_owner_filter(request)).order_by('-created_at', '-pk')[position - 1:position].first()
        return [note] if note else []
    return list(AINote.objects.filter(_ai_owner_filter(request)).filter(
        Q(heading__icontains=target) | Q(content__icontains=target),
    )[:6])


def _ai_read_note_response(request, conversation, target):
    """Open one owned note by its displayed number or a unique title phrase."""
    if not target:
        return _ai_note_action_reply(request, conversation, 'Tell me which note to open, e.g. "open note 1".')
    matches = _ai_matching_notes(request, target)
    if not matches:
        return _ai_note_action_reply(request, conversation, f'I couldn\'t find a note matching "{target}".')
    if len(matches) > 1:
        listing = '\n'.join(f"- {n.heading or 'Untitled note'}" for n in matches)
        return _ai_note_action_reply(request, conversation, f"I found more than one matching note:\n\n{listing}\n\nUse its number from \"show my notes\" or a more specific title.")
    note = matches[0]
    _ai_set_selected_note(request, conversation, note.pk)
    created = timezone.localtime(note.created_at).strftime('%b %d, %Y at %I:%M %p')
    return _ai_note_action_reply(
        request, conversation,
        f"**{note.heading or 'Untitled note'}**\n\n{note.content}\n\n_Saved {created}_",
        {'X-Note-Id': str(note.id)},
    )


def _ai_delete_note_response(request, conversation, target):
    """'Delete note about X' / 'delete all my notes' (see
    request_router.match_delete_note)."""
    if target == request_router.DELETE_ALL_NOTES:
        owner_notes = AINote.objects.filter(_ai_owner_filter(request))
        count = owner_notes.count()
        owner_notes.delete()
        if _ai_notes_snapshot(request):
            return _ai_note_action_reply(request, conversation, 'I couldn\'t delete all notes. Please try again.')
        confirmation = f"Deleted all {count} of your saved notes." if count else "You don’t have any saved notes."
        return _ai_note_action_reply(request, conversation, confirmation, {'X-Notes-Changed': '1'})

    if not target:
        confirmation = 'Tell me which note to delete, e.g. "delete note about milk".'
        return _ai_note_action_reply(request, conversation, confirmation)

    matches = _ai_matching_notes(request, target)
    if not matches:
        confirmation = f'I couldn\'t find a note matching "{target}".'
        return _ai_note_action_reply(request, conversation, confirmation)
    if len(matches) > 1:
        listing = '\n'.join(f"- {n.heading or 'Untitled note'}" for n in matches)
        confirmation = f"I found more than one note matching that:\n\n{listing}\n\nBe more specific about which one to delete."
        return _ai_note_action_reply(request, conversation, confirmation)

    note = matches[0]
    heading = note.heading or 'Untitled note'
    note_id = note.pk
    note.delete()
    latest_notes = _ai_notes_snapshot(request)
    if any(item['id'] == note_id for item in latest_notes):
        return _ai_note_action_reply(request, conversation, 'I couldn\'t delete that note. Please try again.')
    confirmation = f"Deleted your note — **{heading}**."
    return _ai_note_action_reply(request, conversation, confirmation, {'X-Notes-Changed': '1'})


_AI_NOTE_TIME_RE = re.compile(
    r"\b(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:am|pm)\b|"
    r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b",
    re.IGNORECASE,
)


def _ai_note_final_text(note):
    if (note.heading or '').strip() == note.content.strip():
        return f"**{note.content}**"
    return f"**{note.heading or 'Untitled note'}**\n\n{note.content}"


def _ai_commit_note_update(request, conversation, note, *, content=None, heading=None):
    update_fields = []
    if content is not None:
        note.content = _ai_safe_note_text(content)
        note.heading = _ai_note_heading(note.content)
        update_fields.extend(['content', 'heading'])
    if heading is not None:
        note.heading = _ai_safe_note_text(heading)[:120]
        if 'heading' not in update_fields:
            update_fields.append('heading')
    note.save(update_fields=update_fields)

    saved = AINote.objects.filter(_ai_owner_filter(request), pk=note.pk).first()
    expected_content = note.content if content is not None else saved.content if saved else None
    expected_heading = note.heading if heading is not None else saved.heading if saved else None
    if not saved or saved.content != expected_content or saved.heading != expected_heading:
        return _ai_note_action_reply(request, conversation, 'I couldn\'t update that note. Please try again.')

    _ai_notes_snapshot(request)
    _ai_set_selected_note(request, conversation, saved.pk)
    _ai_set_pending_note_edit(request, conversation)
    confirmation = "Updated your note. The saved note is now:\n\n" + _ai_note_final_text(saved)
    return _ai_note_action_reply(request, conversation, confirmation, {
        'X-Notes-Changed': '1', 'X-Note-Id': str(saved.pk),
    })


def _ai_replace_note_time(request, conversation, note, new_time, old_time=None):
    if old_time:
        match = re.search(re.escape(old_time), note.content, re.IGNORECASE)
        if not match:
            return _ai_note_action_reply(request, conversation, f'I couldn\'t find "{old_time}" in that note.')
        updated = note.content[:match.start()] + new_time + note.content[match.end():]
    else:
        times = list(_AI_NOTE_TIME_RE.finditer(note.content))
        if not times:
            return _ai_note_action_reply(request, conversation, 'That note does not contain a time to update.')
        if len(times) > 1:
            choices = ', '.join(match.group(0) for match in times)
            return _ai_note_action_reply(request, conversation, f"That note contains multiple times ({choices}). Which one should I change?")
        match = times[0]
        updated = note.content[:match.start()] + new_time + note.content[match.end():]
    return _ai_commit_note_update(request, conversation, note, content=updated)


def _ai_apply_note_instruction(request, conversation, note, instruction, original_message):
    instruction = (instruction or '').strip(' :-—')
    original_message = original_message or instruction
    _ai_set_selected_note(request, conversation, note.pk)

    if not instruction:
        return _ai_note_action_reply(
            request, conversation,
            _ai_note_final_text(note) + "\n\nWhat would you like to change in this note?",
            {'X-Note-Id': str(note.pk)},
        )

    if re.search(r"\brename\b", original_message, re.IGNORECASE):
        return _ai_commit_note_update(request, conversation, note, heading=instruction)

    time_change = re.search(
        r"(?:(?:update|change|correct|set)\s+)?"
        r"(?:(?:only\s+)?(?:the\s+)?(?:meeting\s+)?time|(?:this|the)\s+note(?:'s)?\s+(?:meeting\s+)?time)"
        r"(?:\s+from\s+(?P<old>(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:am|pm)))?"
        r"\s+to\s+(?P<new>(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:am|pm))\b",
        original_message, re.IGNORECASE,
    )
    if time_change:
        return _ai_replace_note_time(
            request, conversation, note,
            time_change.group('new').strip(),
            (time_change.group('old') or '').strip() or None,
        )

    exact_change = re.search(
        r"(?:change|replace|correct)\s+(.+?)\s+(?:to|with)\s+(.+?)\s*$",
        original_message, re.IGNORECASE,
    )
    if exact_change and exact_change.group(1).lower().strip() not in ('it', 'this', 'this note', 'the note'):
        old, new = exact_change.group(1).strip(), _ai_safe_note_text(exact_change.group(2))
        found = re.search(re.escape(old), note.content, re.IGNORECASE)
        if found:
            updated = note.content[:found.start()] + new + note.content[found.end():]
            return _ai_commit_note_update(request, conversation, note, content=updated)

    explicit_full = bool(re.search(r"\b(?:replace|rewrite|full note|entire note)\b", original_message, re.IGNORECASE))
    if explicit_full:
        return _ai_commit_note_update(request, conversation, note, content=instruction)

    possible_time = re.fullmatch(r"(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:am|pm)", instruction, re.IGNORECASE)
    field = 'time' if possible_time and _AI_NOTE_TIME_RE.search(note.content) else 'part'
    _ai_set_pending_note_edit(request, conversation, {
        'note_id': note.pk, 'new_content': instruction, 'field': field,
    })
    if field == 'time':
        question = f"Should I update only the time to {instruction}, or replace the full note?"
    else:
        question = "Should I update only part of this note, or replace the full note?"
    return _ai_note_action_reply(request, conversation, question, {'X-Note-Id': str(note.pk)})


def _ai_edit_note_response(request, conversation, target, new_content, original_message):
    if not target:
        return _ai_note_action_reply(request, conversation, 'Which note would you like to edit? Use its sidebar number or database ID.')

    matches = _ai_matching_notes(request, target)
    if not matches:
        return _ai_note_action_reply(request, conversation, f'I couldn\'t find a note matching "{target}".')
    if len(matches) > 1:
        listing = '\n'.join(f"- {n.heading or 'Untitled note'}" for n in matches)
        return _ai_note_action_reply(request, conversation, f"I found more than one matching note:\n\n{listing}\n\nWhich note do you mean?")

    note = matches[0]
    return _ai_apply_note_instruction(request, conversation, note, new_content, original_message)


def _ai_contextual_note_edit_response(request, conversation, message):
    note = _ai_get_selected_note(request, conversation)
    if not note:
        return None
    ambiguous = re.search(r"(?:update|change|set)\s+(?:it|this note|the note)\s+to\s+(.+?)\s*$", message, re.IGNORECASE)
    instruction = ambiguous.group(1).strip() if ambiguous else message
    return _ai_apply_note_instruction(request, conversation, note, instruction, message)


def _ai_pending_note_edit_response(request, conversation, message):
    pending = _ai_get_pending_note_edit(request, conversation)
    if not pending:
        return None
    note = AINote.objects.filter(_ai_owner_filter(request), pk=pending.get('note_id')).first()
    if not note:
        _ai_set_pending_note_edit(request, conversation)
        return _ai_note_action_reply(request, conversation, 'That note no longer exists.')
    if re.fullmatch(r"\s*(?:(?:only|just)\s+(?:the\s+)?time|update\s+only\s+(?:the\s+)?time)\s*", message, re.IGNORECASE):
        if pending.get('field') != 'time':
            return _ai_note_action_reply(request, conversation, 'What should the new time be?')
        return _ai_replace_note_time(request, conversation, note, pending['new_content'])
    if re.search(r"\b(?:replace|rewrite)\s+(?:the\s+)?(?:full|entire|whole)?\s*note\b|\bfull replacement\b", message, re.IGNORECASE):
        return _ai_commit_note_update(request, conversation, note, content=pending['new_content'])
    return None


def _chatgpt_image_error_detail(error, model_key=ai_chat.CHATGPT_56_MODEL_KEY):
    """Hide internal image providers/workers behind the ChatGPT identity."""
    detail = str(error).strip()
    label = ai_chat.MODELS[model_key]['label']
    lower_detail = detail.lower()
    if 'content filter' in lower_detail or 'content_filtered' in lower_detail:
        return 'That image request was blocked by the safety filter. Try a different prompt or image.'
    if 'limit' in lower_detail or getattr(error, 'status_code', None) == 429:
        return 'The image-generation limit has been reached. Please wait and try again later.'
    if any(term in lower_detail for term in (
        'nvidia', 'flux', 'nemotron', 'black-forest', 'black forest', 'api_key',
    )):
        return f'{label} could not generate that image. Please try again later.'
    return detail or f'{label} could not generate that image. Please try again.'


_CHATGPT_HIDDEN_MODEL_PATTERNS = (
    re.compile(
        r'black[-\s]?forest[-\s]?labs\s*/\s*flux(?:[_\s.-]*2)?(?:[_\s.-]*klein)?(?:[_\s.-]*4b)?',
        re.IGNORECASE,
    ),
    re.compile(r'\bflux(?:\s*\.?\s*2)?(?:[\s._-]*klein)?(?:[\s._-]*4b)?(?:\s+model)?\b', re.IGNORECASE),
    re.compile(
        r'\b(?:nvidia[\s/-]+)?nemotron(?:[-\s._]*3)?(?:[-\s._]*(?:nano|super|ultra|omni))?'
        r'(?:[-\s._]*\d+[a-z]?)?(?:[-\s._]*a\d+b)?(?:[-\s._]*reasoning)?(?:\s+model)?\b',
        re.IGNORECASE,
    ),
    re.compile(r'\b(?:Vidhyora|EduTrellis)\s+(?:Ultra|Quick|Light|Code|Vision)(?:\s+model)?\b', re.IGNORECASE),
)

# The backend vendors this persona must never name as its own origin. Bare
# "nvidia" deliberately isn't in _CHATGPT_HIDDEN_MODEL_PATTERNS above, because
# blindly rewriting the word would corrupt a legitimate answer *about* the
# company ("NVIDIA is a semiconductor firm" must stay correct). The two
# patterns below therefore only fire on self-attribution.
_CHATGPT_BACKEND_VENDOR = (
    r'nvidia|nemotron|meta(?:\s+ai)?|llama|mistral|black[-\s]?forest(?:\s+labs)?'
)
# Used only in first-person patterns ("I was made by OpenAI", "I'm ChatGPT"),
# never in the architecture pattern, so a factual answer about GPT or OpenAI
# is left alone — only the assistant's claims about *itself* are corrected.
_SELF_CLAIM_VENDOR = (
    _CHATGPT_BACKEND_VENDOR
    + r'|openai|google(?:\s+deepmind)?|anthropic|chatgpt|gpt(?:-?\d[\w.]*)?|gemini|claude'
)

# "I was trained by NVIDIA", "my underlying model was developed by Meta",
# "I'm running on Llama" — a first-person subject earlier in the SAME sentence
# is what separates these from "GPUs are made by NVIDIA", which must survive
# untouched. Only the vendor name itself is replaced, so the sentence keeps
# its shape and reads naturally.
_CHATGPT_SELF_ATTRIBUTION_RE = re.compile(
    # "this"/"this" (demonstrative, clearly self-referential) is included
    # alongside the first-person forms; "the model"/"the assistant" is
    # deliberately left out — too generic, and would risk rewriting a
    # factual sentence about some other company's model in the same reply.
    r"((?:\bi\b|\bi'?m\b|\bi\s+am\b|\bmy\b|\bme\b|"
    r"\bthis\s+(?:model|assistant|ai)\b)[^.!?\n]{0,80}?"
    r'\b(?:trained|train|built|build|created|develop(?:ed)?|made|designed|'
    r'powered|based|running|run|fine[-\s]?tuned|hosted|provided|'
    r'comes?|came|derived?|originates?|released?|launch(?:ed)?|deploy(?:ed)?|'
    r'operat(?:ed|es|ing)?|maintain(?:ed)?)\b'
    r'[^.!?\n]{0,40}?\b(?:by|on|upon|from|with)\s+)'
    rf'(?:{_SELF_CLAIM_VENDOR})\b',
    re.IGNORECASE,
)

# Swapping a vendor name into a sentence that then denies OpenAI leaves a
# self-contradiction ("developed by OpenAI, not OpenAI"). Collapse it.
_CHATGPT_REDUNDANT_DENIAL_RE = re.compile(
    r'\bthe EduTrellis team\b(\s*[,;-]?\s*(?:and\s+)?not\s+(?:by\s+)?the EduTrellis team\b)',
    re.IGNORECASE,
)

# "I am an NVIDIA model", "I'm Nemotron", "I am Meta's assistant".
_CHATGPT_SELF_IDENTITY_RE = re.compile(
    r"\b(?:i'?m|i\s+am)\s+(?:an?\s+)?"
    rf"(?:{_SELF_CLAIM_VENDOR})(?:'s)?"
    r'(?:\s+(?:ai|model|assistant|llm))?\b',
    re.IGNORECASE,
)

# "My name is Nemotron", "I'm called Nemotron" — a distinct phrasing from
# both self-attribution ("trained by X") and self-identity ("I'm an X
# model") above: naming the vendor's model as this assistant's own name,
# live-observed via the developer API ("My name is Nemotron. I am created
# by the Vidhyora team researchers.") where only the attribution half of
# that same reply was being rewritten.
_CHATGPT_SELF_NAME_RE = re.compile(
    rf"\b(?:my\s+name\s+is|i'?m\s+(?:called|named)|i\s+am\s+(?:called|named))\s+"
    rf"(?:{_SELF_CLAIM_VENDOR})\b",
    re.IGNORECASE,
)

# "the Llama architecture", "a Nemotron base model" — a vendor named as the
# thing this assistant is built out of, rather than as an actor. Requires one
# of the architecture nouns, so "Meta's quarterly results" stays intact.
_CHATGPT_ARCHITECTURE_RE = re.compile(
    rf'\b(?:the\s+|an?\s+)?(?:{_CHATGPT_BACKEND_VENDOR})(?:\'s)?\s+'
    r'(?:architecture|base\s+model|foundation\s+model|model\s+family|family|'
    r'backbone|weights|checkpoint)\b',
    re.IGNORECASE,
)

# A sentence can attribute twice ("I run on X and was built by Y"); a single
# re.sub pass consumes the first-person prefix along with the first match, so
# the second one needs another look. Bounded so a pathological input can't spin.
_CHATGPT_SANITIZE_MAX_PASSES = 4

# How much of the tail of a streaming ChatGPT 5.6 reply is withheld from the
# browser so the sanitizer above can still rewrite a name that straddles the
# release point. Must comfortably exceed the longest pattern there
# (_CHATGPT_SELF_ATTRIBUTION_RE tops out around 155 characters); larger only
# costs a slightly later first paint, while too small would let a leak
# through, so this errs generous.
# stream_chat already validates the first 180 characters before yielding a
# ChatGPT 5.6 reply. This smaller second buffer is only needed to catch a
# provider name split across network chunks; 240 characters delayed the first
# visible text unnecessarily on the most-used model.
CHATGPT_STREAM_HOLDBACK_CHARS = 96

_AI_HOME_SELF_LINK_RE = re.compile(
    r'\[(?P<origin>https?://[^/\]\s)]+)(?:/AI/?)\]\('
    r'(?P=origin)(?:/AI/?)\)',
    re.IGNORECASE,
)
_ROOT_SELF_LINK_RE = re.compile(
    r'\[(?P<origin>https?://[^/\]\s)]+)/?\]\((?P=origin)/?\)',
    re.IGNORECASE,
)


def _normalize_ai_home_links(reply):
    """Show one canonical site-root link instead of duplicate root + /AI links."""
    text = _AI_HOME_SELF_LINK_RE.sub(
        lambda match: f"[{match.group('origin')}]({match.group('origin')})",
        str(reply or ''),
    )
    seen_origins = set()

    def keep_first_root(match):
        origin = match.group('origin')
        key = origin.lower()
        if key in seen_origins:
            return ''
        seen_origins.add(key)
        return f'[{origin}]({origin})'

    text = _ROOT_SELF_LINK_RE.sub(keep_first_root, text)
    return re.sub(r'[ \t]{2,}', ' ', text)


def _chatgpt_public_reply(reply, name='Zyvion'):
    """Keep routed worker identities (and claims to be another company's AI)
    out of the Zyvion models' visible response text.

    Runs over the whole visible reply, not just its opening — ai_chat's
    _IDENTITY_LEAK_RE retry only inspects the first few hundred characters,
    so a backend name mentioned midway through a long answer (reported by the
    user as replies still saying "I was trained by NVIDIA") reaches the
    browser unless it is rewritten here too.
    """
    cleaned = str(reply or '')
    # Self-attribution runs FIRST, while the text is still the model's own
    # words. The replacements below inject "ChatGPT 5.6", whose "5.6" contains
    # a period — and these patterns are deliberately sentence-bounded
    # ([^.!?\n]), so a substitution made first would wall off a second vendor
    # mention later in the same sentence and let it through.
    for _ in range(_CHATGPT_SANITIZE_MAX_PASSES):
        replaced = _CHATGPT_SELF_ATTRIBUTION_RE.sub(r'\1the EduTrellis team', cleaned)
        replaced = _CHATGPT_SELF_IDENTITY_RE.sub(f'I am {name}', replaced)
        replaced = _CHATGPT_SELF_NAME_RE.sub(f'My name is {name}', replaced)
        if replaced == cleaned:
            break
        cleaned = replaced
    cleaned = _CHATGPT_ARCHITECTURE_RE.sub(name, cleaned)
    for pattern in _CHATGPT_HIDDEN_MODEL_PATTERNS:
        cleaned = pattern.sub(name, cleaned)
    cleaned = _CHATGPT_REDUNDANT_DENIAL_RE.sub('the EduTrellis team', cleaned)
    return _normalize_ai_home_links(cleaned)


# The mirror image of _CHATGPT_HIDDEN_MODEL_PATTERNS above: a plain Vidhyora
# mode (Ultra/Quick/Code/Vision — anything that is not the ChatGPT 5.6
# persona) must never claim to be ChatGPT or one of its Sol/Terra/Luna names.
# Live-observed after switching the picker away from that persona
# mid-conversation: the model echoes its own earlier "I'm ChatGPT 5.6
# Sol..." reply (or the sibling-model facts CHATGPT_56_SYSTEM_SUFFIX had it
# explain) still sitting in conversation history, overriding the current
# turn's mode_reminder in ai_chat.stream_chat. Requires an actual
# self-identification claim ("I'm ChatGPT...") or the literal "ChatGPT 5.6"
# product name — deliberately does not match a bare "Sol"/"Terra"/"Luna"
# on its own, since those are also ordinary words, and never flags a reply
# that merely discusses or compares itself to ChatGPT in passing ("unlike
# ChatGPT, I can also...").
_WRONG_PERSONA_SELF_CLAIM_RE = re.compile(
    r"\bi(?:'m| am)\s+chatgpt(?:\s*5\.6)?(?:\s+(?:in\s+)?(?:sol|terra|luna))?\b",
    re.IGNORECASE,
)
_WRONG_PERSONA_BARE_MENTION_RE = re.compile(
    r"\bchatgpt\s*5\.6(?:\s+(?:in\s+)?(?:sol|terra|luna))?\b",
    re.IGNORECASE,
)


def _vidhyora_public_reply(reply, mode_label):
    """Keep the ChatGPT 5.6 persona's name AND the real backend vendor
    (NVIDIA/Nemotron/Meta/Llama/etc) out of a plain Vidhyora mode's response
    text — see _WRONG_PERSONA_SELF_CLAIM_RE above for the persona-name half.
    The vendor half reuses the exact same detection patterns
    _chatgpt_public_reply uses (_CHATGPT_SELF_ATTRIBUTION_RE etc — these
    only hardcode "OpenAI"/"ChatGPT 5.6" in their *replacement* text, not
    their matching), just substituting "the Vidhyora team" instead, per
    COMPACT_SYSTEM_PROMPT's own instruction ("say it is created and
    maintained by the Vidhyora team — never NVIDIA, Nemotron..."). Live
    reported: "Who trained me? My underlying models are trained by
    researchers from NVIDIA." from Vidhyora Quick.

    Mirrors _chatgpt_public_reply's whole-reply rewrite approach: fast,
    in-place substitution rather than ai_chat's hold-back-and-regenerate
    retry, since Ultra/Quick/Code are the highest-volume modes and a leak
    reaching this far already survived ai_chat's own opening-window checks
    (which only run for the ChatGPT 5.6 persona), so a whole-reply pass is
    the only backstop these modes have.
    """
    brand_name = ai_chat.get_ai_brand_name()
    cleaned = str(reply or '')
    for _ in range(_CHATGPT_SANITIZE_MAX_PASSES):
        replaced = _CHATGPT_SELF_ATTRIBUTION_RE.sub(rf'\1the {brand_name} team', cleaned)
        replaced = _CHATGPT_SELF_IDENTITY_RE.sub(f"I'm an AI model built by the {brand_name} team", replaced)
        replaced = _CHATGPT_SELF_NAME_RE.sub(f'My name is {mode_label}', replaced)
        if replaced == cleaned:
            break
        cleaned = replaced
    cleaned = _CHATGPT_ARCHITECTURE_RE.sub(mode_label, cleaned)
    cleaned = _WRONG_PERSONA_SELF_CLAIM_RE.sub(f"I'm {mode_label}", cleaned)
    cleaned = _WRONG_PERSONA_BARE_MENTION_RE.sub(mode_label, cleaned)
    return cleaned


def _owner_identity_public_reply(reply, identity, mode_label):
    """The reply filter for a model whose identity the site owner set on API
    Settings: a backend vendor the model names as its maker is replaced by the
    owner's answer instead of the built-in one. When the owner's identity
    itself names such a vendor (say "trained by NVIDIA"), that is the wanted
    answer and nothing is rewritten."""
    cleaned = str(reply or '')
    if re.search(_CHATGPT_BACKEND_VENDOR, ' '.join(identity.values()), re.IGNORECASE):
        return _normalize_ai_home_links(cleaned)
    creator = identity.get('creator') or f'the {ai_chat.get_ai_brand_name()} team'
    name = identity.get('name') or mode_label
    for _ in range(_CHATGPT_SANITIZE_MAX_PASSES):
        replaced = _CHATGPT_SELF_ATTRIBUTION_RE.sub(lambda m: m.group(1) + creator, cleaned)
        replaced = _CHATGPT_SELF_IDENTITY_RE.sub(f'I am {name}', replaced)
        replaced = _CHATGPT_SELF_NAME_RE.sub(f'My name is {name}', replaced)
        if replaced == cleaned:
            break
        cleaned = replaced
    cleaned = _CHATGPT_ARCHITECTURE_RE.sub(identity.get('model') or name, cleaned)
    return _normalize_ai_home_links(cleaned)


def _ai_public_routed_model_key(response_model_key, routed_model_key):
    """Never expose ChatGPT's private worker selection to the browser."""
    if response_model_key in (ai_chat.CHATGPT_56_MODEL_KEY, ai_chat.SOL_MODEL_KEY, ai_chat.TERRA_MODEL_KEY):
        return response_model_key
    return routed_model_key


def _support_email():
    """The support address saved under dashboard → Customize."""
    try:
        return AISiteSettings.get_solo().support_email or 'support@edutrellis.in'
    except (OperationalError, ProgrammingError):
        return 'support@edutrellis.in'


def _ai_chat_failure_reply(error, response_model_key, is_staff=False):
    """Turn upstream failures into safe, accurate, public-facing guidance.
    A missing/invalid key or unavailable model is never explained in terms of
    settings or environment variables, even to staff: everyone is pointed to
    the administrator at the saved support email instead."""
    label = ai_chat.MODELS.get(response_model_key, {}).get('label', 'The selected AI model')
    status_code = getattr(error, 'status_code', None)
    contact = f' Please contact the administrator at {_support_email()}.'
    if ai_chat._is_unconfigured_key_error(error) or ai_chat._is_model_unavailable_error(error):
        return f'{label} text access is currently disconnected.{contact}'
    if status_code in (401, 403):
        return f'{label} authentication is currently unavailable.{contact}'
    if status_code == 429:
        return (
            f'{label} is currently at its request limit. Please wait a moment and try again, '
            f'or contact the administrator at {_support_email()} if it keeps happening.'
        )
    if ai_chat._is_transient_error(error):
        return (
            f'{label} is temporarily unavailable. Please wait a moment and try again, '
            f'or contact the administrator at {_support_email()} if it keeps happening.'
        )
    return (
        f'{label} is taking longer than expected to respond. Please try again in a moment, '
        f'or contact the administrator at {_support_email()} if it keeps happening.'
    )


def _ai_flux_response(conversation, prompt, source_image, response_model_key=None, owner_email=''):
    """Run a FLUX generation/editing turn and persist the real image URL."""
    display_model_key = response_model_key or ai_chat.FLUX_KLEIN_4B_MODEL_KEY
    model_controls.record_request(ai_chat.FLUX_KLEIN_4B_MODEL_KEY)
    try:
        try:
            # A poster with words that must be exact (brand name, price,
            # contact details) gets AI artwork with the words lettered on top
            # by the server — see myapp/poster.py. Anything else is drawn
            # entirely by the image model, as before.
            generated = None if source_image else poster.make_poster(prompt)
            if generated is None:
                generated = image_generation.generate_image(prompt, source_image or None)
        except image_generation.ImageGenerationError as exc:
            # The connected backend can't actually edit an uploaded reference
            # image at all (see ImageGenerationError.editing_unavailable) —
            # rather than dead-ending on "contact support", read the photo
            # with the vision model and generate a brand-new image from the
            # combined description + requested changes instead.
            if not (exc.editing_unavailable and source_image):
                raise
            description = ai_chat.describe_image_for_edit(source_image)
            if not description:
                raise
            combined_prompt = (
                f"{prompt.strip()}. The reference photo shows: {description}"
            )[:image_generation.MAX_PROMPT_CHARS]
            generated = image_generation.generate_image(combined_prompt, None)
        dated_folder = timezone.now().strftime('%Y/%m/%d')
        filename = f"ai_generated/{dated_folder}/{secrets.token_hex(16)}.{generated.extension}"
        stored_name = default_storage.save(filename, ContentFile(generated.content))
        generated_url = default_storage.url(stored_name)
    except image_generation.ImageGenerationError as exc:
        model_controls.record_error(ai_chat.FLUX_KLEIN_4B_MODEL_KEY, str(exc))
        service_down = exc.status_code == 503 and not exc.blocked and not exc.editing_unavailable
        # Every picked model, not only Luna: a raw error can name the image
        # model, NVIDIA or a key setting, which reads as a different model
        # answering than the one the user picked.
        detail = _chatgpt_image_error_detail(exc, display_model_key)
        if service_down:
            detail = f'Image generation is currently unavailable. Please contact the administrator at {_support_email()}.'
        response = JsonResponse(
            {'status': 'error', 'detail': detail},
            status=exc.status_code,
        )
        response['X-Conversation-Id'] = str(conversation.id)
        response['X-Model-Key'] = display_model_key
        response['X-Routed-Model-Key'] = _ai_public_routed_model_key(
            display_model_key, ai_chat.FLUX_KLEIN_4B_MODEL_KEY,
        )
        return response
    except Exception as exc:
        model_controls.record_error(ai_chat.FLUX_KLEIN_4B_MODEL_KEY, f'{exc.__class__.__name__}: {exc}')
        logger.exception("Failed to save generated FLUX image")
        response = JsonResponse(
            {'status': 'error', 'detail': 'The image was generated but could not be saved. Please try again.'},
            status=503,
        )
        response['X-Conversation-Id'] = str(conversation.id)
        response['X-Model-Key'] = display_model_key
        response['X-Routed-Model-Key'] = _ai_public_routed_model_key(
            display_model_key, ai_chat.FLUX_KLEIN_4B_MODEL_KEY,
        )
        return response

    model_controls.record_success(ai_chat.FLUX_KLEIN_4B_MODEL_KEY)

    # No caption text — the image speaks for itself, and a canned "Image
    # generated successfully." heading above every single image was just
    # noise. The frontend (finalizeBubble in ai.html) knows not to fall
    # back to its usual "No response" placeholder when generatedImageUrl
    # is set, so an empty reply here renders as just the image.
    reply = ''
    assistant_message = AIMessage.objects.create(
        conversation=conversation,
        role=AIMessage.ROLE_ASSISTANT,
        content=reply,
        image_data=generated_url,
        model_key=display_model_key,
    )
    # Kept outside the conversation so deleting the chat never destroys the
    # picture — the gallery reads this table, not AIMessage. Best-effort: a
    # failure here must not turn a successfully generated image into an error.
    try:
        AIUserImage.objects.create(
            user=conversation.user,
            session_key=conversation.session_key or '',
            conversation=conversation,
            url=generated_url,
            prompt=(prompt or '')[:AI_CHAT_MAX_MESSAGE_CHARS],
            model_key=display_model_key,
        )
    except Exception:
        logger.exception('Could not record a generated image for the gallery')
    response = HttpResponse(reply, content_type='text/plain; charset=utf-8')
    response['Cache-Control'] = 'private, no-store'
    response['X-Conversation-Id'] = str(conversation.id)
    response['X-Model-Key'] = display_model_key
    response['X-Routed-Model-Key'] = _ai_public_routed_model_key(
        display_model_key, ai_chat.FLUX_KLEIN_4B_MODEL_KEY,
    )
    response['X-Request-Category'] = 'image_edit' if source_image else 'image_generation'
    response['X-Generated-Image-Url'] = generated_url
    response['X-Message-Id'] = str(assistant_message.pk)
    return response


def _ai_video_response(request, conversation, prompt, video_row, response_model_key):
    """Start a video for this turn. The reply is a "making your video" note;
    the chat page polls ai_video_status and swaps in the clip when ready."""
    assistant_message = AIMessage.objects.create(
        conversation=conversation, role=AIMessage.ROLE_ASSISTANT,
        content=video_generation.WORKING_TEXT, model_key=response_model_key or video_row.model_key,
    )
    job = AIVideoJob.objects.create(
        user=request.user if request.user.is_authenticated else None,
        session_key=request.session.session_key or '', message=assistant_message,
        model_key=video_row.model_key, prompt=(prompt or '')[:AI_CHAT_MAX_MESSAGE_CHARS],
    )
    video_generation.start(job.pk)
    response = HttpResponse(video_generation.WORKING_TEXT, content_type='text/plain; charset=utf-8')
    response['Cache-Control'] = 'private, no-store'
    response['X-Conversation-Id'] = str(conversation.id)
    response['X-Model-Key'] = assistant_message.model_key
    response['X-Routed-Model-Key'] = _ai_public_routed_model_key(assistant_message.model_key, video_row.model_key)
    response['X-Request-Category'] = 'video_generation'
    response['X-Video-Job'] = str(job.token)
    response['X-Message-Id'] = str(assistant_message.pk)
    return response


def ai_video_status(request, token):
    """Where a video job is: working, ready (with the video) or failed."""
    job = AIVideoJob.objects.select_related('message__conversation').filter(token=token).first()
    owner = AIConversation.objects.filter(_ai_owner_filter(request), pk=job.message.conversation_id).exists() if job else False
    if not owner:
        return JsonResponse({'status': 'error', 'detail': 'Video not found.'}, status=404)
    video_generation.expire_if_stale(job)
    response = JsonResponse({'status': job.status, 'video_url': job.video_url, 'error': job.error})
    response['Cache-Control'] = 'private, no-store'
    return response


def _ai_visible_messages(request, conversation):
    """The conversation's live messages: not replaced by a retry/edit, and —
    while a retry/edit is being processed — not the turn it is replacing."""
    messages = conversation.messages.filter(superseded=False)
    replace_from = getattr(request, '_ai_replace_from_pk', None)
    if replace_from:
        messages = messages.exclude(pk__gte=replace_from)
    return messages


def _ai_replace_target(request, payload):
    """The sent user message a Retry/Edit is replacing (payload
    replace_message_id), or None. Only the owner's own live message counts,
    and it must belong to the conversation the request names."""
    try:
        message_id = int(payload.get('replace_message_id'))
    except (TypeError, ValueError):
        return None
    target = AIMessage.objects.filter(
        pk=message_id, role=AIMessage.ROLE_USER, superseded=False,
    ).first()
    if target is None or str(payload.get('conversation_id') or '') != str(target.conversation_id):
        return None
    if not AIConversation.objects.filter(_ai_owner_filter(request), pk=target.conversation_id).exists():
        return None
    request._ai_replace_from_pk = target.pk
    return target


AI_REPLY_QUOTE_MAX_CHARS = 1500


def _ai_reply_quote(payload):
    """The earlier answer (or the part of it the user selected) that this
    message replies to (payload reply_to), tidied and capped. Empty when the
    message is not a reply."""
    quote = payload.get('reply_to')
    if not isinstance(quote, str):
        return ''
    quote = re.sub(r'[ \t]+', ' ', quote.replace('\r\n', '\n'))
    quote = re.sub(r'\n{3,}', '\n\n', quote).strip()
    return quote[:AI_REPLY_QUOTE_MAX_CHARS].rstrip()


def _ai_reply_context(quote):
    """What the model is told before a message that replies to one of its
    earlier answers, so it knows which answer 'this' or 'that' means."""
    return (
        'The user is replying to this specific earlier answer of yours:\n"""\n'
        f'{quote}\n"""\nTheir message about it follows.\n\n'
    )


def _ai_image_daily_count(request):
    """Count images successfully saved today for this account/session."""
    images = AIUserImage.objects.filter(created_at__date=timezone.localdate())
    if request.user.is_authenticated:
        return images.filter(user=request.user).count()
    return images.filter(
        user__isnull=True,
        session_key=request.session.session_key or '',
    ).count()


def _ai_previous_image_for_edit(request, conversation_id, message):
    """Return the latest usable image when a follow-up clearly asks to edit it:
    an edit verb ("change", "remove"...), or - straight after the AI has shown
    a picture - a few words about how it should look ("hands up pose")."""
    explicit_edit = ai_chat.is_image_edit_instruction(message)
    if not conversation_id or not (explicit_edit or ai_chat.is_image_followup(message)):
        return ''
    try:
        conversation_id = int(conversation_id)
    except (TypeError, ValueError):
        return ''
    conversation = AIConversation.objects.filter(
        _ai_owner_filter(request), pk=conversation_id,
    ).first()
    if not conversation:
        return ''
    if not explicit_edit:
        latest = _ai_visible_messages(request, conversation).order_by('-created_at', '-pk').first()
        if not latest or latest.role != AIMessage.ROLE_ASSISTANT or not latest.image_data:
            return ''
    previous = _ai_visible_messages(request, conversation).exclude(image_data='').order_by(
        '-created_at', '-pk',
    ).values_list('image_data', flat=True).first()
    snapshotted = _snapshot_ai_report_image(previous or '')
    return snapshotted if snapshotted.startswith('data:image/') else ''


def _ai_previous_generated_image(request, conversation_id, message):
    """Find an earlier generated image for an explicit show/display follow-up."""
    if not conversation_id or not ai_chat.is_show_previous_image_request(message):
        return ''
    try:
        conversation_id = int(conversation_id)
    except (TypeError, ValueError):
        return ''
    conversation = AIConversation.objects.filter(
        _ai_owner_filter(request), pk=conversation_id,
    ).first()
    if not conversation:
        return ''
    return _ai_visible_messages(request, conversation).filter(
        role=AIMessage.ROLE_ASSISTANT,
    ).exclude(image_data='').order_by(
        '-created_at', '-pk',
    ).values_list('image_data', flat=True).first() or ''


def _ai_pending_image_prompt(request, conversation_id, message):
    """Combine a clarification answer with the image request that prompted it."""
    if (
        not conversation_id or not message
        or ai_chat.is_image_flow_cancel(message)
    ):
        return ''
    try:
        conversation_id = int(conversation_id)
    except (TypeError, ValueError):
        return ''
    conversation = AIConversation.objects.filter(
        _ai_owner_filter(request), pk=conversation_id,
    ).first()
    if not conversation:
        return ''

    latest = _ai_visible_messages(request, conversation).order_by('-created_at', '-pk').first()
    if (
        not latest or latest.role != AIMessage.ROLE_ASSISTANT
        or not ai_chat.is_image_details_question(latest.content)
    ):
        return ''
    earlier = _ai_visible_messages(request, conversation).filter(
        Q(created_at__lt=latest.created_at)
        | Q(created_at=latest.created_at, pk__lt=latest.pk),
        role=AIMessage.ROLE_USER,
    ).order_by('-created_at', '-pk').first()
    if not earlier:
        return ''
    base_prompt = (earlier.content or '').strip()
    if (
        not base_prompt
        or ai_chat.is_image_capability_question(base_prompt)
        or ai_chat.is_image_prompt_writing_request(base_prompt)
        or not (
            ai_chat.is_image_generation_request(base_prompt)
            or ai_chat.is_natural_image_prompt(base_prompt)
            or ai_chat.is_probable_image_prompt(base_prompt)
        )
    ):
        return ''
    return (
        f'{base_prompt}\nAdditional image details: {message}'
    )[:AI_CHAT_MAX_MESSAGE_CHARS]


def _ai_recalled_image_response(conversation, image_value, display_model_key):
    """Re-display an existing image without generating or billing another one."""
    assistant_message = AIMessage.objects.create(
        conversation=conversation,
        role=AIMessage.ROLE_ASSISTANT,
        content='',
        image_data=image_value,
        model_key=display_model_key,
    )
    response = HttpResponse('', content_type='text/plain; charset=utf-8')
    response['Cache-Control'] = 'private, no-store'
    response['X-Conversation-Id'] = str(conversation.pk)
    response['X-Model-Key'] = display_model_key
    response['X-Routed-Model-Key'] = display_model_key
    response['X-Request-Category'] = 'image_recall'
    response['X-Generated-Image-Url'] = image_value
    response['X-Message-Id'] = str(assistant_message.pk)
    return response



# "add this image inside pdf", "put the poster in a word file", "this photo as
# pptx": an image already in the chat goes into a file as it is. Needs both a
# pointer at an existing picture and a file format; "create a pdf with an image
# of a logo" (no existing picture) stays an ordinary written document.
_AI_IMAGE_REFERENCE_RE = re.compile(
    r"\b(?:this|that|the|above|previous|last|same|generated|my|your|ye|yeh|is)\s+"
    r"(?:image|picture|photo|pic|poster|logo|banner|flyer|design|graphic|wallpaper|thumbnail)s?\b"
    r"(?!\s+(?:generat|creat|edit|model|prompt))|"
    r"\b(?:image|picture|photo|poster)\s+(?:above|you\s+(?:made|created|generated|gave))\b|"
    r"\b(?:it|this)\s+(?:in|into|inside|as|to)\s+(?:a\s+|an\s+)?(?:pdf|word|docx|ppt|pptx|powerpoint)\b",
    re.IGNORECASE,
)
_AI_IMAGE_FILE_FORMATS = (
    (re.compile(r'\bpdf\b', re.IGNORECASE), 'pdf'),
    (re.compile(r'\b(?:word|docx?)\b', re.IGNORECASE), 'docx'),
    (re.compile(r'\b(?:pptx?|powerpoint|presentation|slides?|slide\s?deck)\b', re.IGNORECASE), 'pptx'),
)


def _ai_image_file_request(message):
    """'pdf', 'docx' or 'pptx' when the message asks to put a chat image into
    that kind of file, else ''."""
    text = message or ''
    if not _AI_IMAGE_REFERENCE_RE.search(text):
        return ''
    found = [(m.start(), ext) for pattern, ext in _AI_IMAGE_FILE_FORMATS for m in [pattern.search(text)] if m]
    return min(found)[1] if found else ''


def _ai_image_file_response(request, conversation, user_message, extension, display_model_key):
    """Put the picture this turn points at (one attached now, else the latest
    one in the chat) into a PDF/Word/PowerPoint file and reply with its link."""
    picture = user_message.image_data or (
        _ai_visible_messages(request, conversation).exclude(pk=user_message.pk).exclude(image_data='')
        .order_by('-created_at', '-pk').values_list('image_data', flat=True).first() or ''
    )
    picture = _snapshot_ai_report_image(picture) if picture else ''
    labels = {'pdf': 'PDF', 'docx': 'Word file', 'pptx': 'PowerPoint file'}
    if not picture:
        reply = (
            f"There's no image in this chat yet to put in a {labels[extension]}. "
            "Create one or attach one, then ask again."
        )
    elif not picture.startswith('data:image/'):
        reply = "I couldn't open that image any more. Please create or attach it again, then ask for the file."
    else:
        explicit = next(
            (m.group(1) for m in _AI_EXPLICIT_FILENAME_RE.finditer(user_message.content or '')
             if m.group(2).lower() == extension), '',
        )
        noun = re.search(r'\b(poster|logo|banner|flyer|photo|picture|design|wallpaper|thumbnail)\b',
                         user_message.content or '', re.IGNORECASE)
        file_name = explicit or f"{(noun.group(1).lower() if noun else 'image')}.{extension}"
        file_name = re.sub(r'[^A-Za-z0-9_.-]+', '-', Path(file_name.replace('\\', '/')).name).strip('.-')[:120]
        generated_file = AIGeneratedFile.objects.create(
            user=request.user if request.user.is_authenticated else None,
            session_key='' if request.user.is_authenticated else (request.session.session_key or ''),
            file_name=file_name,
            content=doc_image.stored_content(picture),
        )
        download_url = request.build_absolute_uri(reverse('ai_generated_file_download', args=[generated_file.token]))
        reply = f"Your file is ready.\n\n[Download {generated_file.file_name}]({download_url})"
    assistant_message = AIMessage.objects.create(
        conversation=conversation, role=AIMessage.ROLE_ASSISTANT, content=reply, model_key=display_model_key,
    )
    response = HttpResponse(reply, content_type='text/plain; charset=utf-8')
    response['Cache-Control'] = 'private, no-store'
    response['X-Conversation-Id'] = str(conversation.pk)
    response['X-Model-Key'] = display_model_key
    response['X-Routed-Model-Key'] = display_model_key
    response['X-Request-Category'] = 'file_generation'
    response['X-Message-Id'] = str(assistant_message.pk)
    return response


def ai_chat_send(request):
    response = _ai_chat_send(request)
    # Lets the page tag the message it just rendered, so a later Edit/Retry
    # can name the exact message it replaces.
    user_message_id = getattr(request, '_ai_user_message_id', None)
    if user_message_id:
        response['X-User-Message-Id'] = str(user_message_id)
    return response


def _ai_chat_send(request):
    request_started = time.perf_counter()
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)

    # A real, billable API key sits behind this — a basic per-IP rate limit
    # is the brake against an account (or a guest, or a script cycling
    # through either) hammering it. Uses Django's cache, which on a
    # single-process dev server is exact; under multiple gunicorn workers
    # each worker has its own count, so the effective ceiling is
    # (limit × worker count) — a soft brake, not a hard guarantee.
    #
    # A fixed window with its own stored reset_at (rather than just an
    # integer whose cache TTL gets renewed on every message) so the exact
    # wait time can be told to whoever's blocked, instead of a vague "wait a
    # bit" — and so someone sending at a slow, steady trickle isn't kept
    # perpetually blocked by their own TTL renewing before it ever expires.
    ip = _client_ip(request)
    # Staff-issued blocks (see dashboard AI Activity) — checked before the
    # rate limiter below since a blocked spammer shouldn't even get to
    # accrue against it. Staff are never checked, so a mistaken block can
    # never accidentally lock out someone who can undo it.
    if not _ai_has_admin_access(request.user):
        block_filter = Q(ip_address=ip) if ip and ip != 'unknown' else Q(pk__isnull=True)
        if request.user.is_authenticated:
            block_filter |= Q(user=request.user)
        if AIBlock.objects.filter(block_filter).exists():
            return JsonResponse(
                {'status': 'error', 'detail': "Your access to EduTrellis AI has been restricted. Contact support if you think this is a mistake."},
                status=403,
            )
    cache_key = f'ai_chat_rate:{ip}'
    now = time.time()
    window = cache.get(cache_key)
    if not window or now >= window['reset_at']:
        window = {'count': 0, 'reset_at': now + AI_CHAT_RATE_WINDOW}
    if window['count'] >= AI_CHAT_RATE_LIMIT:
        wait_for = _format_wait_time(window['reset_at'] - now)
        return JsonResponse(
            {
                'status': 'rate_limited',
                'detail': f"Limit reached — please wait {wait_for} and try again later.",
            },
            status=429,
        )
    # Counted immediately, before any further validation — a request that
    # gets rejected downstream (bad payload, guest cap, etc.) still cost a
    # request and should still count against the brake, otherwise a blocked
    # guest can hit this endpoint an unlimited number of times for free.
    window['count'] += 1
    cache.set(cache_key, window, AI_CHAT_RATE_WINDOW)

    payload = _parse_json_body(request)
    if not isinstance(payload, dict):
        return JsonResponse({'status': 'error', 'detail': 'Invalid request body.'}, status=400)
    message = str(payload.get('message', ''))[:AI_CHAT_MAX_MESSAGE_CHARS].strip()

    image_data = payload.get('image')
    if not isinstance(image_data, str) or not image_data.startswith('data:image/'):
        image_data = ''
    if image_data and len(image_data) > AI_IMAGE_MAX_DATA_URI_CHARS:
        return JsonResponse({'status': 'error', 'detail': 'That image is too large — please use a smaller one.'}, status=400)

    # OCR supplements the multimodal model for screenshots and documents.
    # Failure is harmless: the original image is still analysed by Vision.
    image_ocr_text = image_ocr.extract_data_uri(image_data) if image_data else ''

    # document_text/document_name come from a prior call to /ai/api/extract/
    # (the raw file itself is never sent here) — re-capped defensively since
    # the client is untrusted, even though it already capped it once too.
    document_text = payload.get('document_text')
    document_name = payload.get('document_name')
    if isinstance(document_text, str) and document_text.strip() and isinstance(document_name, str) and document_name.strip():
        document_text = document_text.strip()[:doc_extract.TOTAL_MAX_CHARS]
        document_name = document_name.strip()[:255]
    else:
        document_text = ''
        document_name = ''
    # Retry / Edit of an earlier message: the attachment it had carries over
    # unless a new one was supplied, since the page never re-uploads it.
    replace_target = _ai_replace_target(request, payload)
    stored_image = ''   # what the new message row keeps when it reuses a stored picture
    if replace_target is not None and not image_data and not document_text:
        if replace_target.image_data:
            stored_image = replace_target.image_data
            # The model and the image tools need the bytes, not a file link.
            image_data = _snapshot_ai_report_image(stored_image)
            if not image_data.startswith('data:image/'):
                image_data = ''
            image_ocr_text = replace_target.document_text
        elif replace_target.document_name and replace_target.document_text:
            document_text = replace_target.document_text
            document_name = replace_target.document_name
    requested_document_mode = payload.get('document_mode')
    document_mode = requested_document_mode if document_text and requested_document_mode in AI_DOCUMENT_MODES else ''
    document_truncated = bool(payload.get('document_truncated')) if document_text else False
    document_instruction = _ai_document_instruction(document_mode, document_name, document_truncated)
    voice_instruction = _ai_voice_call_instruction(payload) if payload.get('voice_call') is True else ''

    if not message and not image_data and not document_text:
        return JsonResponse({'status': 'error', 'detail': 'No message provided.'}, status=400)

    requested_language = payload.get('language')
    language = requested_language if requested_language in ai_chat.LANGUAGES else ai_chat.DEFAULT_LANGUAGE

    pending_image_prompt = ''
    if not image_data and message:
        pending_image_prompt = _ai_pending_image_prompt(
            request, payload.get('conversation_id'), message,
        )
    image_generation_prompt = pending_image_prompt or message

    # Short follow-ups such as "8k", "upscale this", or "more poses" refer
    # to the latest image in the open conversation. Keep this separate from
    # image_data so stored history still reflects what was actually attached
    # on the current turn.
    previous_display_image = _ai_previous_generated_image(
        request, payload.get('conversation_id'), message,
    ) if message else ''
    previous_edit_image = ''
    if not image_data and message:
        previous_edit_image = _ai_previous_image_for_edit(
            request, payload.get('conversation_id'), message,
        )
    source_image_data = image_data or previous_edit_image

    # ChatGPT 5.6 is a stable user-facing selection backed by the existing
    # task-specific workers. Keep its public identity while routing the actual
    # turn to Vision, Code, or Quick.
    full_model_access = _ai_has_full_model_access(request.user)
    default_model_key = (
        ai_chat.SOL_MODEL_KEY
        if full_model_access and ai_chat.is_model_enabled(ai_chat.SOL_MODEL_KEY) else 'quick'
    )
    requested_model_key = payload.get('model')
    selected_model_key = (
        requested_model_key
        if requested_model_key in ai_chat.MODELS
        else default_model_key
    )
    if not ai_chat.is_model_enabled(selected_model_key):
        return JsonResponse({
            'status': 'model_disabled',
            'detail': (
                f"{ai_chat.MODELS[selected_model_key]['label']} is temporarily unavailable. "
                'Please pick another model from the model menu.'
            ),
        }, status=403)
    chatgpt_mode = selected_model_key == ai_chat.CHATGPT_56_MODEL_KEY
    # Whatever the turn is auto-routed to (image generation, vision, code...),
    # the reply is always shown, labelled and saved under the model the user
    # picked, never under the worker that actually produced it. Only the
    # hidden internal 'vision' key has no picker entry of its own.
    response_model_key = selected_model_key if selected_model_key != 'vision' else None

    # Gated on ai_chat.is_image_generation_request rather than just "FLUX is
    # selected" — that regex is what decides whether a message genuinely
    # reads as an image create/edit request (verb+noun within 40 chars,
    # bidirectional for Hindi/Hinglish word order) before anything gets
    # routed to an image-only model, so a plain "hi" doesn't get treated as
    # a picture prompt just because FLUX happened to be selected, and a real
    # "generate a poster for..." gets routed to FLUX even from a completely
    # different model without the user having to switch manually. A generic
    # capability question ("can you edit or generate images?") matches the
    # same regex but isn't an actual subject to draw, so it's excluded here
    # and answered conversationally instead — otherwise FLUX would try to
    # render the question text itself as an image.
    image_prompt_writing = bool(message) and ai_chat.is_image_prompt_writing_request(message)
    is_image_request = bool(message) and (
        (
            ai_chat.is_image_generation_request(message)
            and not ai_chat.is_image_capability_question(message)
            and not image_prompt_writing
        )
        # A raw Midjourney/DALL-E-style scene description (no generate/create
        # verb at all, e.g. "A realistic brown dog..., soft lighting, 4k.")
        # never matches the verb+noun regex above — see AIReport #19. Two or
        # more photography/art style cues and no question mark is a strong
        # enough signal to route it the same way.
        or (not image_data and ai_chat.is_probable_image_prompt(message))
        # Direct visual descriptions are also common real requests: "girl
        # sitting in a park", "Krishna image", "Instagram post for my shop".
        or (not image_data and ai_chat.is_natural_image_prompt(message))
        # A longer, paragraph-style scene description — e.g. "A peaceful
        # green forest with tall trees, soft sunlight, and colorful
        # wildflowers..." — matches none of the three checks above (no
        # verb, no photography jargon, too long/loosely shaped for the
        # narrow natural-prompt pattern) but is just as clearly a real
        # prompt to generate, not a question to answer conversationally.
        or (not image_data and ai_chat.is_scene_description_prompt(message))
        # A design named on its own, with no "create"/"image" word: "Instagram
        # poster 4:5 ...", "need a flyer for my yoga class", "--ar 4:5 --v 6".
        or (not image_data and ai_chat.is_design_request(message))
        # If the previous assistant turn explicitly asked for image details,
        # this reply completes that request instead of starting a text chat.
        or bool(pending_image_prompt)
    )
    generated_file_spec = (
        # Deliberately not gated on "no attached image" — AIReport #33
        # asked for a real PDF with a logo image attached "to add", and
        # having an image on the turn silently dropped the file request
        # straight through to Vision (which can only describe an image,
        # never produce a download).
        #
        # Also deliberately NOT gated on "not is_image_request" any more —
        # _ai_generated_file_spec only matches an explicit file/document
        # word or a real filename extension (never an image noun; see
        # AI_GENERATED_FILE_TYPE_EXTENSIONS/AI_GENERATED_FILE_EXTENSIONS,
        # neither of which lists an image format), so it can never wrongly
        # steal a genuine "generate a poster" request. Letting is_image_request
        # win instead used to route "create a polished, professional PDF
        # report of every country's population" straight to FLUX, which
        # can't render legible text/numbers at all — the result was a
        # picture full of garbled pseudo-text standing in for a real,
        # accurate document. The elif chain below already checks
        # generated_file_spec before is_image_request, so this just lets
        # that ordering actually take effect.
        _ai_generated_file_spec(message)
        if message and not document_text
        else None
    )
    image_file_extension = _ai_image_file_request(message) if message else ''
    # "make a video of …" goes to the video model switched on under API
    # Settings; with none switched on it stays an ordinary chat turn.
    video_row = (
        media_models.active('video')
        if message and not image_data and not generated_file_spec and ai_chat.is_video_generation_request(message)
        else None
    )
    if image_file_extension:
        # Built directly from the picture already in the chat - no model call,
        # and never mistaken for a request to draw or edit a picture.
        model_key = 'quick'
        request_category = 'image_file'
    elif previous_display_image:
        model_key = 'quick'
        request_category = 'image_recall'
    elif generated_file_spec:
        document_instruction = _ai_generated_file_instruction(generated_file_spec['file_name'])
        # A real server-created download must behave the same from every
        # picker choice (including FLUX, which cannot produce text files).
        model_key = 'code'
        request_category = 'file_generation'
    elif video_row is not None:
        model_key = video_row.model_key
        request_category = 'video_generation'
    elif image_prompt_writing:
        model_key = 'vision' if image_data else 'quick'
        request_category = 'image' if image_data else 'writing'
    elif selected_model_key == ai_chat.FLUX_KLEIN_4B_MODEL_KEY:
        if source_image_data or (message and not ai_chat.is_image_capability_question(message)) or not message:
            # Once the user deliberately selects FLUX, descriptive prompts
            # such as "a robot in a futuristic classroom" are valid even
            # without an explicit generate/draw verb. Attachments are edits;
            # a bare capability question still falls back to normal chat.
            model_key = ai_chat.FLUX_KLEIN_4B_MODEL_KEY
            request_category = 'image_edit' if source_image_data else 'image_generation'
        else:
            # FLUX can only generate/edit images — it has no chat capability
            # at all, so a plain message typed while it happens to be
            # selected ("hi", a real question) is answered normally by
            # Quick instead of being forced through the image pipeline.
            model_key = 'quick'
            request_category = request_router.classify(message)
    elif is_image_request:
        # Any other model selected, but the message itself is genuinely
        # asking to create/edit an image — auto-route straight to FLUX
        # instead of just telling the user to switch models manually.
        model_key = ai_chat.FLUX_KLEIN_4B_MODEL_KEY
        request_category = 'image_edit' if image_data else 'image_generation'
    elif image_data:
        if ai_chat.is_image_edit_instruction(message):
            # An attached image already supplies the implicit subject, so an
            # edit-shaped verb alone is enough — "REmove all Names" on an
            # attached invitation (AIReport #11) has no image-shaped noun and
            # never matched is_image_request above, but sending it to Vision
            # (read-only) instead of FLUX (edit-capable) is exactly why it
            # got an unrelated reply instead of an edit.
            model_key = ai_chat.FLUX_KLEIN_4B_MODEL_KEY
            request_category = 'image_edit'
        else:
            model_key = selected_model_key if ai_chat.MODELS[selected_model_key].get('vision') else 'vision'
            # Previously left unset here — harmless only because the old
            # 'image' if image_data else request_category' response header
            # below never actually evaluated this variable on this branch.
            # Setting it explicitly avoids relying on that short-circuit.
            request_category = 'image'
    elif previous_edit_image:
        model_key = ai_chat.FLUX_KLEIN_4B_MODEL_KEY
        request_category = 'image_edit'
    elif document_mode == 'coding':
        # "Start coding" is an explicit mode choice, so use the code-tuned
        # route even if the general model picker was previously on Light/etc.
        model_key = 'code'
        request_category = 'coding'
    else:
        model_key = selected_model_key
        if chatgpt_mode:
            model_key, request_category = request_router.choose_chatgpt_worker(message)
        elif model_key == ai_chat.DEFAULT_MODEL_KEY and payload.get('auto_route', True):
            model_key, request_category = request_router.choose_model(message, model_key)
        else:
            request_category = request_router.classify(message)

    if response_model_key is None:
        response_model_key = model_key

    # Image generation/editing is available on every plan. Keep the existing
    # premium gate for text/reasoning models, including requests that merely
    # selected one of them, but never let that picker state block a turn that
    # was actually routed to the image model.
    if (
        not full_model_access
        and model_key != ai_chat.FLUX_KLEIN_4B_MODEL_KEY
        and request_category not in ('image_recall', 'image_file')
        and (
        selected_model_key not in AI_FREE_MODEL_KEYS or model_key not in AI_FREE_MODEL_KEYS
        )
    ):
        # A once-authenticated tab can become anonymous between messages
        # when its session expires or the same account logs in on another
        # device.  The browser still displays the cached account name/model,
        # so calling this a subscription failure produces a false "Request
        # Premium Access" card even for a backend-granted premium/admin
        # account.  Ask it to log in again instead; the client already keeps
        # the pending message and resumes it after authentication.
        if not request.user.is_authenticated:
            return JsonResponse({
                'status': 'login_required',
                'detail': 'Your login session ended. Log in again to continue with this model.',
            }, status=403)
        return JsonResponse({
            'status': 'subscription_required',
            'detail': (
                'The free plan includes EduTrellis Quick, Code, and image generation. '
                'Premium access is required for this model or capability.'
            ),
        }, status=403)

    # Image generation switched off on the dashboard: refuse any turn that
    # would be drawn by FLUX, whichever model the user picked.
    if (
        model_key == ai_chat.FLUX_KLEIN_4B_MODEL_KEY
        and not ai_chat.is_model_enabled(ai_chat.FLUX_KLEIN_4B_MODEL_KEY)
    ):
        return JsonResponse({
            'status': 'model_disabled',
            'detail': 'Image generation is temporarily unavailable. Please try again later.',
        }, status=403)

    if model_key == ai_chat.FLUX_KLEIN_4B_MODEL_KEY and not message:
        return JsonResponse({
            'status': 'error',
            'detail': 'Describe the image you want, or how you want the attached image changed.',
        }, status=400)

    if not request.user.is_authenticated:
        if not request.session.session_key:
            request.session.create()
        # IP-derived rather than the session counter alone — the session
        # counter still gets tracked below for the on-page meter, but an
        # incognito window (or just clearing cookies) gets a fresh session
        # for free, so it can't be what actually gates access. Falls back to
        # the session counter only if the IP genuinely couldn't be read.
        guest_count = _ip_free_messages_used(ip) if ip and ip != 'unknown' else request.session.get('ai_guest_msg_count', 0)
        if guest_count >= AI_GUEST_MESSAGE_LIMIT:
            return JsonResponse({
                'status': 'login_required',
                'detail': "You've reached the free message limit — log in or sign up to keep chatting. Your conversation is saved and will carry over.",
            }, status=403)
    else:
        gate = _ai_profile_gate(request.user, ip)
        if gate:
            return JsonResponse(gate, status=403)

    if (
        video_row is not None and request.user.is_authenticated and not _ai_has_admin_access(request.user)
        and AIVideoJob.objects.filter(user=request.user, created_at__date=timezone.localdate()).count() >= AI_VIDEO_DAILY_LIMIT
    ):
        return JsonResponse({
            'status': 'rate_limited',
            'detail': f'Daily video limit reached. You can make up to {AI_VIDEO_DAILY_LIMIT} videos per day. Try again tomorrow.',
            'limit': AI_VIDEO_DAILY_LIMIT,
        }, status=429)

    if (
        model_key == ai_chat.FLUX_KLEIN_4B_MODEL_KEY
        and (getattr(request.user, 'email', '') or '').strip().lower() not in AI_UNLIMITED_IMAGE_EMAILS
        and _ai_image_daily_count(request) >= AI_IMAGE_DAILY_LIMIT
    ):
        return JsonResponse({
            'status': 'rate_limited',
            'detail': (
                f'Daily image limit reached. You can generate up to '
                f'{AI_IMAGE_DAILY_LIMIT} images per day. Try again tomorrow.'
            ),
            'limit': AI_IMAGE_DAILY_LIMIT,
        }, status=429)

    owner_filter = _ai_owner_filter(request)
    conversation_id = payload.get('conversation_id')
    conversation = None
    if conversation_id:
        # payload is untrusted JSON — conversation_id could be a string, a
        # float, a list/dict, etc. Casting explicitly here means a malformed
        # value is just "not found" instead of an unhandled TypeError from
        # the ORM's pk lookup (which, with DEBUG on, would otherwise hand
        # back a full stack trace to whoever sent it).
        try:
            conversation_id = int(conversation_id)
        except (TypeError, ValueError):
            return JsonResponse({'status': 'error', 'detail': 'Conversation not found.'}, status=404)
        conversation = AIConversation.objects.filter(owner_filter, pk=conversation_id).first()
        if not conversation:
            return JsonResponse({'status': 'error', 'detail': 'Conversation not found.'}, status=404)
    if conversation is None:
        title = message[:AI_CONVERSATION_TITLE_CHARS] if message else (document_name or 'Image')
        conv_ip = ip if ip and ip != 'unknown' else None
        if request.user.is_authenticated:
            conversation = AIConversation.objects.create(user=request.user, title=title, ip_address=conv_ip)
        else:
            conversation = AIConversation.objects.create(session_key=request.session.session_key, title=title, ip_address=conv_ip)

    if replace_target is not None and replace_target.conversation_id == conversation.id:
        conversation.messages.filter(pk__gte=replace_target.pk).update(superseded=True)
    # From here the old turn is flagged in the database, so the temporary
    # "ignore everything from the replaced message on" rule must not also hide
    # the message created next.
    request._ai_replace_from_pk = None
    user_message = AIMessage.objects.create(
        conversation=conversation, role=AIMessage.ROLE_USER, content=message,
        image_data=stored_image or image_data, document_name=document_name,
        document_text=document_text or image_ocr_text,
        reply_to_text=_ai_reply_quote(payload),
    )
    if user_message.image_data.startswith('data:image/'):
        _ai_store_attachment_later(user_message.pk, image_data)
    request._ai_user_message_id = user_message.pk
    conversation.updated_at = timezone.now()
    conversation.save(update_fields=['updated_at'])
    request.session[AI_CURRENT_CONVERSATION_SESSION_KEY] = conversation.id

    if image_file_extension:
        return _ai_image_file_response(
            request, conversation, user_message, image_file_extension, response_model_key,
        )
    if previous_display_image:
        return _ai_recalled_image_response(
            conversation, previous_display_image, response_model_key,
        )

    # My Notes: 'show my notes' / 'delete note about X' / 'edit note about X
    # to Y' / 'take this note' — handled entirely here, no AI model call, so
    # none of it costs a free-message credit. Skipped whenever an image or
    # document is attached so that kind of turn can never get mistaken for
    # note-taking.
    if message and not image_data and not document_text:
        pending_response = _ai_pending_note_edit_response(request, conversation, message)
        if pending_response is not None:
            return pending_response
        if request_router.is_show_notes_intent(message):
            return _ai_show_notes_response(request, conversation)
        read_target = request_router.match_read_note(message)
        if read_target is not None:
            return _ai_read_note_response(request, conversation, read_target)
        delete_target = request_router.match_delete_note(message)
        if delete_target is not None:
            return _ai_delete_note_response(request, conversation, delete_target)
        edit_match = request_router.match_edit_note(message)
        if edit_match is not None:
            return _ai_edit_note_response(request, conversation, edit_match[0], edit_match[1], message)
        if request_router.is_contextual_note_edit(message):
            contextual_response = _ai_contextual_note_edit_response(request, conversation, message)
            if contextual_response is not None:
                return contextual_response
        if request_router.is_note_intent(message):
            return _ai_save_note_response(request, conversation, message)

    if not request.user.is_authenticated:
        request.session['ai_guest_msg_count'] = request.session.get('ai_guest_msg_count', 0) + 1
    elif not _ai_has_admin_access(request.user):
        # Only counts against the free-tier cap while unsubscribed — a
        # subscribed account's messages shouldn't erode the free allotment
        # that's waiting for them once the subscription lapses.
        StoreProfile.objects.filter(user=request.user).exclude(
            ai_subscription_until__gt=timezone.now(),
        ).update(ai_free_messages_used=F('ai_free_messages_used') + 1)

    if model_key == ai_chat.FLUX_KLEIN_4B_MODEL_KEY:
        # request.user is already loaded by the auth middleware, so reading the
        # email here is free — resolving it inside the upload thread would cost
        # an extra query and a database connection per image.
        owner_email = request.user.email if request.user.is_authenticated else ''
        return _ai_flux_response(
            conversation, image_generation_prompt, source_image_data, response_model_key,
            owner_email=owner_email,
        )
    if video_row is not None:
        return _ai_video_response(request, conversation, message, video_row, response_model_key)

    # First-time-in-AI-chat onboarding: ask a genuinely new user (once) for
    # their name/location/Instagram, then deterministically capture whatever
    # they give in their very next reply — never trust the model itself to
    # record it, same reasoning as the My Notes system above. Skipped for
    # staff (site admins/developers, not real customers) and guests (there's
    # no account to save it against).
    onboarding_ask = False
    if request.user.is_authenticated and not _ai_has_admin_access(request.user):
        onboarding_profile, _ = StoreProfile.objects.get_or_create(user=request.user)
        if onboarding_profile.ai_onboarding_pending:
            fields = ai_chat.extract_onboarding_fields(message) if message else {}
            update_fields = ['ai_onboarding_pending', 'ai_onboarded']
            onboarding_profile.ai_onboarding_pending = False
            onboarding_profile.ai_onboarded = True
            if fields.get('name'):
                onboarding_profile.ai_display_name = fields['name'][:100]
                update_fields.append('ai_display_name')
            if fields.get('location'):
                onboarding_profile.ai_location = fields['location'][:150]
                update_fields.append('ai_location')
            if fields.get('instagram'):
                onboarding_profile.ai_instagram_handle = fields['instagram'].lstrip('@')[:60]
                update_fields.append('ai_instagram_handle')
            onboarding_profile.save(update_fields=update_fields)
        elif not onboarding_profile.ai_onboarded:
            onboarding_ask = True
            onboarding_profile.ai_onboarding_pending = True
            onboarding_profile.save(update_fields=['ai_onboarding_pending'])

    recent = list(
        _ai_visible_messages(request, conversation).order_by('-created_at')
        .values('pk', 'role', 'content', 'image_data', 'document_name', 'document_text', 'reply_to_text')[:AI_CHAT_MAX_HISTORY]
    )
    recent.reverse()

    model_cfg = ai_chat.MODELS[model_key]

    # Older messages already folded into the conversation's running summary
    # are sent as that summary instead (see ai_app.context_compress). An
    # edit/retry of a message the summary covers makes it stale, so it goes.
    replace_from = getattr(request, '_ai_replace_from_pk', None)
    if replace_from:
        context_compress.forget_summary(conversation, replace_from)
    summary_until = conversation.context_summary_until if conversation.context_summary else 0

    clean_history = []
    for index, m in enumerate(recent):
        if m['pk'] <= summary_until and index < len(recent) - 1:
            continue
        is_current_image = bool(m['image_data']) and index == len(recent) - 1
        if is_current_image and model_cfg['vision']:
            ocr_note = f"\n\nLocally detected text:\n{m['document_text']}" if m['document_text'] else ''
            content = [
                {'type': 'text', 'text': (m['content'] or 'Describe and analyse this image.') + ocr_note},
                # The bytes from this request: the row may already point at Dropbox.
                {'type': 'image_url', 'image_url': {'url': image_data or m['image_data']}},
            ]
        elif m['image_data'] and m['role'] == AIMessage.ROLE_ASSISTANT:
            # This is a generated media URL, not a new user attachment.
            content = (m['content'] + ' [An image was generated in this turn.]').strip()
        elif m['image_data']:
            # Historical base64 image bytes are not resent. The earlier
            # assistant analysis remains in history, with OCR as backup.
            ocr_note = f" Earlier image OCR:\n{m['document_text']}" if m['document_text'] else ''
            content = (m['content'] + ' [Earlier image was analysed.]' + ocr_note).strip()
        elif m['document_name'] and m['document_text']:
            # Persisted in full (capped by doc_extract.MAX_CHARS) so a
            # follow-up question about this document works without
            # re-uploading it, for as long as it stays within the replayed
            # AI_CHAT_MAX_HISTORY window.
            content = (
                f"[Attached document: {m['document_name']}]\n{m['document_text']}\n\n---\n"
                f"{m['content'] or 'Please review the attached document.'}"
            )
        elif m['document_name']:
            # Older row from before document_text was persisted — degrade
            # to a plain filename note instead of silently having nothing.
            content = (m['content'] + f" [Attached document: {m['document_name']}]").strip()
        elif m['role'] == AIMessage.ROLE_USER and len(m['content'] or '') > context_compress.LONG_MESSAGE_CHARS:
            # A very long message goes with its middle shortened: the one being
            # answered now is condensed on the spot, an earlier one reuses the
            # copy made when it was sent. The saved message itself is unchanged.
            if index == len(recent) - 1 and not generated_file_spec and not document_instruction:
                content = context_compress.condense_long_message(m['content']) or m['content']
            else:
                content = context_compress.cached_condensed(m['content']) or m['content']
        else:
            content = m['content']
        if m['reply_to_text'] and m['role'] == AIMessage.ROLE_USER:
            reply_context = _ai_reply_context(m['reply_to_text'])
            if isinstance(content, list):
                content[0]['text'] = reply_context + content[0]['text']
            else:
                content = reply_context + content
        clean_history.append({'role': m['role'], 'content': content})

    # Drop the oldest turns (documents/images first pushed the total over
    # budget) until the whole history fits, always keeping at least the
    # current turn — a long-running conversation with attachments degrades
    # gracefully instead of eventually overflowing the model's context
    # window (see AI_CHAT_HISTORY_CHAR_BUDGET above).
    def _content_char_len(content):
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            return sum(len(b.get('text', '')) for b in content if isinstance(b, dict))
        return 0

    history_chars = sum(_content_char_len(m['content']) for m in clean_history)
    history_start = 0
    while len(clean_history) - history_start > 1 and history_chars > AI_CHAT_HISTORY_CHAR_BUDGET:
        history_chars -= _content_char_len(clean_history[history_start]['content'])
        history_start += 1
    clean_history = clean_history[history_start:]

    # Keep saved conversation text intact for the user, but redact common
    # personal identifiers in the copy sent to the external model.
    for history_message in clean_history:
        content = history_message['content']
        if isinstance(content, str):
            history_message['content'] = privacy.redact(content)
        elif isinstance(content, list):
            for block in content:
                if block.get('type') == 'text':
                    block['text'] = privacy.redact(block.get('text', ''))

    if summary_until:
        clean_history.insert(0, {
            'role': 'system',
            'content': privacy.redact(context_compress.summary_note(conversation.context_summary)),
        })

    user_context = _ai_user_context(request.user) if request.user.is_authenticated else None
    if user_context:
        user_context = privacy.redact(user_context)

    # Once the secret phrase has appeared anywhere in this conversation, the
    # warm/personal tone stays on for the rest of it rather than resetting
    # every message. The greeting/introduction itself, though, should only
    # happen on the message where she actually said the phrase — not be
    # repeated on every later message just because the tone stays on.
    #
    # Sumudrika and Jagu are deliberately mutually exclusive within one
    # conversation — whichever trigger phrase appears EARLIEST wins and
    # locks in for the rest of it, so a later "my name is jagu" mentioned
    # mid-Sumudrika-conversation (or vice versa) can't make the model try
    # to address two different people as the same "her" and get confused.
    sumudrika_idx = next(
        (i for i, m in enumerate(recent) if m['role'] == AIMessage.ROLE_USER and ai_chat.is_sumudrika_trigger(m['content'])),
        None,
    )
    jagu_idx = next(
        (i for i, m in enumerate(recent) if m['role'] == AIMessage.ROLE_USER and ai_chat.is_jagu_trigger(m['content'])),
        None,
    )
    if sumudrika_idx is not None and (jagu_idx is None or sumudrika_idx <= jagu_idx):
        is_sumudrika, is_jagu = True, False
    elif jagu_idx is not None:
        is_sumudrika, is_jagu = False, True
    else:
        is_sumudrika, is_jagu = False, False

    is_sumudrika_greet = is_sumudrika and recent[-1]['role'] == AIMessage.ROLE_USER \
        and ai_chat.is_sumudrika_trigger(recent[-1]['content'])
    is_jagu_greet = is_jagu and recent[-1]['role'] == AIMessage.ROLE_USER \
        and ai_chat.is_jagu_trigger(recent[-1]['content'])
    # Checked only against the current message, not the whole history — see
    # ai_chat.is_persona_farewell.
    is_persona_farewell = (is_sumudrika or is_jagu) and bool(recent) \
        and recent[-1]['role'] == AIMessage.ROLE_USER and ai_chat.is_persona_farewell(recent[-1]['content'])

    # The Sumudrika/Jagu personas depend on subtle instruction-following
    # (warm tone, correct pronouns, never fabricating a quote from Rudra)
    # that live-testing showed the smaller/faster modes don't reliably
    # deliver — EduTrellis Quick (the site default) was caught inventing
    # fake quotes attributed to Rudra and misgendering him. These are rare,
    # low-volume, personally-important conversations, so correctness wins
    # over speed/cost here: force EduTrellis Ultra once triggered,
    # regardless of whatever mode was actually selected — except when an
    # image is attached this turn, since Ultra has no vision capability and
    # that has to win.
    if full_model_access and (is_sumudrika or is_jagu) and model_key != 'vision':
        model_key = 'ultra'

    # Verified, static business-fact grounding only — never a saved/editable
    # answer store. No AI reply is ever generated from a knowledge base or
    # cached past answer.
    retrieved_context = retrieved_source = None
    web_sources = []
    recent_company_text = ' '.join(
        str(item.get('content') or '') for item in recent[-5:]
        if isinstance(item.get('content'), str)
    )
    power = payload.get('power')
    if power not in ai_chat.POWER_LEVELS:
        power = ai_chat.DEFAULT_POWER
    web_search_enabled = payload.get('web_search') is True
    if (
        web_search_enabled and message and not image_data and not document_text
    ):
        # Used to also exclude generated_file_spec, which meant manually
        # enabling web search did nothing for a file-generation turn — the
        # one case where grounding matters most, since the output is a
        # downloadable document rather than a reply that scrolls away.
        search_started = time.perf_counter()
        web_context, web_sources = web_search.build_context_with_sources(message)
        logger.info(
            "AI timing web_search=%.3fs hit=%s forced=True",
            time.perf_counter() - search_started, bool(web_context),
        )
        if web_context:
            retrieved_context = web_context
            retrieved_source = 'web_search'
    elif message and company_knowledge.is_company_query(recent_company_text):
        retrieved_context = company_knowledge.public_site_context()
        retrieved_source = 'company_site'
    elif message and not image_data and (
        # A downloadable reference document (population/GDP/rankings/etc.)
        # gets the broader, no-freshness-word-required check — see
        # web_search.needs_search_for_document — since a model's confident,
        # unsourced guess at real-world figures is exactly what produced
        # AIReport's population-by-country PDF full of made-up numbers.
        web_search.needs_search_for_document(message) if generated_file_spec
        else web_search.needs_search(message)
    ):
        # Anything time-sensitive ("latest", "today's rate", "who won") is
        # otherwise answered from the model's training cutoff. Only runs when
        # the wording actually calls for it — this is a network round trip on
        # the critical path — and a failure just returns None, leaving the
        # model to answer as it did before.
        search_started = time.perf_counter()
        web_context, web_sources = web_search.build_context_with_sources(message)
        logger.info(
            "AI timing web_search=%.3fs hit=%s", time.perf_counter() - search_started,
            bool(web_context),
        )
        if web_context:
            retrieved_context = web_context
            retrieved_source = 'web_search'

    logger.info(
        "AI timing preprocessing=%.3fs model=%s history=%d image=%s ocr_chars=%d",
        time.perf_counter() - request_started, model_key, len(recent),
        bool(image_data), len(image_ocr_text),
    )

    def model_stream(instruction=document_instruction):
        if voice_instruction and not generated_file_spec:
            instruction = ((instruction or '') + ' ' + voice_instruction).strip()
        return ai_chat.stream_chat(
            clean_history, model_key=model_key,
            identity_model_key=(response_model_key if response_model_key != model_key else None),
            user_context=user_context,
            retrieved_context=retrieved_context, retrieved_source=retrieved_source,
            sumudrika=is_sumudrika, sumudrika_greet=is_sumudrika_greet,
            jagu=is_jagu, jagu_greet=is_jagu_greet,
            persona_farewell=is_persona_farewell, language=language,
            document_instruction=instruction,
            max_tokens=(
                AI_GENERATED_FILE_MAX_OUTPUT_TOKENS
                if generated_file_spec
                else AI_DOCUMENT_CODE_MAX_OUTPUT_TOKENS
                if document_mode == 'coding' or ai_chat.wants_long_form_output(message)
                else None
            ),
            onboarding_ask=onboarding_ask,
            power=power,
        )

    def event_stream():
        full_reply = ''
        saved_reply = ''
        # How much of the sanitized reply the browser already has. Only
        # meaningful on the hide_chatgpt_worker path below, which releases
        # text progressively instead of all at once.
        released_chars = 0
        had_error = False
        hide_chatgpt_worker = response_model_key == ai_chat.CHATGPT_56_MODEL_KEY
        # The mirror case: a plain Vidhyora mode must never claim the
        # ChatGPT 5.6 persona's name — see _vidhyora_public_reply.
        fix_wrong_persona_identity = response_model_key not in (
            ai_chat.CHATGPT_56_MODEL_KEY, ai_chat.SOL_MODEL_KEY, ai_chat.TERRA_MODEL_KEY,
        )
        # Refreshes MODELS' brand-suffixed labels before reading one — usually
        # already synced by an earlier stream_chat()/ai_page call this
        # process, but not guaranteed (e.g. this is the very first AI request
        # since a restart, or a developer-API caller who never loaded the
        # page), so this call makes response_model_label reliable regardless.
        ai_chat.get_ai_brand_name()
        response_model_label = ai_chat.MODELS.get(response_model_key, {}).get('label', 'EduTrellis AI')
        # A file-generation turn gets the same held-back streaming on every
        # model, because the model may write its own fabricated download link
        # mid-reply and streamed text cannot be taken back. public_text is run
        # over the WHOLE reply each time and the browser is only ever fed from
        # its output, so a fake link is removed before any of it is released.
        owner_identity = ai_chat.owner_identity(response_model_key)

        def public_text(text):
            if owner_identity:
                text = _owner_identity_public_reply(text, owner_identity, response_model_label)
            elif hide_chatgpt_worker:
                text = _chatgpt_public_reply(text, ai_chat.chatgpt_persona_name(response_model_key))
            elif fix_wrong_persona_identity:
                text = _vidhyora_public_reply(text, response_model_label)
            if generated_file_spec:
                text = _strip_fake_download_links(text)
            return text

        buffered = hide_chatgpt_worker or fix_wrong_persona_identity or bool(owner_identity) or bool(generated_file_spec)
        try:
            for chunk in model_stream():
                full_reply += chunk
                # Generated files are an internal construction payload. Showing
                # thousands of raw HTML/CSS tokens in a narrow chat bubble made
                # the page look broken and exposed incomplete output before it
                # could be validated. Keep it behind the progress indicator.
                if generated_file_spec:
                    continue
                if not buffered:
                    yield chunk
                    continue
                # ChatGPT 5.6 used to withhold the entire reply until
                # generation finished, so its (default, most-used) mode showed
                # nothing at all for the whole wait — the single biggest cause
                # of the chat feeling slow. It streams now, holding back only
                # a short tail: _chatgpt_public_reply is re-run over the whole
                # reply each time and only the part older than the tail is
                # released, so a worker name split across chunks ("FL" + "UX")
                # or a sentence-spanning identity claim is still rewritten
                # before any of it can reach the browser.
                sanitized = public_text(full_reply)
                safe_upto = len(sanitized) - CHATGPT_STREAM_HOLDBACK_CHARS
                if safe_upto > released_chars:
                    yield sanitized[released_chars:safe_upto]
                    released_chars = safe_upto
            if buffered and not generated_file_spec:
                full_reply = public_text(full_reply)
                if len(full_reply) > released_chars:
                    yield full_reply[released_chars:]
                    released_chars = len(full_reply)
            if generated_file_spec:
                try:
                    file_content = _extract_ai_generated_file_content(
                        full_reply, generated_file_spec['file_name'],
                    )
                    if not file_content.strip() and full_reply.strip() and not (
                        _AI_CLARIFYING_REPLY_RE.search(full_reply)
                        or full_reply.rstrip().endswith('?')
                    ):
                        # A token-limited or malformed first attempt is retried
                        # silently once with a stronger completion reminder.
                        retry_instruction = (
                            document_instruction
                            + " The previous attempt was incomplete or split across files. Start again "
                            "from the beginning and return a smaller COMPLETE file that obeys every rule."
                        )
                        full_reply = ''.join(model_stream(retry_instruction))
                        file_content = _extract_ai_generated_file_content(
                            full_reply, generated_file_spec['file_name'],
                        )
                    # No usable content means the model asked a question back
                    # (or only produced a fabricated link). Creating a file
                    # here would hand the user a document containing their own
                    # question, plus a second, dead download link.
                    if not file_content.strip():
                        raise _NoGeneratedFileContent

                    generated_file = AIGeneratedFile.objects.create(
                        user=request.user if request.user.is_authenticated else None,
                        session_key='' if request.user.is_authenticated else (request.session.session_key or ''),
                        file_name=generated_file_spec['file_name'],
                        content=file_content,
                    )
                    download_url = request.build_absolute_uri(reverse(
                        'ai_generated_file_download', args=[generated_file.token],
                    ))
                    saved_reply = (
                        f"Your file is ready.\n\n"
                        f"[Download {generated_file.file_name}]({download_url})"
                    )
                    yield saved_reply
                except _NoGeneratedFileContent:
                    cleaned_reply = public_text(full_reply).strip()
                    if (
                        cleaned_reply and (
                            _AI_CLARIFYING_REPLY_RE.search(cleaned_reply)
                            or cleaned_reply.endswith('?')
                        )
                    ):
                        saved_reply = cleaned_reply
                    else:
                        saved_reply = (
                            "I couldn’t finish a complete, valid file, so no broken download was created. "
                            "Please retry with a slightly shorter website request."
                        )
                    yield saved_reply
                except Exception:
                    # The model response is still useful if persistence ever
                    # fails; don't mislabel a completed answer as a stream
                    # failure merely because its download could not be saved.
                    logger.exception("Failed to save an AI-generated download")
                    saved_reply = "I couldn’t save the downloadable file. Please try again."
                    yield saved_reply
        except Exception as e:
            # ai_chat.stream_chat already retries transient failures on its
            # own before ever raising here — reaching this point means every
            # retry failed (or a real answer had already started streaming
            # when it broke, so a clean retry wasn't possible). Either way,
            # what's in full_reply (if anything) is not a complete, trustworthy
            # answer, so it must never be saved as one.
            logger.exception("AI chat stream failed after retries: %s", e)
            had_error = True
            if generated_file_spec:
                saved_reply = (
                    "File generation was interrupted before the file was complete. "
                    "No broken download was created; please try again."
                )
                yield saved_reply
            elif full_reply.strip():
                # A dropped mobile connection or upstream stream can happen
                # after useful text has arrived. Keep that text visible
                # instead of replacing it with a generic failure message.
                # Only the still-withheld tail is emitted — the rest already
                # reached the browser progressively, and re-sending the whole
                # reply here would show it twice.
                if hide_chatgpt_worker or fix_wrong_persona_identity:
                    sanitized = public_text(full_reply)
                    if len(sanitized) > released_chars:
                        yield sanitized[released_chars:]
                        released_chars = len(sanitized)
                yield "\n\n[Response interrupted. You can retry if anything is missing.]"
            elif ai_chat._is_context_length_error(e):
                # Retrying would just fail again identically — the fixed
                # generic message was misleading here, since it reads as
                # transient when the real fix is a shorter message/thread.
                yield AI_REPLY_FAILED_MARK + (
                    "This conversation (or an attached document) has gotten "
                    "too long for the AI to process in one go. Please start "
                    "a new chat, or ask about a shorter excerpt."
                )
            else:
                yield AI_REPLY_FAILED_MARK + _ai_chat_failure_reply(
                    e, response_model_key,
                    is_staff=_ai_has_admin_access(request.user),
                )
        finally:
            # The conversation can have been deleted (by this same user, in
            # another tab, or via the sidebar delete button) while this reply
            # was still streaming — check it still exists before trying to
            # attach a message to it, instead of letting that blow up here.
            message_reply = _normalize_ai_home_links(saved_reply or full_reply)
            if message_reply.strip() and not had_error:
                try:
                    if AIConversation.objects.filter(pk=conversation.pk).exists():
                        AIMessage.objects.create(
                            conversation=conversation, role=AIMessage.ROLE_ASSISTANT,
                            content=message_reply, model_key=response_model_key,
                            web_sources=web_sources,
                        )
                        context_compress.schedule_fold(conversation)
                except Exception:
                    logger.exception("Failed to save AI assistant reply for conversation %s", conversation.pk)

    response = StreamingHttpResponse(event_stream(), content_type='text/plain; charset=utf-8')
    response['Cache-Control'] = 'private, no-cache, no-store, no-transform'
    response['X-Accel-Buffering'] = 'no'
    response['X-Conversation-Id'] = str(conversation.id)
    response['X-Model-Key'] = response_model_key
    response['X-Routed-Model-Key'] = _ai_public_routed_model_key(response_model_key, model_key)
    # 'file_generation' is the one legitimate case where an attached image
    # doesn't mean a vision-analysis turn — AIReport #33 attached a logo
    # alongside an explicit "...in pdf" request, and it must still report
    # as the real download it produced, not get folded into plain 'image'.
    response['X-Request-Category'] = (
        request_category if not image_data or request_category == 'file_generation' else 'image'
    )
    # Tells the frontend to auto-play this reply and show the persona
    # follow-up chips — true for every turn once the matching trigger
    # phrase has appeared anywhere in the conversation (same scope as
    # is_sumudrika/is_jagu above, not just the one turn that said it).
    response['X-Sumudrika'] = '1' if is_sumudrika else ''
    response['X-Jagu'] = '1' if is_jagu else ''
    # Tells the frontend this was her goodbye reply — lock the composer
    # instead of showing the usual follow-up chips (see is_persona_farewell
    # above; ai_chat.stream_chat was told to make this a closing message).
    response['X-Persona-End'] = '1' if is_persona_farewell else ''
    if web_sources:
        # Base64 JSON: page titles are often not plain ASCII, which a header can't carry.
        response['X-Web-Sources'] = base64.b64encode(json.dumps(web_sources).encode('utf-8')).decode('ascii')
    return response


# ── Deep research (/ai/api/research/) ───────────────────────────────────────
# A question is planned, searched on the web, read and written up as a cited
# report with PDF / Word / Markdown downloads. The research itself lives in
# ai_app.deep_research; this is the web side of it: who may run it, how often,
# the progress stream and what gets saved.

AI_RESEARCH_PLAN_LIMIT = 20          # plans per 10 minutes, per account
AI_RESEARCH_RUN_LIMIT = 8            # runs started per 10 minutes, per account
AI_RESEARCH_DAILY_LIMIT = 3          # finished reports a day on the free plan
AI_RESEARCH_DAILY_LIMIT_FULL = 20    # ...and for staff and subscribers
AI_RESEARCH_MAX_PARALLEL = 2         # reports being researched at once, per server process
AI_RESEARCH_ACTIVE_SECONDS = 420     # a person's one-at-a-time lock is dropped after this long
AI_RESEARCH_QUESTION_CHARS = 2000
AI_RESEARCH_FORMATS = (('pdf', 'PDF'), ('docx', 'Word'), ('md', 'Markdown'))
_AI_RESEARCH_SLOTS = threading.BoundedSemaphore(AI_RESEARCH_MAX_PARALLEL)


def _ai_research_used_today(user):
    """Reports this account finished today (each one leaves a .md file)."""
    return AIGeneratedFile.objects.filter(
        user=user, file_name__startswith='deep-research-', file_name__endswith='.md',
        created_at__date=timezone.localdate(),
    ).count()


def _ai_research_daily_limit(user):
    if (user.email or '').strip().lower() in AI_UNLIMITED_IMAGE_EMAILS:
        return None
    return AI_RESEARCH_DAILY_LIMIT_FULL if _ai_has_full_model_access(user) else AI_RESEARCH_DAILY_LIMIT


def _ai_research_models(user, requested):
    """(model shown to the person, model that does the work, persona) for a
    research turn, or None when no model is available. A report is general
    writing, so the coding mode does the work as Quick; a free account gets
    Quick whatever it picked, the same as everywhere else."""
    full_access = _ai_has_full_model_access(user)
    fallback = (
        ai_chat.SOL_MODEL_KEY if full_access and ai_chat.is_model_enabled(ai_chat.SOL_MODEL_KEY) else 'quick'
    )
    shown = requested if requested in ai_chat.MODELS else fallback
    if shown in ('vision', 'code', ai_chat.FLUX_KLEIN_4B_MODEL_KEY):
        shown = fallback if shown != 'code' else 'quick'
    if not full_access and shown not in AI_FREE_MODEL_KEYS:
        shown = 'quick'
    if not ai_chat.is_model_enabled(shown):
        shown = fallback
        if not ai_chat.is_model_enabled(shown):
            return None
    if shown == ai_chat.CHATGPT_56_MODEL_KEY:
        return shown, 'quick', shown     # Luna: a Quick worker, answering under Luna's name and key
    return shown, shown, None


def _ai_research_rate_limited(request, name, limit):
    key = f'ai_research_{name}_rate:{request.user.pk}'
    cache.add(key, 0, AI_CHAT_RATE_WINDOW)
    try:
        count = cache.incr(key)
    except ValueError:      # expired between add and incr
        cache.set(key, 1, AI_CHAT_RATE_WINDOW)
        count = 1
    return count > limit


def _ai_research_chat_context(request, payload):
    """The last few turns of the open conversation, as a few short redacted
    lines — enough for "now compare it with Chrome" to know its subject."""
    try:
        conversation = AIConversation.objects.filter(
            _ai_owner_filter(request), pk=int(payload.get('conversation_id')),
        ).first()
    except (TypeError, ValueError):
        conversation = None
    if conversation is None:
        return ''
    _ai_replace_target(request, payload)
    rows = list(
        _ai_visible_messages(request, conversation).exclude(content='')
        .order_by('-created_at', '-pk').values_list('role', 'content')[:deep_research.CONTEXT_TURNS]
    )
    rows.reverse()
    return privacy.redact(deep_research.chat_context(rows))


def _ai_research_request(request, kind):
    """Checks shared by the plan and the run. Returns ``(context, None)`` or
    ``(None, error response)``."""
    if request.method != 'POST':
        return None, JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return None, JsonResponse({
            'status': 'login_required',
            'detail': 'Log in to use deep research — the finished report is saved to your account.',
        }, status=403)
    user = request.user
    if not _ai_has_admin_access(user):
        ip = _client_ip(request)
        block = Q(user=user)
        if ip and ip != 'unknown':
            block |= Q(ip_address=ip)
        if AIBlock.objects.filter(block).exists():
            return None, JsonResponse({
                'status': 'error',
                'detail': "Your access to EduTrellis AI has been restricted. Contact support if you think this is a mistake.",
            }, status=403)
    if _ai_research_rate_limited(
        request, kind, AI_RESEARCH_PLAN_LIMIT if kind == 'plan' else AI_RESEARCH_RUN_LIMIT,
    ):
        return None, JsonResponse({
            'status': 'rate_limited',
            'detail': 'Too many research requests in a short time — please wait a few minutes and try again.',
        }, status=429)
    payload = _parse_json_body(request)
    if not isinstance(payload, dict):
        return None, JsonResponse({'status': 'error', 'detail': 'Invalid request body.'}, status=400)
    message = str(payload.get('message', ''))[:AI_RESEARCH_QUESTION_CHARS].strip()
    if len(message) < 3:
        return None, JsonResponse({'status': 'error', 'detail': 'Tell me what you would like researched.'}, status=400)
    gate = _ai_profile_gate(user, _client_ip(request))
    if gate:
        return None, JsonResponse(gate, status=403)
    limit = _ai_research_daily_limit(user)
    if limit is not None and _ai_research_used_today(user) >= limit:
        return None, JsonResponse({
            'status': 'rate_limited',
            'detail': (
                f"You have used today's {limit} deep research report{'s' if limit != 1 else ''}. "
                'The limit resets tomorrow' + ('' if _ai_has_full_model_access(user) else ' — premium access raises it') + '.'
            ),
        }, status=429)
    models = _ai_research_models(user, payload.get('model'))
    if models is None:
        return None, JsonResponse({
            'status': 'model_disabled',
            'detail': 'Deep research is temporarily unavailable. Please try again later.',
        }, status=403)
    requested_language = payload.get('language')
    language = requested_language if requested_language in ai_chat.LANGUAGES else ai_chat.DEFAULT_LANGUAGE
    return {
        'payload': payload, 'message': message, 'language': language,
        'shown_model': models[0], 'worker_model': models[1], 'identity_model': models[2],
    }, None


def ai_research_plan(request):
    """Step one: a short research plan the person can read, edit and start."""
    ctx, error = _ai_research_request(request, 'plan')
    if error:
        return error
    started = time.perf_counter()
    plan = deep_research.make_plan(
        privacy.redact(ctx['message']),
        model_key=ctx['worker_model'], identity_model_key=ctx['identity_model'],
        language_name=ai_chat.LANGUAGES.get(ctx['language'], 'English'),
        chat_context=_ai_research_chat_context(request, ctx['payload']),
    )
    logger.info('AI timing research_plan=%.3fs fallback=%s', time.perf_counter() - started, bool(plan.get('fallback')))
    return JsonResponse({'status': 'ok', **deep_research.plan_for_page(plan)})


def _ndjson(event):
    return json.dumps(event, ensure_ascii=False) + '\n'


def ai_research_run(request):
    """Step two: research the confirmed plan and write the report. The reply is
    a stream of JSON lines — progress events, then the report text as it is
    written, then one ``final`` event with the finished report and its files."""
    ctx, error = _ai_research_request(request, 'run')
    if error:
        return error
    user = request.user
    payload, message = ctx['payload'], ctx['message']
    active_key = f'ai_research_active:{user.pk}'
    if cache.get(active_key):
        return JsonResponse({
            'status': 'busy',
            'detail': 'A deep research report is already being written for your account. Wait for it to finish.',
        }, status=409)

    question = privacy.redact(message)
    plan = deep_research.plan_from_page(payload.get('title'), payload.get('steps'), question)
    context_text = _ai_research_chat_context(request, payload)
    replace_target = _ai_replace_target(request, payload)

    conversation = None
    if payload.get('conversation_id'):
        try:
            conversation = AIConversation.objects.filter(
                _ai_owner_filter(request), pk=int(payload.get('conversation_id')),
            ).first()
        except (TypeError, ValueError):
            conversation = None
        if conversation is None:
            return JsonResponse({'status': 'error', 'detail': 'Conversation not found.'}, status=404)
    if conversation is None:
        ip = _client_ip(request)
        conversation = AIConversation.objects.create(
            user=user, title=message[:AI_CONVERSATION_TITLE_CHARS], ip_address=ip if ip and ip != 'unknown' else None,
        )
    if replace_target is not None and replace_target.conversation_id == conversation.id:
        conversation.messages.filter(pk__gte=replace_target.pk).update(superseded=True)
    request._ai_replace_from_pk = None
    user_message = AIMessage.objects.create(conversation=conversation, role=AIMessage.ROLE_USER, content=message)
    conversation.updated_at = timezone.now()
    conversation.save(update_fields=['updated_at'])
    request.session[AI_CURRENT_CONVERSATION_SESSION_KEY] = conversation.id
    if not _ai_has_admin_access(user):
        StoreProfile.objects.filter(user=user).exclude(
            ai_subscription_until__gt=timezone.now(),
        ).update(ai_free_messages_used=F('ai_free_messages_used') + 1)

    shown_model, worker_model, identity_model = ctx['shown_model'], ctx['worker_model'], ctx['identity_model']
    language = ctx['language']
    is_staff = _ai_has_admin_access(user)
    brand = ai_chat.get_ai_brand_name()
    model_label = ai_chat.MODELS.get(shown_model, {}).get('label', 'EduTrellis AI')
    persona_name = ai_chat.chatgpt_persona_name(shown_model)

    owner_identity = ai_chat.owner_identity(shown_model)

    def public_text(text):
        """Keeps the backend vendor's and other modes' names out of the report,
        the same way a chat reply does."""
        if owner_identity:
            return _owner_identity_public_reply(text, owner_identity, model_label)
        if shown_model == ai_chat.CHATGPT_56_MODEL_KEY:
            return _chatgpt_public_reply(text, persona_name)
        if shown_model not in (ai_chat.SOL_MODEL_KEY, ai_chat.TERRA_MODEL_KEY):
            return _vidhyora_public_reply(text, model_label)
        return text

    def event_stream():
        started = time.monotonic()
        if not _AI_RESEARCH_SLOTS.acquire(blocking=False):
            yield _ndjson({'t': 'error', 'msg': 'Deep research is busy with other reports right now. Please try again in a minute.'})
            return
        cache.set(active_key, True, AI_RESEARCH_ACTIVE_SECONDS)
        writing = False     # True while the model is being called: its failures get the model's own wording
        try:
            # The plan as it will be run, so the page's step list always matches the server's.
            yield _ndjson({'t': 'plan', **deep_research.plan_for_page(plan)})
            pool = deep_research.SourcePool()
            topic = deep_research.topic_of(question)
            for event in deep_research.research(plan['steps'], pool, topic=topic, question=question):
                yield _ndjson(event)
            logger.info(
                'AI timing research_gathered=%.1fs sources=%d', time.monotonic() - started, len(pool),
            )

            yield _ndjson({'t': 'write', 'i': len(plan['steps'])})
            writing = True
            stream = ai_chat.stream_chat(
                [{'role': 'user', 'content': deep_research.research_brief(question, plan, context_text)}],
                model_key=worker_model, identity_model_key=identity_model,
                retrieved_context=deep_research.build_context(pool), retrieved_source='web_search',
                language=language,
                document_instruction=deep_research.report_instruction(has_sources=bool(len(pool))),
                max_tokens=deep_research.REPORT_MAX_TOKENS,
            )
            report, released = '', 0
            for chunk in stream:
                report += chunk
                # Only text older than a short tail is released: the public-name
                # rewrite runs over the whole report each time, so a vendor name
                # split across two chunks is still caught before it is shown.
                cleaned = public_text(report)
                safe = len(cleaned) - CHATGPT_STREAM_HOLDBACK_CHARS
                if safe > released:
                    yield _ndjson({'t': 'd', 'x': cleaned[released:safe]})
                    released = safe
            writing = False
            report = public_text(report)
            if len(report.strip()) < 400:
                raise ValueError('The report came back empty or far too short.')

            created = timezone.now()
            markdown, cited = deep_research.finalize_report(
                report, pool, plan_title=plan['title'], brand=brand, created=created,
            )
            title = deep_research.report_title(markdown) or plan['title']
            slug = deep_research.file_slug(title) or 'report'
            files = []
            for extension, label in AI_RESEARCH_FORMATS:
                saved = AIGeneratedFile.objects.create(
                    user=user, file_name=f'deep-research-{slug}.{extension}', content=markdown,
                )
                files.append({
                    'kind': extension, 'label': label, 'name': saved.file_name,
                    'url': request.build_absolute_uri(reverse('ai_generated_file_download', args=[saved.token])),
                })
            stored = markdown.rstrip() + '\n\n**Download this report:** ' + ' · '.join(
                f"[{item['label']}]({item['url']})" for item in files
            ) + '\n'
            reply = AIMessage.objects.create(
                conversation=conversation, role=AIMessage.ROLE_ASSISTANT, content=stored, model_key=shown_model,
            )
            seconds = round(time.monotonic() - started)
            logger.info('AI timing research_total=%ds sources=%d cited=%d', seconds, len(pool), len(cited))
            yield _ndjson({
                't': 'final', 'md': stored, 'message_id': reply.pk, 'files': files, 'sources': cited,
                'stats': {'reviewed': len(pool), 'cited': len(cited), 'seconds': seconds, 'steps': len(plan['steps'])},
            })
        except Exception as exc:
            logger.exception('Deep research failed: %s', exc)
            if writing:
                detail = _ai_chat_failure_reply(exc, shown_model, is_staff=is_staff)
            else:
                detail = (
                    'The report could not be finished this time, and nothing was saved. '
                    'Please try again — a narrower question often works better.'
                )
            yield _ndjson({'t': 'error', 'msg': detail})
        finally:
            cache.delete(active_key)
            _AI_RESEARCH_SLOTS.release()

    response = StreamingHttpResponse(event_stream(), content_type='application/x-ndjson; charset=utf-8')
    response['Cache-Control'] = 'private, no-cache, no-store, no-transform'
    response['X-Accel-Buffering'] = 'no'
    response['X-Conversation-Id'] = str(conversation.id)
    response['X-User-Message-Id'] = str(user_message.pk)
    response['X-Model-Key'] = shown_model
    return response


AI_DOC_RATE_LIMIT = 40   # one request per file, and a message can carry several
AI_DOC_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # raw file cap, before extraction
AI_DOC_MAX_UPLOAD_MB = AI_DOC_MAX_UPLOAD_BYTES // (1024 * 1024)


def ai_extract_document(request):
    """Extracts text from an uploaded PDF/DOCX/CSV and hands it back to the
    browser — the raw file is never stored or forwarded anywhere else, and
    the caller sends the returned text back in the next /ai/api/send/ call
    (see ai_chat_send's document_text/document_name handling)."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)

    ip = _client_ip(request)
    cache_key = f'ai_doc_rate:{ip}'
    count = cache.get(cache_key, 0)
    if count >= AI_DOC_RATE_LIMIT:
        return JsonResponse(
            {'status': 'error', 'detail': "You're uploading files too quickly — please wait a bit and try again."},
            status=429,
        )
    cache.set(cache_key, count + 1, AI_CHAT_RATE_WINDOW)

    f = request.FILES.get('file')
    if not f:
        return JsonResponse({'status': 'error', 'detail': 'No file provided.'}, status=400)
    if f.size > AI_DOC_MAX_UPLOAD_BYTES:
        return JsonResponse({'status': 'error', 'detail': f'That file is too large — please use one under {AI_DOC_MAX_UPLOAD_MB}MB.'}, status=400)

    try:
        file_bytes = f.read()
        text, truncated = doc_extract.extract(f.name, file_bytes)
        coding_text, coding_truncated = doc_extract.extract_editable_source(
            f.name, file_bytes, text, extracted_truncated=truncated,
        )
    except doc_extract.ExtractError as e:
        return JsonResponse({'status': 'error', 'detail': str(e)}, status=400)
    except Exception:
        logger.exception("Document extraction failed for %s", f.name)
        return JsonResponse({'status': 'error', 'detail': "Couldn't read that file — please try a different one."}, status=400)

    return JsonResponse({
        'status': 'ok', 'filename': f.name,
        'text': text, 'truncated': truncated,
        'coding_text': coding_text, 'coding_truncated': coding_truncated,
    })


def ai_convert_file(request):
    """Convert one uploaded file into another format and return it directly.

    Runs entirely on local libraries (see file_convert) — the upload is held
    in memory for the length of the request and never stored, forwarded, or
    sent to the AI, so this costs nothing on the chat path and adds no
    latency to a reply.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)

    ip = _client_ip(request)
    cache_key = f'ai_convert_rate:{ip}'
    count = cache.get(cache_key, 0)
    if count >= AI_DOC_RATE_LIMIT:
        return JsonResponse(
            {'status': 'error', 'detail': "You're converting files too quickly — please wait a bit and try again."},
            status=429,
        )
    cache.set(cache_key, count + 1, AI_CHAT_RATE_WINDOW)

    upload = request.FILES.get('file')
    target = str(request.POST.get('target', '')).strip()
    if not upload:
        return JsonResponse({'status': 'error', 'detail': 'No file provided.'}, status=400)
    if upload.size > AI_DOC_MAX_UPLOAD_BYTES:
        return JsonResponse({'status': 'error', 'detail': f'That file is too large — please use one under {AI_DOC_MAX_UPLOAD_MB}MB.'}, status=400)
    if not target:
        return JsonResponse({'status': 'error', 'detail': 'Choose a format to convert to.'}, status=400)

    try:
        payload, filename, content_type = file_convert.convert(upload.read(), upload.name, target)
    except file_convert.ConvertError as exc:
        return JsonResponse({'status': 'error', 'detail': str(exc)}, status=400)
    except Exception:
        logger.exception("File conversion failed for %s -> %s", upload.name, target)
        return JsonResponse(
            {'status': 'error', 'detail': "Couldn't convert that file — please try a different one."},
            status=400,
        )

    response = HttpResponse(payload, content_type=content_type)
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, no-store'
    return response


def _ai_speaker_label(message):
    """Who spoke, as the chat itself labels it: "You", or the model that
    answered (never an internal worker name)."""
    if message.role == AIMessage.ROLE_USER:
        return 'You'
    return ai_chat.MODELS.get(message.model_key, {}).get('label') or 'EduTrellis AI'


def _ai_transcript_lines(conversation, messages, markdown=True):
    """One conversation as text lines. markdown=True gives "# title / ## who —
    when" headings (for the PDF and Word renderers); False gives plain text."""
    title = conversation.title or 'EduTrellis AI conversation'
    if markdown:
        lines = [f'# {title}', '']
    else:
        lines = ['=' * 60, title, '=' * 60, '']
    for message in messages:
        stamp = timezone.localtime(message.created_at).strftime('%d %b %Y, %I:%M %p')
        header = f'{_ai_speaker_label(message)} — {stamp}'
        lines.append(f'## {header}' if markdown else header)
        content = (message.content or '').strip()
        if content:
            lines.append(content)
        # The image itself isn't embedded (a generated image is a URL and an
        # upload is a data URI); note it so the transcript doesn't look like
        # it silently lost a turn.
        if message.image_data:
            lines.append('- [image in this message]' if markdown else '[image in this message]')
        if message.document_name:
            note = f'attached file: {message.document_name}'
            lines.append(f'- [{note}]' if markdown else f'[{note}]')
        lines.append('')
    return lines


def _ai_export_chat(conversation, messages):
    """A conversation as chat_export.Chat data (names, times and notes already
    resolved), for the designed PDF and the plain-text export."""
    turns = []
    for message in messages:
        local = timezone.localtime(message.created_at)
        notes = []
        if message.image_data:
            notes.append('[image in this message]')
        if message.document_name:
            notes.append(f'[attached file: {message.document_name}]')
        turns.append(chat_export.Turn(
            role='user' if message.role == AIMessage.ROLE_USER else 'assistant',
            who=_ai_speaker_label(message),
            when=local.strftime('%d %b %Y, %I:%M %p'),
            time=local.strftime('%I:%M %p'),
            content=(message.content or '').strip(),
            notes=notes,
        ))
    started = turns[0].when if turns else ''
    count = len(turns)
    return chat_export.Chat(
        title=conversation.title or 'EduTrellis AI conversation',
        turns=turns,
        started=started,
        subtitle=f'Started {started} · {count} message{"" if count == 1 else "s"}',
    )


def _ai_download_name(stem, extension):
    stem = re.sub(r'[^A-Za-z0-9_-]+', '-', stem or '').strip('-')[:60]
    return f'{stem or "conversation"}.{extension}'


def ai_conversation_export(request, conversation_id, file_format):
    """Download one conversation as a real PDF or Word document."""
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if file_format not in ('pdf', 'docx'):
        return JsonResponse({'status': 'error', 'detail': 'Unsupported export format.'}, status=400)

    conversation = AIConversation.objects.filter(
        _ai_owner_filter(request), pk=conversation_id,
    ).first()
    if not conversation:
        return JsonResponse({'status': 'error', 'detail': 'Conversation not found.'}, status=404)

    ai_chat.get_ai_brand_name()  # refresh model labels before they are printed
    live_messages = conversation.messages.filter(superseded=False).order_by('created_at', 'pk')
    try:
        if file_format == 'pdf':
            payload = chat_export.build_pdf(
                ai_chat.get_ai_brand_name(), [_ai_export_chat(conversation, live_messages)], single=True,
            )
        else:
            payload = file_convert.text_to_docx_bytes('\n'.join(_ai_transcript_lines(conversation, live_messages)))
    except file_convert.ConvertError as exc:
        return JsonResponse({'status': 'error', 'detail': str(exc)}, status=503)

    response = HttpResponse(payload, content_type=file_convert.mime_for(file_format))
    response['Content-Disposition'] = f'attachment; filename="{_ai_download_name(conversation.title, file_format)}"'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, no-store'
    return response


def _ai_message_with_sources(message):
    """A reply's own text, with any live web sources it used appended as a
    numbered reference list — what the PDF/Word download of one answer
    (the small buttons under its Sources card) actually renders."""
    content = (message.content or '').strip()
    if message.web_sources:
        lines = ['', '', '## Sources', '']
        for index, source in enumerate(message.web_sources, 1):
            title = (source.get('title') or source.get('url') or '').strip()
            url = (source.get('url') or '').strip()
            lines.append(f'{index}. [{title}]({url})' if url else f'{index}. {title}')
        content += '\n'.join(lines)
    return content


def ai_message_export(request, message_id, file_format):
    """Download one assistant reply (the one shown with its web sources) as
    a real PDF or Word document, sources included as a reference list."""
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if file_format not in ('pdf', 'docx'):
        return JsonResponse({'status': 'error', 'detail': 'Unsupported export format.'}, status=400)

    message = AIMessage.objects.filter(
        conversation__in=AIConversation.objects.filter(_ai_owner_filter(request)),
        pk=message_id, role=AIMessage.ROLE_ASSISTANT,
    ).select_related('conversation').first()
    if not message:
        return JsonResponse({'status': 'error', 'detail': 'Message not found.'}, status=404)

    content = _ai_message_with_sources(message)
    if not content.strip():
        return JsonResponse({'status': 'error', 'detail': 'This reply has nothing to export.'}, status=400)

    brand = ai_chat.get_ai_brand_name()  # refresh model labels before they are printed
    title = message.conversation.title or _ai_speaker_label(message)
    local = timezone.localtime(message.created_at)
    try:
        if file_format == 'pdf':
            chat = chat_export.Chat(title=title, turns=[chat_export.Turn(
                role='assistant', who=_ai_speaker_label(message),
                when=local.strftime('%d %b %Y, %I:%M %p'), time=local.strftime('%I:%M %p'),
                content=content,
            )])
            payload = chat_export.build_pdf(brand, [chat], single=True)
        else:
            payload = file_convert.text_to_docx_bytes(content, title=title, brand=brand, created=message.created_at)
    except file_convert.ConvertError as exc:
        return JsonResponse({'status': 'error', 'detail': str(exc)}, status=503)

    response = HttpResponse(payload, content_type=file_convert.mime_for(file_format))
    response['Content-Disposition'] = f'attachment; filename="{_ai_download_name(title, file_format)}"'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, no-store'
    return response


AI_EXPORT_ALL_LIMIT = 500


def ai_conversations_export_all(request, file_format):
    """Download every one of the caller's chats in a single PDF or text file
    (account menu → "Export all chats"). Newest chat first; replaced turns
    are left out, like everywhere else the thread is shown."""
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if file_format not in ('pdf', 'txt'):
        return JsonResponse({'status': 'error', 'detail': 'Unsupported export format.'}, status=400)

    brand = ai_chat.get_ai_brand_name()  # also refreshes the model labels
    conversations = list(
        AIConversation.objects.filter(_ai_owner_filter(request)).order_by('-updated_at', '-pk')[:AI_EXPORT_ALL_LIMIT + 1]
    )
    truncated = len(conversations) > AI_EXPORT_ALL_LIMIT
    conversations = conversations[:AI_EXPORT_ALL_LIMIT]

    by_conversation = {}
    for message in (
        AIMessage.objects.filter(conversation__in=conversations, superseded=False)
        .order_by('created_at', 'pk')
    ):
        by_conversation.setdefault(message.conversation_id, []).append(message)
    conversations = [c for c in conversations if by_conversation.get(c.pk)]
    if not conversations:
        return JsonResponse({'status': 'error', 'detail': 'You have no chats to export yet.'}, status=404)

    exported_at = timezone.localtime().strftime('%d %b %Y, %I:%M %p')
    note = f'the {AI_EXPORT_ALL_LIMIT} most recent chats' if truncated else ''
    chats = [_ai_export_chat(c, by_conversation[c.pk]) for c in conversations]
    if file_format == 'pdf':
        try:
            payload = chat_export.build_pdf(brand, chats, exported_at=exported_at, note=note)
        except file_convert.ConvertError as exc:
            return JsonResponse({'status': 'error', 'detail': str(exc)}, status=503)
        content_type = file_convert.mime_for('pdf')
    else:
        # A BOM so Windows Notepad opens it as UTF-8 and Indic/CJK text survives.
        payload = chat_export.build_text(brand, chats, exported_at, note).encode('utf-8-sig')
        content_type = 'text/plain; charset=utf-8'

    filename = _ai_download_name(f'{brand}-chats-{timezone.localdate():%Y-%m-%d}', file_format)
    response = HttpResponse(payload, content_type=content_type)
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, no-store'
    return response


def ai_transcribe_audio(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    uploaded = request.FILES.get('file')
    if not uploaded:
        return JsonResponse({'status': 'error', 'detail': 'No audio file provided.'}, status=400)
    if uploaded.size > 12 * 1024 * 1024:
        return JsonResponse({'status': 'error', 'detail': 'Audio must be under 12MB.'}, status=400)
    try:
        text = audio_transcribe.transcribe(uploaded.name, uploaded.read())
    except RuntimeError as exc:
        return JsonResponse({'status': 'error', 'detail': str(exc)}, status=400)
    if not text:
        return JsonResponse({'status': 'error', 'detail': 'No speech was detected.'}, status=400)
    return JsonResponse({'status': 'ok', 'text': text})


def _cleanup_youtube_downloads():
    expired = list(YouTubeDownloadJob.objects.filter(expires_at__lt=timezone.now()))
    media_root = Path(settings.PRIVATE_FILES_ROOT).resolve()
    for job in expired:
        for relative in (job.video_path, job.audio_path):
            if not relative:
                continue
            path = (media_root / relative).resolve()
            if media_root in path.parents:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        job.delete()


def ai_youtube_start(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'login_required', 'detail': 'Log in to prepare downloads.'}, status=403)
    if not request.user.is_superuser:
        return JsonResponse({'status': 'error', 'detail': 'YouTube downloads are restricted to superadmins.'}, status=403)
    payload = _parse_json_body(request)
    url = str(payload.get('url', '')).strip() if isinstance(payload, dict) else ''
    quality = str(payload.get('quality', '1080')).strip() if isinstance(payload, dict) else '1080'
    if not youtube_download.is_youtube_url(url):
        return JsonResponse({'status': 'error', 'detail': 'Enter a valid YouTube video URL.'}, status=400)
    if quality not in ('720', '1080', 'audio'):
        return JsonResponse({'status': 'error', 'detail': 'Choose 720p, 1080p, or MP3.'}, status=400)
    ip = _client_ip(request)
    rate_key = f'ai_youtube_rate:{request.user.pk}:{ip}'
    count = cache.get(rate_key, 0)
    if count >= 3:
        return JsonResponse({'status': 'error', 'detail': 'Download limit reached — try again in one hour.'}, status=429)
    if YouTubeDownloadJob.objects.filter(user=request.user, status__in=['pending', 'working']).exists():
        return JsonResponse({'status': 'error', 'detail': 'Another video is already being prepared.'}, status=409)
    _cleanup_youtube_downloads()
    job = YouTubeDownloadJob.objects.create(
        user=request.user, source_url=url, quality=quality,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    cache.set(rate_key, count + 1, 60 * 60)
    youtube_download.start(job.pk)
    return JsonResponse({'status': 'ok', 'job': str(job.token)}, status=202)


def _youtube_job_for_user(request, token):
    if not request.user.is_authenticated:
        return None
    return YouTubeDownloadJob.objects.filter(user=request.user, token=token).first()


def ai_youtube_status(request, token):
    job = _youtube_job_for_user(request, token)
    if not job:
        return JsonResponse({'status': 'error', 'detail': 'Download not found.'}, status=404)
    payload = {'status': job.status, 'progress': job.progress, 'title': job.title, 'error': job.error}
    if job.status == YouTubeDownloadJob.STATUS_READY:
        if job.video_path:
            payload['video_url'] = f'/ai/api/youtube/{job.token}/file/video/'
        if job.audio_path:
            payload['audio_url'] = f'/ai/api/youtube/{job.token}/file/audio/'
        payload['expires_at'] = timezone.localtime(job.expires_at).isoformat()
    return JsonResponse(payload)


def ai_youtube_file(request, token, file_kind):
    job = _youtube_job_for_user(request, token)
    if not job or job.status != YouTubeDownloadJob.STATUS_READY or job.expires_at <= timezone.now():
        return JsonResponse({'status': 'error', 'detail': 'Download unavailable or expired.'}, status=404)
    relative = job.video_path if file_kind == 'video' else job.audio_path if file_kind == 'audio' else ''
    media_root = Path(settings.PRIVATE_FILES_ROOT).resolve()
    path = (media_root / relative).resolve() if relative else None
    if not path or media_root not in path.parents or not path.is_file():
        return JsonResponse({'status': 'error', 'detail': 'File not found.'}, status=404)
    extension = path.suffix.lower()
    content_type = 'audio/mpeg' if file_kind == 'audio' else (mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
    filename = f"{job.title or 'youtube-download'}{extension}"
    response = FileResponse(open(path, 'rb'), content_type=content_type, as_attachment=True, filename=filename)
    response['Cache-Control'] = 'private, no-store'
    return response


AI_CONV_SEARCH_LIMIT = 100
AI_CONV_RANGE_DAYS = {'week': 7, 'month': 30}


def _ai_search_snippet(text, query, radius=36):
    """A short excerpt of text around the first match of query."""
    text = ' '.join((text or '').split())
    index = text.lower().find(query.lower())
    if index < 0:
        return text[:radius * 3]
    start = max(0, index - radius)
    end = min(len(text), index + len(query) + radius * 2)
    return ('…' if start else '') + text[start:end] + ('…' if end < len(text) else '')


def ai_conversations_list(request):
    """The sidebar chat list. With no parameters: the 100 most recent chats.
    Optional filters (all combine): q (matches the chat title or any live
    message), range (today/week/month), has (images/files) and model (a chat
    the model answered in)."""
    conversations = AIConversation.objects.filter(_ai_owner_filter(request))
    live_messages = AIMessage.objects.filter(conversation=OuterRef('pk'), superseded=False)

    query = request.GET.get('q', '').strip()[:100]
    date_range = request.GET.get('range', '')
    has = request.GET.get('has', '')
    model_key = request.GET.get('model', '')

    now = timezone.localtime()
    if date_range == 'today':
        conversations = conversations.filter(
            updated_at__gte=now.replace(hour=0, minute=0, second=0, microsecond=0),
        )
    elif date_range in AI_CONV_RANGE_DAYS:
        conversations = conversations.filter(updated_at__gte=now - timedelta(days=AI_CONV_RANGE_DAYS[date_range]))
    if has == 'images':
        conversations = conversations.filter(Exists(live_messages.exclude(image_data='')))
    elif has == 'files':
        conversations = conversations.filter(Exists(live_messages.exclude(document_name='')))
    if model_key in ai_chat.MODELS:
        conversations = conversations.filter(
            Exists(live_messages.filter(role=AIMessage.ROLE_ASSISTANT, model_key=model_key)),
        )
    if query:
        conversations = conversations.filter(
            Q(title__icontains=query) | Exists(live_messages.filter(content__icontains=query)),
        )

    rows = list(
        conversations.values('id', 'title', 'updated_at').order_by('-updated_at')[:AI_CONV_SEARCH_LIMIT]
    )
    for row in rows:
        row['updated_at'] = timezone.localtime(row['updated_at']).isoformat()

    if query and rows:
        ids = [row['id'] for row in rows]
        first_match, match_count = {}, {}
        matching = (
            AIMessage.objects.filter(conversation_id__in=ids, superseded=False, content__icontains=query)
            .order_by('conversation_id', 'created_at').values_list('conversation_id', 'content')[:800]
        )
        for conversation_id, content in matching:
            match_count[conversation_id] = match_count.get(conversation_id, 0) + 1
            first_match.setdefault(conversation_id, content)
        for row in rows:
            row['matches'] = match_count.get(row['id'], 0)
            row['snippet'] = (
                _ai_search_snippet(first_match[row['id']], query) if row['id'] in first_match else ''
            )
    return JsonResponse({'status': 'ok', 'conversations': rows})


def ai_conversation_messages(request, conversation_id):
    conversation = AIConversation.objects.filter(_ai_owner_filter(request), pk=conversation_id).first()
    if not conversation:
        return JsonResponse({'status': 'error', 'detail': 'Conversation not found.'}, status=404)
    request.session[AI_CURRENT_CONVERSATION_SESSION_KEY] = conversation.id
    messages_qs = list(
        conversation.messages.filter(superseded=False).order_by('created_at')
        .values('id', 'role', 'content', 'image_data', 'document_name', 'model_key', 'reply_to_text', 'video_url',
                'web_sources')
    )
    # A video still being made: the page keeps polling it after a reload.
    working = dict(AIVideoJob.objects.filter(
        message__conversation=conversation, status=AIVideoJob.STATUS_WORKING,
    ).values_list('message_id', 'token'))
    for m in messages_qs:
        if m['id'] in working:
            m['video_job'] = str(working[m['id']])
    return JsonResponse({'status': 'ok', 'title': conversation.title, 'messages': messages_qs})


def ai_conversation_delete(request, conversation_id):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    conversation = AIConversation.objects.filter(_ai_owner_filter(request), pk=conversation_id).first()
    if not conversation:
        return JsonResponse({'status': 'error', 'detail': 'Conversation not found.'}, status=404)
    conversation.delete()
    if request.session.get(AI_CURRENT_CONVERSATION_SESSION_KEY) == conversation_id:
        request.session.pop(AI_CURRENT_CONVERSATION_SESSION_KEY, None)
    return JsonResponse({'status': 'ok'})


def ai_notes_list(request):
    response = JsonResponse({'status': 'ok', 'notes': _ai_notes_snapshot(request)})
    response['Cache-Control'] = 'private, no-store'
    return response


AI_NOTE_MAX_CHARS = 4000
AI_NOTES_PER_OWNER = 200


def _ai_note_response(request, status=200, **extra):
    response = JsonResponse({'status': 'ok', 'notes': _ai_notes_snapshot(request), **extra}, status=status)
    response['Cache-Control'] = 'private, no-store'
    return response


def _ai_note_input(request):
    """(heading, content, error) from a create/update request."""
    payload = _parse_json_body(request)
    if not isinstance(payload, dict):
        payload = {}
    content = str(payload.get('content') or '').strip()
    heading = str(payload.get('heading') or '').strip()[:120]
    if not content and not heading:
        return '', '', 'Write something in the note first.'
    if not content:
        content = heading
    if len(content) > AI_NOTE_MAX_CHARS:
        return '', '', f'A note can be at most {AI_NOTE_MAX_CHARS} characters.'
    return heading or _ai_note_heading(content), content, ''


def ai_note_create(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    heading, content, error = _ai_note_input(request)
    if error:
        return JsonResponse({'status': 'error', 'detail': error}, status=400)
    if not request.user.is_authenticated and not request.session.session_key:
        request.session.create()
    if AINote.objects.filter(_ai_owner_filter(request)).count() >= AI_NOTES_PER_OWNER:
        return JsonResponse({'status': 'error', 'detail': f'You can keep up to {AI_NOTES_PER_OWNER} notes. Delete one first.'}, status=400)
    note = AINote.objects.create(
        user=request.user if request.user.is_authenticated else None,
        session_key='' if request.user.is_authenticated else (request.session.session_key or ''),
        heading=heading, content=content,
    )
    return _ai_note_response(request, status=201, note_id=note.pk)


def ai_note_update(request, note_id):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    note = AINote.objects.filter(_ai_owner_filter(request), pk=note_id).first()
    if not note:
        return JsonResponse({'status': 'error', 'detail': 'Note not found.'}, status=404)
    heading, content, error = _ai_note_input(request)
    if error:
        return JsonResponse({'status': 'error', 'detail': error}, status=400)
    note.heading, note.content = heading, content
    note.save(update_fields=['heading', 'content'])
    return _ai_note_response(request, note_id=note.pk)


def ai_note_delete(request, note_id):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    note = AINote.objects.filter(_ai_owner_filter(request), pk=note_id).first()
    if not note:
        return JsonResponse({'status': 'error', 'detail': 'Note not found.'}, status=404)
    note.delete()
    if any(item['id'] == note_id for item in _ai_notes_snapshot(request)):
        return JsonResponse({'status': 'error', 'detail': 'Could not delete note.'}, status=500)
    response = JsonResponse({'status': 'ok', 'notes': _ai_notes_snapshot(request)})
    response['Cache-Control'] = 'private, no-store'
    return response


def ai_images_delete_all(request):
    """Wipe the signed-in user's "My Images" gallery.

    AIUserImage rows are otherwise never deleted by the app (see the model's
    docstring) — this is the one deliberate, user-initiated exception, scoped
    strictly to request.user so it can never touch another account's images.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)
    AIUserImage.objects.filter(user=request.user).delete()
    response = JsonResponse({'status': 'ok', 'images': []})
    response['Cache-Control'] = 'private, no-store'
    return response


AI_IMAGES_ZIP_PER_HOUR = 10
_AI_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}


def ai_images_download_all(request):
    """Every image in the caller's "My Images" gallery as one .zip, with a
    prompts.txt listing what each picture was made from. Images already are
    compressed files, so they are stored as they are."""
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)
    throttle_key = f'ai-images-zip:{request.user.pk}'
    cache.add(throttle_key, 0, 3600)
    try:
        if cache.incr(throttle_key) > AI_IMAGES_ZIP_PER_HOUR:
            return JsonResponse({'status': 'error', 'detail': 'Please wait a little before downloading your images again.'}, status=429)
    except ValueError:
        cache.set(throttle_key, 1, 3600)

    ai_chat.get_ai_brand_name()  # refresh model labels before they are listed
    archive_file = tempfile.TemporaryFile()
    saved = 0
    missing = 0
    notes = []
    with zipfile.ZipFile(archive_file, 'w', zipfile.ZIP_STORED) as archive:
        for image in AIUserImage.objects.filter(user=request.user).order_by('created_at', 'pk').iterator():
            storage_name = _ai_storage_name_from_url(image.url)
            extension = Path(storage_name).suffix.lower() if storage_name else ''
            if not storage_name or extension not in _AI_IMAGE_EXTENSIONS:
                missing += 1
                continue
            try:
                with default_storage.open(storage_name, 'rb') as stored:
                    data = stored.read()
            except Exception:
                missing += 1
                continue
            stamp = timezone.localtime(image.created_at).strftime('%Y-%m-%d_%H%M%S')
            filename = f'{stamp}_{image.pk}{extension}'
            archive.writestr(filename, data)
            saved += 1
            model_label = ai_chat.MODELS.get(image.model_key, {}).get('label', image.model_key)
            prompt = ' '.join((image.prompt or '').split()) or '(no prompt saved)'
            notes.append(f'{filename}\n    Made with: {model_label or "AI"}\n    Prompt: {prompt}\n')
        if saved:
            header = f'{ai_chat.get_ai_brand_name()} AI — your generated images\nDownloaded {timezone.localtime():%d %b %Y, %I:%M %p}\n{saved} image{"" if saved == 1 else "s"}'
            if missing:
                header += f' ({missing} could not be included — the file is no longer available)'
            archive.writestr('prompts.txt', (header + '\n\n' + '\n'.join(notes)).encode('utf-8-sig'))
    if not saved:
        archive_file.close()
        detail = 'You have no images to download yet.' if not missing else 'Your images are no longer available to download.'
        return JsonResponse({'status': 'error', 'detail': detail}, status=404)
    archive_file.seek(0)
    response = FileResponse(
        archive_file, as_attachment=True, content_type='application/zip',
        filename=_ai_download_name(f'{ai_chat.get_ai_brand_name()}-images-{timezone.localdate():%Y-%m-%d}', 'zip'),
    )
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, no-store'
    return response


AI_REPORT_MAX_EXPLANATION_CHARS = 2000


def _is_reportable_unsaved_ai_failure(reply):
    """Allow reports for streamed provider failures that are not AIMessage rows."""
    lowered = (reply or '').casefold()
    return any(marker in lowered for marker in (
        'did not respond',
        'taking longer than expected',
        'temporarily unavailable',
        'currently disconnected',
        'authentication is currently unavailable',
        'currently at its request limit',
        '[response interrupted.',
        'too long for the ai to process',
    ))


def _ai_storage_name_from_url(image_value):
    """Return a safe storage-relative name for one of our file URLs (a /media/
    URL or a stored Dropbox link)."""
    return dropbox_storage.name_for_url(image_value)


def _ai_store_attachment_later(message_id, data_uri):
    """Moves a picture attached to a chat message out of the database into
    Dropbox, on a background thread so the reply never waits for it. The row
    keeps the data: URI until the file is safely stored, then points at it."""
    match = re.match(r'^data:image/(png|jpe?g|webp|gif);base64,', data_uri[:40], re.I)
    if not match:
        return

    def store_ai_attachment():
        raw = base64.b64decode(data_uri[match.end():])
        extension = match.group(1).lower().replace('jpeg', 'jpg')
        name = default_storage.save(
            f'ai_uploads/{timezone.now():%Y/%m/%d}/{secrets.token_hex(16)}.{extension}', ContentFile(raw),
        )
        AIMessage.objects.filter(pk=message_id, image_data__startswith='data:').update(
            image_data=default_storage.url(name),
        )

    dropbox_storage.run_later(store_ai_attachment)


def _snapshot_ai_report_image(image_value):
    """Put visual report evidence in the DB when its media file is readable."""
    if not image_value:
        return ''
    if image_value.startswith('data:image/'):
        return image_value[:AI_IMAGE_MAX_DATA_URI_CHARS]
    storage_name = _ai_storage_name_from_url(image_value)
    if not storage_name:
        return image_value
    try:
        with default_storage.open(storage_name, 'rb') as image_file:
            raw = image_file.read(AI_IMAGE_MAX_DATA_URI_CHARS + 1)
        if len(raw) > AI_IMAGE_MAX_DATA_URI_CHARS:
            return image_value
        content_type = mimetypes.guess_type(storage_name)[0] or 'image/png'
        return f'data:{content_type};base64,{base64.b64encode(raw).decode("ascii")}'
    except Exception:
        # The report is still useful if an old deployment already lost the
        # media file; retain its original URL and surface that health problem
        # to staff on the reports dashboard.
        logger.warning('Could not snapshot reported AI image %s', storage_name)
        return image_value


def ai_report_submit(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    payload = _parse_json_body(request)
    if not isinstance(payload, dict):
        return JsonResponse({'status': 'error', 'detail': 'Invalid request body.'}, status=400)

    conversation_id = payload.get('conversation_id')
    try:
        conversation_id = int(conversation_id)
    except (TypeError, ValueError):
        return JsonResponse({'status': 'error', 'detail': 'Conversation not found.'}, status=400)
    conversation = AIConversation.objects.filter(_ai_owner_filter(request), pk=conversation_id).first()
    if not conversation:
        return JsonResponse({'status': 'error', 'detail': 'Conversation not found.'}, status=404)

    reported_reply = str(payload.get('reply_text', ''))[:AI_CHAT_MAX_MESSAGE_CHARS].strip()
    reported_image = payload.get('reply_image', '')
    if not isinstance(reported_image, str):
        reported_image = ''
    reported_image = reported_image[:AI_IMAGE_MAX_DATA_URI_CHARS].strip()
    explanation = str(payload.get('explanation', ''))[:AI_REPORT_MAX_EXPLANATION_CHARS].strip()
    model_key = str(payload.get('model_key', ''))[:20]
    if not reported_reply and not reported_image:
        return JsonResponse({'status': 'error', 'detail': 'Nothing to report.'}, status=400)
    if not explanation:
        return JsonResponse({'status': 'error', 'detail': 'Please explain what went wrong before submitting.'}, status=400)

    # Best-effort link to the actual saved AIMessage row, purely so staff can
    # jump straight to it from admin — the report is still saved (with its
    # own snapshot of the text) even when this doesn't find one, e.g. the
    # message has since been edited or the conversation deleted.
    raw_message_id = payload.get('message_id')
    message = None
    if raw_message_id not in (None, ''):
        try:
            message_id = int(raw_message_id)
        except (TypeError, ValueError):
            return JsonResponse({'status': 'error', 'detail': 'Reported response not found.'}, status=400)
        message = conversation.messages.filter(
            pk=message_id, role=AIMessage.ROLE_ASSISTANT,
        ).first()
        if message is None:
            return JsonResponse({'status': 'error', 'detail': 'Reported response not found.'}, status=404)
    else:
        candidates = conversation.messages.filter(role=AIMessage.ROLE_ASSISTANT)
        if reported_reply:
            candidates = candidates.filter(content=reported_reply)
        if reported_image:
            candidates = candidates.filter(image_data=reported_image)
        message = candidates.order_by('-created_at', '-pk').first()

    # Stored messages are authoritative: a browser cannot substitute a
    # different reply, model, or image in the report payload.
    if message is not None:
        reported_reply = message.content[:AI_CHAT_MAX_MESSAGE_CHARS].strip()
        reported_image = message.image_data
        model_key = message.model_key[:20]
    elif reported_image:
        # Image responses are persisted before being returned. An unmatched
        # URL/data URI therefore cannot be genuine evidence from this chat.
        return JsonResponse({'status': 'error', 'detail': 'Reported image not found.'}, status=404)
    elif not _is_reportable_unsaved_ai_failure(reported_reply):
        return JsonResponse({'status': 'error', 'detail': 'Reported response not found.'}, status=404)

    # Snapshot the user turn that prompted this reply, so staff reviewing
    # the report don't have to go dig through the (possibly since-deleted)
    # conversation to see what was actually asked. Anchored to the matched
    # message's timestamp when we have one; otherwise best-effort fall back
    # to the most recent user turn in the conversation.
    preceding = conversation.messages.filter(role=AIMessage.ROLE_USER)
    if message:
        preceding = preceding.filter(
            Q(created_at__lt=message.created_at) |
            Q(created_at=message.created_at, pk__lt=message.pk)
        )
    preceding = preceding.order_by('-created_at', '-pk')
    user_message = preceding.first()
    user_prompt = user_message.content[:AI_CHAT_MAX_MESSAGE_CHARS].strip() if user_message else ''
    user_image = _snapshot_ai_report_image(user_message.image_data) if user_message else ''
    user_document_name = user_message.document_name if user_message else ''
    user_document_excerpt = user_message.document_text[:8000] if user_message else ''

    # A turn that was only an image, only a file, or a follow-up to an
    # earlier instruction leaves user_prompt blank, and the report then
    # arrives with no way to tell what was actually asked — six real reports
    # (#13, #14, #20, #22, #23, #27) came in exactly like that and could not
    # be diagnosed at all. Fall back to the nearest earlier user turn that
    # does have text, clearly labelled so nobody mistakes it for the
    # reported turn itself.
    if not user_prompt:
        earlier = preceding.exclude(content='').first()
        earlier_text = earlier.content[:AI_CHAT_MAX_MESSAGE_CHARS].strip() if earlier else ''
        if user_message is None:
            described = 'no user turn was recorded before this reply'
        elif user_message.image_data:
            described = 'the reported turn was an image with no text'
        elif user_message.document_name:
            described = f'the reported turn was a file upload ({user_message.document_name}) with no text'
        else:
            described = 'the reported turn had no text'
        user_prompt = (
            f'[{described}; earlier instruction in this chat: "{earlier_text}"]'
            if earlier_text else f'[{described}]'
        )[:AI_CHAT_MAX_MESSAGE_CHARS]
    reported_image = _snapshot_ai_report_image(reported_image)

    ip = _client_ip(request)
    report = AIReport.objects.create(
        conversation=conversation, message=message,
        user_prompt=user_prompt, user_image=user_image,
        user_document_name=user_document_name,
        user_document_excerpt=user_document_excerpt,
        reported_reply=reported_reply, reported_image=reported_image,
        model_key=model_key, explanation=explanation,
        user=request.user if request.user.is_authenticated else None,
        session_key='' if request.user.is_authenticated else (request.session.session_key or ''),
        ip_address=ip if ip and ip != 'unknown' else None,
    )

    # Archive the report's visual evidence alongside the reporter's own images,
    # so a reviewer can still see what they were looking at after local media
    # has been recycled. Queued for the background worker exactly like a
    # generated image — reporting a bad reply must stay instant.
    dropbox_images.enqueue_report_images(
        report.pk,
        request.user.email if request.user.is_authenticated else '',
        user_image=user_image,
        reply_image=reported_image,
    )
    return JsonResponse({'status': 'ok'})


# ── AI GitHub mode: connect a repo, let the AI CRUD + push to it on prompt ──
# Available to every authenticated account. Each user has a separate
# connection/token and all queries remain scoped to request.user, so no
# repository credentials or selections are shared between accounts.

AI_GITHUB_RATE_LIMIT = 10
AI_GITHUB_MAX_FILE_CHARS = 60_000  # skip pulling absurdly large files into the prompt


def _github_guard(request):
    return request.user.is_authenticated


def _github_forbidden():
    return JsonResponse({'status': 'error', 'detail': 'Log in to use GitHub.'}, status=401)


def github_status(request):
    if not _github_guard(request):
        return _github_forbidden()
    conn = GitHubConnection.objects.filter(user=request.user).first()
    if not conn:
        return JsonResponse({'status': 'ok', 'connected': False})
    return JsonResponse({
        'status': 'ok', 'connected': True, 'github_username': conn.github_username,
        'repo': conn.repo_full_name, 'branch': conn.default_branch,
    })


def _github_oauth_redirect_uri(request):
    return request.build_absolute_uri('/ai/api/github/oauth/callback/')


def _save_github_connection(user, token, github_username, repos):
    """Save credentials and keep only a repository visible to this token."""
    conn, _created = GitHubConnection.objects.update_or_create(
        user=user,
        defaults={'access_token': token, 'github_username': github_username},
    )
    available = {repo['full_name']: repo for repo in repos}
    selected = available.get(conn.repo_full_name)
    if selected is None:
        selected = next(
            (repo for repo in repos if 'edutrellis' in repo['full_name'].lower()),
            repos[0] if repos else None,
        )
    conn.repo_full_name = selected['full_name'] if selected else ''
    conn.default_branch = selected['default_branch'] if selected else 'main'
    conn.save(update_fields=['repo_full_name', 'default_branch'])
    return conn


def github_oauth_start(request):
    # A redirect, not a JSON endpoint — the browser navigates here directly
    # (window.location.href), so an unauthorized visit bounces to the
    # AI page instead of showing a raw 403 JSON body.
    if not _github_guard(request):
        return redirect('ai_home')
    if not settings.GITHUB_OAUTH_CLIENT_ID:
        return redirect('/?github_error=not_configured')
    state = secrets.token_urlsafe(24)
    request.session['github_oauth_state'] = state
    params = {
        'client_id': settings.GITHUB_OAUTH_CLIENT_ID,
        'redirect_uri': _github_oauth_redirect_uri(request),
        # 'repo' grants full read/write on both private and public repos —
        # the closest standard GitHub OAuth scope to "all permission on the
        # repo", without also requesting unrelated org/user-admin scopes.
        'scope': 'repo',
        'state': state,
    }
    return redirect('https://github.com/login/oauth/authorize?' + urlencode(params))


def github_oauth_callback(request):
    if not _github_guard(request):
        return redirect('ai_home')
    expected_state = request.session.pop('github_oauth_state', None)
    state = request.GET.get('state')
    if not state or not expected_state or not secrets.compare_digest(state, expected_state):
        return redirect('/?github_error=state')
    code = request.GET.get('code')
    if not code:
        return redirect('/?github_error=denied')
    try:
        token_resp = requests.post(
            'https://github.com/login/oauth/access_token',
            data={
                'client_id': settings.GITHUB_OAUTH_CLIENT_ID,
                'client_secret': settings.GITHUB_OAUTH_CLIENT_SECRET,
                'code': code,
                'redirect_uri': _github_oauth_redirect_uri(request),
            },
            headers={'Accept': 'application/json'},
            timeout=20,
        )
        token = token_resp.json().get('access_token')
        if not token:
            return redirect('/?github_error=token')
        gh_user = github_ops.get_authenticated_user(token)
        repos = github_ops.list_user_repos(token)
    except Exception:
        logger.exception("GitHub OAuth callback failed")
        return redirect('/?github_error=1')

    _save_github_connection(request.user, token, gh_user.get('login', ''), repos)
    return redirect('/?github_connected=1')


def github_connect(request):
    if not _github_guard(request):
        return _github_forbidden()
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    payload = _parse_json_body(request)
    if not isinstance(payload, dict):
        return JsonResponse({'status': 'error', 'detail': 'Invalid request body.'}, status=400)
    token = str(payload.get('token', '')).strip()
    if not token:
        return JsonResponse({'status': 'error', 'detail': 'Token required.'}, status=400)
    try:
        gh_user = github_ops.get_authenticated_user(token)
        repos = github_ops.list_user_repos(token)
    except github_ops.GitHubAPIError as e:
        return JsonResponse({'status': 'error', 'detail': f"Couldn't connect: {e}"}, status=400)
    conn = _save_github_connection(request.user, token, gh_user.get('login', ''), repos)
    return JsonResponse({
        'status': 'ok', 'github_username': conn.github_username, 'repos': repos,
        'repo': conn.repo_full_name, 'branch': conn.default_branch,
    })


def github_repos(request):
    if not _github_guard(request):
        return _github_forbidden()
    conn = GitHubConnection.objects.filter(user=request.user).first()
    if not conn:
        return JsonResponse({'status': 'error', 'detail': 'Not connected.'}, status=400)
    try:
        repos = github_ops.list_user_repos(conn.access_token)
    except github_ops.GitHubAPIError as e:
        return JsonResponse({'status': 'error', 'detail': str(e)}, status=400)
    return JsonResponse({'status': 'ok', 'repos': repos})


def github_set_repo(request):
    if not _github_guard(request):
        return _github_forbidden()
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    conn = GitHubConnection.objects.filter(user=request.user).first()
    if not conn:
        return JsonResponse({'status': 'error', 'detail': 'Not connected.'}, status=400)
    payload = _parse_json_body(request)
    repo = str(payload.get('repo', '')).strip() if isinstance(payload, dict) else ''
    if '/' not in repo:
        return JsonResponse({'status': 'error', 'detail': 'Invalid repo.'}, status=400)
    owner, _, name = repo.partition('/')
    try:
        info = github_ops.get_repo(conn.access_token, owner, name)
    except github_ops.GitHubAPIError as e:
        return JsonResponse({'status': 'error', 'detail': str(e)}, status=400)
    conn.repo_full_name = info['full_name']
    conn.default_branch = info['default_branch']
    conn.save(update_fields=['repo_full_name', 'default_branch'])
    return JsonResponse({'status': 'ok', 'repo': conn.repo_full_name, 'branch': conn.default_branch})


def github_disconnect(request):
    if not _github_guard(request):
        return _github_forbidden()
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    GitHubConnection.objects.filter(user=request.user).delete()
    return JsonResponse({'status': 'ok'})


def ai_github_send(request):
    if not _github_guard(request):
        return _github_forbidden()
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)

    cache_key = f'ai_github_rate:{request.user.pk}'
    count = cache.get(cache_key, 0)
    if count >= AI_GITHUB_RATE_LIMIT:
        return JsonResponse({'status': 'error', 'detail': 'Too many GitHub requests — please wait a bit.'}, status=429)
    cache.set(cache_key, count + 1, AI_CHAT_RATE_WINDOW)

    conn = GitHubConnection.objects.filter(user=request.user).first()
    if not conn or not conn.repo_full_name:
        return JsonResponse({'status': 'error', 'detail': 'Connect a GitHub repo first.'}, status=400)

    payload = _parse_json_body(request)
    if not isinstance(payload, dict):
        return JsonResponse({'status': 'error', 'detail': 'Invalid request body.'}, status=400)
    message = str(payload.get('message', ''))[:AI_CHAT_MAX_MESSAGE_CHARS].strip()
    if not message:
        return JsonResponse({'status': 'error', 'detail': 'No instruction provided.'}, status=400)
    # Shown, labelled and saved under the model the user picked, never under a
    # "GitHub" name of its own (same rule as every other feature).
    requested_model_key = payload.get('model')
    if requested_model_key in ai_chat.MODELS and requested_model_key != 'vision':
        display_model_key = requested_model_key
    else:
        display_model_key = (
            ai_chat.SOL_MODEL_KEY
            if _ai_has_full_model_access(request.user) and ai_chat.is_model_enabled(ai_chat.SOL_MODEL_KEY)
            else 'quick'
        )
    if not ai_chat.is_model_enabled(display_model_key):
        return JsonResponse({
            'status': 'model_disabled',
            'detail': (
                f"{ai_chat.MODELS[display_model_key]['label']} is temporarily unavailable. "
                'Please pick another model from the model menu.'
            ),
        }, status=403)
    replace_target = _ai_replace_target(request, payload)

    owner, _, repo = conn.repo_full_name.partition('/')
    branch = conn.default_branch or 'main'

    owner_filter = _ai_owner_filter(request)
    conversation_id = payload.get('conversation_id')
    conversation = AIConversation.objects.filter(owner_filter, pk=conversation_id).first() if conversation_id else None
    if conversation is None:
        conversation = AIConversation.objects.create(user=request.user, title=message[:AI_CONVERSATION_TITLE_CHARS])
    request.session[AI_CURRENT_CONVERSATION_SESSION_KEY] = conversation.id
    if replace_target is not None and replace_target.conversation_id == conversation.id:
        conversation.messages.filter(pk__gte=replace_target.pk).update(superseded=True)
    user_message = AIMessage.objects.create(conversation=conversation, role=AIMessage.ROLE_USER, content=message)
    conversation.updated_at = timezone.now()
    conversation.save(update_fields=['updated_at'])

    def finish(reply_text):
        AIMessage.objects.create(
            conversation=conversation, role=AIMessage.ROLE_ASSISTANT,
            content=reply_text, model_key=display_model_key,
        )
        return JsonResponse({
            'status': 'ok', 'reply': reply_text,
            'conversation_id': conversation.id, 'model_key': display_model_key,
            'user_message_id': user_message.pk,
        })

    try:
        file_paths = github_ops.get_tree(conn.access_token, owner, repo, branch)
    except github_ops.GitHubAPIError as e:
        return finish(f"Couldn't read the repository: {e}")

    wanted = ai_chat.github_select_files(message, file_paths)
    # file_contents is what the planner is shown (trimmed to keep the prompt
    # small); file_full keeps the real text so "edit" operations are applied
    # to the whole file, not to the trimmed copy.
    file_contents, file_full, file_shas = {}, {}, {}
    shown_chars = 0
    for path in wanted:
        if github_ops.is_path_blocked(path):
            # The blocked list protects settings/migrations/secrets/etc from
            # writes below — it must cover reads too, or a blocked file's
            # contents (e.g. settings.py) would still get pasted into the
            # planning prompt even though it can never actually be changed.
            continue
        try:
            content, sha = github_ops.get_file(conn.access_token, owner, repo, path, branch)
        except github_ops.GitHubAPIError:
            continue
        file_full[path] = content
        file_shas[path] = sha
        shown = content
        if len(shown) > ai_chat.GITHUB_FILE_PROMPT_CHARS:
            shown = shown[:ai_chat.GITHUB_FILE_PROMPT_CHARS] + '\n...[truncated]'
        if shown_chars + len(shown) > ai_chat.GITHUB_TOTAL_PROMPT_CHARS:
            continue  # keep the prompt a size the model can answer in time
        shown_chars += len(shown)
        file_contents[path] = shown

    try:
        plan = ai_chat.github_plan_changes(message, file_paths, file_contents)
    except Exception as e:
        logger.exception("GitHub plan generation failed: %s", e)
        contact = f"If it keeps happening, contact the administrator at {_support_email()}."
        if isinstance(e, ai_chat.GitHubPlanTooLarge):
            return finish(
                "This change is too big to plan in one go. Ask for it in smaller pieces — for example one folder "
                f"or a few named files at a time. {contact}"
            )
        if 'timeout' in e.__class__.__name__.lower():
            return finish(
                "The AI took too long to plan this change, usually because it touches many files. Try naming the "
                f"specific files, or ask for a smaller change. {contact}"
            )
        return finish(
            f"Something went wrong while planning the change — please try again or rephrase your request. {contact}"
        )

    if not isinstance(plan, dict):
        return finish("The AI's response wasn't understood — please try rephrasing your request.")

    summary = str(plan.get('summary') or 'No changes were made.')
    commit_message = str(plan.get('commit_message') or message)[:200] or message[:200]
    operations = plan.get('operations') or []

    # Validate every proposed operation before touching the GitHub API at
    # all, so we know whether there's anything real to commit BEFORE
    # creating a working branch for it (never open an empty branch/PR).
    valid_ops, skipped = [], []
    edited = {}  # path -> text after the edit operations so far (several ops may hit one file)
    for op in operations[:60]:
        if not isinstance(op, dict):
            continue
        action = op.get('action')
        path = str(op.get('path', '')).strip()
        if not path or github_ops.is_path_blocked(path):
            skipped.append(f"{path or '(blank path)'} — blocked")
            continue
        if action == 'edit':
            # Find-and-replace edits applied here, to the file's real text, so
            # the model only has to send the few lines that change.
            edits = op.get('edits')
            if not isinstance(edits, list) or not edits:
                skipped.append(f"{path} — no edits given")
                continue
            original = edited.get(path, file_full.get(path))
            if original is None:
                try:
                    original, file_shas[path] = github_ops.get_file(conn.access_token, owner, repo, path, branch)
                except github_ops.GitHubAPIError:
                    skipped.append(f"{path} — not found")
                    continue
            updated, replaced = original, 0
            for edit in edits[:200]:
                find = edit.get('find') if isinstance(edit, dict) else None
                replacement = edit.get('replace') if isinstance(edit, dict) else None
                if not isinstance(find, str) or not find or not isinstance(replacement, str):
                    continue
                occurrences = updated.count(find)
                if occurrences == 0:
                    skipped.append(f"{path} — text to replace not found: {find[:40]!r}")
                elif occurrences > 1 and not edit.get('all'):
                    skipped.append(f"{path} — {find[:40]!r} appears {occurrences} times (not specific enough)")
                else:
                    updated = updated.replace(find, replacement)
                    replaced += occurrences
            if updated == original:
                if not replaced:
                    skipped.append(f"{path} — no matching text, left unchanged")
                continue
            edited[path] = updated
            valid_ops = [v for v in valid_ops if v['path'] != path]
            valid_ops.append({'action': 'update', 'path': path, 'content': updated})
        elif action in ('update', 'create'):
            content = op.get('content')
            if not isinstance(content, str):
                skipped.append(f"{path} — no content given")
                continue
            valid_ops.append({'action': action, 'path': path, 'content': content})
        elif action == 'delete':
            valid_ops.append({'action': 'delete', 'path': path})
        else:
            skipped.append(f"{path} — unknown action '{action}'")

    applied, pr_url, work_branch = [], None, None
    if valid_ops:
        # Every change lands on a fresh branch + PR rather than a direct
        # commit to the default branch — an AI-proposed change (from a
        # small, non-specialized model, on a plain-English instruction) has
        # no business landing on main unreviewed. If nothing ends up
        # actually applied below, the branch is deleted again so this
        # doesn't litter the repo with empty branches.
        work_branch = f"ai/{timezone.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        try:
            base_sha = github_ops.get_branch_sha(conn.access_token, owner, repo, branch)
            github_ops.create_branch(conn.access_token, owner, repo, work_branch, base_sha)
        except github_ops.GitHubAPIError as e:
            return finish(f"Couldn't create a working branch for this change: {e}")

        for op in valid_ops:
            path = op['path']
            try:
                if op['action'] in ('update', 'create'):
                    sha = file_shas.get(path)
                    if sha is None and op['action'] == 'update':
                        try:
                            _, sha = github_ops.get_file(conn.access_token, owner, repo, path, work_branch)
                        except github_ops.GitHubAPIError:
                            sha = None
                    github_ops.upsert_file(
                        conn.access_token, owner, repo, path, op['content'], commit_message, work_branch, sha=sha,
                    )
                    applied.append(path)
                else:
                    sha = file_shas.get(path)
                    if sha is None:
                        try:
                            _, sha = github_ops.get_file(conn.access_token, owner, repo, path, work_branch)
                        except github_ops.GitHubAPIError:
                            sha = None
                    if sha is None:
                        skipped.append(f"{path} — not found")
                        continue
                    github_ops.delete_file(conn.access_token, owner, repo, path, commit_message, work_branch, sha)
                    applied.append(path)
            except github_ops.GitHubAPIError as e:
                skipped.append(f"{path} — {e}")

        if applied:
            try:
                pr = github_ops.create_pull_request(
                    conn.access_token, owner, repo, title=commit_message,
                    head=work_branch, base=branch, body=summary,
                )
                pr_url = pr.get('html_url')
            except github_ops.GitHubAPIError as e:
                skipped.append(f"(pull request could not be opened automatically: {e})")
        else:
            github_ops.delete_branch(conn.access_token, owner, repo, work_branch)

    lines = [summary]
    if applied:
        lines.append(f"\n**Proposed on `{work_branch}` — not yet merged:**")
        lines.extend(f"- {p}" for p in applied)
        if pr_url:
            lines.append(f"\nReview and merge: {pr_url}")
        else:
            lines.append(
                f"\nPushed to `{work_branch}` in {conn.repo_full_name}, but opening a pull "
                "request automatically failed — you can open one manually from that branch."
            )
    if skipped:
        lines.append("\n**Skipped:**")
        lines.extend(f"- {s}" for s in skipped)
    if not applied and not skipped and not plan.get('summary'):
        lines.append("\nNo file changes were made.")
    return finish('\n'.join(lines))


# ---- EduTrellis-only views (not in Vidhyora) ----

# Limits for importing a chat exported from another assistant (ai_conversation_import).
AI_IMPORT_MAX_MESSAGES = 500
AI_IMPORT_MAX_MESSAGE_CHARS = 8000

@dashboard_staff_required
def dashboard_ai_add_user(request):
    """Manually create a customer account from AI Management's inline 'Add
    user' panel, optionally granting AI premium access in the same step —
    e.g. so staff can onboard someone who paid over phone/WhatsApp without
    them self-registering first. Separate from myapp's own
    dashboard_user_add (Signups), which knows nothing about AI access."""
    # The dashboard JS submits this via fetch() so a successful (or failed)
    # add never navigates the page — it just patches the form/table in
    # place. A non-AJAX POST (no JS, or a stale tab) still gets the original
    # message+redirect behaviour below.
    ajax = _is_ajax(request)
    if request.method == 'POST':
        form = AIAddUserForm(request.POST)
        if form.is_valid():
            # Keep the fallback here as well as in AIAddUserForm.clean_name
            # so an empty name can never reach the User record.
            name = (form.cleaned_data.get('name') or '').strip() or 'Admin'
            email = form.cleaned_data['email']
            phone = form.cleaned_data['phone']
            amount_paid = form.cleaned_data['amount_paid']
            payment_received_at = form.cleaned_data['payment_received_at']
            password = form.cleaned_data['password'] or 'admin54321'
            access_days_value = form.cleaned_data.get('ai_access_days')
            access_days = int(access_days_value) if access_days_value else 0
            first_name, _, last_name = name.partition(' ')
            user = User.objects.create_user(
                username=email, email=email, password=password,
                first_name=first_name, last_name=last_name,
            )
            access_started = timezone.now() if access_days else None
            access_until = access_started + timedelta(days=access_days) if access_days else None
            profile = StoreProfile.objects.create(
                user=user, phone=phone, manual_amount_paid=amount_paid,
                manual_payment_received_at=(
                    (payment_received_at or timezone.now()) if amount_paid > 0 else None
                ),
                ai_subscription_started_at=access_started,
                ai_subscription_until=access_until,
            )
            success_message = f'Created account for {email}.'
            whatsapp_payload = None
            if access_days:
                whatsapp_message = AIAccountMessageSettings.get_solo().render_message(
                    email=email, password=password, access_days=access_days,
                )
                whatsapp_payload = {
                    'message': whatsapp_message, 'email': email, 'password': password,
                    'days': access_days,
                }
            if ajax:
                response_data = {'status': 'ok', 'message': success_message}
                if access_days:
                    response_data['subscriber_row_html'] = render_to_string(
                        'dashboard/_subscriber_row.html',
                        {'profile': profile, 'now': timezone.now()}, request=request,
                    )
                if whatsapp_payload:
                    response_data['whatsapp'] = whatsapp_payload
                return JsonResponse(response_data)
            messages.success(request, success_message)
            if whatsapp_payload:
                # Popped by dashboard_ai_management so credentials only appear once.
                request.session['dashboard_new_account_whatsapp'] = whatsapp_payload
        else:
            if ajax:
                error_response = {
                    'status': 'validation_error',
                    'errors': {field: [str(e) for e in errs] for field, errs in form.errors.items()},
                }
                email_errors = form.errors.get('email') or []
                if any('already exists' in msg for msg in email_errors):
                    posted_email = (request.POST.get('email') or '').strip().lower()
                    existing_user = User.objects.filter(email__iexact=posted_email).select_related('store_profile').first()
                    if existing_user:
                        existing_profile = getattr(existing_user, 'store_profile', None)
                        remaining_days = 0
                        if existing_profile and existing_profile.ai_subscription_until and existing_profile.ai_subscription_until > timezone.now():
                            remaining_days = (existing_profile.ai_subscription_until - timezone.now()).days + 1
                        resend_password = 'admin54321'
                        resend_message = AIAccountMessageSettings.get_solo().render_message(
                            email=existing_user.email, password=resend_password, access_days=remaining_days,
                        )
                        error_response['existing_whatsapp'] = {
                            'message': resend_message, 'email': existing_user.email,
                            'password': resend_password, 'days': remaining_days,
                        }
                return JsonResponse(error_response, status=400)
            for errs in form.errors.values():
                for error in errs:
                    messages.error(request, error)
            # Re-open the "Add user manually" popup with what was typed still
            # filled in, instead of staff having to retype everything after a
            # duplicate-email error. Password is deliberately left out —
            # re-enter it after an error.
            repost = {k: v for k, v in request.POST.items() if k not in ('csrfmiddlewaretoken', 'password')}
            request.session['dashboard_ai_add_user_retry'] = repost
    return redirect('dashboard_ai_management')


@dashboard_staff_required
def dashboard_ai_settings(request):
    settings_obj = AISiteSettings.get_solo()
    form = AISiteSettingsForm(request.POST or None, request.FILES or None, instance=settings_obj)
    saved = False
    if request.method == 'POST' and form.is_valid():
        form.save()
        saved = True
        form = AISiteSettingsForm(instance=settings_obj)
    return render(request, 'dashboard/ai_settings.html', {
        'active': 'ai_settings', 'form': form, 'settings_obj': settings_obj, 'saved': saved,
    })


def ai_conversation_import(request):
    """Creates a new conversation from a chat transcript the user uploads
    from the account dropdown's "Import chat" option (see the Vidhyora AI
    account menu in ai.html) — a JSON body shaped either as a bare list of
    messages or {title, messages}, each message {role, content} with role
    'user'/'assistant' (a handful of common aliases are accepted client-side
    in normalizeImportedChat before this ever gets called). Owned the same
    dual way as a conversation started from chat itself, so an imported
    guest chat still carries over on login like any other."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    payload = _parse_json_body(request)
    if not isinstance(payload, dict):
        return JsonResponse({'status': 'error', 'detail': 'Invalid import file.'}, status=400)
    raw_messages = payload.get('messages')
    if not isinstance(raw_messages, list) or not raw_messages:
        return JsonResponse({'status': 'error', 'detail': 'That file has no recognizable chat messages.'}, status=400)

    cleaned = []
    for item in raw_messages[:AI_IMPORT_MAX_MESSAGES]:
        if not isinstance(item, dict):
            continue
        role = item.get('role')
        content = item.get('content')
        if role not in (AIMessage.ROLE_USER, AIMessage.ROLE_ASSISTANT):
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        cleaned.append((role, content[:AI_IMPORT_MAX_MESSAGE_CHARS]))
    if not cleaned:
        return JsonResponse({'status': 'error', 'detail': 'That file has no recognizable chat messages.'}, status=400)

    title = payload.get('title')
    title = title.strip()[:AI_CONVERSATION_TITLE_CHARS] if isinstance(title, str) and title.strip() else ''
    if not title:
        first_user = next((content for role, content in cleaned if role == AIMessage.ROLE_USER), cleaned[0][1])
        title = first_user[:AI_CONVERSATION_TITLE_CHARS]

    if not request.user.is_authenticated and not request.session.session_key:
        request.session.create()
    ip = _client_ip(request)
    conv_ip = ip if ip and ip != 'unknown' else None
    if request.user.is_authenticated:
        conversation = AIConversation.objects.create(user=request.user, title=title, ip_address=conv_ip)
    else:
        conversation = AIConversation.objects.create(session_key=request.session.session_key, title=title, ip_address=conv_ip)

    AIMessage.objects.bulk_create([
        AIMessage(conversation=conversation, role=role, content=content) for role, content in cleaned
    ])

    return JsonResponse({'status': 'ok', 'conversation': {'id': conversation.id, 'title': conversation.title}})
