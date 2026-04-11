#!/usr/bin/env python3
"""
Example: User Chat with Workflows

This example demonstrates how to use the new user chat functionality in ergo:
1. Creating workflows with knowledgebases
2. Creating user chats
3. Processing messages through workflows
4. Accessing tools and knowledgebases

Run this example with:
    python manage.py shell < papa/apps/ergo/examples/chat_example.py
"""

import asyncio
import os

import django
from asgiref.sync import sync_to_async

# Setup Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "papa.settings.project")
django.setup()

from django.contrib.auth import get_user_model
from papa.apps.ergo.models import Article
from papa.apps.ergo.models import Knowledgebase
from papa.apps.ergo.models import MessageRole
from papa.apps.ergo.models import UserChat
from papa.apps.ergo.models import Workflow
from papa.apps.ergo.workflows import process_chat_message
from papa.apps.ergo.workflows import workflow_engine

User = get_user_model()


async def main():
    """Main example function."""
    print("🚀 Ergo User Chat Example")
    print("=" * 50)

    # Step 1: Create a test user
    print("\n1. Creating test user...")
    user, created = await sync_to_async(User.objects.get_or_create)(
        username="chat_example_user",
        defaults={
            "email": "chat@example.com",
            "first_name": "Chat",
            "last_name": "Example",
        },
    )
    if created:
        await sync_to_async(user.set_password)("example123")
        await sync_to_async(user.save)()
        print(f"   ✅ Created user: {user.username}")
    else:
        print(f"   ✅ Using existing user: {user.username}")

    # Step 2: Create knowledgebases with content
    print("\n2. Creating knowledgebases...")

    # Python knowledgebase
    python_kb, created = await sync_to_async(Knowledgebase.objects.get_or_create)(
        name="Python Programming",
        defaults={"description": "Information about Python programming language"},
    )
    if created:
        print(f"   ✅ Created knowledgebase: {python_kb.name}")
    else:
        print(f"   ✅ Using existing knowledgebase: {python_kb.name}")

    # Add Python articles
    python_articles = [
        {
            "title": "Python Basics",
            "content": "Python is a high-level, interpreted programming language known for its simplicity and readability. It supports multiple programming paradigms including procedural, object-oriented, and functional programming.",
            "hierarchy_code": "0",
        },
        {
            "title": "Python Data Structures",
            "content": "Python provides several built-in data structures: lists, tuples, dictionaries, and sets. Lists are mutable sequences, tuples are immutable sequences, dictionaries store key-value pairs, and sets store unique elements.",
            "hierarchy_code": "1",
        },
        {
            "title": "Python Functions",
            "content": "Functions in Python are defined using the 'def' keyword. They can take parameters, return values, and support default arguments, keyword arguments, and variable-length argument lists.",
            "hierarchy_code": "2",
        },
    ]

    for article_data in python_articles:
        article, created = await sync_to_async(Article.objects.get_or_create)(
            knowledgebase=python_kb,
            hierarchy_code=article_data["hierarchy_code"],
            defaults={
                "title": article_data["title"],
                "content": article_data["content"],
            },
        )
        if created:
            print(f"      ✅ Added article: {article.title}")

    # Django knowledgebase
    django_kb, created = await sync_to_async(Knowledgebase.objects.get_or_create)(
        name="Django Framework",
        defaults={"description": "Information about Django web framework"},
    )
    if created:
        print(f"   ✅ Created knowledgebase: {django_kb.name}")
    else:
        print(f"   ✅ Using existing knowledgebase: {django_kb.name}")

    # Add Django articles
    django_articles = [
        {
            "title": "Django Overview",
            "content": "Django is a high-level Python web framework that encourages rapid development and clean, pragmatic design. It follows the Model-View-Template (MVT) architectural pattern.",
            "hierarchy_code": "0",
        },
        {
            "title": "Django Models",
            "content": "Django models define the structure of your database tables. They are Python classes that inherit from django.db.models.Model and define fields that correspond to database columns.",
            "hierarchy_code": "1",
        },
        {
            "title": "Django Views",
            "content": "Django views are Python functions or classes that handle web requests and return web responses. They can render templates, return JSON data, or redirect to other URLs.",
            "hierarchy_code": "2",
        },
    ]

    for article_data in django_articles:
        article, created = await sync_to_async(Article.objects.get_or_create)(
            knowledgebase=django_kb,
            hierarchy_code=article_data["hierarchy_code"],
            defaults={
                "title": article_data["title"],
                "content": article_data["content"],
            },
        )
        if created:
            print(f"      ✅ Added article: {article.title}")

    # Step 3: Create a workflow
    print("\n3. Creating workflow...")
    workflow, created = await sync_to_async(Workflow.objects.get_or_create)(
        name="Programming Assistant",
        defaults={
            "description": "A helpful assistant for programming questions",
            "instructions": """You are a helpful programming assistant that can search knowledgebases and answer questions about Python and Django programming.

            When users ask questions:
            1. Search the available knowledgebases for relevant information
            2. Provide clear, helpful answers based on the search results
            3. If you don't find specific information, acknowledge that and offer general guidance
            4. Always be encouraging and supportive of the user's learning journey""",
            "tools_config": {
                "search_knowledgebase": {"enabled": True, "max_results": 5}
            },
        },
    )

    if created:
        # Associate knowledgebases with the workflow
        await sync_to_async(workflow.knowledgebases.add)(python_kb, django_kb)
        print(f"   ✅ Created workflow: {workflow.name}")
        print(
            f"      Associated knowledgebases: {', '.join(await sync_to_async(workflow.get_knowledgebases_list)())}"
        )
    else:
        print(f"   ✅ Using existing workflow: {workflow.name}")

    # Step 4: Create a user chat
    print("\n4. Creating user chat...")
    chat, created = await sync_to_async(UserChat.objects.get_or_create)(
        user=user,
        workflow=workflow,
        title="Programming Help Chat",
        defaults={"metadata": {"topic": "programming", "level": "beginner"}},
    )
    if created:
        print(f"   ✅ Created chat: {chat.title}")
    else:
        print(f"   ✅ Using existing chat: {chat.title}")

    # Step 5: Demonstrate message processing
    print("\n5. Processing messages...")

    # Test questions
    test_questions = [
        "What is Python?",
        "How do Django models work?",
        "What are Python data structures?",
        "Tell me about Django views",
    ]

    for i, question in enumerate(test_questions, 1):
        print(f"\n   Question {i}: {question}")
        print("   " + "-" * 40)

        # Process the message
        result = await process_chat_message(chat, question)

        if result.success:
            print(f"   ✅ Response: {result.content[:200]}...")
            if result.tool_calls:
                print(f"   🔧 Tools used: {len(result.tool_calls)}")
            if result.metadata.get("search_results_count"):
                print(
                    f"   📚 Articles found: {result.metadata['search_results_count']}"
                )
        else:
            print(f"   ❌ Error: {result.error}")

    # Step 6: Show conversation history
    print("\n6. Conversation history:")
    print("   " + "-" * 40)

    messages = await sync_to_async(chat.get_messages)()
    for i, message in enumerate(messages, 1):
        role_emoji = "👤" if message.role == MessageRole.USER else "🤖"
        print(f"   {i}. {role_emoji} {message.role}: {message.content[:100]}...")

    # Step 7: Demonstrate workflow tools
    print("\n7. Available workflow tools:")
    print("   " + "-" * 40)

    for tool_name, tool in workflow_engine.tools.items():
        schema = tool.get_schema()
        print(f"   🔧 {tool_name}: {schema['description']}")

    print("\n" + "=" * 50)
    print("✅ Example completed successfully!")
    print(f"   User: {user.username}")
    print(f"   Chat: {chat.title}")
    print(f"   Workflow: {workflow.name}")
    print(
        f"   Knowledgebases: {', '.join(await sync_to_async(workflow.get_knowledgebases_list)())}"
    )
    print(f"   Total messages: {len(messages)}")


if __name__ == "__main__":
    # Run the async example
    asyncio.run(main())
