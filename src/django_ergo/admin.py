from datetime import timedelta

from django.contrib import admin
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.template.response import TemplateResponse
from django.urls import path
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from django_ergo.models import Article
from django_ergo.models import ChatMessage
from django_ergo.models import Knowledgebase
from django_ergo.models import UserChat
from django_ergo.models import Workflow


@admin.register(Workflow)
class WorkflowAdmin(admin.ModelAdmin):
    """Admin interface for Workflow model with execution monitoring."""

    list_display = [
        "name",
        "is_active",
        "knowledgebase_count",
        "chat_count",
        "active_chats",
        "recent_activity",
        "execution_status",
        "created_at",
    ]
    list_filter = ["is_active", "created_at"]
    search_fields = ["name", "description"]
    readonly_fields = ["created_at", "updated_at"]

    fieldsets = (
        ("Basic Information", {"fields": ("name", "description", "is_active")}),
        ("Configuration", {"fields": ("instructions", "tools_config")}),
        (
            "Execution Monitoring",
            {
                "fields": ("execution_monitoring_info",),
                "classes": ("collapse",),
                "description": "View workflow execution statistics and monitoring data.",
            },
        ),
        (
            "Metadata",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    def get_urls(self):
        """Add custom URLs for workflow monitoring."""
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:workflow_id>/monitor/",
                self.admin_site.admin_view(self.monitor_workflow),
                name="django_ergo_workflow_monitor",
            ),
            path(
                "<int:workflow_id>/execution_stats/",
                self.admin_site.admin_view(self.workflow_execution_stats),
                name="django_ergo_workflow_execution_stats",
            ),
        ]
        return custom_urls + urls

    @admin.display(description="Knowledge Bases")
    def knowledgebase_count(self, obj):
        """Display count of associated knowledge bases."""
        return obj.knowledgebases.count()

    @admin.display(description="Total Chats")
    def chat_count(self, obj):
        """Display count of associated chats."""
        return obj.chats.count()

    @admin.display(description="Active Chats")
    def active_chats(self, obj):
        """Display count of active chats."""
        active_count = obj.chats.filter(is_active=True).count()
        if active_count > 0:
            return format_html('<span style="color: green;">{}</span>', active_count)
        return format_html('<span style="color: gray;">0</span>')

    @admin.display(description="Recent Activity")
    def recent_activity(self, obj):
        """Display recent activity indicator."""
        recent_threshold = timezone.now() - timedelta(hours=24)
        recent_messages = ChatMessage.objects.filter(
            chat__workflow=obj, created_at__gte=recent_threshold
        ).count()

        if recent_messages > 0:
            return format_html(
                '<span style="color: green;" title="{} messages in last 24h">🟢 Active</span>',
                recent_messages,
            )
        return format_html('<span style="color: gray;">🔘 Quiet</span>')

    @admin.display(description="Execution Monitoring")
    def execution_status(self, obj):
        """Display workflow execution status with monitoring link."""
        monitor_url = reverse("admin:django_ergo_workflow_monitor", args=[obj.pk])
        return format_html('<a href="{}" class="button">📊 Monitor</a>', monitor_url)

    @admin.display(description="")
    def execution_monitoring_info(self, obj):
        """Display execution monitoring information in the form."""
        if obj.pk:
            monitor_url = reverse("admin:django_ergo_workflow_monitor", args=[obj.pk])
            stats_url = reverse(
                "admin:django_ergo_workflow_execution_stats", args=[obj.pk]
            )

            return format_html(
                '<div style="background: #f8f9fa; padding: 15px; border-radius: 5px;">'
                "<h4>Workflow Execution Monitoring</h4>"
                "<p><strong>Active Chats:</strong> {}</p>"
                "<p><strong>Total Messages:</strong> {}</p>"
                "<p><strong>Recent Activity:</strong> {} messages in last 24h</p>"
                '<div style="margin-top: 10px;">'
                '<a href="{}" class="button default">📊 Full Monitor Dashboard</a> '
                '<a href="{}" class="button" target="_blank">📈 Execution Stats (JSON)</a>'
                "</div>"
                "</div>",
                obj.chats.filter(is_active=True).count(),
                ChatMessage.objects.filter(chat__workflow=obj).count(),
                ChatMessage.objects.filter(
                    chat__workflow=obj,
                    created_at__gte=timezone.now() - timedelta(hours=24),
                ).count(),
                monitor_url,
                stats_url,
            )
        return "Save the workflow to view execution monitoring information."

    def monitor_workflow(self, request, workflow_id):
        """Custom view for workflow execution monitoring."""
        workflow = get_object_or_404(Workflow, pk=workflow_id)

        # Get execution statistics
        total_chats = workflow.chats.count()
        active_chats = workflow.chats.filter(is_active=True).count()
        total_messages = ChatMessage.objects.filter(chat__workflow=workflow).count()

        # Recent activity (last 7 days)
        week_ago = timezone.now() - timedelta(days=7)
        recent_messages = (
            ChatMessage.objects.filter(
                chat__workflow=workflow, created_at__gte=week_ago
            )
            .values("created_at__date")
            .annotate(count=Count("id"))
            .order_by("created_at__date")
        )

        # Message type distribution
        message_types = (
            ChatMessage.objects.filter(chat__workflow=workflow)
            .values("message_type")
            .annotate(count=Count("id"))
        )

        # Tool usage statistics
        tool_messages = ChatMessage.objects.filter(
            chat__workflow=workflow, message_type="tool_response"
        ).count()

        approval_requests = ChatMessage.objects.filter(
            chat__workflow=workflow, message_type="tool_approval_request"
        ).count()

        context = {
            "workflow": workflow,
            "title": f"Workflow Monitoring: {workflow.name}",
            "opts": self.model._meta,  # noqa: SLF001
            "has_view_permission": True,
            "statistics": {
                "total_chats": total_chats,
                "active_chats": active_chats,
                "total_messages": total_messages,
                "tool_executions": tool_messages,
                "approval_requests": approval_requests,
            },
            "recent_activity": list(recent_messages),
            "message_types": list(message_types),
        }

        return render(request, "admin/django_ergo/workflow_monitor.html", context)

    def workflow_execution_stats(self, request, workflow_id):
        """API endpoint for workflow execution statistics."""
        workflow = get_object_or_404(Workflow, pk=workflow_id)

        stats = {
            "workflow_id": workflow.id,
            "workflow_name": workflow.name,
            "is_active": workflow.is_active,
            "total_chats": workflow.chats.count(),
            "active_chats": workflow.chats.filter(is_active=True).count(),
            "total_messages": ChatMessage.objects.filter(
                chat__workflow=workflow
            ).count(),
            "message_type_counts": dict(
                ChatMessage.objects.filter(chat__workflow=workflow)
                .values_list("message_type")
                .annotate(count=Count("id"))
            ),
            "recent_activity": {
                "last_24h": ChatMessage.objects.filter(
                    chat__workflow=workflow,
                    created_at__gte=timezone.now() - timedelta(hours=24),
                ).count(),
                "last_week": ChatMessage.objects.filter(
                    chat__workflow=workflow,
                    created_at__gte=timezone.now() - timedelta(days=7),
                ).count(),
            },
        }

        return JsonResponse(stats)


@admin.register(Knowledgebase)
class KnowledgebaseAdmin(admin.ModelAdmin):
    """Admin interface for Knowledgebase model with enhanced management tools."""

    list_display = [
        "name",
        "owner_id",
        "article_count",
        "workflow_count",
        "content_size",
        "last_updated",
        "management_actions",
        "created_at",
    ]
    list_filter = ["created_at", "updated_at"]
    search_fields = ["name", "description", "owner_id"]
    readonly_fields = ["created_at", "updated_at"]
    filter_horizontal = ["workflows"]

    fieldsets = (
        ("Basic Information", {"fields": ("name", "description", "owner_id")}),
        (
            "Organization Strategy",
            {
                "fields": ("organization_strategy",),
                "classes": ("collapse",),
                "description": "Describes the hierarchy layout — what each tree prefix means, how articles should be organized.",
            },
        ),
        ("Associations", {"fields": ("workflows",)}),
        (
            "Knowledge Base Management",
            {
                "fields": ("kb_management_info",),
                "classes": ("collapse",),
                "description": "Knowledge base management tools and analytics.",
            },
        ),
        (
            "Metadata",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    def get_urls(self):
        """Add custom URLs for knowledge base management."""
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:kb_id>/manage/",
                self.admin_site.admin_view(self.manage_knowledgebase),
                name="django_ergo_knowledgebase_manage",
            ),
            path(
                "<int:kb_id>/analytics/",
                self.admin_site.admin_view(self.kb_analytics),
                name="django_ergo_knowledgebase_analytics",
            ),
            path(
                "<int:kb_id>/export/",
                self.admin_site.admin_view(self.export_kb),
                name="django_ergo_knowledgebase_export",
            ),
        ]
        return custom_urls + urls

    @admin.display(description="Articles")
    def article_count(self, obj):
        """Display count of articles in this knowledge base."""
        count = obj.articles.count()
        if count > 0:
            return format_html('<span style="color: green;">{}</span>', count)
        return format_html('<span style="color: gray;">0</span>')

    @admin.display(description="Workflows")
    def workflow_count(self, obj):
        """Display count of associated workflows."""
        return obj.workflows.count()

    @admin.display(description="Content Size")
    def content_size(self, obj):
        """Display approximate content size."""
        total_chars = sum(
            len(article.content or "") + len(article.title or "")
            for article in obj.articles.all()
        )

        if total_chars > 1_000_000:  # noqa: PLR2004
            return format_html("{:.1f}M chars", total_chars / 1_000_000)
        if total_chars > 1000:  # noqa: PLR2004
            return format_html("{:.1f}K chars", total_chars / 1000)
        return f"{total_chars} chars"

    @admin.display(description="Last Updated")
    def last_updated(self, obj):
        """Display when the knowledge base was last updated."""
        latest_article = obj.articles.order_by("-updated_at").first()
        if latest_article:
            return latest_article.updated_at
        return obj.updated_at

    @admin.display(description="Management")
    def management_actions(self, obj):
        """Display management action buttons."""
        manage_url = reverse("admin:django_ergo_knowledgebase_manage", args=[obj.pk])
        analytics_url = reverse(
            "admin:django_ergo_knowledgebase_analytics", args=[obj.pk]
        )

        return format_html(
            '<a href="{}" class="button">🔧 Manage</a> '
            '<a href="{}" class="button">📊 Analytics</a>',
            manage_url,
            analytics_url,
        )

    @admin.display(description="")
    def kb_management_info(self, obj):
        """Display knowledge base management information."""
        if obj.pk:
            manage_url = reverse(
                "admin:django_ergo_knowledgebase_manage", args=[obj.pk]
            )
            analytics_url = reverse(
                "admin:django_ergo_knowledgebase_analytics", args=[obj.pk]
            )
            export_url = reverse(
                "admin:django_ergo_knowledgebase_export", args=[obj.pk]
            )

            return format_html(
                '<div style="background: #f8f9fa; padding: 15px; border-radius: 5px;">'
                "<h4>Knowledge Base Management</h4>"
                "<p><strong>Articles:</strong> {}</p>"
                "<p><strong>Total Content:</strong> {}</p>"
                "<p><strong>Associated Workflows:</strong> {}</p>"
                '<div style="margin-top: 10px;">'
                '<a href="{}" class="button default">🔧 Manage KB</a> '
                '<a href="{}" class="button">📊 Analytics</a> '
                '<a href="{}" class="button">📁 Export</a>'
                "</div>"
                "</div>",
                obj.articles.count(),
                self.content_size(obj),
                obj.workflows.count(),
                manage_url,
                analytics_url,
                export_url,
            )
        return "Save the knowledge base to view management tools."

    def manage_knowledgebase(self, request, kb_id):
        """Custom view for knowledge base management."""
        kb = get_object_or_404(Knowledgebase, pk=kb_id)

        # Get management statistics
        articles = kb.articles.all()
        total_articles = articles.count()

        # Content analysis
        articles_with_summaries = (
            articles.exclude(summary__isnull=True).exclude(summary="").count()
        )

        # Hierarchy analysis
        hierarchy_stats = {}
        for article in articles:
            level = len(article.hierarchy_code)
            hierarchy_stats[level] = hierarchy_stats.get(level, 0) + 1

        context = {
            "knowledgebase": kb,
            "title": f"Manage Knowledge Base: {kb.name}",
            "opts": self.model._meta,  # noqa: SLF001
            "has_change_permission": True,
            "statistics": {
                "total_articles": total_articles,
                "articles_with_summaries": articles_with_summaries,
                "summary_coverage": (articles_with_summaries / total_articles * 100)
                if total_articles > 0
                else 0,
                "hierarchy_levels": hierarchy_stats,
            },
            "recent_articles": articles.order_by("-updated_at")[:10],
        }

        return render(request, "admin/django_ergo/knowledgebase_manage.html", context)

    def kb_analytics(self, request, kb_id):
        """API endpoint for knowledge base analytics."""
        kb = get_object_or_404(Knowledgebase, pk=kb_id)

        articles = kb.articles.all()

        analytics = {
            "knowledgebase_id": kb.id,
            "knowledgebase_name": kb.name,
            "total_articles": articles.count(),
            "articles_with_summaries": articles.exclude(summary__isnull=True)
            .exclude(summary="")
            .count(),
            "content_analysis": {
                "total_characters": sum(len(a.content or "") for a in articles),
                "average_article_length": sum(len(a.content or "") for a in articles)
                / articles.count()
                if articles.count() > 0
                else 0,
                "hierarchy_distribution": dict(
                    articles.values_list("hierarchy_code").annotate(count=Count("id"))
                ),
            },
            "workflow_usage": [
                {"workflow_id": w.id, "workflow_name": w.name, "is_active": w.is_active}
                for w in kb.workflows.all()
            ],
        }

        return JsonResponse(analytics)

    def export_kb(self, request, kb_id):
        """Export knowledge base data."""
        kb = get_object_or_404(Knowledgebase, pk=kb_id)

        export_data = {
            "knowledgebase": {
                "id": str(kb.id),
                "name": kb.name,
                "description": kb.description,
                "owner_id": kb.owner_id,
                "created_at": kb.created_at.isoformat(),
                "updated_at": kb.updated_at.isoformat(),
            },
            "articles": [
                {
                    "id": str(article.id),
                    "title": article.title,
                    "content": article.content,
                    "summary": article.summary,
                    "hierarchy_code": article.hierarchy_code,
                    "created_at": article.created_at.isoformat(),
                    "updated_at": article.updated_at.isoformat(),
                }
                for article in kb.articles.all().order_by("hierarchy_code")
            ],
        }

        return JsonResponse(export_data, json_dumps_params={"indent": 2})


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    """Admin interface for Article model."""

    list_display = [
        "hierarchy_code",
        "title_truncated",
        "knowledgebase",
        "has_summary",
        "content_length",
        "created_at",
    ]
    list_filter = ["knowledgebase", "created_at"]
    search_fields = ["title", "content", "hierarchy_code"]
    readonly_fields = ["summary", "created_at", "updated_at"]

    fieldsets = (
        ("Basic Information", {"fields": ("knowledgebase", "hierarchy_code", "title")}),
        ("Content", {"fields": ("content",)}),
        (
            "Generated Fields",
            {
                "fields": ("summary",),
                "classes": ("collapse",),
                "description": "These fields are automatically generated when content changes.",
            },
        ),
        (
            "Metadata",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    @admin.display(description="Title")
    def title_truncated(self, obj):
        """Display truncated title."""
        if len(obj.title) > 50:  # noqa: PLR2004
            return obj.title[:47] + "..."
        return obj.title

    @admin.display(
        description="Summary",
        ordering="summary",
    )
    def has_summary(self, obj):
        """Display whether article has summary."""
        if obj.summary:
            return format_html('<span style="color: green;">✓</span>')
        return format_html('<span style="color: red;">✗</span>')

    @admin.display(description="Content Length")
    def content_length(self, obj):
        """Display content length."""
        length = len(obj.content or "")
        if length > 1000:  # noqa: PLR2004
            return format_html("{:.1f}K chars", length / 1000)
        return f"{length} chars"


@admin.register(UserChat)
class UserChatAdmin(admin.ModelAdmin):
    """Admin interface for UserChat model with enhanced chat history viewing."""

    list_display = [
        "title_truncated",
        "user",
        "workflow",
        "is_active",
        "message_count",
        "last_activity",
        "chat_actions",
        "updated_at",
    ]
    list_filter = ["is_active", "workflow", "created_at", "updated_at"]
    search_fields = ["title", "user__username", "user__email"]
    readonly_fields = ["created_at", "updated_at"]

    fieldsets = (
        ("Basic Information", {"fields": ("user", "workflow", "title", "is_active")}),
        (
            "Chat History Management",
            {
                "fields": ("chat_history_info",),
                "classes": ("collapse",),
                "description": "View and manage chat conversation history.",
            },
        ),
        (
            "Configuration",
            {"fields": ("metadata", "workflow_state"), "classes": ("collapse",)},
        ),
        (
            "Metadata",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    def get_urls(self):
        """Add custom URLs for chat history management."""
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:chat_id>/history/",
                self.admin_site.admin_view(self.view_chat_history),
                name="django_ergo_userchat_history",
            ),
            path(
                "<int:chat_id>/export_history/",
                self.admin_site.admin_view(self.export_chat_history),
                name="django_ergo_userchat_export_history",
            ),
        ]
        return custom_urls + urls

    @admin.display(description="Title")
    def title_truncated(self, obj):
        """Display truncated title."""
        if len(obj.title) > 30:  # noqa: PLR2004
            return obj.title[:27] + "..."
        return obj.title

    @admin.display(description="Messages")
    def message_count(self, obj):
        """Display count of messages in this chat."""
        count = obj.messages.count()
        if count > 0:
            return format_html('<span style="color: green;">{}</span>', count)
        return format_html('<span style="color: gray;">0</span>')

    @admin.display(description="Last Activity")
    def last_activity(self, obj):
        """Display last activity time."""
        last_message = obj.messages.order_by("-created_at").first()
        if last_message:
            return last_message.created_at
        return obj.updated_at

    @admin.display(description="Actions")
    def chat_actions(self, obj):
        """Display chat action buttons."""
        history_url = reverse("admin:django_ergo_userchat_history", args=[obj.pk])
        export_url = reverse("admin:django_ergo_userchat_export_history", args=[obj.pk])

        return format_html(
            '<a href="{}" class="button">💬 View History</a> '
            '<a href="{}" class="button">📁 Export</a>',
            history_url,
            export_url,
        )

    @admin.display(description="")
    def chat_history_info(self, obj):
        """Display chat history management information."""
        if obj.pk:
            history_url = reverse("admin:django_ergo_userchat_history", args=[obj.pk])
            export_url = reverse(
                "admin:django_ergo_userchat_export_history", args=[obj.pk]
            )

            messages = obj.messages.all()
            message_types = messages.values("message_type").annotate(count=Count("id"))

            return format_html(
                '<div style="background: #f8f9fa; padding: 15px; border-radius: 5px;">'
                "<h4>Chat History Overview</h4>"
                "<p><strong>Total Messages:</strong> {}</p>"
                "<p><strong>Message Types:</strong> {}</p>"
                "<p><strong>Last Activity:</strong> {}</p>"
                '<div style="margin-top: 10px;">'
                '<a href="{}" class="button default">💬 View Full History</a> '
                '<a href="{}" class="button">📁 Export Chat</a>'
                "</div>"
                "</div>",
                messages.count(),
                ", ".join(
                    f'{mt["message_type"]}: {mt["count"]}' for mt in message_types
                ),
                self.last_activity(obj),
                history_url,
                export_url,
            )
        return "Save the chat to view history management tools."

    def view_chat_history(self, request, chat_id):
        """Custom view for chat history."""
        chat = get_object_or_404(UserChat, pk=chat_id)

        messages = chat.messages.all().order_by("created_at")

        # Message statistics
        message_stats = {
            "total": messages.count(),
            "by_type": dict(
                messages.values_list("message_type").annotate(count=Count("id"))
            ),
            "by_role": dict(messages.values_list("role").annotate(count=Count("id"))),
        }

        context = {
            "userchat": chat,
            "title": f"Chat History: {chat.title}",
            "opts": self.model._meta,  # noqa: SLF001
            "has_view_permission": True,
            "messages": messages,
            "message_stats": message_stats,
        }

        return render(request, "admin/django_ergo/chat_history.html", context)

    def export_chat_history(self, request, chat_id):
        """Export chat history."""
        chat = get_object_or_404(UserChat, pk=chat_id)

        export_data = {
            "chat": {
                "id": str(chat.id),
                "title": chat.title,
                "user": chat.user.username,
                "workflow": chat.workflow.name,
                "is_active": chat.is_active,
                "created_at": chat.created_at.isoformat(),
                "updated_at": chat.updated_at.isoformat(),
            },
            "messages": [
                {
                    "id": str(msg.id),
                    "message_type": msg.message_type,
                    "role": msg.role,
                    "content": msg.content,
                    "metadata": msg.metadata,
                    "created_at": msg.created_at.isoformat(),
                }
                for msg in chat.messages.all().order_by("created_at")
            ],
        }

        return JsonResponse(export_data, json_dumps_params={"indent": 2})


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    """Admin interface for ChatMessage model."""

    list_display = [
        "chat_title",
        "message_type",
        "role",
        "content_truncated",
        "has_metadata",
        "created_at",
    ]
    list_filter = ["message_type", "role", "created_at"]
    search_fields = ["content", "chat__title", "chat__user__username"]
    readonly_fields = ["created_at", "updated_at"]

    fieldsets = (
        ("Basic Information", {"fields": ("chat", "message_type", "role")}),
        ("Content", {"fields": ("content",)}),
        (
            "Advanced",
            {"fields": ("metadata", "agent_context"), "classes": ("collapse",)},
        ),
        (
            "Metadata",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    @admin.display(
        description="Chat",
        ordering="chat__title",
    )
    def chat_title(self, obj):
        """Display chat title."""
        return obj.chat.title

    @admin.display(description="Content")
    def content_truncated(self, obj):
        """Display truncated content."""
        if len(obj.content) > 50:  # noqa: PLR2004
            return obj.content[:47] + "..."
        return obj.content

    @admin.display(description="Metadata")
    def has_metadata(self, obj):
        """Display whether message has metadata."""
        if obj.metadata:
            return format_html('<span style="color: green;">✓</span>')
        return format_html('<span style="color: red;">✗</span>')


# ---------------------------------------------------------------------------
# Conversation admin (ConversationSession, ClaudeMessage, ClaudeContentBlock)
# ---------------------------------------------------------------------------

try:
    from django_ergo.conversation.models import ClaudeContentBlock
    from django_ergo.conversation.models import ClaudeMessage
    from django_ergo.conversation.models import ConversationSession

    class ClaudeMessageInline(admin.TabularInline):
        model = ClaudeMessage
        extra = 0
        fields = [
            "sequence",
            "role",
            "stop_reason",
            "model_name",
            "input_tokens",
            "output_tokens",
        ]
        readonly_fields = fields
        ordering = ["sequence"]
        show_change_link = True

        def has_add_permission(self, request, obj=None):
            return False

        def has_delete_permission(self, request, obj=None):
            return False

    class ClaudeContentBlockInline(admin.TabularInline):
        model = ClaudeContentBlock
        extra = 0
        fields = ["sequence", "block_type", "content_preview"]
        readonly_fields = fields
        ordering = ["sequence"]

        def has_add_permission(self, request, obj=None):
            return False

        def has_delete_permission(self, request, obj=None):
            return False

        @admin.display(description="Content")
        def content_preview(self, obj):
            if obj.block_type == "text":
                return (obj.text or "")[:120]
            if obj.block_type == "thinking":
                return f"[thinking: {len(obj.thinking or '')} chars]"
            if obj.block_type == "tool_use":
                return f"[{obj.tool_name}]"
            if obj.block_type == "tool_result":
                tag = " ERR" if obj.is_error else ""
                return f"[result{tag}: {len(str(obj.tool_result_content or ''))} chars]"
            return "—"

    @admin.register(ConversationSession)
    class ConversationSessionAdmin(admin.ModelAdmin):
        list_display = [
            "session_id_short",
            "user",
            "message_count",
            "engine_type",
            "status",
            "metadata_preview",
            "created_at",
        ]
        list_filter = ["engine_type", "status"]
        search_fields = ["session_id", "metadata"]
        readonly_fields = ["id", "created_at", "updated_at"]
        inlines = [ClaudeMessageInline]

        def get_urls(self):
            urls = super().get_urls()
            custom = [
                path(
                    "<path:object_id>/transcript/",
                    self.admin_site.admin_view(self.transcript_view),
                    name="django_ergo_conversationsession_transcript",
                ),
            ]
            return custom + urls

        @admin.display(description="Session")
        def session_id_short(self, obj):
            sid = obj.session_id or str(obj.id)
            url = reverse(
                "admin:django_ergo_conversationsession_transcript", args=[obj.pk]
            )
            return format_html('<a href="{}">{}</a>', url, sid[:12] + "...")

        @admin.display(description="Msgs")
        def message_count(self, obj):
            return obj.claude_messages.count()

        @admin.display(description="Info")
        def metadata_preview(self, obj):
            # Show project_name if present (from CLI import), otherwise first 80 chars
            project = obj.metadata.get("project_name", "")
            if project:
                return project
            summary = obj.metadata.get("summary", "")
            if summary:
                return summary[:100] + "..." if len(summary) > 100 else summary  # noqa: PLR2004
            keys = list(obj.metadata.keys())
            return ", ".join(keys[:4]) if keys else "—"

        def transcript_view(self, request, object_id):
            session = ConversationSession.objects.get(pk=object_id)
            messages = session.claude_messages.prefetch_related(
                "content_blocks"
            ).order_by("sequence")

            transcript = []
            for msg in messages:
                blocks = [
                    {
                        "type": block.block_type,
                        "text": block.text,
                        "thinking": block.thinking,
                        "tool_name": block.tool_name,
                        "tool_input": block.tool_input,
                        "tool_result_content": block.tool_result_content,
                        "is_error": block.is_error,
                    }
                    for block in msg.content_blocks.order_by("sequence")
                ]
                transcript.append(
                    {
                        "sequence": msg.sequence,
                        "role": msg.role,
                        "model": msg.model_name,
                        "input_tokens": msg.input_tokens,
                        "output_tokens": msg.output_tokens,
                        "stop_reason": msg.stop_reason,
                        "blocks": blocks,
                    }
                )

            title_parts = []
            if session.metadata.get("project_name"):
                title_parts.append(session.metadata["project_name"])
            title_parts.append(
                session.session_id[:12] if session.session_id else str(session.id)[:12]
            )

            context = {
                **self.admin_site.each_context(request),
                "title": f"Transcript: {' — '.join(title_parts)}",
                "session": session,
                "transcript": transcript,
                "summary": session.metadata.get("summary", ""),
                "opts": self.model._meta,  # noqa: SLF001
            }
            return TemplateResponse(
                request, "admin/django_ergo/conversation_transcript.html", context
            )

    @admin.register(ClaudeMessage)
    class ClaudeMessageAdmin(admin.ModelAdmin):
        list_display = [
            "session_short",
            "sequence",
            "role",
            "block_summary",
            "model_name",
            "token_info",
        ]
        list_filter = ["role", "model_name"]
        readonly_fields = ["created_at", "updated_at"]
        ordering = ["session", "sequence"]
        inlines = [ClaudeContentBlockInline]

        @admin.display(description="Session")
        def session_short(self, obj):
            sid = obj.session.session_id or str(obj.session.id)
            return sid[:12] + "..."

        @admin.display(description="Content")
        def block_summary(self, obj):
            blocks = obj.content_blocks.order_by("sequence")
            parts = []
            for b in blocks:
                if b.block_type == "text":
                    parts.append((b.text or "")[:80])
                elif b.block_type == "tool_use":
                    parts.append(f"[tool: {b.tool_name}]")
                elif b.block_type == "tool_result":
                    tag = " ERR" if b.is_error else ""
                    parts.append(f"[result{tag}]")
                elif b.block_type == "thinking":
                    parts.append("[thinking]")
            return " | ".join(parts)[:200] if parts else "—"

        @admin.display(description="Tokens")
        def token_info(self, obj):
            if obj.input_tokens or obj.output_tokens:
                return f"in:{obj.input_tokens or 0} out:{obj.output_tokens or 0}"
            return "—"

except ImportError:
    # Conversation models not available (e.g., migrations not run yet)
    pass


# Custom admin site configuration
admin.site.site_header = "Django Ergo Administration"
admin.site.site_title = "Django Ergo Admin"
admin.site.index_title = "Welcome to Django Ergo Administration"
