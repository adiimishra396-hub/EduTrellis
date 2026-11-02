"""Moves everything still in the old local media folder into Dropbox
('/EduTrellis Site/<same name>') and deletes the local copies.

    python manage.py move_media_to_dropbox            # do it
    python manage.py move_media_to_dropbox --dry-run  # only list what it would do

Safe to run again and again (the Procfile runs it on every start): files
already moved are skipped, and a local file is only deleted after Dropbox
confirms it holds exactly the same bytes. It also:

* moves call recordings (ai_calls/) to the private folder instead — they are
  never put anywhere public;
* deletes leftover temporary YouTube downloads (ai_youtube/);
* moves pictures attached to AI chat messages out of the database;
* points saved /media/ links at the permanent Dropbox links, once Dropbox
  allows shared links.
"""
import base64
import binascii
import re
import secrets
import shutil
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.utils import timezone

from myapp import dropbox_storage

TEMPORARY = {'ai_youtube'}
PRIVATE_DISK = {'ai_calls'}
_DATA_URI_RE = re.compile(r'^data:image/(png|jpe?g|webp|gif);base64,', re.I)


class Command(BaseCommand):
    help = 'Move the old local media folder into Dropbox and delete the local copies.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Only list what would happen.')

    def handle(self, *args, dry_run=False, **options):
        self.dry_run = dry_run
        self.verbosity = options.get('verbosity', 1)
        self.moved = self.failed = 0
        self.move_files()
        self.move_chat_attachments()
        self.rewrite_links()
        verb = 'Would move' if dry_run else 'Moved'
        self.stdout.write(f'{verb} {self.moved} item(s) to Dropbox; {self.failed} failed.')

    # ── Files in the old media folder ──────────────────────────────────────

    def move_files(self):
        base = Path(settings.LEGACY_MEDIA_ROOT)
        if not base.is_dir():
            return
        for path in sorted(p for p in base.rglob('*') if p.is_file()):
            name = path.relative_to(base).as_posix()
            top = name.split('/', 1)[0]
            if top in TEMPORARY:
                self.say(f'delete temporary {name}')
                if not self.dry_run:
                    path.unlink(missing_ok=True)
            elif top in PRIVATE_DISK:
                target = Path(settings.PRIVATE_FILES_ROOT) / name
                self.say(f'private disk {name}')
                if not self.dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        path.unlink()
                    else:
                        shutil.move(str(path), str(target))
            else:
                self.say(f'upload {name}')
                if self.dry_run:
                    self.moved += 1
                    continue
                try:
                    dropbox_storage.store_existing(name, path.read_bytes())
                except Exception as exc:
                    self.failed += 1
                    self.stderr.write(f'  could not move {name}: {exc}')
                    continue
                path.unlink()
                self.moved += 1
        if not self.dry_run:
            self.remove_empty_folders(base)

    def remove_empty_folders(self, base):
        for folder in sorted((p for p in base.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                folder.rmdir()
            except OSError:
                pass   # not empty: something failed to move and is still there
        try:
            base.rmdir()
            self.say(f'removed the empty folder {base}')
        except OSError:
            pass

    # ── Pictures stored inside the database ────────────────────────────────

    def move_chat_attachments(self):
        from ai_app.models import AIMessage
        rows = AIMessage.objects.filter(image_data__startswith='data:image/').values_list('pk', flat=True)
        for pk in list(rows):
            value = AIMessage.objects.filter(pk=pk).values_list('image_data', flat=True).first() or ''
            match = _DATA_URI_RE.match(value[:40])
            if not match:
                continue
            self.say(f'chat attachment on message {pk}')
            if self.dry_run:
                self.moved += 1
                continue
            try:
                raw = base64.b64decode(value[match.end():])
                extension = match.group(1).lower().replace('jpeg', 'jpg')
                name = default_storage.save(
                    f'ai_uploads/{timezone.now():%Y/%m/%d}/{secrets.token_hex(16)}.{extension}', ContentFile(raw),
                )
            except (binascii.Error, ValueError) as exc:
                self.stderr.write(f'  message {pk} has an unreadable picture, left as it is: {exc}')
                continue
            except Exception as exc:
                self.failed += 1
                self.stderr.write(f'  could not move the picture on message {pk}: {exc}')
                continue
            AIMessage.objects.filter(pk=pk).update(image_data=default_storage.url(name))
            self.moved += 1

    # ── Saved links ────────────────────────────────────────────────────────

    def rewrite_links(self):
        """Text fields that hold a file's URL rather than a FileField: point
        them at the permanent Dropbox link when the file has one."""
        from ai_app.models import AIMessage, AIUserImage, AIVideoJob, EduChatsAIMessage
        fields = [
            (AIMessage, 'image_data'), (AIMessage, 'video_url'), (AIUserImage, 'url'),
            (AIVideoJob, 'video_url'), (EduChatsAIMessage, 'image_url'),
        ]
        prefixes = (settings.MEDIA_URL, 'https://www.edutrellis.in' + settings.MEDIA_URL)
        for model, field in fields:
            for prefix in prefixes:
                rows = model.objects.filter(**{f'{field}__startswith': prefix}).values_list('pk', field)
                for pk, value in rows.iterator():
                    name = dropbox_storage.name_for_url(value)
                    if not name:
                        continue
                    link = default_storage.url(name)
                    if link != value and not link.startswith(settings.MEDIA_URL):
                        if not self.dry_run:
                            model.objects.filter(pk=pk).update(**{field: link})
                        self.say(f'link {model.__name__}.{field} #{pk}')

    def say(self, text):
        if self.verbosity >= 1:
            self.stdout.write(('[dry run] ' if self.dry_run else '') + text)
