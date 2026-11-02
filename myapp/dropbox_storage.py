"""Django's default file storage: every uploaded file lives in Dropbox under
settings.DROPBOX_MEDIA_ROOT ('/EduTrellis Site/<name>'), nothing on this
server's disk.

Each file gets a StoredFile row holding its permanent Dropbox shared link, and
FieldFile.url returns that link. Until the Dropbox app is allowed to create
shared links (the 'sharing.write' permission), url() returns /media/<name>
instead, which media_redirect() answers with a short-lived Dropbox download
link — so pages work either way and pick up the permanent links on their own
once the permission is there.

Private files (EduChats chats, call recordings — PRIVATE_PREFIXES) never get a
public link at all: /media/ refuses them, and the chat views stream them to the
people allowed to see them through storage.open().

Files saved before the move to Dropbox may still be sitting in
settings.LEGACY_MEDIA_ROOT; the move_media_to_dropbox command uploads them,
and until it has, any one of them is uploaded the first time it is asked for.
"""
import functools
import io
import logging
import os
import posixpath
import queue
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, Storage
from django.db import close_old_connections
from django.utils.deconstruct import deconstructible

from .dropbox_backup import _content_hash

logger = logging.getLogger(__name__)

# Chats and call recordings: only ever streamed through permission-checked views.
PRIVATE_PREFIXES = ('support_chat/', 'educhats/', 'ai_calls/')

SINGLE_UPLOAD_LIMIT = 140 * 1024 * 1024   # Dropbox's one-shot upload limit is 150 MB
UPLOAD_CHUNK = 8 * 1024 * 1024
TIMEOUT_SECONDS = 60
# Temporary links last 4 hours. Handing one out for at most 3 hours, with the
# redirect cached by browsers for 1 hour, keeps every cached redirect valid.
TEMP_LINK_TTL = 3 * 60 * 60
REDIRECT_MAX_AGE = 60 * 60
# After Dropbox refuses to make shared links (permission missing), stop asking
# for a while instead of paying a failed round trip per file.
SHARING_RETRY_SECONDS = 10 * 60
CREDENTIAL_TTL_SECONDS = 30

# Big photos are scaled down before upload: pages load faster and Dropbox's
# daily link bandwidth goes further.
OPTIMIZE_MIN_BYTES = 600 * 1024
OPTIMIZE_MAX_SIDE = 2400
OPTIMIZE_FORMATS = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.png': 'PNG', '.webp': 'WEBP'}


class DropboxFileError(Exception):
    """Dropbox could not store or return a file."""


def root():
    return getattr(settings, 'DROPBOX_MEDIA_ROOT', '/EduTrellis Site').rstrip('/')


def is_private(name):
    return (name or '').replace('\\', '/').lstrip('/').startswith(PRIVATE_PREFIXES)


def is_safe_name(name):
    """A relative storage name with no way out of the media folder."""
    if not name or '\\' in name or '\x00' in name or name.startswith('/'):
        return False
    return '..' not in Path(name).parts


def dropbox_path(name):
    return f'{root()}/{name}'


# ── Dropbox client ─────────────────────────────────────────────────────────

_credentials_cache = (0.0, None)
_local = threading.local()


def _credentials():
    """(app key, app secret, refresh token): the dashboard's Backup & Restore
    settings win, the server settings fill in whatever it left blank."""
    global _credentials_cache
    stamp, value = _credentials_cache
    if value and time.monotonic() - stamp < CREDENTIAL_TTL_SECONDS:
        return value
    saved = ('', '', '')
    try:
        from .models import DropboxSettings
        row = DropboxSettings.objects.filter(pk=1).values_list('app_key', 'app_secret', 'refresh_token').first()
        if row:
            saved = tuple((part or '').strip() for part in row)
    except Exception:
        logger.debug('Could not read the saved Dropbox credentials', exc_info=True)
    names = ('DROPBOX_APP_KEY', 'DROPBOX_APP_SECRET', 'DROPBOX_REFRESH_TOKEN')
    value = tuple(saved[i] or (getattr(settings, n, '') or '').strip() for i, n in enumerate(names))
    _credentials_cache = (time.monotonic(), value)
    return value


def client():
    """One client per thread (requests sessions aren't shared across threads),
    rebuilt when the dashboard changes the Dropbox account."""
    import dropbox
    if not getattr(settings, 'DROPBOX_STORAGE_ENABLED', True):
        raise DropboxFileError('Dropbox is switched off here (DROPBOX_STORAGE_ENABLED).')
    credentials = _credentials()
    if not all(credentials):
        raise DropboxFileError('Dropbox is not connected — add the App Key, App Secret and Refresh Token '
                               'under Backup & Restore → Settings.')
    cached = getattr(_local, 'client', None)
    if cached is None or _local.credentials != credentials:
        app_key, app_secret, refresh_token = credentials
        cached = dropbox.Dropbox(oauth2_refresh_token=refresh_token, app_key=app_key,
                                 app_secret=app_secret, timeout=TIMEOUT_SECONDS)
        _local.client, _local.credentials = cached, credentials
    return cached


def _not_found(exc):
    return 'not_found' in str(getattr(exc, 'error', exc)).lower()


def upload(data, name, overwrite=False):
    """Uploads bytes to /EduTrellis Site/<name> and checks Dropbox kept exactly
    them. Without overwrite a taken name is auto-renamed: the metadata returned
    says what it became."""
    import dropbox
    dbx = client()
    path = dropbox_path(name)
    mode = dropbox.files.WriteMode.overwrite if overwrite else dropbox.files.WriteMode.add
    if len(data) <= SINGLE_UPLOAD_LIMIT:
        meta = dbx.files_upload(data, path, mode=mode, autorename=not overwrite, mute=True)
    else:
        session = dbx.files_upload_session_start(data[:UPLOAD_CHUNK])
        offset = UPLOAD_CHUNK
        while len(data) - offset > UPLOAD_CHUNK:
            cursor = dropbox.files.UploadSessionCursor(session_id=session.session_id, offset=offset)
            dbx.files_upload_session_append_v2(data[offset:offset + UPLOAD_CHUNK], cursor)
            offset += UPLOAD_CHUNK
        cursor = dropbox.files.UploadSessionCursor(session_id=session.session_id, offset=offset)
        commit = dropbox.files.CommitInfo(path=path, mode=mode, autorename=not overwrite, mute=True)
        meta = dbx.files_upload_session_finish(data[offset:], cursor, commit)
    if getattr(meta, 'content_hash', None) and meta.content_hash != _content_hash(data):
        raise DropboxFileError(f'Dropbox stored a damaged copy of {name}.')
    return meta


def download(name):
    """The file's bytes. FileNotFoundError when neither Dropbox nor the old
    local media folder has it."""
    try:
        _, response = client().files_download(dropbox_path(name))
        try:
            return response.content
        finally:
            response.close()
    except Exception as exc:
        if not _not_found(exc):
            raise
    legacy = legacy_path(name)
    if legacy:
        return legacy.read_bytes()
    raise FileNotFoundError(name)


def legacy_path(name):
    """The not-yet-moved copy of `name` in the old local media folder, if any."""
    base = getattr(settings, 'LEGACY_MEDIA_ROOT', None)
    if not base or not is_safe_name(name):
        return None
    path = Path(base) / name
    return path if path.is_file() else None


# ── Links ──────────────────────────────────────────────────────────────────

_urls = {}            # name -> permanent link ('' while it has none yet)
_urls_loaded = False
_urls_lock = threading.Lock()
_temp_links = {}      # name -> (temporary link, hand out until)
_sharing_blocked_until = 0.0


def _raw(link):
    """A shared link that shows the file itself rather than Dropbox's preview page."""
    parts = urlsplit(link)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in ('dl', 'raw')] + [('raw', '1')]
    return urlunsplit(parts._replace(query=urlencode(query)))


def shared_link(name):
    """Creates (or finds) the permanent public link for a stored file. '' when
    the Dropbox app lacks the sharing permission, or for private files."""
    global _sharing_blocked_until
    if is_private(name) or time.monotonic() < _sharing_blocked_until:
        return ''
    import dropbox
    dbx = client()
    path = dropbox_path(name)
    try:
        link = dbx.sharing_create_shared_link_with_settings(path).url
    except dropbox.exceptions.AuthError as exc:
        _sharing_blocked_until = time.monotonic() + SHARING_RETRY_SECONDS
        logger.warning('Dropbox will not create shared links (%s); serving files through /media/ until it does. '
                       "Give the Dropbox app the 'sharing.write' and 'sharing.read' permissions and save a new "
                       'refresh token under Backup & Restore → Settings.', exc)
        return ''
    except dropbox.exceptions.ApiError as exc:
        error = getattr(exc, 'error', None)
        if not (error is not None and getattr(error, 'is_shared_link_already_exists', lambda: False)()):
            raise
        links = dbx.sharing_list_shared_links(path=path, direct_only=True).links
        if not links:
            return ''
        link = links[0].url
    return _raw(link)


def _load_urls():
    global _urls_loaded
    if _urls_loaded:
        return
    from .models import StoredFile
    with _urls_lock:
        if not _urls_loaded:
            _urls.update(StoredFile.objects.values_list('name', 'url'))
            _urls_loaded = True


def _known_url(name):
    """(is stored, permanent link) — from memory, else one database lookup."""
    _load_urls()
    if name in _urls:
        return True, _urls[name]
    from .models import StoredFile
    row = StoredFile.objects.filter(name=name).values_list('url', flat=True).first()
    if row is None:
        return False, ''
    _urls[name] = row
    return True, row


def permanent_link(name):
    """The stored permanent link for a public file, '' when it has none (yet)."""
    if is_private(name):
        return ''
    return _known_url(name)[1]


def record(name, size, link=''):
    from .models import StoredFile
    StoredFile.objects.update_or_create(name=name, defaults={'url': link, 'size': size})
    _urls[name] = link


def forget(name):
    from .models import StoredFile
    StoredFile.objects.filter(name=name).delete()
    _urls.pop(name, None)
    _temp_links.pop(name, None)


def name_for_url(value):
    """The storage name behind one of our file URLs (/media/<name>, an absolute
    edutrellis.in/media/ URL, or a stored Dropbox link), else ''."""
    if not value or value.startswith('data:'):
        return ''
    from urllib.parse import unquote
    path = unquote(urlsplit(value).path or '')
    prefix = urlsplit(settings.MEDIA_URL).path.rstrip('/') + '/'
    if path.startswith(prefix):
        name = path[len(prefix):]
        return name if is_safe_name(name) else ''
    if value.startswith('https://www.dropbox.com/'):
        from .models import StoredFile
        return StoredFile.objects.filter(url=value).values_list('name', flat=True).first() or ''
    return ''


def temporary_link(name):
    """A Dropbox download link good for at least another hour. Uploads the old
    local copy first when the file hasn't been moved to Dropbox yet."""
    now = time.monotonic()
    cached = _temp_links.get(name)
    if cached and cached[1] > now:
        return cached[0]
    try:
        link = client().files_get_temporary_link(dropbox_path(name)).link
    except Exception as exc:
        if not _not_found(exc):
            raise
        legacy = legacy_path(name)
        if not legacy:
            raise FileNotFoundError(name) from exc
        store_existing(name, legacy.read_bytes())
        link = client().files_get_temporary_link(dropbox_path(name)).link
    if len(_temp_links) > 5000:
        _temp_links.clear()
    _temp_links[name] = (link, now + TEMP_LINK_TTL)
    return link


def store_existing(name, data):
    """Puts a file that already has this exact name (the database points at it)
    into Dropbox. Returns its permanent link, '' when there isn't one yet."""
    meta = upload(data, name, overwrite=True)
    try:
        link = shared_link(name)
    except Exception:
        logger.warning('Could not create a Dropbox shared link for %s', name, exc_info=True)
        link = ''
    record(name, meta.size, link)
    return link


# ── Background work ────────────────────────────────────────────────────────

_jobs = None
_worker = None
_worker_lock = threading.Lock()
_queued_links = set()


def _work():
    while True:
        fn, args = _jobs.get()
        close_old_connections()
        try:
            fn(*args)
        except Exception:
            logger.warning('Dropbox background job %s failed', getattr(fn, '__name__', fn), exc_info=True)
        finally:
            close_old_connections()
            _jobs.task_done()


def run_later(fn, *args):
    """Runs fn(*args) on one background thread, so a request never waits on
    Dropbox for work whose result it doesn't need. Never raises."""
    global _jobs, _worker
    if not getattr(settings, 'DROPBOX_STORAGE_ENABLED', True):
        return False
    try:
        with _worker_lock:
            if _jobs is None:
                _jobs = queue.Queue(maxsize=500)
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_work, name='dropbox-storage', daemon=True)
                _worker.start()
        _jobs.put_nowait((fn, args))
        return True
    except queue.Full:
        logger.warning('Dropbox background queue is full; skipped %s', getattr(fn, '__name__', fn))
        return False


def _fill_link(name):
    try:
        link = shared_link(name)
        if link:
            from .models import StoredFile
            StoredFile.objects.filter(name=name).update(url=link)
            _urls[name] = link
    finally:
        _queued_links.discard(name)


# ── Image optimization ─────────────────────────────────────────────────────

def optimize_image(name, data):
    """Scales a very large photo down to OPTIMIZE_MAX_SIDE in the same format.
    Anything that isn't clearly worth it (or can't be read) is left untouched."""
    fmt = OPTIMIZE_FORMATS.get(os.path.splitext(name)[1].lower())
    if not fmt or len(data) < OPTIMIZE_MIN_BYTES:
        return data
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as img:
            if max(img.size) <= OPTIMIZE_MAX_SIDE or getattr(img, 'is_animated', False):
                return data
            img = ImageOps.exif_transpose(img)
            img.thumbnail((OPTIMIZE_MAX_SIDE, OPTIMIZE_MAX_SIDE), Image.Resampling.LANCZOS)
            if fmt == 'JPEG' and img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            out = io.BytesIO()
            options = {'optimize': True}
            if fmt in ('JPEG', 'WEBP'):
                options['quality'] = 85
            img.save(out, format=fmt, **options)
        smaller = out.getvalue()
        return smaller if len(smaller) < len(data) else data
    except Exception:
        logger.info('Left %s as uploaded: could not optimize it', name, exc_info=True)
        return data


# ── The storage ────────────────────────────────────────────────────────────

@deconstructible
class DropboxStorage(Storage):

    def _open(self, name, mode='rb'):
        if any(flag in mode for flag in 'wa+'):
            raise ValueError('Dropbox files are read-only once saved; save a new file instead.')
        return ContentFile(download(name), name=name)

    def _save(self, name, content):
        name = name.replace('\\', '/')
        if hasattr(content, 'seek'):
            content.seek(0)
        data = content.read()
        if isinstance(data, str):
            data = data.encode('utf-8')
        if not is_private(name):
            data = optimize_image(name, data)
        meta = upload(data, name)
        # An auto-rename only ever changes the file name, never its folder.
        stored = posixpath.join(posixpath.dirname(name), meta.name)
        try:
            link = shared_link(stored)
        except Exception:
            logger.warning('Could not create a Dropbox shared link for %s', stored, exc_info=True)
            link = ''
        record(stored, meta.size, link)
        return stored

    def exists(self, name):
        known, _ = _known_url(name)
        return known or legacy_path(name) is not None

    def delete(self, name):
        if not name:
            return
        try:
            client().files_delete_v2(dropbox_path(name))
        except Exception as exc:
            if not _not_found(exc):
                raise
        forget(name)
        legacy = legacy_path(name)
        if legacy:
            legacy.unlink(missing_ok=True)

    def size(self, name):
        from .models import StoredFile
        size = StoredFile.objects.filter(name=name).values_list('size', flat=True).first()
        if size is None:
            legacy = legacy_path(name)
            if legacy:
                return legacy.stat().st_size
            raise FileNotFoundError(name)
        return size

    def url(self, name):
        name = (name or '').replace('\\', '/')
        fallback = settings.MEDIA_URL + quote(name)
        if is_private(name):
            return fallback
        known, link = _known_url(name)
        if link:
            return link
        if known and name not in _queued_links and time.monotonic() >= _sharing_blocked_until:
            # Make the permanent link in the background; this page uses /media/.
            _queued_links.add(name)
            if not run_later(_fill_link, name):
                _queued_links.discard(name)
        return fallback


@functools.lru_cache(maxsize=None)
def _private_files():
    return FileSystemStorage(location=settings.PRIVATE_FILES_ROOT, base_url=settings.MEDIA_URL)


def private_storage():
    """Disk storage for files that are never public and are written in pieces
    (call recordings). Kept on the server's volume, outside anything served."""
    return _private_files()
