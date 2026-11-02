"""Backs up/restores db.sqlite3 to/from a Dropbox app folder, using the
store owner's Dropbox App Key/Secret + a long-lived OAuth2 refresh token
(configured from the store dashboard). Degrades gracefully if the
`dropbox` package isn't installed or credentials aren't set yet.
"""
import datetime
import functools
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import zipfile

from django.conf import settings as dj_settings

try:
    import dropbox
except ImportError:  # pragma: no cover - optional dependency until configured
    dropbox = None

BACKUP_ROOT = '/EduTrellis Store'
BACKUP_FOLDER = f'{BACKUP_ROOT}/backups'
LATEST_NAME = 'db_latest.sqlite3'
ASSETS_LATEST_NAME = 'customize_latest.zip'


class BackupError(Exception):
    """Raised for any Dropbox/backup failure with a message safe to show the admin."""


def db_path():
    return Path(dj_settings.DATABASES['default']['NAME'])


def _client(settings_obj):
    if dropbox is None:
        raise BackupError("The 'dropbox' Python package isn't installed on this server.")
    if not settings_obj.is_configured:
        raise BackupError('Dropbox is not configured yet — add your App Key, App Secret and Refresh Token first.')
    return dropbox.Dropbox(
        oauth2_refresh_token=settings_obj.effective_refresh_token,
        app_key=settings_obj.effective_app_key,
        app_secret=settings_obj.effective_app_secret,
    )


def _ensure_folder(dbx, path):
    try:
        dbx.files_create_folder_v2(path)
    except Exception as exc:
        if 'conflict' not in str(exc).lower():  # folder already exists — fine
            raise BackupError(f"Could not create the Dropbox folder '{path}': {exc}")


def _asset_backup_name(database_filename):
    if database_filename == LATEST_NAME:
        return ASSETS_LATEST_NAME
    if database_filename.startswith('db_') and database_filename.endswith('.sqlite3'):
        stamp = database_filename[len('db_'):-len('.sqlite3')]
        return f'customize_{stamp}.zip'
    return None


def _download_optional(dbx, path):
    try:
        _, response = dbx.files_download(path)
        return response.content
    except Exception as exc:
        if 'not_found' in str(exc).lower():
            return None
        raise


def _restore_customization_archive(content):
    """Older backups carry a zip of the customization/PWA images. Images now
    live in Dropbox, so any of them that are missing there are put back."""
    if content is None:
        return False
    from django.core.files.base import ContentFile
    from . import dropbox_storage
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for item in archive.infolist():
                if item.is_dir():
                    continue
                if not dropbox_storage.is_safe_name(item.filename):
                    raise BackupError('Customization backup contains an unsafe file path.')
                if not dropbox_storage.DropboxStorage().exists(item.filename):
                    dropbox_storage.store_existing(item.filename, archive.read(item))
        return True
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f'Could not restore customization images: {exc}')


class BackupFile:
    """One timestamped database backup as the dashboard shows it."""

    def __init__(self, meta):
        self.name = meta.name
        self.size = meta.size or 0
        self.safety = meta.name.endswith(SAFETY_SUFFIX + '.sqlite3')
        modified = meta.client_modified
        if modified is not None:
            from django.utils import timezone
            if timezone.is_naive(modified):   # Dropbox reports UTC
                modified = modified.replace(tzinfo=datetime.timezone.utc)
            modified = timezone.localtime(modified)
        self.modified = modified


SAFETY_SUFFIX = '_before_restore'
UPLOAD_CHUNK = 8 * 1024 * 1024
SINGLE_UPLOAD_LIMIT = 140 * 1024 * 1024   # Dropbox's one-shot upload limit is 150 MB

# Backups, restores and deletes never run at the same time (a double click, two
# admins): one would read or replace the database while the other is using it.
_busy = threading.Lock()


def _one_at_a_time(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _busy.acquire(blocking=False):
            raise BackupError('Another backup, restore or delete is already running. Wait for it to finish and try again.')
        try:
            return fn(*args, **kwargs)
        finally:
            _busy.release()
    return wrapper


def _content_hash(data):
    """Dropbox's content_hash: SHA-256 of the SHA-256s of each 4 MB block."""
    blocks = b''.join(hashlib.sha256(data[i:i + 4194304]).digest() for i in range(0, len(data), 4194304))
    return hashlib.sha256(blocks).hexdigest()


def _upload(dbx, data, path, overwrite=False):
    """Uploads and checks that Dropbox stored exactly these bytes."""
    mode = dropbox.files.WriteMode.overwrite if overwrite else dropbox.files.WriteMode.add
    if len(data) <= SINGLE_UPLOAD_LIMIT:
        meta = dbx.files_upload(data, path, mode=mode)
    else:
        session = dbx.files_upload_session_start(data[:UPLOAD_CHUNK])
        offset = UPLOAD_CHUNK
        while len(data) - offset > UPLOAD_CHUNK:
            cursor = dropbox.files.UploadSessionCursor(session_id=session.session_id, offset=offset)
            dbx.files_upload_session_append_v2(data[offset:offset + UPLOAD_CHUNK], cursor)
            offset += UPLOAD_CHUNK
        cursor = dropbox.files.UploadSessionCursor(session_id=session.session_id, offset=offset)
        meta = dbx.files_upload_session_finish(data[offset:], cursor, dropbox.files.CommitInfo(path=path, mode=mode))
    expected = _content_hash(data)
    if getattr(meta, 'content_hash', expected) != expected:
        raise BackupError(f'Dropbox stored a damaged copy of {path.rsplit("/", 1)[-1]}. Please try again.')
    return meta


def _check_database_file(path):
    """Raises BackupError unless `path` is a healthy SQLite database of this site."""
    with open(path, 'rb') as fh:
        if fh.read(16) != b'SQLite format 3\x00':
            raise BackupError('That file is not an SQLite database.')
    try:
        conn = sqlite3.connect(f'file:{Path(path).as_posix()}?mode=ro', uri=True)
        try:
            result = conn.execute('PRAGMA integrity_check').fetchall()
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f'That database file is damaged: {exc}')
    if result != [('ok',)]:
        raise BackupError('That database file is damaged.')
    if not {'django_migrations', 'auth_user', 'django_session'} <= tables:
        raise BackupError("That file isn't an EduTrellis database.")


def _temp_path(suffix):
    folder = db_path().parent
    fd, name = tempfile.mkstemp(prefix='.backup-', suffix=suffix, dir=folder)
    os.close(fd)
    return Path(name)


def _snapshot_database():
    """A consistent, compacted copy of the live database. SQLite copies it
    inside one read transaction, so a write landing halfway through can't
    leave a half-old, half-new (corrupt) file the way copying the bytes can;
    VACUUM INTO also leaves out free pages, which keeps uploads small."""
    path = db_path()
    if not path.exists():
        raise BackupError('Local database file was not found.')
    snapshot = _temp_path('.sqlite3')
    try:
        src = sqlite3.connect(str(path), timeout=30)
        try:
            snapshot.unlink()
            src.execute('VACUUM INTO ?', (str(snapshot),))
        except sqlite3.OperationalError:   # SQLite older than 3.27
            snapshot.touch()
            dst = sqlite3.connect(str(snapshot))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        _check_database_file(snapshot)
        return snapshot.read_bytes()
    except sqlite3.Error as exc:
        raise BackupError(f'Could not read the live database: {exc}')
    finally:
        snapshot.unlink(missing_ok=True)


def _upload_backup(dbx, suffix='', refresh_latest=True):
    _ensure_folder(dbx, BACKUP_ROOT)
    _ensure_folder(dbx, BACKUP_FOLDER)
    data = _snapshot_database()
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S') + suffix
    filename = f'db_{stamp}.sqlite3'
    # Uploaded images already live in Dropbox ('/EduTrellis Site', see
    # dropbox_storage), so a backup is just the database.
    _upload(dbx, data, f'{BACKUP_FOLDER}/{filename}')
    if refresh_latest:
        _upload(dbx, data, f'{BACKUP_FOLDER}/{LATEST_NAME}', overwrite=True)
    return filename


@_one_at_a_time
def create_backup(settings_obj):
    """Uploads a database backup."""
    try:
        return _upload_backup(_client(settings_obj))
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f'Backup to Dropbox failed: {exc}')


def _list_folder(dbx):
    try:
        res = dbx.files_list_folder(BACKUP_FOLDER)
    except Exception as exc:
        if 'not_found' in str(exc).lower():
            return []
        raise
    entries = list(res.entries)
    while res.has_more:
        res = dbx.files_list_folder_continue(res.cursor)
        entries.extend(res.entries)
    return [e for e in entries if isinstance(e, dropbox.files.FileMetadata)]


def list_backups(settings_obj):
    """Returns timestamped backups (newest first), excluding db_latest.sqlite3."""
    try:
        files = [
            BackupFile(e) for e in _list_folder(_client(settings_obj))
            if e.name.startswith('db_') and e.name.endswith('.sqlite3') and e.name != LATEST_NAME
        ]
        files.sort(key=lambda f: f.name, reverse=True)
        return files
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f'Could not list Dropbox backups: {exc}')


def _is_backup_file(name):
    return ((name.startswith('db_') and name.endswith('.sqlite3'))
            or (name.startswith('customize_') and name.endswith('.zip')))


@_one_at_a_time
def delete_all_backups(settings_obj):
    """Deletes every backup in the backups folder: the timestamped database and
    image copies and the db_latest / customize_latest ones. Returns (database
    backups removed, files removed). Nothing else in the Dropbox app folder is touched."""
    try:
        dbx = _client(settings_obj)
        files = [e for e in _list_folder(dbx) if _is_backup_file(e.name)]
        if not files:
            return 0, 0
        databases = sum(1 for e in files if e.name.endswith('.sqlite3') and e.name != LATEST_NAME)
        for start in range(0, len(files), 1000):   # Dropbox takes up to 1000 per batch
            batch = [dropbox.files.DeleteArg(e.path_lower) for e in files[start:start + 1000]]
            job = dbx.files_delete_batch(batch)
            if job.is_async_job_id():
                job_id = job.get_async_job_id()
                for _ in range(60):
                    job = dbx.files_delete_batch_check(job_id)
                    if not job.is_in_progress():
                        break
                    time.sleep(1)
                else:
                    raise BackupError('Dropbox is still deleting the backups. Refresh this page in a minute.')
            if not job.is_complete():
                raise BackupError('Dropbox could not delete the backups. Please try again.')
            failed = [r for r in job.get_complete().entries if r.is_failure()]
            if failed:
                raise BackupError(f'Dropbox could not delete {len(failed)} of the backup files. Please try again.')
        return databases, len(files)
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f'Could not delete the Dropbox backups: {exc}')


def _copy_database(source, target):
    """Copies one SQLite database over another through SQLite itself, so the
    live file is replaced under SQLite's own locks: requests running at the
    same moment wait instead of reading a half-written file."""
    src = sqlite3.connect(str(source), timeout=30)
    dst = sqlite3.connect(str(target), timeout=30)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _migrate():
    """Brings an older backup up to the current tables (new features added
    since it was taken), exactly like a deploy does."""
    from django.core.management import call_command
    from django.db import connections
    connections.close_all()
    call_command('migrate', interactive=False, verbosity=0)
    connections.close_all()


@_one_at_a_time
def restore_backup(settings_obj, filename):
    """Restores the database (and, for older backups, any customization images
    in its paired archive that are missing from Dropbox).

    Safe order: download → check the file is a healthy EduTrellis database →
    save the current data to Dropbox as db_<now>_before_restore → swap the
    database in → update its tables. If any step fails the live data is left
    as it was (or put back). Returns the name of the safety copy."""
    if (not filename or '/' in filename or '\\' in filename
            or not filename.startswith('db_') or not filename.endswith('.sqlite3')):
        raise BackupError('Invalid backup filename.')

    try:
        dbx = _client(settings_obj)
        meta, resp = dbx.files_download(f'{BACKUP_FOLDER}/{filename}')
        content = resp.content
        asset_filename = _asset_backup_name(filename)
        asset_content = _download_optional(dbx, f'{BACKUP_FOLDER}/{asset_filename}') if asset_filename else None
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f'Could not download that backup from Dropbox: {exc}')
    if getattr(meta, 'content_hash', None) and meta.content_hash != _content_hash(content):
        raise BackupError('The backup came down from Dropbox incomplete. Nothing was changed — please try again.')

    incoming = _temp_path('.sqlite3')
    current = _temp_path('.sqlite3')
    try:
        incoming.write_bytes(content)
        try:
            _check_database_file(incoming)
        except BackupError as exc:
            raise BackupError(f'{filename} can\'t be restored: {exc} Nothing was changed.')
        if asset_content is not None:
            try:
                zipfile.ZipFile(io.BytesIO(asset_content)).testzip()
            except zipfile.BadZipFile:
                asset_content = None   # restore the data anyway; keep the current images

        # Safety copy of what's live now, kept locally for rollback and on
        # Dropbox so this restore can be undone from the Backup History list.
        from django.db import connections
        connections.close_all()
        _copy_database(db_path(), current)
        try:
            safety = _upload_backup(dbx, suffix=SAFETY_SUFFIX, refresh_latest=False)
        except Exception as exc:
            raise BackupError(f'Restore cancelled — could not save a safety copy of the current data first ({exc}). Nothing was changed.')

        try:
            _copy_database(incoming, db_path())
            _migrate()
        except Exception as exc:
            try:
                _copy_database(current, db_path())
                connections.close_all()
            except Exception as undo_exc:
                raise BackupError(f'Restore failed ({exc}) and the previous data could not be put back automatically '
                                  f'({undo_exc}). Restore {safety} to get it back.')
            raise BackupError(f'Restore failed and was undone — your data is as it was. ({exc})')

        _restore_customization_archive(asset_content)
        return safety
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f'Restore failed: {exc}')
    finally:
        incoming.unlink(missing_ok=True)
        current.unlink(missing_ok=True)
