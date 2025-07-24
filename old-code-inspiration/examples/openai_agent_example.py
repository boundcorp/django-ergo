#!/usr/bin/env python3
"""
Example: OpenAI Agents Integration with Ergo Chat

This example demonstrates how to use OpenAI Agents for LLM-powered responses
in the ergo chat system. It shows both the fallback behavior (when OpenAI Agents
is not available) and the LLM-powered behavior (when available).

Run this example with:
    python manage.py shell < papa/apps/ergo/examples/openai_agent_example.py

Note: This example will work even without OpenAI Agents installed - it will
fall back to the template-based responses.
"""

import asyncio
import os
import sys
import django
from asgiref.sync import sync_to_async

# Setup Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "papa.settings.project")
django.setup()

from django.contrib.auth import get_user_model
from papa.apps.ergo.models import (
    Knowledgebase,
    Article,
    Workflow,
    UserChat,
    MessageType,
    MessageRole,
)
from papa.apps.ergo.workflows import (
    create_default_workflow,
    create_user_chat,
    process_chat_message,
    OPENAI_AGENTS_AVAILABLE,
    OpenAIAgentConfig,
    process_message_with_openai_agent,
)

User = get_user_model()


async def main():
    """Main example function."""
    print("🚀 Ergo OpenAI Agents Integration Example")
    print("=" * 60)

    # Check if OpenAI Agents is available
    if OPENAI_AGENTS_AVAILABLE:
        print("✅ OpenAI Agents is available!")
        print("   This example will use LLM-powered responses.")
    else:
        print("⚠️  OpenAI Agents is not available.")
        print("   This example will use fallback template responses.")
        print("   Install with: pip install openai-agents")

    # Step 1: Create a test user
    print("\n1. Creating test user...")
    user, created = await sync_to_async(User.objects.get_or_create)(
        username="openai_agent_user",
        defaults={
            "email": "openai@example.com",
            "first_name": "OpenAI",
            "last_name": "Agent",
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

    # Step 3: Create a workflow with detailed instructions
    print("\n3. Creating workflow...")
    workflow, created = await sync_to_async(Workflow.objects.get_or_create)(
        name="AI Programming Assistant",
        defaults={
            "description": "An AI-powered assistant for programming questions",
            "instructions": """You are an expert programming assistant with access to knowledgebases about Python and other technologies.

Your role is to:
1. Help users understand programming concepts
2. Provide clear, practical explanations
3. Use the available knowledgebases to find relevant information
4. Give helpful code examples when appropriate
5. Be encouraging and supportive of learning

When users ask questions:
- Search the knowledgebases for relevant information
- Provide comprehensive answers based on the search results
- If you don't find specific information, offer general guidance
- Always be helpful and encouraging""",
            "tools_config": {
                "search_knowledgebase": {"enabled": True, "max_results": 5}
            },
        },
    )

    if created:
        # Associate knowledgebases with the workflow
        await sync_to_async(workflow.knowledgebases.add)(python_kb)
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
        title="AI Programming Help Chat",
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
        "What is Python and why should I learn it?",
        "How do I work with lists in Python?",
        "Can you explain Python data structures?",
        "What are the benefits of using Python for web development?",
    ]

    for i, question in enumerate(test_questions, 1):
        print(f"\n   Question {i}: {question}")
        print("   " + "-" * 50)

        # Process the message
        result = await process_chat_message(chat, question)

        if result.success:
            print(f"   ✅ Response: {result.content[:300]}...")
            if result.tool_calls:
                print(f"   🔧 Tools used: {len(result.tool_calls)}")
            if result.metadata.get("search_results_count"):
                print(
                    f"   📚 Articles found: {result.metadata['search_results_count']}"
                )
            if result.metadata.get("agent_type"):
                print(f"   🤖 Agent type: {result.metadata['agent_type']}")
        else:
            print(f"   ❌ Error: {result.error}")

    # Step 6: Show conversation history
    print("\n6. Conversation history:")
    print("   " + "-" * 50)

    messages = await sync_to_async(chat.get_messages)()
    for i, message in enumerate(messages, 1):
        role_emoji = "👤" if message.role == MessageRole.USER else "🤖"
        print(f"   {i}. {role_emoji} {message.role}: {message.content[:100]}...")

    # Step 7: Demonstrate OpenAI Agent configuration (if available)
    if OPENAI_AGENTS_AVAILABLE:
        print("\n7. OpenAI Agent Configuration:")
        print("   " + "-" * 50)

        # Create a custom configuration
        config = OpenAIAgentConfig(
            model="gpt-4o-mini",
            temperature=0.7,
            system_prompt="You are a helpful programming tutor. Be concise and practical.",
        )

        print(f"   Model: {config.model}")
        print(f"   Temperature: {config.temperature}")
        print(f"   System Prompt: {config.system_prompt[:100]}...")

        # Test with custom configuration
        print("\n   Testing with custom configuration...")
        custom_result = await process_message_with_openai_agent(
            chat=chat, user_message="Give me a quick Python tip", config=config
        )

        if custom_result.success:
            print(f"   ✅ Custom response: {custom_result.content[:200]}...")
        else:
            print(f"   ❌ Custom response error: {custom_result.error}")

    print("\n" + "=" * 60)
    print("✅ Example completed successfully!")
    print(f"   User: {user.username}")
    print(f"   Chat: {chat.title}")
    print(f"   Workflow: {workflow.name}")
    print(
        f"   OpenAI Agents: {'Available' if OPENAI_AGENTS_AVAILABLE else 'Not Available'}"
    )
    print(f"   Total messages: {len(messages)}")

    if not OPENAI_AGENTS_AVAILABLE:
        print("\n💡 To enable LLM-powered responses:")
        print("   1. Install OpenAI Agents: pip install openai-agents")
        print("   2. Set your OpenAI API key: export OPENAI_API_KEY=your_key_here")
        print("   3. Run this example again")


if __name__ == "__main__":
    # Run the async example
    asyncio.run(main())
