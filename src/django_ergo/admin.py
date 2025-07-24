from django.contrib import admin
from django.utils.html import format_html
from django_ergo.models import (
    Workflow, Knowledgebase, Article, UserChat, ChatMessage
)


@admin.register(Workflow)
class WorkflowAdmin(admin.ModelAdmin):
    """Admin interface for Workflow model."""
    
    list_display = ['name', 'is_active', 'knowledgebase_count', 'chat_count', 'created_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'description']
    readonly_fields = ['created_at', 'updated_at']
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'description', 'is_active')
        }),
        ('Configuration', {
            'fields': ('instructions', 'tools_config')
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    
    def knowledgebase_count(self, obj):
        """Display count of associated knowledge bases."""
        return obj.knowledgebases.count()
    knowledgebase_count.short_description = 'Knowledge Bases'
    
    def chat_count(self, obj):
        """Display count of associated chats."""
        return obj.chats.count()
    chat_count.short_description = 'Chats'


@admin.register(Knowledgebase)
class KnowledgebaseAdmin(admin.ModelAdmin):
    """Admin interface for Knowledgebase model."""
    
    list_display = ['name', 'owner_id', 'article_count', 'workflow_count', 'created_at']
    list_filter = ['created_at', 'updated_at']
    search_fields = ['name', 'description', 'owner_id']
    readonly_fields = ['created_at', 'updated_at']
    filter_horizontal = ['workflows']
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'description', 'owner_id')
        }),
        ('Associations', {
            'fields': ('workflows',)
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    
    def article_count(self, obj):
        """Display count of articles in this knowledge base."""
        return obj.articles.count()
    article_count.short_description = 'Articles'
    
    def workflow_count(self, obj):
        """Display count of associated workflows."""
        return obj.workflows.count()
    workflow_count.short_description = 'Workflows'


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    """Admin interface for Article model."""
    
    list_display = ['hierarchy_code', 'title_truncated', 'knowledgebase', 'has_summary', 'created_at']
    list_filter = ['knowledgebase', 'created_at']
    search_fields = ['title', 'content', 'hierarchy_code']
    readonly_fields = ['summary', 'created_at', 'updated_at']
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('knowledgebase', 'hierarchy_code', 'title')
        }),
        ('Content', {
            'fields': ('content',)
        }),
        ('Generated Fields', {
            'fields': ('summary',),
            'classes': ('collapse',),
            'description': 'These fields are automatically generated when content changes.'
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    
    def title_truncated(self, obj):
        """Display truncated title."""
        if len(obj.title) > 50:
            return obj.title[:47] + "..."
        return obj.title
    title_truncated.short_description = 'Title'
    
    def has_summary(self, obj):
        """Display whether article has summary."""
        if obj.summary:
            return format_html('<span style="color: green;">✓</span>')
        return format_html('<span style="color: red;">✗</span>')
    has_summary.short_description = 'Summary'
    has_summary.admin_order_field = 'summary'


@admin.register(UserChat)
class UserChatAdmin(admin.ModelAdmin):
    """Admin interface for UserChat model."""
    
    list_display = ['title_truncated', 'user', 'workflow', 'is_active', 'message_count', 'updated_at']
    list_filter = ['is_active', 'workflow', 'created_at', 'updated_at']
    search_fields = ['title', 'user__username', 'user__email']
    readonly_fields = ['created_at', 'updated_at']
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('user', 'workflow', 'title', 'is_active')
        }),
        ('Configuration', {
            'fields': ('metadata', 'workflow_state'),
            'classes': ('collapse',)
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    
    def title_truncated(self, obj):
        """Display truncated title."""
        if len(obj.title) > 30:
            return obj.title[:27] + "..."
        return obj.title
    title_truncated.short_description = 'Title'
    
    def message_count(self, obj):
        """Display count of messages in this chat."""
        return obj.messages.count()
    message_count.short_description = 'Messages'


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    """Admin interface for ChatMessage model."""
    
    list_display = ['chat_title', 'message_type', 'role', 'content_truncated', 'has_metadata', 'created_at']
    list_filter = ['message_type', 'role', 'created_at']
    search_fields = ['content', 'chat__title', 'chat__user__username']
    readonly_fields = ['created_at', 'updated_at']
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('chat', 'message_type', 'role')
        }),
        ('Content', {
            'fields': ('content',)
        }),
        ('Advanced', {
            'fields': ('metadata', 'agent_context'),
            'classes': ('collapse',)
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    
    def chat_title(self, obj):
        """Display chat title."""
        return obj.chat.title
    chat_title.short_description = 'Chat'
    chat_title.admin_order_field = 'chat__title'
    
    def content_truncated(self, obj):
        """Display truncated content."""
        if len(obj.content) > 50:
            return obj.content[:47] + "..."
        return obj.content
    content_truncated.short_description = 'Content'
    
    def has_metadata(self, obj):
        """Display whether message has metadata."""
        if obj.metadata:
            return format_html('<span style="color: green;">✓</span>')
        return format_html('<span style="color: red;">✗</span>')
    has_metadata.short_description = 'Metadata'


# Custom admin site configuration
admin.site.site_header = "Django Ergo Administration"
admin.site.site_title = "Django Ergo Admin"
admin.site.index_title = "Welcome to Django Ergo Administration"
