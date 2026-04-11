"""
Management command to create sample data for Django Ergo.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from django_ergo.models import Article
from django_ergo.models import Knowledgebase
from django_ergo.models import UserChat
from django_ergo.models import Workflow

User = get_user_model()


class Command(BaseCommand):
    help = "Create sample data for Django Ergo"

    def add_arguments(self, parser):
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Clear existing data before creating new data",
        )

    def handle(self, *args, **options):  # noqa: C901, PLR0912
        if options["clear"]:
            self.stdout.write("Clearing existing data...")
            UserChat.objects.all().delete()
            Article.objects.all().delete()
            Knowledgebase.objects.all().delete()
            Workflow.objects.all().delete()
            self.stdout.write(self.style.SUCCESS("Existing data cleared."))

        # Create or get admin user
        admin_user, created = User.objects.get_or_create(
            username="admin",
            defaults={
                "email": "admin@example.com",
                "is_staff": True,
                "is_superuser": True,
            },
        )
        if created:
            admin_user.set_password("admin123")
            admin_user.save()
            self.stdout.write(f"Created admin user: {admin_user.username}")
        else:
            self.stdout.write(f"Using existing admin user: {admin_user.username}")

        # Create sample workflow
        workflow, created = Workflow.objects.get_or_create(
            name="Personal Assistant",
            defaults={
                "description": "A personal assistant workflow for managing knowledge and tasks",
                "instructions": """You are a helpful personal assistant. You can help users:
1. Search their knowledge bases for information
2. Create new articles in their knowledge bases
3. Answer questions based on their stored knowledge
4. Provide general assistance

You have access to tools for searching and managing knowledge bases. Always be helpful and accurate.""",
                "tools_config": {
                    "enabled_tools": [
                        "search_user_kb",
                        "list_user_knowledgebases",
                        "get_kb_table_of_contents",
                        "get_article_by_hierarchy",
                        "create_article",
                    ]
                },
                "is_active": True,
            },
        )
        if created:
            self.stdout.write(f"Created workflow: {workflow.name}")
        else:
            self.stdout.write(f"Using existing workflow: {workflow.name}")

        # Create sample knowledge base
        kb, created = Knowledgebase.objects.get_or_create(
            name="Personal Knowledge",
            owner_id=str(admin_user.id),
            defaults={
                "description": "Personal knowledge base for storing important information"
            },
        )
        if created:
            self.stdout.write(f"Created knowledge base: {kb.name}")
        else:
            self.stdout.write(f"Using existing knowledge base: {kb.name}")

        # Associate workflow with knowledge base
        kb.workflows.add(workflow)

        # Create sample articles
        sample_articles = [
            {
                "hierarchy_code": "0",
                "title": "Getting Started with Django Ergo",
                "content": """Django Ergo is an AI knowledgebase toolkit for Django applications.

Key Features:
- Multi-tenant knowledge bases with hierarchical article organization
- AI-powered workflows with tool execution and approval systems
- Hybrid search combining text and semantic similarity
- OpenAI agent context serialization for pause/resume functionality
- Extensible tool system for custom functionality

The system uses a hexadecimal hierarchy code system where each article has a position like "0", "1", "A", "B", etc. for top-level articles, and "01", "02", "A1", "A2" for sub-articles.""",
            },
            {
                "hierarchy_code": "1",
                "title": "Workflow System",
                "content": """The workflow system in Django Ergo provides a framework for processing chat messages through AI agents.

Key Components:
- Workflow: Defines AI logic, tools, and instructions
- UserChat: Individual chat sessions owned by users
- ChatMessage: Typed messages supporting multiple roles (user, assistant, system, tool)
- WorkflowEngine: Processes messages through AI agents with tool execution

The system supports tool approval workflows where certain tools require user approval before execution, and workflow state can be persisted for pause/resume functionality.""",
            },
            {
                "hierarchy_code": "2",
                "title": "Knowledge Base Management",
                "content": """Knowledge bases in Django Ergo are hierarchical collections of articles with multi-tenant support.

Features:
- Owner-based access control using owner_id
- Hierarchical article organization with hex codes
- Automatic summary and embedding generation (when using PostgreSQL)
- Hybrid search combining full-text and semantic search
- Export/import capabilities for agentic processing

Articles can be searched semantically, browsed by hierarchy, or retrieved by specific codes. The system is optimized for both keyword and semantic search patterns.""",
            },
            {
                "hierarchy_code": "3",
                "title": "Tool System",
                "content": """The tool system provides extensible functionality for AI agents.

Built-in Tools:
- search_user_kb: Search user's knowledge bases
- search_garden_kb: Search garden-related knowledge bases
- get_kb_table_of_contents: Get knowledge base table of contents
- get_article_by_hierarchy: Retrieve specific articles
- list_user_knowledgebases: List user's knowledge bases
- create_article: Create new articles (requires approval)

Tools can be configured as read-only or require approval. The system supports both automatic execution and user approval workflows.""",
            },
        ]

        for article_data in sample_articles:
            article, created = Article.objects.get_or_create(
                knowledgebase=kb,
                hierarchy_code=article_data["hierarchy_code"],
                defaults={
                    "title": article_data["title"],
                    "content": article_data["content"],
                },
            )
            if created:
                self.stdout.write(
                    f"Created article: {article.hierarchy_code} - {article.title}"
                )
            else:
                self.stdout.write(
                    f"Using existing article: {article.hierarchy_code} - {article.title}"
                )

        # Create sample chat
        chat, created = UserChat.objects.get_or_create(
            user=admin_user,
            workflow=workflow,
            title="Sample Chat Session",
            defaults={
                "is_active": True,
                "metadata": {
                    "created_by": "sample_data_command",
                    "purpose": "demonstration",
                },
            },
        )
        if created:
            self.stdout.write(f"Created chat: {chat.title}")
        else:
            self.stdout.write(f"Using existing chat: {chat.title}")

        # Create a garden knowledge base for demonstration
        garden_kb, created = Knowledgebase.objects.get_or_create(
            name="Garden Knowledge",
            defaults={
                "description": "Knowledge base for gardening and plant care information"
            },
        )
        if created:
            self.stdout.write(f"Created garden knowledge base: {garden_kb.name}")
        else:
            self.stdout.write(f"Using existing garden knowledge base: {garden_kb.name}")

        # Add sample garden articles
        garden_articles = [
            {
                "hierarchy_code": "0",
                "title": "Tomato Growing Guide",
                "content": """Tomatoes are warm-season crops that require full sun and well-drained soil.

Planting:
- Start seeds indoors 6-8 weeks before last frost
- Transplant after soil temperature reaches 60°F
- Space plants 24-36 inches apart

Care:
- Water deeply but infrequently
- Mulch around plants to retain moisture
- Stake or cage tall varieties
- Remove suckers for better fruit production

Common varieties: Roma, Cherry, Beefsteak, Heirloom""",
            },
            {
                "hierarchy_code": "1",
                "title": "Herb Garden Basics",
                "content": """Herbs are among the easiest plants to grow and provide fresh flavors for cooking.

Popular Culinary Herbs:
- Basil: Warm season, pinch flowers to keep leaves tender
- Rosemary: Perennial, drought tolerant once established
- Thyme: Low maintenance, good ground cover
- Oregano: Spreads easily, harvest before flowering
- Parsley: Biennial, prefers partial shade

Most herbs prefer well-drained soil and moderate watering. Many can be grown in containers.""",
            },
        ]

        for article_data in garden_articles:
            article, created = Article.objects.get_or_create(
                knowledgebase=garden_kb,
                hierarchy_code=article_data["hierarchy_code"],
                defaults={
                    "title": article_data["title"],
                    "content": article_data["content"],
                },
            )
            if created:
                self.stdout.write(
                    f"Created garden article: {article.hierarchy_code} - {article.title}"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nSample data created successfully!\n"
                f"- Admin user: admin (password: admin123)\n"
                f"- Workflow: {workflow.name}\n"
                f"- Knowledge bases: {kb.name}, {garden_kb.name}\n"
                f"- Articles: {Article.objects.count()} total\n"
                f"- Chat: {chat.title}\n\n"
                f"You can now:\n"
                f'1. Run "python manage.py runserver" to start the development server\n'
                f"2. Visit http://127.0.0.1:8000/admin/ to access the Django admin\n"
                f'3. Login with username "admin" and password "admin123"\n'
            )
        )
