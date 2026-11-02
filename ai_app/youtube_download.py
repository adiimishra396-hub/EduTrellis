"""Bounded background YouTube downloads for content users may lawfully save."""
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.db import close_old_connections

MAX_DURATION_SECONDS = 2 * 60 * 60
MAX_FILE_BYTES = 500 * 1024 * 1024
ALLOWED_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be', 'music.youtube.com'}
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def is_youtube_url(url):
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme in ('http', 'https') and parsed.hostname and parsed.hostname.lower() in ALLOWED_HOSTS
    except Exception:
        return False


def _run(job_id):
    from .models import YouTubeDownloadJob, AISiteSettings
    close_old_connections()
    job = YouTubeDownloadJob.objects.get(pk=job_id)
    job.status = YouTubeDownloadJob.STATUS_WORKING
    job.progress = 2
    job.save(update_fields=['status', 'progress'])
    output_dir = Path(settings.PRIVATE_FILES_ROOT) / 'ai_youtube'
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = str(job.token)
    cookie_path = None

    try:
        import imageio_ffmpeg
        import yt_dlp
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

        # Server/datacenter IPs (which is what every host, including this
        # one, downloads from) get YouTube's "Sign in to confirm you're not
        # a bot" check far more often than a home connection ever would —
        # that's why this can work fine when run locally and still fail in
        # production. A cookies.txt from a real logged-in browser session
        # (dashboard EduTrellis AI -> AI Settings -> YouTube downloader) is the standard fix.
        cookies_txt = AISiteSettings.get_solo().youtube_cookies_txt.strip()
        if cookies_txt:
            # A dedicated temp file, not output_dir — that directory's
            # "{stem}.*" glob below is how the finished video file gets
            # found after download, and a cookies file living there would
            # match that same glob and could get picked up as the result.
            cookie_fd = tempfile.NamedTemporaryFile(
                mode='w', suffix='.txt', prefix=f'ytcookies-{stem}-',
                delete=False, encoding='utf-8',
            )
            cookie_fd.write(cookies_txt)
            cookie_fd.close()
            cookie_path = Path(cookie_fd.name)

        def match_filter(info, *, incomplete=False):
            if info.get('_type') == 'playlist':
                return 'Playlists are not supported.'
            duration = info.get('duration')
            if duration and duration > MAX_DURATION_SECONDS:
                return 'Videos longer than two hours are not supported.'
            return None

        def progress_hook(data):
            if data.get('status') != 'downloading':
                return
            total = data.get('total_bytes') or data.get('total_bytes_estimate') or 0
            downloaded = data.get('downloaded_bytes') or 0
            if downloaded > MAX_FILE_BYTES:
                raise RuntimeError('The video exceeds the 500MB limit.')
            percent = min(82, int(downloaded * 80 / total) + 2) if total else 10
            YouTubeDownloadJob.objects.filter(pk=job_id).update(progress=percent)

        audio_only = job.quality == 'audio'
        height = 720 if job.quality == '720' else 1080
        options = {
            'format': 'bestaudio/best' if audio_only else f'bestvideo[height<={height}]+bestaudio/best[height<={height}]',
            'outtmpl': str(output_dir / f'{stem}.%(ext)s'),
            # imageio-ffmpeg ships a versioned/nonstandard executable name.
            # Passing its directory makes yt-dlp search for a separate
            # `ffmpeg.exe`; pass the exact executable path instead.
            'ffmpeg_location': ffmpeg_exe,
            'noplaylist': True,
            'max_filesize': MAX_FILE_BYTES,
            'socket_timeout': 20,
            'retries': 2,
            'match_filter': match_filter,
            'progress_hooks': [progress_hook],
            'quiet': True,
            'no_warnings': True,
            # The android client's player response usually isn't gated behind
            # the "confirm you're not a bot" check the way the web client's
            # is, so trying it first is free extra resilience even with
            # cookies configured; 'web' stays as a fallback for anything the
            # android client can't resolve.
            'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
        }
        if cookie_path:
            options['cookiefile'] = str(cookie_path)
        if audio_only:
            options['postprocessors'] = [{
                'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '320',
            }]
        else:
            options['merge_output_format'] = 'mp4'
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(job.source_url, download=True)

        if audio_only:
            audio_path = output_dir / f'{stem}.mp3'
            video_path = None
            if not audio_path.exists():
                raise RuntimeError('The MP3 file was not produced.')
        else:
            candidates = [p for p in output_dir.glob(f'{stem}.*') if p.suffix.lower() != '.part']
            if not candidates:
                raise RuntimeError('The downloaded video file was not produced.')
            video_path = max(candidates, key=lambda p: p.stat().st_mtime)
            audio_path = None
            if video_path.stat().st_size > MAX_FILE_BYTES:
                raise RuntimeError('The video exceeds the 500MB limit.')
        YouTubeDownloadJob.objects.filter(pk=job_id).update(
            status=YouTubeDownloadJob.STATUS_READY, progress=100,
            title=(info.get('title') or 'YouTube download')[:300],
            video_path=str(video_path.relative_to(settings.PRIVATE_FILES_ROOT)) if video_path else '',
            audio_path=str(audio_path.relative_to(settings.PRIVATE_FILES_ROOT)) if audio_path else '',
        )
    except Exception as exc:
        for path in output_dir.glob(f'{stem}.*'):
            try:
                path.unlink()
            except OSError:
                pass
        clean_error = _ANSI_RE.sub('', str(exc)).strip()
        YouTubeDownloadJob.objects.filter(pk=job_id).update(
            status=YouTubeDownloadJob.STATUS_FAILED, error=clean_error[:500],
        )
    finally:
        if cookie_path:
            try:
                cookie_path.unlink(missing_ok=True)
            except OSError:
                pass
        close_old_connections()


def start(job_id):
    threading.Thread(target=_run, args=(job_id,), daemon=True, name=f'youtube-download-{job_id}').start()
