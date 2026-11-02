from django.contrib import admin

from .models import (
    AIConversation, AIMessage, AIBlock, AIReport, GitHubConnection,
    KnowledgeEntry, AISiteSettings, AIAccountMessageSettings, AIAPIAccess,
    AIAPIKey, ProviderAPICredential,
)


class AIMessageInline(admin.TabularInline):
    model = AIMessage
    extra = 0
    readonly_fields = ('role', 'content', 'image_data', 'document_name', 'model_key', 'created_at')
    can_delete = False


@admin.register(AIConversation)
class AIConversationAdmin(admin.ModelAdmin):
    # who_asked makes it obvious at a glance whether a conversation belongs
    # to a real account or an anonymous guest, without opening each row.
    list_display = ('__str__', 'who_asked', 'created_at', 'updated_at')
    search_fields = ('title', 'user__username', 'user__email', 'session_key')
    list_filter = ('created_at',)
    inlines = [AIMessageInline]

    def who_asked(self, obj):
        if obj.user:
            return obj.user.get_full_name() or obj.user.username
        return f'Guest ({obj.session_key[:10]}…)' if obj.session_key else 'Guest'
    who_asked.short_description = 'Asked by'


@admin.register(AIMessage)
class AIMessageAdmin(admin.ModelAdmin):
    list_display = ('conversation', 'who_asked', 'role', 'attachment', 'model_key', 'created_at')
    list_select_related = ('conversation', 'conversation__user')
    list_filter = ('role', 'model_key')
    search_fields = ('content', 'document_name', 'conversation__title', 'conversation__user__username', 'conversation__user__email', 'conversation__session_key')

    def who_asked(self, obj):
        user = obj.conversation.user
        if user:
            return user.get_full_name() or user.username
        session_key = obj.conversation.session_key
        return f'Guest ({session_key[:10]}…)' if session_key else 'Guest'
    who_asked.short_description = 'Asked by'

    def attachment(self, obj):
        if obj.document_name:
            return f'📄 {obj.document_name}'
        if obj.image_data:
            return '🖼️ image'
        return ''
    attachment.short_description = 'Attachment'


@admin.register(AIBlock)
class AIBlockAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'ip_address', 'user', 'reason', 'created_by', 'created_at')
    search_fields = ('ip_address', 'user__username', 'user__email', 'reason')
    list_filter = ('created_at',)


@admin.register(AIReport)
class AIReportAdmin(admin.ModelAdmin):
    # reported_by mirrors AIConversationAdmin.who_asked — shows at a glance
    # whether the report came from a real account (with the email needed to
    # follow up) or an anonymous guest, without opening each row.
    list_display = ('id', 'reported_by', 'issue_summary', 'model_key', 'status', 'created_at')
    list_editable = ('status',)
    list_filter = ('status', 'model_key', 'created_at')
    search_fields = (
        'explanation', 'reported_reply', 'user_prompt', 'user_document_name', 'user__username',
        'user__email', 'session_key', 'conversation__title',
    )
    list_select_related = ('user', 'conversation')
    readonly_fields = (
        'conversation', 'message', 'user_prompt', 'user_image',
        'user_document_name', 'user_document_excerpt',
        'reported_reply', 'reported_image', 'model_key',
        'user', 'session_key', 'ip_address', 'created_at',
    )

    def reported_by(self, obj):
        if obj.user:
            return f"{obj.user.get_full_name() or obj.user.username} ({obj.user.email or 'no email'})"
        return f'Guest ({obj.session_key[:10]}…)' if obj.session_key else 'Guest'
    reported_by.short_description = 'Reported by'

    def issue_summary(self, obj):
        text = obj.explanation.strip()
        return (text[:60] + '…') if len(text) > 60 else (text or '(no explanation given)')
    issue_summary.short_description = 'Issue'


@admin.register(KnowledgeEntry)
class KnowledgeEntryAdmin(admin.ModelAdmin):
    # Legacy data from the removed EduTrellis Light feature — see
    # KnowledgeEntry's docstring. Kept browsable here only; no AI reply
    # reads from this table any more. visible_to makes it obvious whether an
    # entry was shared with everyone or private to one person.
    list_display = ('topic', 'source', 'visible_to', 'updated_at')
    list_select_related = ('user',)
    list_filter = ('source',)
    search_fields = ('topic', 'content', 'user__username', 'user__email', 'session_key')

    def visible_to(self, obj):
        if obj.user:
            return obj.user.get_full_name() or obj.user.username
        if obj.session_key:
            return f'Guest ({obj.session_key[:10]}…)'
        return 'Everyone'
    visible_to.short_description = 'Visible to'


@admin.register(GitHubConnection)
class GitHubConnectionAdmin(admin.ModelAdmin):
    # access_token deliberately left out of list_display so it's never
    # casually visible while scanning the list view.
    list_display = ('user', 'github_username', 'repo_full_name', 'default_branch', 'connected_at')
    search_fields = ('user__username', 'github_username', 'repo_full_name')


@admin.register(AISiteSettings)
class AISiteSettingsAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'ai_brand_name', 'updated_at')

    def has_add_permission(self, request):
        return not AISiteSettings.objects.exists()


@admin.register(AIAccountMessageSettings)
class AIAccountMessageSettingsAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'updated_at')

    def has_add_permission(self, request):
        return not AIAccountMessageSettings.objects.exists()


@admin.register(AIAPIAccess)
class AIAPIAccessAdmin(admin.ModelAdmin):
    list_display = ('user', 'model_keys', 'granted_by', 'updated_at')
    search_fields = ('user__username', 'user__email')


@admin.register(AIAPIKey)
class AIAPIKeyAdmin(admin.ModelAdmin):
    # key_hash deliberately left out of list_display/readonly view exposure
    # beyond the prefix — the raw key is one-time-shown only, never stored.
    list_display = ('user', 'key_prefix', 'created_at', 'last_used_at')
    search_fields = ('user__username', 'user__email', 'key_prefix')
    readonly_fields = ('key_hash', 'key_prefix', 'created_at', 'last_used_at')


@admin.register(ProviderAPICredential)
class ProviderAPICredentialAdmin(admin.ModelAdmin):
    # value deliberately left out of list_display so a secret isn't casually
    # visible while scanning the list view.
    list_display = ('setting_name', 'updated_at', 'updated_by')
    search_fields = ('setting_name',)
