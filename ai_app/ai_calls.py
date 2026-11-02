"""Voice calls on the /ai/ page: saving them and giving them back.

The browser runs the call (see the phone button in ai.html). Here a call is
saved as a row (``models.AICall``) that holds the transcript of both sides and,
when the caller allowed recording, their microphone audio. The audio arrives in
small pieces while the call is running, so closing the tab loses at most the
last few seconds. Everything is private to the account that made the call.

    POST /ai/api/calls/start/                  -> {id}
    POST /ai/api/calls/<id>/save/              transcript so far / final
    POST /ai/api/calls/<id>/audio/             one piece of the recording
    GET  /ai/api/calls/<id>/                   full call with transcript
    GET  /ai/api/calls/<id>/download/<txt|pdf>
    GET  /ai/api/calls/<id>/audio/             the recording (supports seeking)
    POST /ai/api/calls/<id>/delete/            and  /ai/api/calls/delete-all/
"""
import json
import logging
import os
import re
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.core.cache import cache
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.utils import timezone

from myapp.dropbox_storage import private_storage
from . import ai_chat, chat_export, file_convert

logger = logging.getLogger(__name__)

MAX_CHUNK_BYTES = 4 * 1024 * 1024
MAX_AUDIO_BYTES = 80 * 1024 * 1024
MAX_TURNS = 800
MAX_TEXT = 4000
LIST_LIMIT = 200
CALLS_PER_HOUR = 60

_AUDIO_EXTENSIONS = {'audio/webm': 'webm', 'audio/ogg': 'ogg', 'audio/mp4': 'm4a', 'audio/mpeg': 'mp3', 'audio/wav': 'wav'}
_RANGE_RE = re.compile(r'^bytes=(\d*)-(\d*)$')


def _error(detail, status):
    return JsonResponse({'status': 'error', 'detail': detail}, status=status)


def _guard(request, method):
    if request.method != method:
        return _error('Invalid request method.', 405)
    if not request.user.is_authenticated:
        return _error('Log in to save and see your calls.', 401)
    return None


def _own(request, pk):
    from .models import AICall
    return AICall.objects.filter(pk=pk, user=request.user).first()


def _payload(request):
    """JSON body, or a form with a ``payload`` field (what sendBeacon sends)."""
    raw = request.POST.get('payload') if request.POST else None
    if raw is None:
        raw = request.body or '{}'
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _local(moment):
    return timezone.localtime(moment) if timezone.is_aware(moment) else moment


def _clean_transcript(raw):
    turns = []
    if not isinstance(raw, list):
        return turns
    for item in raw[:MAX_TURNS]:
        if not isinstance(item, dict) or item.get('who') not in ('ai', 'you'):
            continue
        text = str(item.get('text') or '').strip()[:MAX_TEXT]
        if not text:
            continue
        turn = {'who': item['who'], 'text': text}
        at = item.get('at')
        if isinstance(at, str) and re.match(r'^\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?$', at):
            turn['at'] = at
        if item.get('interrupted') is True:
            turn['interrupted'] = True
        if item.get('step') in ('greeting', 'name', 'chat', 'closing'):
            turn['step'] = item['step']
        turns.append(turn)
    return turns


def _preview(call):
    for turn in call.transcript or []:
        if turn.get('who') == 'you' and turn.get('step') != 'name':
            text = turn.get('text', '')
            return text if len(text) <= 120 else text[:119].rstrip() + '…'
    return ''


def summary(call):
    ended = call.ended_at is not None
    return {
        'id': call.pk,
        'started_at': _local(call.started_at).isoformat(),
        'duration_seconds': call.duration_seconds,
        'caller_name': call.caller_name,
        'turn_count': call.turn_count,
        'has_audio': bool(call.audio) and call.audio_bytes > 0,
        'audio_bytes': call.audio_bytes,
        'preview': _preview(call),
        'ended': ended,
    }


def summaries(user, limit=LIST_LIMIT):
    from .models import AICall
    return [summary(call) for call in AICall.objects.filter(user=user).order_by('-started_at')[:limit]]


# ------------------------------------------------------------------ saving

def start(request):
    from .models import AICall
    refused = _guard(request, 'POST')
    if refused:
        return refused
    key = f'ai-call-start:{request.user.pk}'
    cache.add(key, 0, 3600)
    try:
        if cache.incr(key) > CALLS_PER_HOUR:
            return _error('Too many calls in the last hour. Please try again later.', 429)
    except ValueError:
        cache.set(key, 1, 3600)
    data = _payload(request)
    call = AICall.objects.create(user=request.user, language=str(data.get('language') or '')[:12])
    return JsonResponse({'status': 'ok', 'id': call.pk, 'max_audio_bytes': MAX_AUDIO_BYTES})


def save(request, pk):
    """The transcript so far (or the finished call)."""
    from .models import AIConversation
    refused = _guard(request, 'POST')
    if refused:
        return refused
    call = _own(request, pk)
    if not call:
        return _error('Call not found.', 404)
    data = _payload(request)
    call.transcript = _clean_transcript(data.get('transcript'))
    call.turn_count = sum(1 for turn in call.transcript if turn['who'] == 'you')
    call.caller_name = str(data.get('caller_name') or call.caller_name)[:60]
    conversation_id = data.get('conversation_id')
    if isinstance(conversation_id, int) and not isinstance(conversation_id, bool):
        conversation = AIConversation.objects.filter(pk=conversation_id, user=request.user).first()
        if conversation:
            call.conversation = conversation
    now = timezone.now()
    if data.get('final') is True and not call.ended_at:
        call.ended_at = now
    call.duration_seconds = max(0, int(((call.ended_at or now) - call.started_at).total_seconds()))
    call.save()
    return JsonResponse({'status': 'ok', 'call': summary(call)})


def upload_audio(request, pk):
    """One piece of the recording; pieces are appended in order."""
    refused = _guard(request, 'POST')
    if refused:
        return refused
    call = _own(request, pk)
    if not call:
        return _error('Call not found.', 404)
    chunk = request.FILES.get('chunk')
    mime = (request.POST.get('mime') or '').split(';')[0].strip().lower()
    extension = _AUDIO_EXTENSIONS.get(mime)
    if not chunk or not extension:
        return _error('That recording could not be read.', 400)
    if chunk.size > MAX_CHUNK_BYTES:
        return _error('That piece of the recording is too large.', 413)
    if call.audio_bytes + chunk.size > MAX_AUDIO_BYTES:
        return _error('This call reached the recording size limit.', 413)
    try:
        seq = int(request.POST.get('seq', ''))
    except ValueError:
        return _error('That recording could not be read.', 400)
    if seq < call.audio_chunks:
        return JsonResponse({'status': 'ok', 'duplicate': True})
    if seq > call.audio_chunks:
        return JsonResponse({'status': 'error', 'detail': 'A piece of the recording is missing.', 'expected': call.audio_chunks}, status=409)

    name = call.audio.name or f'ai_calls/{call.user_id}/{call.pk}.{extension}'
    try:
        target = Path(private_storage().path(name))
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, 'ab') as handle:
            for part in chunk.chunks():
                handle.write(part)
    except NotImplementedError:
        return _error('Recording storage is not available on this server.', 503)
    except OSError:
        logger.exception('Could not store a call recording piece for call %s', call.pk)
        return _error('Could not save the recording.', 500)
    call.audio.name = name
    call.audio_mime = mime
    call.audio_bytes += chunk.size
    call.audio_chunks += 1
    call.save(update_fields=['audio', 'audio_mime', 'audio_bytes', 'audio_chunks', 'updated_at'])
    return JsonResponse({'status': 'ok', 'chunks': call.audio_chunks})


# ------------------------------------------------------------------ reading

def detail(request, pk):
    refused = _guard(request, 'GET')
    if refused:
        return refused
    call = _own(request, pk)
    if not call:
        return _error('Call not found.', 404)
    body = summary(call)
    body['transcript'] = call.transcript or []
    body['conversation_id'] = call.conversation_id
    body['status'] = 'ok'
    response = JsonResponse(body)
    response['Cache-Control'] = 'private, no-store'
    return response


def _clock(value, fmt):
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return ''
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, dt_timezone.utc)
    return timezone.localtime(moment).strftime(fmt)


def _duration_text(seconds):
    minutes, secs = divmod(int(seconds or 0), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f'{hours}h {minutes:02d}m {secs:02d}s'
    return f'{minutes}m {secs:02d}s'


def _as_chat(call, brand):
    started = _local(call.started_at)
    you = call.caller_name or 'You'
    turns = []
    for turn in call.transcript or []:
        when = _clock(turn.get('at'), '%d %b %Y, %I:%M %p') or started.strftime('%d %b %Y, %I:%M %p')
        stamp = _clock(turn.get('at'), '%I:%M %p') or started.strftime('%I:%M %p')
        ai = turn['who'] == 'ai'
        notes = ['[interrupted by the caller]'] if turn.get('interrupted') else []
        turns.append(chat_export.Turn(
            role='assistant' if ai else 'user', who=f'{brand} AI' if ai else you,
            when=when, time=stamp, content=turn.get('text', ''), notes=notes,
        ))
    count = len(turns)
    return chat_export.Chat(
        title=f'Voice call · {started.strftime("%d %b %Y, %I:%M %p")}',
        turns=turns,
        started=started.strftime('%d %b %Y, %I:%M %p'),
        subtitle=f'{_duration_text(call.duration_seconds)} · {count} line{"" if count == 1 else "s"}',
    )


def _as_text(call, brand):
    chat = _as_chat(call, brand)
    rule = '=' * 70
    lines = [rule, f'{brand} AI — voice call', rule,
             f'Date      : {chat.started}',
             f'Length    : {_duration_text(call.duration_seconds)}',
             f'Caller    : {call.caller_name or "—"}',
             f'Recording : {"yes — your voice only" if call.audio_bytes else "no"}',
             '', '-' * 70, '']
    for turn in chat.turns:
        lines.append(f'[{turn.time}] {turn.who}')
        lines.append('    ' + turn.content.replace('\n', '\n    '))
        for note in turn.notes:
            lines.append('    ' + note)
        lines.append('')
    return '\n'.join(lines)


def download(request, pk, file_format):
    refused = _guard(request, 'GET')
    if refused:
        return refused
    if file_format not in ('txt', 'pdf'):
        return _error('Unsupported format.', 400)
    return download_response(_own(request, pk), file_format)


def download_response(call, file_format):
    """One call as a .txt or .pdf transcript (also used by the dashboard)."""
    if not call:
        return _error('Call not found.', 404)
    brand = ai_chat.get_ai_brand_name()
    stem = f'voice-call-{_local(call.started_at):%Y-%m-%d-%H%M}'
    if file_format == 'pdf':
        try:
            payload = chat_export.build_pdf(brand, [_as_chat(call, brand)], single=True)
        except file_convert.ConvertError as exc:
            return _error(str(exc), 503)
        content_type = file_convert.mime_for('pdf')
    else:
        payload = _as_text(call, brand).encode('utf-8-sig')
        content_type = 'text/plain; charset=utf-8'
    response = HttpResponse(payload, content_type=content_type)
    response['Content-Disposition'] = f'attachment; filename="{stem}.{file_format}"'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, no-store'
    return response


def _slice(path, start, length, block=64 * 1024):
    with open(path, 'rb') as handle:
        handle.seek(start)
        left = length
        while left > 0:
            data = handle.read(min(block, left))
            if not data:
                break
            left -= len(data)
            yield data


def audio(request, pk):
    """The recording, with Range support so the player can seek."""
    refused = _guard(request, 'GET')
    if refused:
        return refused
    return audio_response(request, _own(request, pk))


def audio_response(request, call):
    """Stream one call's recording (also used by the dashboard's Call Data)."""
    if not call or not call.audio or not call.audio_bytes:
        return _error('There is no recording for this call.', 404)
    try:
        path = private_storage().path(call.audio.name)
        size = os.path.getsize(path)
    except (NotImplementedError, OSError):
        return _error('The recording file is no longer available.', 404)
    content_type = call.audio_mime or 'audio/webm'
    extension = Path(call.audio.name).suffix or '.webm'
    disposition = 'attachment' if request.GET.get('download') else 'inline'
    filename = f'voice-call-{_local(call.started_at):%Y-%m-%d-%H%M}{extension}'

    match = _RANGE_RE.match(request.META.get('HTTP_RANGE', '').strip())
    if match and (match.group(1) or match.group(2)):
        if match.group(1):
            first = int(match.group(1))
            last = int(match.group(2)) if match.group(2) else size - 1
        else:
            first = max(size - int(match.group(2)), 0)
            last = size - 1
        last = min(last, size - 1)
        if first > last or first >= size:
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{size}'
            return response
        response = StreamingHttpResponse(_slice(path, first, last - first + 1), status=206, content_type=content_type)
        response['Content-Range'] = f'bytes {first}-{last}/{size}'
        response['Content-Length'] = str(last - first + 1)
    else:
        response = StreamingHttpResponse(_slice(path, 0, size), content_type=content_type)
        response['Content-Length'] = str(size)
    response['Accept-Ranges'] = 'bytes'
    response['Content-Disposition'] = f'{disposition}; filename="{filename}"'
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


def recording(request, pk):
    """POST adds a piece of the recording; GET plays or downloads it."""
    if request.method == 'POST':
        return upload_audio(request, pk)
    return audio(request, pk)


# ----------------------------------------------------------------- deleting

def _remove_audio(call):
    if call.audio:
        try:
            private_storage().delete(call.audio.name)
        except Exception:
            logger.warning('Could not delete call recording %s', call.audio.name)


def delete(request, pk):
    refused = _guard(request, 'POST')
    if refused:
        return refused
    call = _own(request, pk)
    if not call:
        return _error('Call not found.', 404)
    _remove_audio(call)
    call.delete()
    return JsonResponse({'status': 'ok'})


def delete_all(request):
    from .models import AICall
    refused = _guard(request, 'POST')
    if refused:
        return refused
    for call in AICall.objects.filter(user=request.user):
        _remove_audio(call)
    AICall.objects.filter(user=request.user).delete()
    return JsonResponse({'status': 'ok', 'calls': []})
