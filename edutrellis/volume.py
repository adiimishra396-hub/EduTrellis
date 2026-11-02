"""Railway volume: keeps the database across redeploys.

On Railway a persistent disk is attached to the web service and its folder is in
RAILWAY_VOLUME_MOUNT_PATH (EduTrellis: /media). settings.py then keeps

    <volume>/data/db.sqlite3   the database (never served to the web)
    <volume>/private/          call recordings and temporary downloads
    <volume>/uploads/          old uploads, until move_media_to_dropbox moves them

Uploaded files themselves live in Dropbox (myapp.dropbox_storage).

The first time the app starts on an empty volume, prepare() copies the database
that came with the code onto it. It never overwrites anything already on the
volume: from then on the volume is the live copy.
"""
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)


def _copy_database(seed, target):
    """A consistent copy through SQLite's backup API (safe even with a -wal file next to it)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + '.copying')
    src, dst = sqlite3.connect(str(seed)), sqlite3.connect(str(tmp))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    tmp.replace(target)


def prepare(base_dir, db_file):
    base_dir, db_file = Path(base_dir), Path(db_file)
    seed = base_dir / 'db.sqlite3'
    if not db_file.exists() and seed.exists():
        _copy_database(seed, db_file)   # a failure here stops the start: better than an empty site
        log.warning('Railway volume: database copied to %s', db_file)
