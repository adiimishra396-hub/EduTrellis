"""The "Start coding" backend: an OpenAI-compatible endpoint that terminal
coding agents such as OpenCode can use as a provider.

    GET  /ai/api/v1/code/models             -> the one model on offer
    POST /ai/api/v1/code/chat/completions   -> chat, streaming or not, with tools

Callers authenticate with a personal ``vdc_…`` key (models.AICodingKey) that
the user creates under account menu → Start coding. Requests are forwarded to
the Vidhyora Code model (a Nemotron endpoint behind the shared chat key); the
vendor and model names never reach the client — it only ever sees
``vidhyora-code`` and the brand name.

Like every other model here there is no fallback: when the backend is down or
not configured the caller gets a plain "contact the administrator" error.
"""
import hashlib
import json
import logging
import re
import time

from django.conf import settings
from django.core.cache import cache
from django.db.models import F
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from . import ai_chat, model_controls

logger = logging.getLogger(__name__)

MODEL_ID = 'edutrellis-code'           # what the client sees and configures
CONTROL_KEY = 'coding-cli'           # dashboard on/off switch and counters
# The manual setup (a personal key copied into OpenCode by hand) is switched
# off for now: no new manual keys, and manual keys made earlier are refused.
# The one-command setup, with one key per approved computer, is unaffected.
MANUAL_SETUP_ENABLED = False
UPSTREAM_MODEL_KEY = 'code'          # the ai_chat.MODELS entry that answers
# Its own key and model can be set on API Settings; until then it uses the
# shared chat key and the Vidhyora Code model.
API_KEY_SETTING = 'NVIDIA_CODING_API_KEY'


def upstream_model():
    """(model id, sends the hide-thinking switch) for the upstream call."""
    model_id, thinking = model_controls.get_model_override(CONTROL_KEY)
    if model_id:
        return model_id, thinking
    return ai_chat.MODELS[UPSTREAM_MODEL_KEY]['id'], True


def test_connection():
    """(ok, message) from one tiny request with the key and model in use."""
    from .provider_keys import get_key
    model_id, thinking = upstream_model()
    own_key = bool(get_key(API_KEY_SETTING).strip())
    ok, message = ai_chat.test_model_connection(
        UPSTREAM_MODEL_KEY, key_setting=API_KEY_SETTING if own_key else 'NVIDIA_API_KEY',
        model_id=model_id, reasoning=thinking,
    )
    return ok, message.replace(ai_chat.MODELS[UPSTREAM_MODEL_KEY]['label'] + ' (', 'Start Coding (', 1)


def upstream_client():
    """The coding key's client when one is set, else the shared chat key's."""
    from .provider_keys import get_key
    if get_key(API_KEY_SETTING).strip():
        return ai_chat._get_client(API_KEY_SETTING)
    return ai_chat._get_client()

MAX_MESSAGES = 400
MAX_TOOLS = 128
MAX_PROMPT_CHARS = 1_500_000         # all message text together
DEFAULT_MAX_TOKENS = 8192
MAX_OUTPUT_TOKENS = 16384
NON_STREAM_TIMEOUT = 180.0
STREAM_TIMEOUT = 600.0

_ROLES = {'system', 'user', 'assistant', 'tool'}
_VENDOR_WORDS = re.compile(r'nvidia|nemotron|nim\b', re.IGNORECASE)


def rate_limits():
    """(requests per minute, requests per day) for one key; settings can tune."""
    return (
        int(getattr(settings, 'CODING_RATE_PER_MINUTE', 60)),
        int(getattr(settings, 'CODING_REQUESTS_PER_DAY', 1500)),
    )


def has_access(user):
    """Coding follows the in-app AI plan: staff, or an active subscription."""
    from myapp.models import StoreProfile
    from .views import _ai_has_admin_access
    if _ai_has_admin_access(user):
        return True
    profile = StoreProfile.objects.filter(user=user).first()
    return bool(profile and profile.is_ai_subscribed)


# ------------------------------------------------------------------ responses

def _error(message, status, kind='invalid_request_error', code=None, headers=None):
    response = JsonResponse({'error': {'message': message, 'type': kind, 'code': code}}, status=status)
    for name, value in (headers or {}).items():
        response[name] = value
    response['Cache-Control'] = 'no-store'
    return response


def _contact():
    from .views import _support_email
    return f'Please contact the administrator at {_support_email()}.'


def _contact_clause():
    from .views import _support_email
    return f'contact the administrator at {_support_email()}.'


def _label():
    return ai_chat.MODELS[UPSTREAM_MODEL_KEY]['label']


def _scrub(text):
    """Upstream error text with any vendor/model name removed."""
    return _VENDOR_WORDS.sub('the model', str(text or ''))[:400]


def upstream_error_response(exc):
    """Map a failed upstream call to an OpenAI-style error the CLI understands."""
    label = _label()
    status = getattr(exc, 'status_code', None)
    if ai_chat._is_context_length_error(exc):
        return _error(
            f'This conversation is too long for {label}. Start a new session or compact it. ({_scrub(exc)})',
            400, code='context_length_exceeded',
        )
    if isinstance(exc, ValueError) or ai_chat._is_unconfigured_key_error(exc) or ai_chat._is_model_unavailable_error(exc):
        return _error(f'{label} is currently disconnected. {_contact()}', 503, kind='server_error', code='service_unavailable')
    if status in (401, 403):
        return _error(f'{label} authentication is currently unavailable. {_contact()}', 502, kind='server_error')
    if status == 429:
        return _error(
            f'{label} is at its request limit right now. Wait a moment and retry; if it keeps happening, '
            f'{_contact_clause()}',
            429, kind='rate_limit_error', code='rate_limit_exceeded', headers={'Retry-After': '10'},
        )
    if status in (400, 422):
        return _error(_scrub(getattr(exc, 'message', None) or exc), 400)
    return _error(
        f'{label} is temporarily unavailable. Wait a moment and retry; if it keeps happening, '
        f'{_contact_clause()}',
        503, kind='server_error', code='service_unavailable', headers={'Retry-After': '5'},
    )


# --------------------------------------------------------------- authenticate

def _authenticate(request):
    """(AICodingKey, None) or (None, error response)."""
    from .models import AICodingKey
    header = request.META.get('HTTP_AUTHORIZATION', '')
    raw_key = header[7:] if header.lower().startswith('bearer ') else ''
    key = AICodingKey.resolve(raw_key)
    if not key or not key.user.is_active:
        return None, _error(
            'Invalid or missing API key. Create one under Start coding in your EduTrellis account '
            'and sign in again with `opencode auth login`.',
            401, kind='authentication_error', code='invalid_api_key',
        )
    if not key.label and not MANUAL_SETUP_ENABLED:
        return None, _error(
            'Manual keys are switched off. Connect this computer with the one-command setup under '
            'Start coding in your EduTrellis account instead.',
            401, kind='authentication_error', code='manual_keys_disabled',
        )
    if not has_access(key.user):
        return None, _error(
            'Your EduTrellis AI plan does not include Start coding. Upgrade your plan or ask the administrator.',
            403, kind='permission_error', code='plan_required',
        )
    if not model_controls.is_enabled(CONTROL_KEY):
        return None, _error(
            f'Start coding is switched off right now. {_contact()}',
            503, kind='server_error', code='service_disabled',
        )
    return key, None


def _check_rate(key):
    """None when the key is within its limits, else a 429 response."""
    per_minute, per_day = rate_limits()
    minute_slot = int(time.time() // 60)
    for name, limit, ttl, wait in (
        (f'coding:m:{key.pk}:{minute_slot}', per_minute, 120, 30),
        (f'coding:d:{key.pk}:{timezone.localdate()}', per_day, 90000, 3600),
    ):
        cache.add(name, 0, ttl)
        try:
            count = cache.incr(name)
        except ValueError:
            cache.set(name, 1, ttl)
            count = 1
        if count > limit:
            what = 'per minute' if wait == 30 else 'per day'
            return _error(
                f'Rate limit reached: {limit} requests {what} for this key. Try again shortly.',
                429, kind='rate_limit_error', code='rate_limit_exceeded', headers={'Retry-After': str(wait)},
            )
    return None


# ------------------------------------------------------------ request cleanup

class BadRequest(Exception):
    pass


def _text_of(content):
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict) and part.get('type') in ('text', 'input_text', 'output_text'):
                pieces.append(str(part.get('text') or ''))
            else:
                raise BadRequest(f'{_label()} reads text only — images and other attachments are not supported.')
        return ''.join(pieces)
    raise BadRequest('Message "content" must be a string or a list of text parts.')


def _clean_tool_calls(raw):
    if not isinstance(raw, list):
        raise BadRequest('"tool_calls" must be a list.')
    calls = []
    for call in raw:
        function = call.get('function') if isinstance(call, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get('name'), str):
            raise BadRequest('Each tool call needs a function name.')
        arguments = function.get('arguments')
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments if arguments is not None else {})
        calls.append({
            'id': str(call.get('id') or ''),
            'type': 'function',
            'function': {'name': function['name'], 'arguments': arguments},
        })
    return calls


def clean_messages(raw):
    if not isinstance(raw, list) or not raw:
        raise BadRequest('"messages" must be a non-empty list.')
    if len(raw) > MAX_MESSAGES:
        raise BadRequest(f'Too many messages — send at most {MAX_MESSAGES}.')
    cleaned, total = [], 0
    for item in raw:
        if not isinstance(item, dict):
            raise BadRequest('Each message must be an object with "role" and "content".')
        role = 'system' if item.get('role') == 'developer' else item.get('role')
        if role not in _ROLES:
            raise BadRequest(f'Unsupported message role "{item.get("role")}".')
        message = {'role': role, 'content': _text_of(item.get('content'))}
        total += len(message['content'])
        if isinstance(item.get('name'), str):
            message['name'] = item['name'][:64]
        if role == 'assistant' and item.get('tool_calls'):
            message['tool_calls'] = _clean_tool_calls(item['tool_calls'])
        if role == 'tool':
            if not isinstance(item.get('tool_call_id'), str) or not item['tool_call_id']:
                raise BadRequest('A tool message needs a "tool_call_id".')
            message['tool_call_id'] = item['tool_call_id']
        cleaned.append(message)
    if total > MAX_PROMPT_CHARS:
        raise BadRequest('This conversation is too long for EduTrellis Code. Start a new session or compact it.')
    return cleaned


def _identity_note():
    brand = ai_chat.get_ai_brand_name()
    label = _label()
    return (
        f"You are {label}, the coding assistant of {brand} AI, running inside the user's terminal. "
        f"If asked who or what you are, say you are {label} from {brand}. Never mention or hint at the "
        "underlying model, its vendor or who trained it. Follow the tools and instructions the client gives you."
    )


def with_identity(messages):
    note = _identity_note()
    if messages and messages[0]['role'] == 'system':
        first = dict(messages[0])
        first['content'] = (first['content'] + '\n\n' + note).strip()
        return [first] + messages[1:]
    return [{'role': 'system', 'content': note}] + messages


def build_upstream_kwargs(payload):
    """The sanitized keyword arguments for the upstream completion call."""
    model_id, thinking_control = upstream_model()
    kwargs = {
        'model': model_id,
        'messages': with_identity(clean_messages(payload.get('messages'))),
        'stream': bool(payload.get('stream')),
    }
    if thinking_control:
        kwargs['extra_body'] = {'chat_template_kwargs': {'enable_thinking': False}}
    tools = payload.get('tools')
    if tools:
        if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
            raise BadRequest(f'"tools" must be a list of at most {MAX_TOOLS} tools.')
        for tool in tools:
            if not isinstance(tool, dict) or tool.get('type') != 'function' or not isinstance(tool.get('function'), dict):
                raise BadRequest('Each tool must be {"type": "function", "function": {...}}.')
        kwargs['tools'] = tools
        choice = payload.get('tool_choice')
        if isinstance(choice, (str, dict)):
            kwargs['tool_choice'] = choice
    for name, low, high in (('temperature', 0.0, 2.0), ('top_p', 0.0, 1.0)):
        value = payload.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            kwargs[name] = min(max(float(value), low), high)
    requested = payload.get('max_tokens', payload.get('max_completion_tokens'))
    if isinstance(requested, int) and not isinstance(requested, bool) and requested > 0:
        kwargs['max_tokens'] = min(requested, MAX_OUTPUT_TOKENS)
    else:
        kwargs['max_tokens'] = DEFAULT_MAX_TOKENS
    stop = payload.get('stop')
    if isinstance(stop, str) or (isinstance(stop, list) and 0 < len(stop) <= 4 and all(isinstance(s, str) for s in stop)):
        kwargs['stop'] = stop
    if kwargs['stream'] and isinstance(payload.get('stream_options'), dict):
        kwargs['stream_options'] = {'include_usage': bool(payload['stream_options'].get('include_usage'))}
    kwargs['timeout'] = STREAM_TIMEOUT if kwargs['stream'] else NON_STREAM_TIMEOUT
    return kwargs


# ----------------------------------------------------------------------- views

def _lean(value):
    """Drop the null fields the SDK models carry (tool_calls: null, refusal:
    null, …) so every client sees the compact shape OpenAI itself sends;
    finish_reason stays because clients look for it even when null."""
    if isinstance(value, dict):
        return {k: _lean(v) for k, v in value.items() if v is not None or k == 'finish_reason'}
    if isinstance(value, list):
        return [_lean(v) for v in value]
    return value


def _sse(data):
    return f'data: {json.dumps(data, ensure_ascii=False)}\n\n'


def _record_use(key):
    from .models import AICodingKey
    AICodingKey.objects.filter(pk=key.pk).update(
        last_used_at=timezone.now(), request_count=F('request_count') + 1,
    )


# --------------------------------------------------------- activity logging
#
# Every request is recorded for the dashboard's OpenCode Data page: who made
# it, what was asked (the newest message only — OpenCode resends the whole
# conversation each time, so earlier turns are already in earlier requests)
# and what came back. Long texts are trimmed. Logging must never get in the
# way of an answer, so any failure here is swallowed.

LOG_USER_CHARS = 8000
LOG_TOOL_RESULT_CHARS = 1500
LOG_REPLY_CHARS = 20000
LOG_TOOL_ARGUMENT_CHARS = 1000
LOG_MAX_TOOL_CALLS = 30


def _trim(text, limit):
    text = text or ''
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f'\n… ({len(text) - limit:,} more characters not kept)'


def _session_for(key, messages):
    """The session this request belongs to: requests from one key that begin
    with the same first message are one working session."""
    from django.db import IntegrityError
    from .models import AICodingSession
    first = next((m['content'] for m in messages if m['role'] == 'user'), '')
    fingerprint = hashlib.sha256(f'{key.pk}|{first[:2000]}'.encode('utf-8')).hexdigest()
    title = ' '.join(first.split())[:120] or 'Untitled session'
    lookup = {'user': key.user, 'fingerprint': fingerprint}
    defaults = {'key': key, 'machine': key.label, 'title': title}
    try:
        session, _ = AICodingSession.objects.get_or_create(defaults=defaults, **lookup)
    except IntegrityError:
        session = AICodingSession.objects.get(**lookup)
    return session


def _log_request(key, messages, *, stream, started, status, reply='', tool_calls=None, usage=None, error=''):
    """Record one request. Never raises."""
    try:
        from .models import AICodingRequest, AICodingSession
        session = _session_for(key, messages)
        last = messages[-1] if messages else {'role': 'user', 'content': ''}
        if last['role'] == 'tool':
            user_text = _trim(last['content'], LOG_TOOL_RESULT_CHARS)
        else:
            user_text = _trim(last['content'], LOG_USER_CHARS)
        calls = []
        for call in (tool_calls or [])[:LOG_MAX_TOOL_CALLS]:
            function = call.get('function') or {}
            calls.append({
                'name': str(function.get('name') or '')[:80],
                'arguments': _trim(str(function.get('arguments') or ''), LOG_TOOL_ARGUMENT_CHARS),
            })
        usage = usage if isinstance(usage, dict) else {}
        AICodingRequest.objects.create(
            session=session, user=key.user, status=status, error=(error or '')[:300], stream=stream,
            duration_ms=int((time.monotonic() - started) * 1000),
            trigger='tool' if last['role'] == 'tool' else 'user',
            user_text=user_text, reply_text=_trim(reply, LOG_REPLY_CHARS), tool_calls=calls,
            message_count=len(messages),
            prompt_chars=sum(len(m['content']) for m in messages), reply_chars=len(reply or ''),
            prompt_tokens=usage.get('prompt_tokens') if isinstance(usage.get('prompt_tokens'), int) else None,
            completion_tokens=usage.get('completion_tokens') if isinstance(usage.get('completion_tokens'), int) else None,
        )
        AICodingSession.objects.filter(pk=session.pk).update(
            last_request_at=timezone.now(), request_count=F('request_count') + 1,
        )
    except Exception:
        logger.exception('Could not record the OpenCode request for user %s', getattr(key, 'user_id', '?'))


@csrf_exempt
def models_list(request):
    if request.method != 'GET':
        return _error('Invalid request method. Use GET.', 405)
    key, failure = _authenticate(request)
    if failure:
        return failure
    return JsonResponse({
        'object': 'list',
        'data': [{
            'id': MODEL_ID, 'object': 'model', 'created': 0, 'owned_by': ai_chat.get_ai_brand_name().lower(),
        }],
    })


@csrf_exempt
def chat_completions(request):
    if request.method != 'POST':
        return _error('Invalid request method. Use POST.', 405)
    key, failure = _authenticate(request)
    if failure:
        return failure
    limited = _check_rate(key)
    if limited:
        return limited

    try:
        body = request.body
    except Exception:
        return _error('The request is too large. Start a new session or compact this one.', 413, code='request_too_large')
    try:
        payload = json.loads(body or '{}')
    except json.JSONDecodeError:
        return _error('The request body must be valid JSON.', 400)
    if not isinstance(payload, dict):
        return _error('The request body must be a JSON object.', 400)
    requested_model = str(payload.get('model') or MODEL_ID)
    if requested_model != MODEL_ID:
        return _error(f'Unknown model "{requested_model}". Use "{MODEL_ID}".', 404, code='model_not_found')
    try:
        kwargs = build_upstream_kwargs(payload)
    except BadRequest as exc:
        return _error(str(exc), 400)

    streaming = kwargs['stream']
    started = time.monotonic()
    logged_messages = clean_messages(payload.get('messages'))
    model_controls.record_request(CONTROL_KEY)
    _record_use(key)
    try:
        client = upstream_client()
        upstream = client.chat.completions.create(**kwargs)
    except Exception as exc:
        logger.warning('Coding API upstream call failed for user %s: %s', key.user_id, exc)
        model_controls.record_error(CONTROL_KEY, f'{exc.__class__.__name__}: {_scrub(exc)}')
        _log_request(key, logged_messages, stream=streaming, started=started, status='error', error=_scrub(exc))
        return upstream_error_response(exc)

    if not streaming:
        data = _lean(upstream.model_dump(mode='json'))
        data['model'] = MODEL_ID
        model_controls.record_success(CONTROL_KEY)
        reply_message = ((data.get('choices') or [{}])[0].get('message')) or {}
        _log_request(
            key, logged_messages, stream=False, started=started, status='ok',
            reply=reply_message.get('content') or '', tool_calls=reply_message.get('tool_calls'), usage=data.get('usage'),
        )
        response = JsonResponse(data)
        response['Cache-Control'] = 'no-store'
        return response

    def events():
        pieces = []
        calls = {}
        usage = None
        status, error = 'cancelled', ''
        try:
            for chunk in upstream:
                data = _lean(chunk.model_dump(mode='json'))
                data['model'] = MODEL_ID
                for choice in data.get('choices') or []:
                    delta = choice.get('delta') or {}
                    if delta.get('content'):
                        pieces.append(delta['content'])
                    for part in delta.get('tool_calls') or []:
                        slot = calls.setdefault(part.get('index', 0), {'function': {'name': '', 'arguments': ''}})
                        function = part.get('function') or {}
                        slot['function']['name'] += function.get('name') or ''
                        slot['function']['arguments'] += function.get('arguments') or ''
                if isinstance(data.get('usage'), dict):
                    usage = data['usage']
                yield _sse(data)
        except GeneratorExit:
            raise
        except Exception as exc:
            logger.warning('Coding API stream broke for user %s: %s', key.user_id, exc)
            model_controls.record_error(CONTROL_KEY, f'{exc.__class__.__name__}: {_scrub(exc)}')
            status, error = 'error', _scrub(exc)
            yield _sse({'error': {
                'message': f'{_label()} stopped responding mid-reply. {_contact()}',
                'type': 'server_error', 'code': 'stream_interrupted',
            }})
            return
        else:
            status = 'ok'
            model_controls.record_success(CONTROL_KEY)
            yield 'data: [DONE]\n\n'
        finally:
            _log_request(
                key, logged_messages, stream=True, started=started, status=status, error=error,
                reply=''.join(pieces), tool_calls=[calls[i] for i in sorted(calls)], usage=usage,
            )

    response = StreamingHttpResponse(events(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache, no-store'
    response['X-Accel-Buffering'] = 'no'
    return response


# ----------------------------------------------------- account (session) side

def _key_rows(user):
    return [{
        'id': row.pk,
        'label': row.label,
        'manual': not row.label,
        'prefix': row.key_prefix,
        'created_at': timezone.localtime(row.created_at).isoformat(),
        'last_used_at': timezone.localtime(row.last_used_at).isoformat() if row.last_used_at else None,
        'request_count': row.request_count,
    } for row in user.ai_coding_keys.order_by('-created_at')]


def account_summary(user, allowed, purchase_url=''):
    """What the Start coding panel needs from /ai/api/account/."""
    from .views import _support_email
    per_minute, per_day = rate_limits()
    keys = _key_rows(user)
    manual = next((k for k in keys if k['manual']), None)
    return {
        'allowed': bool(allowed),
        'enabled': model_controls.is_enabled(CONTROL_KEY),
        'purchase_url': purchase_url,
        'support_email': _support_email(),
        'model': {'id': MODEL_ID, 'label': _label()},
        'brand': ai_chat.get_ai_brand_name(),
        'limits': {'per_minute': per_minute, 'per_day': per_day},
        'manual_enabled': MANUAL_SETUP_ENABLED,
        # The manual key (step 2 of the manual setup).
        'has_key': bool(manual),
        'key_prefix': manual['prefix'] if manual else None,
        'key_created_at': manual['created_at'] if manual else None,
        'key_last_used_at': manual['last_used_at'] if manual else None,
        'request_count': manual['request_count'] if manual else 0,
        # Computers connected through the one-line setup.
        'devices': [k for k in keys if not k['manual']],
    }


def key_generate(request):
    """Create (or replace) the signed-in user's manual coding key. Returned once."""
    from .models import AICodingKey
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)
    if not has_access(request.user):
        return JsonResponse({
            'status': 'error',
            'detail': 'Start coding is part of the premium plan. Upgrade your plan to create a key.',
        }, status=403)
    if not model_controls.is_enabled(CONTROL_KEY):
        return JsonResponse({
            'status': 'error', 'detail': f'Start coding is switched off right now. {_contact()}',
        }, status=503)
    if not MANUAL_SETUP_ENABLED:
        return JsonResponse({
            'status': 'error',
            'detail': 'Manual setup is switched off. Use the one-command setup under Start coding instead.',
        }, status=403)
    raw_key = AICodingKey.generate_for(request.user)
    return JsonResponse({'status': 'ok', 'api_key': raw_key, 'key_prefix': raw_key[:11]})


def key_revoke(request):
    """Revoke one key: POST id=<key id> for a connected computer, or nothing
    for the manual key."""
    from .models import AICodingKey
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)
    rows = AICodingKey.objects.filter(user=request.user)
    key_id = request.POST.get('id', '')
    rows = rows.filter(pk=key_id) if key_id.isdigit() else rows.filter(label='')
    rows.delete()
    return JsonResponse({'status': 'ok'})


# --------------------------------------------- one-line setup (device login)

DEVICE_CODE_TTL = 600            # seconds the user has to approve
DEVICE_POLL_INTERVAL = 3
_USER_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'   # no 0/O, 1/I


def _client_ip(request):
    from myapp.ratelimit import client_ip
    return client_ip(request)


def _base_url(request):
    # EduTrellis serves the AI (and so the coding API and setup scripts) under /ai/,
    # so every "$BASE_URL/api/v1/code" / "$BASE_URL/start-coding/..." lands there.
    return f'{request.scheme}://{request.get_host()}/ai'


def _clean_machine(value):
    value = re.sub(r'[^A-Za-z0-9._ -]+', '', str(value or '')).strip()[:40]
    return value or 'unnamed computer'


def _compact(data, status=200):
    """JSON with no spaces, so the shell script can read it with plain grep."""
    return HttpResponse(
        json.dumps(data, separators=(',', ':')), status=status, content_type='application/json',
        headers={'Cache-Control': 'no-store'},
    )


def _throttle(name, limit, ttl):
    cache.add(name, 0, ttl)
    try:
        return cache.incr(name) > limit
    except ValueError:
        cache.set(name, 1, ttl)
        return False


def _hash_device_code(device_code):
    import hashlib
    return hashlib.sha256(device_code.encode('utf-8')).hexdigest()


@csrf_exempt
def device_start(request):
    """POST {machine}: the setup script asks for a code to show the user."""
    import secrets
    from datetime import timedelta
    from .models import AICodingDeviceCode
    if request.method != 'POST':
        return _error('Invalid request method. Use POST.', 405)
    if not model_controls.is_enabled(CONTROL_KEY):
        return _compact({'error': 'disabled', 'message': f'Start coding is switched off right now. {_contact()}'}, 503)
    ip = _client_ip(request)
    if _throttle(f'coding:dev:start:{ip}', 10, 600):
        return _compact({'error': 'rate_limited', 'message': 'Too many setup attempts. Wait a few minutes and try again.'}, 429)
    try:
        payload = json.loads(request.body or '{}')
    except ValueError:
        payload = {}
    machine = _clean_machine(payload.get('machine') if isinstance(payload, dict) else '')

    AICodingDeviceCode.objects.filter(expires_at__lt=timezone.now() - timedelta(days=1)).delete()
    device_code = secrets.token_urlsafe(32)
    for _ in range(10):
        user_code = ''.join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
        user_code = f'{user_code[:4]}-{user_code[4:]}'
        if not AICodingDeviceCode.objects.filter(user_code=user_code).exists():
            break
    AICodingDeviceCode.objects.create(
        device_hash=_hash_device_code(device_code), user_code=user_code, machine=machine,
        requester_ip=ip, expires_at=timezone.now() + timedelta(seconds=DEVICE_CODE_TTL),
    )
    return _compact({
        'device_code': device_code,
        'user_code': user_code,
        'verification_url': f'{_base_url(request)}/start-coding/approve/?code={user_code}',
        'expires_in': DEVICE_CODE_TTL,
        'interval': DEVICE_POLL_INTERVAL,
    })


@csrf_exempt
def device_poll(request):
    """POST {device_code}: pending, or — once, after approval — the key."""
    from .models import AICodingDeviceCode, AICodingKey
    if request.method != 'POST':
        return _error('Invalid request method. Use POST.', 405)
    try:
        payload = json.loads(request.body or '{}')
    except ValueError:
        payload = {}
    device_code = str(payload.get('device_code') or '') if isinstance(payload, dict) else ''
    if not device_code:
        return _compact({'status': 'invalid'}, 400)
    digest = _hash_device_code(device_code)
    if _throttle(f'coding:dev:poll:{digest[:16]}', 400, 900):
        return _compact({'status': 'slow_down'}, 429)
    row = AICodingDeviceCode.objects.filter(device_hash=digest).select_related('user').first()
    if not row or row.status == AICodingDeviceCode.STATUS_USED:
        return _compact({'status': 'invalid'}, 404)
    if row.status == AICodingDeviceCode.STATUS_DENIED:
        return _compact({'status': 'denied'})
    if row.is_expired:
        return _compact({'status': 'expired'})
    if row.status != AICodingDeviceCode.STATUS_APPROVED:
        return _compact({'status': 'pending'})

    # Hand the key out exactly once: only the request that flips approved ->
    # used gets it, so a replayed poll can never mint a second one.
    claimed = AICodingDeviceCode.objects.filter(
        pk=row.pk, status=AICodingDeviceCode.STATUS_APPROVED,
    ).update(status=AICodingDeviceCode.STATUS_USED)
    if not claimed:
        return _compact({'status': 'invalid'}, 404)
    if not has_access(row.user) or not model_controls.is_enabled(CONTROL_KEY):
        return _compact({'status': 'denied'})
    raw_key = AICodingKey.generate_for(row.user, label=row.machine)
    return _compact({'status': 'approved', 'api_key': raw_key})


def _approval_context(request, row, **extra):
    return {
        'brand': ai_chat.get_ai_brand_name(),
        'row': row,
        'user_code': row.user_code if row else '',
        'logged_in': request.user.is_authenticated,
        'user_name': getattr(request.user, 'first_name', '') or getattr(request.user, 'email', '') or request.user.get_username(),
        **extra,
    }


def approve_page(request):
    """The page the setup script opens: confirm the code, then Approve/Deny."""
    from django.shortcuts import render
    from .models import AICodingDeviceCode
    code = (request.POST.get('code') or request.GET.get('code') or '').strip().upper()
    if _throttle(f'coding:dev:approve:{_client_ip(request)}', 60, 600):
        return render(request, 'start_coding_approve.html', _approval_context(request, None, state='limited'), status=429)
    row = AICodingDeviceCode.objects.filter(user_code=code).first() if code else None
    if not row or row.is_expired or row.status != AICodingDeviceCode.STATUS_PENDING:
        state = 'done' if row and row.status in (AICodingDeviceCode.STATUS_APPROVED, AICodingDeviceCode.STATUS_USED) else 'invalid'
        return render(request, 'start_coding_approve.html', _approval_context(request, None, state=state), status=200)

    if not request.user.is_authenticated:
        return render(request, 'start_coding_approve.html', _approval_context(request, row, state='login'))
    if not has_access(request.user):
        return render(request, 'start_coding_approve.html', _approval_context(
            request, row, state='no_plan', purchase_url=account_summary(request.user, False)['purchase_url'] or '/',
        ))
    if not model_controls.is_enabled(CONTROL_KEY):
        from .views import _support_email
        return render(request, 'start_coding_approve.html', _approval_context(request, row, state='disabled', support_email=_support_email()))

    if request.method == 'POST':
        approve = request.POST.get('decision') == 'approve'
        AICodingDeviceCode.objects.filter(pk=row.pk, status=AICodingDeviceCode.STATUS_PENDING).update(
            status=AICodingDeviceCode.STATUS_APPROVED if approve else AICodingDeviceCode.STATUS_DENIED,
            user=request.user if approve else None,
        )
        return render(request, 'start_coding_approve.html', _approval_context(
            request, row, state='approved' if approve else 'denied',
        ))
    return render(request, 'start_coding_approve.html', _approval_context(
        request, row, state='confirm', requested_at=timezone.localtime(row.created_at),
    ))


def setup_script(request, kind):
    """The one-line installer, with this site's address and model baked in."""
    from pathlib import Path
    if kind not in ('sh', 'ps1'):
        return _error('Unknown script.', 404)
    path = Path(__file__).resolve().parent / 'start_coding' / f'setup.{kind}'
    text = path.read_text(encoding='utf-8').replace('\r\n', '\n')

    def plain(value):
        return re.sub(r'[^A-Za-z0-9._ -]+', '', str(value))

    text = (text
            .replace('__BASE_URL__', _base_url(request))
            .replace('__BRAND__', plain(ai_chat.get_ai_brand_name()))
            .replace('__MODEL_ID__', MODEL_ID)
            .replace('__MODEL_LABEL__', plain(_label())))
    if kind == 'ps1':
        text = text.replace('\n', '\r\n')
    response = HttpResponse(text, content_type='text/plain; charset=utf-8')
    response['Cache-Control'] = 'no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response
