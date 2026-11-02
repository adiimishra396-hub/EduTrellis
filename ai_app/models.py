from datetime import timedelta
import hashlib
import secrets
import uuid

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.db.models import F
from django.utils import timezone

from myapp.dropbox_storage import private_storage




class ActiveUserSession(models.Model):
    """The one browser session currently allowed to use a user account."""

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='active_ai_session',
    )
    session_key = models.CharField(max_length=40, unique=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Active AI User Session'
        verbose_name_plural = 'Active AI User Sessions'

    def __str__(self):
        return f'{self.user.username} — {self.session_key}'













class AIConversation(models.Model):
    """One saved chat thread on /ai/, ChatGPT-style. A guest (not logged in)
    can chat for a few messages before being asked to log in — their
    conversation is kept here tied to session_key (user is null) and gets
    handed over to their account (user set, session_key cleared) the moment
    they log in or sign up, the same way an anonymous cart is merged in."""
    user        = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ai_conversations', null=True, blank=True)
    session_key = models.CharField(max_length=40, blank=True, db_index=True)
    title       = models.CharField(max_length=80, blank=True)
    # Captured once at creation from the same IP-detection the chat rate
    # limiter already uses — lets staff (see dashboard AI Activity) spot a
    # repeat spammer across guest sessions/accounts sharing one connection,
    # not just within one rate-limit window. Null when the request's IP
    # couldn't be determined at all (never set to the literal 'unknown').
    ip_address  = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    # A running summary of the older messages, sent to the model in their
    # place so a long chat stays short (see ai_app.context_compress). It covers
    # every live message up to and including the one with this pk.
    context_summary = models.TextField(blank=True)
    context_summary_until = models.BigIntegerField(default=0)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'AI Conversation'
        verbose_name_plural = 'AI Conversations'

    def __str__(self):
        return self.title or f"Conversation #{self.pk}"


class AIBlock(models.Model):
    """Staff-issued block against a repeat spammer on /ai/, checked on every
    ai_chat_send call before anything else runs. Blocks by IP (works
    against a guest, and stops a logged-out spammer from just signing up
    again from the same connection) and/or by account (still works if their
    IP changes) — either or both can be set; see dashboard AI Activity for
    where these get created."""
    ip_address = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    user       = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='ai_blocks')
    reason     = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'AI Block'
        verbose_name_plural = 'AI Blocks'
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ip_address__isnull=False) | models.Q(user__isnull=False),
                name='aiblock_ip_or_user_required',
            ),
        ]

    def __str__(self):
        if self.user_id:
            return f'Blocked account: {self.user.email or self.user.username}'
        return f'Blocked IP: {self.ip_address}'


class GitHubConnection(models.Model):
    """A user's personal-access-token connection to GitHub, used by
    /ai/'s GitHub mode to read files from and push commits to a chosen repo
    on their instruction. One per user, isolated by the user relation, and
    the token itself is never sent back to the browser once saved."""
    user            = models.OneToOneField(User, on_delete=models.CASCADE, related_name='github_connection')
    access_token    = models.CharField(max_length=255)
    github_username = models.CharField(max_length=120, blank=True)
    repo_full_name  = models.CharField(max_length=200, blank=True, help_text="owner/repo this connection reads/writes, e.g. 'boosternotes/EduTrellis'.")
    default_branch  = models.CharField(max_length=100, blank=True, default='main')
    connected_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'GitHub Connection'
        verbose_name_plural = 'GitHub Connections'

    def __str__(self):
        return f"{self.github_username or self.user.username} → {self.repo_full_name or '(no repo set)'}"


class KnowledgeEntry(models.Model):
    """Legacy data from the removed EduTrellis Light model and its
    saved-answer retrieval system (light_mode.py, deleted). No AI reply path
    reads from or writes to this table any more — every response is
    generated fresh, never retrieved from a saved/cached answer. Kept only
    as an inert historical record, browsable from the admin; safe to prune
    or drop entirely if it's no longer wanted."""
    SOURCE_MANUAL = 'manual'
    SOURCE_WEB = 'web_search'
    SOURCE_CHAT = 'chat'
    SOURCE_CHOICES = [(SOURCE_MANUAL, 'Manual'), (SOURCE_WEB, 'Web search'), (SOURCE_CHAT, 'Chat')]

    topic       = models.CharField(max_length=200, help_text="Short label/question this answers, e.g. 'refund policy' or 'GST registration steps'.")
    content     = models.TextField(help_text='Saved text from the retired EduTrellis Light feature — no longer used by any AI reply.')
    source      = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    source_url  = models.URLField(blank=True, help_text='Where this was found, if saved from a web search.')
    user        = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='knowledge_entries', help_text='Set only for an entry private to one logged-in person (from a file/image upload, or their own account details). Blank = visible to everyone.')
    session_key = models.CharField(max_length=40, blank=True, db_index=True, help_text='Same idea as user, for a private entry saved from a guest (not logged in) session.')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'Knowledge Entry'
        verbose_name_plural = 'Knowledge Entries (legacy — unused)'

    def __str__(self):
        return self.topic


class YouTubeDownloadJob(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_WORKING = 'working'
    STATUS_READY = 'ready'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'), (STATUS_WORKING, 'Working'),
        (STATUS_READY, 'Ready'), (STATUS_FAILED, 'Failed'),
    ]

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='youtube_downloads')
    source_url = models.URLField(max_length=500)
    quality = models.CharField(max_length=10, default='1080')
    title = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING)
    progress = models.PositiveSmallIntegerField(default=0)
    video_path = models.CharField(max_length=500, blank=True)
    audio_path = models.CharField(max_length=500, blank=True)
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ['-created_at']


class AIVideoJob(models.Model):
    """One video being made for a chat turn. Video models take minutes, so
    the work runs in the background (ai_app.video_generation) and the chat
    page polls this row; the finished video lands on ``message``."""
    STATUS_WORKING = 'working'
    STATUS_READY = 'ready'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [(STATUS_WORKING, 'Working'), (STATUS_READY, 'Ready'), (STATUS_FAILED, 'Failed')]

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='ai_video_jobs')
    session_key = models.CharField(max_length=40, blank=True, db_index=True)
    message = models.ForeignKey('AIMessage', on_delete=models.CASCADE, related_name='video_jobs')
    model_key = models.CharField(max_length=20)
    prompt = models.TextField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_WORKING)
    video_url = models.CharField(max_length=500, blank=True)
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'AI Video Job'
        verbose_name_plural = 'AI Video Jobs'


class AIGeneratedFile(models.Model):
    """A text/code file created from an AI chat request.

    The opaque token is used in the download URL, while the user/session
    ownership check in the download view keeps the file private.
    """
    token       = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user        = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ai_generated_files', null=True, blank=True)
    session_key = models.CharField(max_length=40, blank=True, db_index=True)
    file_name   = models.CharField(max_length=120)
    content     = models.TextField()
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'AI Generated File'
        verbose_name_plural = 'AI Generated Files'

    def __str__(self):
        return self.file_name


class AIMessage(models.Model):
    ROLE_USER = 'user'
    ROLE_ASSISTANT = 'assistant'
    ROLE_CHOICES = [(ROLE_USER, 'User'), (ROLE_ASSISTANT, 'Assistant')]

    conversation = models.ForeignKey(AIConversation, on_delete=models.CASCADE, related_name='messages')
    role         = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content      = models.TextField()
    image_data   = models.TextField(blank=True)  # data: URI of an attached image, if any (user turns only)
    document_name = models.CharField(max_length=255, blank=True)
    # Extracted text (capped by doc_extract.MAX_CHARS), persisted so
    # follow-up questions about the same document work without re-uploading
    # it — replayed on every turn within AI_CHAT_MAX_HISTORY, same tradeoff
    # as replaying an attached image.
    document_text = models.TextField(blank=True)
    # The earlier AI answer (or the part of it the user selected) this message
    # replies to, set by the Reply button under an answer (user turns only).
    # Shown as a quote above the message and given to the model as context.
    reply_to_text = models.TextField(blank=True)
    model_key    = models.CharField(max_length=20, blank=True)  # which EduTrellis model answered (assistant turns only)
    video_url    = models.CharField(max_length=500, blank=True)  # a generated video (assistant turns only)
    web_sources  = models.JSONField(default=list, blank=True)    # live web results the reply used (web_search.public_sources)
    # Comma-separated slugs of real EduTrellis Store products shown as cards
    # under this reply (assistant turns only) — see ai_app.product_search.
    # Never AI-generated text; always resolved from the real Product table
    # so history replay shows the same real cards, not anything the model
    # claimed.
    product_slugs = models.CharField(max_length=250, blank=True)
    # Set when the user retries a reply or edits a sent message: that turn and
    # everything after it is replaced by the new one. It is hidden from the
    # chat and from the model's context but kept, so the admin AI Activity
    # page still shows what was actually said.
    superseded   = models.BooleanField(default=False)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'AI Message'
        verbose_name_plural = 'AI Messages'

    def __str__(self):
        return f"{self.role}: {self.content[:40]}"


class AINote(models.Model):
    """A note saved from the AI chat, Google-Keep style — created when the
    user says something like 'take this note', 'note it down', or 'save
    details' (see request_router.is_note_intent / views._ai_save_note_response).
    Owned the same dual way as AIConversation: a real account, or a guest
    tied to session_key."""
    user         = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='ai_notes')
    session_key  = models.CharField(max_length=40, blank=True, db_index=True)
    conversation = models.ForeignKey(AIConversation, on_delete=models.SET_NULL, null=True, blank=True, related_name='notes')
    heading      = models.CharField(max_length=120, blank=True)
    content      = models.TextField()
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'AI Note'
        verbose_name_plural = 'AI Notes'

    def __str__(self):
        return self.heading or f"Note #{self.pk}"


class AIUserImage(models.Model):
    """One AI-generated image, kept for the user's "My Images" gallery.

    Deliberately independent of AIConversation and AIMessage. The gallery used
    to read straight from AIMessage.image_data, which meant deleting a chat
    silently destroyed every picture generated in it — people delete old chats
    to tidy up, not to throw away their images. The conversation link below is
    SET_NULL so the image outlives the chat it came from, and nothing in the
    app deletes these rows: they go only when the account itself does.
    """
    user         = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='ai_images')
    session_key  = models.CharField(max_length=40, blank=True, db_index=True)
    conversation = models.ForeignKey(AIConversation, on_delete=models.SET_NULL, null=True, blank=True, related_name='generated_images')
    # The stored media URL produced by views._ai_flux_response — never text the
    # model wrote, and never a data: URI.
    url          = models.TextField()
    # What the user asked for, so the gallery can be searched and a picture can
    # be traced back to its request after its chat is gone.
    prompt       = models.TextField(blank=True)
    model_key    = models.CharField(max_length=20, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'AI Generated Image'
        verbose_name_plural = 'AI Generated Images'
        indexes = [models.Index(fields=['user', '-created_at'])]

    def __str__(self):
        return f"Image #{self.pk}"


class AIReport(models.Model):
    """A user's 'this answer is wrong / abusive' report against one
    assistant reply, submitted from the Report button under every AI chat
    message. Snapshots the reported turn's text, visual/document evidence,
    and model at submit time (not just a message FK) so staff can still see
    what was flagged even if the message or conversation is later deleted."""
    STATUS_OPEN = 'open'
    STATUS_RESOLVED = 'resolved'
    STATUS_CHOICES = [(STATUS_OPEN, 'Open'), (STATUS_RESOLVED, 'Resolved')]

    conversation   = models.ForeignKey(AIConversation, on_delete=models.SET_NULL, null=True, blank=True, related_name='reports')
    message        = models.ForeignKey(AIMessage, on_delete=models.SET_NULL, null=True, blank=True, related_name='reports')
    # Snapshot both sides of the reported turn, including visual inputs and
    # outputs.  Reports must remain reviewable after a chat or its generated
    # media is deleted, so the message foreign key is only a convenient link,
    # never the sole copy of the evidence.
    user_prompt    = models.TextField(blank=True)
    user_image     = models.TextField(blank=True)
    user_document_name = models.CharField(max_length=255, blank=True)
    user_document_excerpt = models.TextField(blank=True)
    reported_reply = models.TextField(blank=True)
    reported_image = models.TextField(blank=True)
    model_key      = models.CharField(max_length=20, blank=True)
    explanation    = models.TextField(blank=True)
    user           = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='ai_reports')
    session_key    = models.CharField(max_length=40, blank=True, db_index=True)
    ip_address     = models.GenericIPAddressField(null=True, blank=True)
    status         = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_OPEN)
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'AI Report'
        verbose_name_plural = 'AI Reports'

    def __str__(self):
        return f"Report #{self.pk} ({self.get_status_display()})"


class AISiteSettings(models.Model):
    """Singleton branding/config for the EduTrellis AI section, managed from
    the dashboard's 'EduTrellis AI → AI Settings' page (was Vidhyora's
    standalone 'Customize' page — renamed here since EduTrellis's own
    Customize group already means storefront content management)."""
    favicon    = models.ImageField(upload_to='ai_branding/', blank=True, null=True, help_text='Browser-tab icon for the /ai/ pages. Square, ideally 512×512px or smaller (PNG/ICO). Leave blank to use the default favicon.')
    ai_brand_name = models.CharField(
        max_length=60, default='EduTrellis',
        help_text='The AI assistant\'s name — shown across the chat UI (header, title, model picker) and used '
                   'in its own replies ("I\'m an AI model built by the ... team"). Changing this takes effect '
                   'immediately, no restart needed.',
    )
    social_preview_title = models.CharField(
        max_length=120, default='EduTrellis AI — Free AI Chat Assistant',
        help_text='Heading shown in WhatsApp and social link previews for /ai/.',
    )
    social_preview_description = models.CharField(
        max_length=300,
        default='Chat with EduTrellis AI for quick answers, writing, coding, images and learning support — free, right from your browser.',
        help_text='Short description shown below the preview heading.',
    )
    social_preview_image = models.ImageField(
        upload_to='ai_branding/social/', blank=True, null=True,
        help_text='Large preview image. 1200×630px is recommended. Leave blank to use the default cover.',
    )
    support_whatsapp_number = models.CharField(
        max_length=20, default='9695953183',
        help_text='Shown in the in-app "Contact support" popup\'s WhatsApp option, exactly as typed. Also used '
                   'to build its wa.me link — with or without a country code both work.',
    )
    support_email = models.EmailField(
        max_length=254, default='support@edutrellis.in',
        help_text='Shown in the in-app "Contact support" popup\'s email option and used for its mailto: link.',
    )
    youtube_cookies_txt = models.TextField(
        blank=True,
        help_text='Paste a YouTube cookies.txt (Netscape format), exported while logged into YouTube in a normal '
                   'browser — fixes "Sign in to confirm you\'re not a bot" download failures, which datacenter/'
                   'server IPs (like most hosting providers) hit far more than a home connection. Leave blank to '
                   'disable. Superadmin-only feature — see ai_app.youtube_download.',
    )
    # A separate installable PWA from the storefront's own (myapp.PWASettings)
    # — /ai/ is a different app a phone can install ("Vidhyora AI"/"ChatGPT
    # 5.6"), with its own icon/name/theme, not the storefront's branding.
    pwa_enabled = models.BooleanField(default=True, help_text="Show the 'Install App' option on the AI chat page.")
    pwa_app_name = models.CharField(max_length=60, blank=True, default='EduTrellis AI')
    pwa_short_name = models.CharField(max_length=30, blank=True, default='EduTrellis AI')
    pwa_description = models.CharField(max_length=200, blank=True, default='Chat with EduTrellis AI.')
    pwa_icon = models.ImageField(upload_to='ai_branding/pwa/', blank=True, null=True, help_text='Square PWA install icon (512×512px recommended). Leave blank to use the bundled default.')
    pwa_theme_color = models.CharField(max_length=7, blank=True, default='#e8001e')
    pwa_background_color = models.CharField(max_length=7, blank=True, default='#ffffff')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'AI Settings'
        verbose_name_plural = 'AI Settings'

    def __str__(self):
        return 'AI settings'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    # Read-only aliases matching myapp.PWASettings' attribute names, so the
    # Vidhyora-derived chat page/manifest code can use /ai/'s own PWA settings.
    @property
    def app_name(self):
        return self.pwa_app_name

    @property
    def short_name(self):
        return self.pwa_short_name

    @property
    def description(self):
        return self.pwa_description

    @property
    def icon(self):
        return self.pwa_icon

    @property
    def theme_color(self):
        return self.pwa_theme_color

    @property
    def background_color(self):
        return self.pwa_background_color

    @property
    def ready(self):
        return self.pwa_ready

    @property
    def pwa_ready(self):
        # No icon requirement, unlike the storefront's PWASettings — falls
        # back to the bundled default ai-icon-192/512.png when none is set.
        return self.pwa_enabled


class AIAccountMessageSettings(models.Model):
    """Reusable message shown after staff create an AI premium account."""

    DEFAULT_TEMPLATE = (
        "✨ Your personal AI account has been successfully activated for {access_days} days! 🎉\n"
        "Enjoy access to powerful AI models, image and file uploads, and other premium features "
        "through your dedicated account. 🚀\n"
        "🔗 Login: https://www.edutrellis.in/ai/\n"
        "📧 Email: {email}\n"
        "🔑 Password: {password}\n"
        "📅 Validity: {access_days} days\n"
        "🔒 This is your private account, and no account sharing is required. Please use the service "
        "responsibly. Fair-use policies and platform limits may apply. ⚖️\n"
        "🛠️ If you face any login or technical issue, please contact us—we’re always happy to help. 🤝\n"
        "🌟 EduTrellis\n"
        "🌐 https://www.edutrellis.in\n"
        "📧 support@edutrellis.in 📞 Calling Support: 10 AM–7 PM 💬 WhatsApp Support Available"
    )

    message_template = models.TextField(default=DEFAULT_TEMPLATE)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'AI Account Message Settings'
        verbose_name_plural = 'AI Account Message Settings'

    def __str__(self):
        return 'AI account message settings'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def render_message(self, *, email, password, access_days):
        return (
            self.message_template
            .replace('{email}', str(email))
            .replace('{password}', str(password))
            .replace('{access_days}', str(access_days))
        )


class AIAPIAccess(models.Model):
    """Which of ai_chat.MODELS a user's developer API key (see AIAPIKey) is
    allowed to call directly over HTTP, from their own code — separate from
    StoreProfile.ai_subscription_until, which only controls in-app chat
    access on the /ai/ page. Granted per-user by staff (dashboard API
    Management); a user with no row here, or an empty model_keys, has no
    developer API access at all and the profile page tells them to contact
    an admin."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='ai_api_access')
    model_keys = models.CharField(
        max_length=500, blank=True,
        help_text='Comma-separated ai_chat.MODELS keys (e.g. "sol,terra,luna") this account\'s API key may call.',
    )
    granted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Staff account that last changed this grant.',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'AI API Access Grant'
        verbose_name_plural = 'AI API Access Grants'

    def __str__(self):
        return f"{self.user} -> {self.model_keys or '(none)'}"

    @property
    def model_key_list(self):
        return [key.strip() for key in self.model_keys.split(',') if key.strip()]


class AIAPIKey(models.Model):
    """One user's developer API key for calling their AIAPIAccess-granted
    models directly (see views.api_chat_completions). Only a salted hash is
    stored — the raw key is shown once, at generation time, and can never be
    retrieved again; regenerating replaces it and immediately invalidates
    the previous key."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='ai_api_key')
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    key_prefix = models.CharField(max_length=12, help_text='First few characters, shown in the UI so the user can recognize their key.')
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    request_count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = 'AI API Key'
        verbose_name_plural = 'AI API Keys'

    def __str__(self):
        return f"{self.user} ({self.key_prefix}…)"

    @staticmethod
    def _hash(raw_key):
        return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()

    @classmethod
    def generate_for(cls, user):
        """Create (or replace) this user's key and return the one-time raw
        value — nothing else ever exposes it again."""
        raw_key = 'edk_' + secrets.token_urlsafe(32)
        key_hash = cls._hash(raw_key)
        key_prefix = raw_key[:11]
        cls.objects.update_or_create(
            user=user,
            defaults={'key_hash': key_hash, 'key_prefix': key_prefix, 'last_used_at': None, 'request_count': 0},
        )
        return raw_key

    @classmethod
    def resolve(cls, raw_key):
        """Look up the owning AIAPIKey for a raw key presented to the API,
        or None if it doesn't match any stored hash."""
        raw_key = (raw_key or '').strip()
        if not raw_key:
            return None
        return cls.objects.select_related('user').filter(key_hash=cls._hash(raw_key)).first()


class AICodingKey(models.Model):
    """A key for the "Start coding" CLI (OpenCode pointed at /api/v1/code/).
    Separate from AIAPIKey so the developer API and the coding CLI can be
    rotated or revoked independently, and so coding access follows the in-app
    AI plan rather than a staff grant. Like AIAPIKey only a hash is stored:
    the raw ``vdc_…`` value is shown once.

    A user has at most one *manual* key (label '', made from the panel's
    "Create key" button) plus one *automatic* key per computer that ran the
    one-line setup (label = that computer's name), so signing in a second
    machine never signs out the first."""
    MAX_PER_USER = 10

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ai_coding_keys')
    label = models.CharField(max_length=60, blank=True, help_text="Empty for the manual key; otherwise the computer it was set up on.")
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    key_prefix = models.CharField(max_length=12)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    request_count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = 'AI Coding Key'
        verbose_name_plural = 'AI Coding Keys'

    def __str__(self):
        return f"{self.user} {self.label or 'manual'} ({self.key_prefix}…)"

    @staticmethod
    def _hash(raw_key):
        return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()

    @classmethod
    def generate_for(cls, user, label=''):
        """Create (or replace the same-label) key and return the one-time raw
        value; replacing immediately invalidates that label's previous key."""
        raw_key = 'vdc_' + secrets.token_urlsafe(32)
        cls.objects.update_or_create(
            user=user, label=label,
            defaults={'key_hash': cls._hash(raw_key), 'key_prefix': raw_key[:11],
                      'last_used_at': None, 'request_count': 0, 'created_at': timezone.now()},
        )
        extra = cls.objects.filter(user=user).exclude(label='').order_by('-created_at')[cls.MAX_PER_USER:]
        cls.objects.filter(pk__in=[row.pk for row in extra]).delete()
        return raw_key

    @classmethod
    def resolve(cls, raw_key):
        raw_key = (raw_key or '').strip()
        if not raw_key:
            return None
        return cls.objects.select_related('user').filter(key_hash=cls._hash(raw_key)).first()


class AICodingDeviceCode(models.Model):
    """One run of the automatic setup script waiting for the user to approve
    it in the browser (a "device login"). The script holds the secret
    device code (only its hash is stored here); the user sees the short
    user_code on the approval page and confirms it matches their terminal."""
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_DENIED = 'denied'
    STATUS_USED = 'used'

    device_hash = models.CharField(max_length=64, unique=True)
    user_code = models.CharField(max_length=9, unique=True, db_index=True)
    machine = models.CharField(max_length=60, blank=True)
    requester_ip = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=10, default=STATUS_PENDING)
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        verbose_name = 'AI Coding Device Code'
        verbose_name_plural = 'AI Coding Device Codes'

    def __str__(self):
        return f"{self.user_code} ({self.status})"

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at


class ProviderAPICredential(models.Model):
    """Dashboard-editable override for one of the outbound provider API keys
    normally hardcoded/env-configured in edutrellis/settings.py (see
    ai_app.provider_keys.get_key, which every live call site reads through
    instead of settings directly). A blank/missing row here just means "use
    the settings.py default" — nothing breaks if this table is empty."""
    setting_name = models.CharField(
        max_length=80, unique=True, db_index=True,
        help_text='The Django settings attribute this overrides, e.g. NVIDIA_LUNA_API_KEY.',
    )
    value = models.CharField(max_length=500, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    class Meta:
        verbose_name = 'Provider API Credential'
        verbose_name_plural = 'Provider API Credentials'

    def __str__(self):
        return self.setting_name


class AIModelControl(models.Model):
    """Dashboard on/off switch plus live usage counters for one picker model
    (keyed by its ai_chat.MODELS key). No row means "enabled, never used" so
    nothing breaks on a fresh database. Counters track real upstream requests
    made through ai_chat.stream_chat, including ones that failed, which the
    AIMessage-based totals on the API Data page cannot see."""
    model_key = models.CharField(max_length=60, unique=True, db_index=True)
    is_enabled = models.BooleanField(default=True)
    # Blank means "use the built-in name/description from ai_chat.MODELS".
    display_name = models.CharField(max_length=60, blank=True)
    description = models.CharField(max_length=300, blank=True)
    request_count = models.PositiveIntegerField(default=0)
    success_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_test_ok = models.BooleanField(null=True, blank=True)
    last_test_message = models.CharField(max_length=300, blank=True)
    # "Remove" on API Settings: off, out of the model picker, and listed under
    # Removed models until restored (built-in models live in code, so they
    # are hidden rather than deleted).
    is_removed = models.BooleanField(default=False)
    # How the model answers "who are you / who made you / which model are
    # you"; blank fields keep the built-in identity rules.
    identity_name = models.CharField(max_length=80, blank=True)
    identity_creator = models.CharField(max_length=120, blank=True)
    identity_model = models.CharField(max_length=120, blank=True)
    identity_cutoff = models.CharField(max_length=80, blank=True, help_text='e.g. "Knowledge up to June 2026".')
    identity_strengths = models.CharField(max_length=220, blank=True, help_text='What it should say it is best at / specialises in.')
    identity_tone = models.CharField(max_length=220, blank=True, help_text='How it should sound — e.g. "formal and concise" or "warm and casual".')
    identity_notes = models.TextField(max_length=1000, blank=True)
    # The upstream model a built-in panel calls instead of the one in code
    # (blank = built-in), and whether it accepted the hide-thinking switch
    # when it was checked.
    model_id = models.CharField(max_length=200, blank=True)
    model_thinking_control = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'AI Model Control'
        verbose_name_plural = 'AI Model Controls'

    def __str__(self):
        return f"{self.model_key} ({'on' if self.is_enabled else 'off'})"


class CustomAIModel(models.Model):
    """A chat model added on the dashboard's API Settings page (see
    ai_app.custom_models), served to the model picker as ai_chat.MODELS
    entry ``custom-<pk>``. Its API key is a ProviderAPICredential named by
    ``key_setting``; on/off and counters are its AIModelControl row."""
    API_NVIDIA = 'nvidia'
    API_OPENAI_COMPATIBLE = 'openai'
    API_TYPE_CHOICES = [
        (API_NVIDIA, 'NVIDIA NIM (build.nvidia.com)'),
        (API_OPENAI_COMPATIBLE, 'Other OpenAI-compatible API'),
    ]

    KIND_CHAT = 'chat'
    KIND_IMAGE = 'image'
    KIND_VIDEO = 'video'
    KIND_CHOICES = [
        (KIND_CHAT, 'Chat model'),
        (KIND_IMAGE, 'Makes images'),
        (KIND_VIDEO, 'Makes videos'),
    ]

    api_type = models.CharField(max_length=10, choices=API_TYPE_CHOICES, default=API_NVIDIA)
    # Chat models join the model picker; image and video models are used by
    # image / video generation when switched on (is_active, one per kind).
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_CHAT)
    is_active = models.BooleanField(default=False, help_text='Image/video models: the one used to make pictures/videos.')
    # Image/video models: the address and request shape the checks found working.
    endpoint_url = models.URLField(max_length=300, blank=True)
    request_style = models.CharField(max_length=20, blank=True)
    base_url = models.URLField(max_length=300, blank=True, help_text='Only for an OpenAI-compatible API; NVIDIA uses its own.')
    model_id = models.CharField(max_length=200, help_text='The provider\'s model name, e.g. nvidia/nemotron-3-super-120b-a12b.')
    display_name = models.CharField(max_length=60)
    description = models.CharField(max_length=300, blank=True)
    # Found by the checks when the model is added (or re-tested).
    thinking_control = models.BooleanField(default=False, help_text='Accepts the "no visible thinking" switch NVIDIA reasoning models use.')
    vision = models.BooleanField(default=False, help_text='Can read attached images.')
    check_report = models.JSONField(default=list, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['created_at']
        verbose_name = 'Added AI Model'
        verbose_name_plural = 'Added AI Models'

    def __str__(self):
        return f'{self.display_name} ({self.model_id})'

    @property
    def model_key(self):
        return f'custom-{self.pk}'

    @property
    def key_setting(self):
        return f'CUSTOM_MODEL_{self.pk}_API_KEY'


class APICheckRun(models.Model):
    """One use of the dashboard's API Checker: the key that was checked (kept
    so it can be re-used from History), whether it worked, and a snapshot of
    every model and its abilities as NVIDIA listed them at that moment.
    The models tested in this run are its APICheckResult rows."""
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    api_key = models.CharField(max_length=300)
    key_status = models.CharField(max_length=20, blank=True)
    key_detail = models.CharField(max_length=300, blank=True)
    key_seconds = models.FloatField(null=True, blank=True)
    key_model = models.CharField(max_length=200, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    models_snapshot = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'API Checker Run'
        verbose_name_plural = 'API Checker Runs'

    def __str__(self):
        return f'API check #{self.pk} ({self.key_status})'

    @property
    def masked_key(self):
        key = self.api_key or ''
        return key[:10] + '…' + key[-4:] if len(key) > 16 else '…'


class APICheckResult(models.Model):
    """One model tested with the key of an API Checker run."""
    run = models.ForeignKey(APICheckRun, on_delete=models.CASCADE, related_name='results')
    model_id = models.CharField(max_length=200)
    status = models.CharField(max_length=20)
    seconds = models.FloatField(null=True, blank=True)
    detail = models.CharField(max_length=300, blank=True)
    reply = models.CharField(max_length=200, blank=True)
    tested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['tested_at']
        verbose_name = 'API Checker Result'
        verbose_name_plural = 'API Checker Results'

    def __str__(self):
        return f'{self.model_id}: {self.status}'


class AICall(models.Model):
    """One voice call on the /ai/ page (the phone button), kept for the
    account's "My calls" list. The words of both sides are saved as a
    transcript; the audio is the caller's own microphone only, because the
    browser's text-to-speech voice cannot be captured. Audio is uploaded in
    pieces while the call is running (see ai_app.ai_calls) so a closed tab
    keeps everything said so far."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ai_calls')
    conversation = models.ForeignKey(AIConversation, on_delete=models.SET_NULL, null=True, blank=True, related_name='calls')
    caller_name = models.CharField(max_length=60, blank=True)
    language = models.CharField(max_length=12, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    turn_count = models.PositiveIntegerField(default=0, help_text='How many things the caller said.')
    transcript = models.JSONField(default=list, blank=True)
    # Written in pieces while the call runs, so it stays on the server's private disk, not Dropbox.
    audio = models.FileField(upload_to='ai_calls/', storage=private_storage, blank=True, max_length=200)
    audio_mime = models.CharField(max_length=60, blank=True)
    audio_bytes = models.PositiveBigIntegerField(default=0)
    audio_chunks = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-started_at']
        verbose_name = 'AI Voice Call'
        verbose_name_plural = 'AI Voice Calls'
        indexes = [models.Index(fields=['user', '-started_at'])]

    def __str__(self):
        return f'Call #{self.pk} ({self.user})'


class AICodingSession(models.Model):
    """One OpenCode working session, found by grouping requests: OpenCode
    resends the whole conversation each time, so requests that start with the
    same first message from the same key belong together."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ai_coding_sessions')
    key = models.ForeignKey(AICodingKey, on_delete=models.SET_NULL, null=True, blank=True, related_name='sessions')
    machine = models.CharField(max_length=60, blank=True, help_text='The computer the key was set up on; empty for the manual key.')
    fingerprint = models.CharField(max_length=64, db_index=True)
    title = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_request_at = models.DateTimeField(default=timezone.now)
    request_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-last_request_at']
        verbose_name = 'OpenCode Session'
        verbose_name_plural = 'OpenCode Sessions'
        indexes = [models.Index(fields=['user', '-last_request_at'])]
        constraints = [models.UniqueConstraint(fields=['user', 'fingerprint'], name='uniq_coding_session_per_user_fingerprint')]

    def __str__(self):
        return f'{self.title or "OpenCode session"} ({self.user})'


class AICodingRequest(models.Model):
    """One request OpenCode made to /api/v1/code/chat/completions: what was
    asked (the newest message only — earlier ones are in earlier requests)
    and what came back, trimmed to a sensible size."""
    STATUS_OK = 'ok'
    STATUS_ERROR = 'error'
    STATUS_CANCELLED = 'cancelled'

    session = models.ForeignKey(AICodingSession, on_delete=models.CASCADE, related_name='requests')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ai_coding_requests')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    duration_ms = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=10, default=STATUS_OK)
    error = models.CharField(max_length=300, blank=True)
    stream = models.BooleanField(default=False)
    trigger = models.CharField(max_length=10, default='user', help_text='Role of the newest message: user, or tool for a tool result.')
    user_text = models.TextField(blank=True)
    reply_text = models.TextField(blank=True)
    tool_calls = models.JSONField(default=list, blank=True)
    message_count = models.PositiveIntegerField(default=0)
    prompt_chars = models.PositiveIntegerField(default=0)
    reply_chars = models.PositiveIntegerField(default=0)
    prompt_tokens = models.PositiveIntegerField(null=True, blank=True)
    completion_tokens = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'OpenCode Request'
        verbose_name_plural = 'OpenCode Requests'
        indexes = [models.Index(fields=['user', '-created_at'])]

    def __str__(self):
        return f'OpenCode request #{self.pk}'


class ChatUser(models.Model):
    name = models.CharField(max_length=120)
    # Picked at sign-up (lowercase letters, numbers, . and _); can log in with
    # it instead of the email. Empty (NULL) for guests and older accounts.
    username = models.CharField(max_length=30, unique=True, null=True, blank=True)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20)
    state = models.CharField(max_length=60)
    city = models.CharField(max_length=80)
    password = models.CharField(max_length=128)   # a django.contrib.auth.hashers hash
    # Shown as "verification needed" — nothing verifies it yet.
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    photo = models.ImageField(upload_to='support_chat/users/', blank=True)
    # Banner behind their photo on the EduChats ID card (⋮ → Cover photo).
    cover = models.ImageField(upload_to='support_chat/covers/', blank=True)
    # Turned off by staff: can't log in, and is logged out of open chats.
    is_active = models.BooleanField(default=True)
    # Chatting without an account; becomes a normal account on sign-up.
    is_guest = models.BooleanField(default=False, db_index=True)
    # Set for the site superuser's own EduChats account: they log in to EduChats
    # with their EduTrellis admin email + password (see educhats_admin.py).
    django_user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='chat_account')
    ip = models.CharField(max_length=64, blank=True, db_index=True)        # where the chat was started
    last_ip = models.CharField(max_length=64, blank=True)
    # Privacy (EduChats ⋮ → Privacy): what people this user has a direct chat
    # with may see. Support staff always see everything in the dashboard.
    show_phone = models.BooleanField(default=False)
    show_email = models.BooleanField(default=False)
    show_ip = models.BooleanField(default=False)
    show_last_seen = models.BooleanField(default=True)    # online dot + "last seen …"
    show_activity = models.BooleanField(default=True)     # "typing…" + "is here"
    # Read receipts (blue ticks) work both ways, like WhatsApp: switch them off
    # and nobody sees that you've read their messages — and you don't see theirs.
    read_receipts = models.BooleanField(default=True)
    TICK_CHOICES = [('double', 'Double tick'), ('single', 'Single tick'), ('none', 'No ticks')]
    tick_style = models.CharField(max_length=8, choices=TICK_CHOICES, default='double')
    # ⋮ → More AI features: EduTrellis AI may send a message for you when you ask it to
    # ("say hello to @rnt"); see educhats_ai_actions.py.
    ai_send_messages = models.BooleanField(default=False)
    # "Delete chat" on the support chat hides messages up to this id for the
    # user only; the support team keeps its copy.
    support_cleared_upto = models.BigIntegerField(default=0)
    # App lock (⋮ → App lock, educhats_lock.py): a hashed 4–6 digit PIN, how long after
    # leaving EduChats it locks again (seconds, 0 = straight away), and the fingerprint /
    # face / Windows Hello keys (WebAuthn) that can unlock it instead of the PIN.
    app_lock_pin = models.CharField(max_length=128, blank=True)
    app_lock_after = models.PositiveIntegerField(default=0)
    app_lock_keys = models.JSONField(default=list, blank=True)
    bio = models.CharField(max_length=160, blank=True)    # short "about me" under their name
    show_bio = models.BooleanField(default=True)
    # Social links (Profile → Social links), stored as clean https profile URLs
    # (see educhats_social.py); each shows on their ID card unless hidden in Privacy.
    snapchat = models.CharField(max_length=200, blank=True)
    instagram = models.CharField(max_length=200, blank=True)
    facebook = models.CharField(max_length=200, blank=True)
    linkedin = models.CharField(max_length=200, blank=True)
    show_snapchat = models.BooleanField(default=True)
    show_instagram = models.BooleanField(default=True)
    show_facebook = models.BooleanField(default=True)
    show_linkedin = models.BooleanField(default=True)
    # ⋮ → Customize: accent colour, font, text size and wallpaper (preset keys),
    # so the look follows the account to other devices.
    appearance = models.JSONField(default=dict, blank=True)
    # Sample accounts that fill the Maps tab until real people join (exampleN@gmail.com).
    # Turned off, so they never log in or show up in search; delete them with
    # `python manage.py educhats_demo_users --delete` or from the Maps tab.
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Chat user'

    def __str__(self):
        return f'{self.name} <{self.email}>'


class MapSpot(models.Model):
    """EduChats → Maps: where someone shows up on the India map and what others
    may see about them. The spot is rounded to about 1 km when it is saved."""
    user = models.OneToOneField(ChatUser, on_delete=models.CASCADE, related_name='map_spot')
    latitude = models.FloatField()
    longitude = models.FloatField()
    city = models.CharField(max_length=80, blank=True)
    state = models.CharField(max_length=60, blank=True)
    visible = models.BooleanField(default=True)
    show_name = models.BooleanField(default=False)
    show_username = models.BooleanField(default=False)
    show_email = models.BooleanField(default=False)
    show_city = models.BooleanField(default=True)
    show_photo = models.BooleanField(default=False)
    show_bio = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['pk']
        verbose_name = 'Map spot'

    def __str__(self):
        return f'User {self.pk} · {self.city or self.state}'


class ChatPasswordReset(models.Model):
    user = models.ForeignKey(ChatUser, on_delete=models.CASCADE, related_name='password_resets')
    token_hash = models.CharField(max_length=64, unique=True)   # sha256 of the emailed token
    created_at = models.DateTimeField(auto_now_add=True)
    used = models.BooleanField(default=False)


class ChatMessage(models.Model):
    SENDER_USER = 'user'
    SENDER_ADMIN = 'admin'
    SENDER_CHOICES = [(SENDER_USER, 'User'), (SENDER_ADMIN, 'Admin')]

    KIND_TEXT = 'text'
    KIND_IMAGE = 'image'
    KIND_VIDEO = 'video'
    KIND_FILE = 'file'
    KIND_LOCATION = 'location'
    KIND_CALL = 'call'
    KIND_STICKER = 'sticker'   # body = animated Noto emoji code (educhats_media.py)
    KIND_GIF = 'gif'           # body = GIF URL on GIPHY / Tenor's media server
    KIND_VOICE = 'voice'       # attachment = the recording, body = its length ("0:12")
    KIND_CHOICES = [(KIND_TEXT, 'Text'), (KIND_IMAGE, 'Photo'), (KIND_VIDEO, 'Video'), (KIND_FILE, 'File'),
                    (KIND_LOCATION, 'Location'), (KIND_CALL, 'Call'), (KIND_STICKER, 'Sticker'), (KIND_GIF, 'GIF'),
                    (KIND_VOICE, 'Voice message')]

    user = models.ForeignKey(ChatUser, on_delete=models.CASCADE, related_name='messages')
    sender = models.CharField(max_length=5, choices=SENDER_CHOICES)
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=KIND_TEXT)
    body = models.TextField(blank=True)   # the text, a caption, or the call summary
    # Served only through support_chat.attachment, which checks who is asking.
    attachment = models.FileField(upload_to='support_chat/%Y/%m/', blank=True)
    attachment_name = models.CharField(max_length=200, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    # A live location keeps updating until this time (None = a one-off pin).
    live_until = models.DateTimeField(null=True, blank=True)
    location_updated_at = models.DateTimeField(null=True, blank=True)
    # A user message is "pending" until an admin opens that chat.
    read_by_admin = models.BooleanField(default=False, db_index=True)
    # An admin reply is "seen" (blue ticks) once the user's open chat loads it.
    read_by_user = models.BooleanField(default=False)
    # View once: the other side can open the photo/video a single time (educhats_share.py).
    view_once = models.BooleanField(default=False)
    opened_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'pk']


class SupportProfile(models.Model):
    """How the support team appears to people on /chats (one row)."""
    display_name = models.CharField(max_length=60, default='Support')
    about = models.CharField(max_length=120, default='We usually reply within a few hours')
    welcome = models.CharField(max_length=300, default='Send us a message, a photo or your location — our team will reply here.')
    photo = models.ImageField(upload_to='support_chat/profile/', blank=True)
    # Shown on /chats "Forgot password" — people write here to get back in.
    support_email = models.EmailField(default='shifn32@gmail.com', blank=True)
    # Shown in EduChats "Support / Report us" (tap to call); blank uses the site's support number.
    support_phone = models.CharField(max_length=20, blank=True)
    # Shown on the Support ID card; clean https URLs (see educhats_social.py).
    snapchat = models.CharField(max_length=200, blank=True)
    instagram = models.CharField(max_length=200, blank=True)
    facebook = models.CharField(max_length=200, blank=True)
    linkedin = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

    def __str__(self):
        return self.display_name


class DirectThread(models.Model):
    """A one-to-one EduChats conversation between two signed-up chat accounts,
    found by username. user_a always has the lower pk, so a pair has one thread."""
    user_a = models.ForeignKey(ChatUser, on_delete=models.CASCADE, related_name='dm_threads_a')
    user_b = models.ForeignKey(ChatUser, on_delete=models.CASCADE, related_name='dm_threads_b')
    # Who opened it — an empty thread only shows up for them until a message is sent.
    started_by = models.ForeignKey(ChatUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    # Either side can block the other; nobody can send while it's set.
    blocked_by = models.ForeignKey(ChatUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    # "Delete chat" is per person: each side hides messages up to this id.
    a_cleared_upto = models.BigIntegerField(default=0)
    b_cleared_upto = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-updated_at']
        constraints = [models.UniqueConstraint(fields=['user_a', 'user_b'], name='unique_direct_thread_pair')]
        verbose_name = 'EduChats direct thread'

    def other(self, user):
        return self.user_b if self.user_a_id == user.pk else self.user_a

    def cleared_upto(self, user):
        return self.a_cleared_upto if self.user_a_id == user.pk else self.b_cleared_upto

    def __str__(self):
        return f'{self.user_a} ↔ {self.user_b}'


class DirectMessage(models.Model):
    thread = models.ForeignKey(DirectThread, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(ChatUser, on_delete=models.CASCADE, related_name='dm_sent')
    # 'call' rows are the "Voice call · 2:31" / "Missed video call" lines a call leaves behind;
    # image / video / file rows carry an attachment (body is the optional caption).
    KIND_CHOICES = [('text', 'Text'), ('call', 'Call'), ('image', 'Photo'), ('video', 'Video'), ('file', 'File'),
                    ('sticker', 'Sticker'), ('gif', 'GIF'), ('voice', 'Voice message'), ('location', 'Location')]
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default='text')
    body = models.TextField(blank=True)
    # Served only through direct_chat.dm_attachment, which checks who is asking.
    attachment = models.FileField(upload_to='educhats/dm/%Y/%m/', blank=True)
    attachment_name = models.CharField(max_length=200, blank=True)
    # Location pins (kind 'location'); a live one keeps moving until live_until.
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    live_until = models.DateTimeField(null=True, blank=True)
    location_updated_at = models.DateTimeField(null=True, blank=True)
    view_once = models.BooleanField(default=False)
    opened_at = models.DateTimeField(null=True, blank=True)   # view once: when the other person opened it
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)   # when the other person loaded it

    class Meta:
        ordering = ['pk']
        verbose_name = 'EduChats direct message'

    @property
    def summary(self):
        """One line for exports and the admin view: "[Photo: x.jpg] caption", "[Call] …" or the text."""
        if self.view_once:
            return '[View once ' + ('video' if self.kind == 'video' else 'photo') + ']'
        if self.kind == 'location' and self.latitude is not None:
            return f'[Location] https://maps.google.com/?q={self.latitude},{self.longitude}'
        if self.kind in ('image', 'video', 'file'):
            label = {'image': 'Photo', 'video': 'Video', 'file': 'File'}[self.kind]
            name = f': {self.attachment_name}' if self.attachment_name else ''
            return f'[{label}{name}]' + (f' {self.body}' if self.body else '')
        if self.kind == 'call':
            return f'[Call] {self.body}'
        if self.kind == 'voice':
            return f'[Voice message {self.body}]'
        if self.kind == 'sticker':
            return '[Sticker]'
        if self.kind == 'gif':
            return f'[GIF] {self.body}'
        return self.body


class EduChatsAIMessage(models.Model):
    """A turn in a member's EduTrellis AI chat inside EduChats (the pinned
    "EduTrellis AI" conversation). Separate from /ai/'s AIConversation."""
    ROLE_USER = 'user'
    ROLE_ASSISTANT = 'assistant'
    user = models.ForeignKey(ChatUser, on_delete=models.CASCADE, related_name='ai_messages')
    role = models.CharField(max_length=10, choices=[(ROLE_USER, 'User'), (ROLE_ASSISTANT, 'Assistant')])
    body = models.TextField(blank=True)
    image_url = models.CharField(max_length=300, blank=True)   # a picture EduTrellis AI created
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['pk']
        verbose_name = 'EduChats AI message'


class ChatGroup(models.Model):
    """An EduChats group (⋮ → New group): any signed-up members, with admins."""
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=500, blank=True)
    photo = models.ImageField(upload_to='support_chat/groups/', blank=True)
    created_by = models.ForeignKey(ChatUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    # Group settings (admins only): who may send, and who may change name / photo / description.
    only_admins_send = models.BooleanField(default=False)
    only_admins_edit = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(default=timezone.now, db_index=True)   # last message — sorts the chat list

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'EduChats group'

    def __str__(self):
        return self.name


class GroupMember(models.Model):
    group = models.ForeignKey(ChatGroup, on_delete=models.CASCADE, related_name='members')
    user = models.ForeignKey(ChatUser, on_delete=models.CASCADE, related_name='group_memberships')
    is_admin = models.BooleanField(default=False)
    added_by = models.ForeignKey(ChatUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    joined_at = models.DateTimeField(auto_now_add=True)
    last_read = models.BigIntegerField(default=0)      # newest message id this member has seen
    cleared_upto = models.BigIntegerField(default=0)   # "Delete chat" hides messages up to here, for them only

    class Meta:
        ordering = ['joined_at', 'pk']
        constraints = [models.UniqueConstraint(fields=['group', 'user'], name='unique_group_member')]
        verbose_name = 'EduChats group member'


class GroupMessage(models.Model):
    KIND_CHOICES = [('text', 'Text'), ('image', 'Photo'), ('video', 'Video'), ('file', 'File'), ('system', 'Update'),
                    ('sticker', 'Sticker'), ('gif', 'GIF'), ('voice', 'Voice message'), ('location', 'Location')]
    group = models.ForeignKey(ChatGroup, on_delete=models.CASCADE, related_name='messages')
    # None for the grey "Rahul added Muskan" lines.
    sender = models.ForeignKey(ChatUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='group_sent')
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default='text')
    body = models.TextField(blank=True)
    # Served only through group_chat.group_file, which checks membership.
    attachment = models.FileField(upload_to='educhats/groups/%Y/%m/', blank=True)
    attachment_name = models.CharField(max_length=200, blank=True)
    # Location pins (kind 'location'); a live one keeps moving until live_until.
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    live_until = models.DateTimeField(null=True, blank=True)
    location_updated_at = models.DateTimeField(null=True, blank=True)
    view_once = models.BooleanField(default=False)
    opened_by = models.JSONField(default=list, blank=True)   # view once: members who have opened it
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['pk']
        verbose_name = 'EduChats group message'

    @property
    def summary(self):
        if self.view_once:
            return '[View once ' + ('video' if self.kind == 'video' else 'photo') + ']'
        if self.kind == 'location' and self.latitude is not None:
            return f'[Location] https://maps.google.com/?q={self.latitude},{self.longitude}'
        if self.kind in ('image', 'video', 'file'):
            label = {'image': 'Photo', 'video': 'Video', 'file': 'File'}[self.kind]
            name = f': {self.attachment_name}' if self.attachment_name else ''
            return f'[{label}{name}]' + (f' {self.body}' if self.body else '')
        if self.kind == 'voice':
            return f'[Voice message {self.body}]'
        if self.kind == 'sticker':
            return '[Sticker]'
        if self.kind == 'gif':
            return f'[GIF] {self.body}'
        return self.body


MATCH_BAR_COLOR = '#0f1623'   # the chat list's colour (--ink in chats.html)


class EduChatsAppSettings(models.Model):
    """How the installed EduChats app looks (Admin → EduChats → App Settings): one row."""
    app_name = models.CharField(max_length=45, default='EduChats')
    # The label under the icon on a phone's home screen — about 12 letters fit.
    short_name = models.CharField(max_length=24, default='EduChats')
    description = models.CharField(max_length=200, default='Chat with the EduTrellis support team and people you know.')
    # Square picture, at least 512×512; the install icons are cut from it. Empty = the built-in red chat icon.
    icon = models.ImageField(upload_to='support_chat/app/', blank=True)
    theme_color = models.CharField(max_length=7, default='#0f1623')        # status bar / title bar (custom mode)
    background_color = models.CharField(max_length=7, default='#0f1623')   # splash screen while it opens
    # The phone's status bar (top) and navigation bar (bottom) in the installed app:
    # 'match' follows whatever is on screen there (dark chat list, a chat's header…),
    # 'custom' uses theme_color on top and nav_color at the bottom.
    BARS_MODES = [('match', 'Match the chat screen'), ('custom', 'Custom colours')]
    bars_mode = models.CharField(max_length=8, choices=BARS_MODES, default='match')
    nav_color = models.CharField(max_length=7, default='#0f1623')
    # Optional TURN relay for calls (e.g. turn:relay.example.com:3478) — carries a
    # call when two phones can't reach each other directly (mobile data, strict Wi-Fi).
    turn_url = models.CharField(max_length=300, blank=True)
    turn_username = models.CharField(max_length=120, blank=True)
    turn_credential = models.CharField(max_length=200, blank=True)
    # GIFs in the emoji panel: a free key from developers.giphy.com or Google's Tenor API.
    GIF_PROVIDERS = [('giphy', 'GIPHY'), ('tenor', 'Tenor')]
    gif_provider = models.CharField(max_length=8, choices=GIF_PROVIDERS, default='giphy')
    gif_api_key = models.CharField(max_length=120, blank=True)
    # Web Push (VAPID) key pair for notifications — made automatically the first time (educhats_push.py).
    vapid_private = models.CharField(max_length=64, blank=True)
    vapid_public = models.CharField(max_length=100, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'EduChats app settings'

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def version(self):
        return int(self.updated_at.timestamp()) if self.updated_at else 0

    @property
    def status_bar_color(self):
        """What the status bar starts as (the app opens on the dark chat list)."""
        return self.theme_color if self.bars_mode == 'custom' else MATCH_BAR_COLOR

    def __str__(self):
        return self.app_name


class PushSubscription(models.Model):
    """One browser / installed app that allows EduChats notifications (educhats_push.py)."""
    user = models.ForeignKey(ChatUser, on_delete=models.CASCADE, related_name='push_subscriptions')
    endpoint = models.CharField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=120)
    auth = models.CharField(max_length=48)
    # Chats muted on this device ('support', a thread id, 'g<id>', 'sx<id>'): no popup, not in the badge.
    muted = models.JSONField(default=list, blank=True)
    user_agent = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_ok = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'{self.user} · {self.user_agent[:40]}'


class Story(models.Model):
    """An EduChats update ("story"): a photo, video or coloured text card that the
    people you chat with can see for 24 hours (educhats_updates.py)."""
    KIND_CHOICES = [('text', 'Text'), ('image', 'Photo'), ('video', 'Video')]
    user = models.ForeignKey(ChatUser, on_delete=models.CASCADE, related_name='stories')
    kind = models.CharField(max_length=5, choices=KIND_CHOICES, default='text')
    text = models.CharField(max_length=700, blank=True)   # the text card, or a photo / video caption
    bg = models.CharField(max_length=7, blank=True)       # text card colour
    media = models.FileField(upload_to='educhats/stories/%Y/%m/', blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.user} · {self.get_kind_display()} · {self.created_at:%d %b %H:%M}'


class StoryView(models.Model):
    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name='views')
    viewer = models.ForeignKey(ChatUser, on_delete=models.CASCADE, related_name='story_views')
    seen_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['story', 'viewer'], name='one_view_per_story')]
