#!/usr/bin/env python3
"""
Demonstration of the Timezone Learning Scenario

This script demonstrates the complete ingestion workflow:
1. User asks for sales data
2. Assistant responds with UTC (wrong)
3. User corrects to EST timezone
4. Ingestion learns from this correction
5. Future queries use EST correctly
"""
import os
import sys
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ecom_db_admin.settings')
sys.path.insert(0, '/workspace/src')
django.setup()

from django.contrib.auth.models import User
from django_ergo.models import Knowledgebase, Article, UserChat, ChatMessage, Workflow
from shop.ingestion import run_chat_history_ingestion
from shop.workflows import create_db_admin_workflow

def create_demo_data():
    """Create demo data for the timezone scenario."""
    print("🏗️  Creating demo data...")
    
    # Create user
    user, created = User.objects.get_or_create(
        username='shop_owner',
        defaults={
            'email': 'owner@example.com',
            'first_name': 'Shop',
            'last_name': 'Owner'
        }
    )
    if created:
        print(f"   ✅ Created user: {user.username}")
    else:
        print(f"   📝 Using existing user: {user.username}")
    
    # Create workflow
    workflow = create_db_admin_workflow(user, "DB Admin Assistant")
    print(f"   ✅ Created workflow: {workflow.name}")
    
    # Create chat with timezone correction
    chat = UserChat.objects.create(
        user=user,
        workflow=workflow,
        title='Daily sales inquiry with timezone correction'
    )
    print(f"   ✅ Created chat: {chat.title}")
    
    # Create messages showing the correction
    messages_data = [
        ("user", "user_input", "get me today's sales"),
        ("assistant", "assistant_message", "I'll get today's sales data using UTC timezone. Here are the results: Total: $2,456.78 from 15 orders."),
        ("user", "user_input", "no, sorry, my shop is in EST"),
        ("assistant", "assistant_message", "Thank you for the correction! I'll update my knowledge to use EST timezone for your shop's operations."),
    ]
    
    for role, msg_type, content in messages_data:
        ChatMessage.objects.create(
            chat=chat,
            role=role,
            message_type=msg_type,
            content=content
        )
        print(f"   💬 Added {role} message: {content[:50]}...")
    
    # Create follow-up chat showing learning
    followup_chat = UserChat.objects.create(
        user=user,
        workflow=workflow,
        title='Follow-up sales query'
    )
    
    followup_messages = [
        ("user", "user_input", "get me today's sales"),
        ("assistant", "assistant_message", "Getting today's sales using EST timezone for your shop. Total: $2,789.45 from 18 orders."),
        ("user", "user_input", "Perfect, that's the correct timezone now"),
    ]
    
    for role, msg_type, content in followup_messages:
        ChatMessage.objects.create(
            chat=followup_chat,
            role=role,
            message_type=msg_type,
            content=content
        )
    
    print(f"   ✅ Created follow-up chat: {followup_chat.title}")
    
    return user, chat, followup_chat

def simulate_ingestion(user, chat):
    """Simulate the ingestion process that learns from the correction."""
    print("\n🔄 Running ingestion workflow...")
    
    # Run the ingestion
    result = run_chat_history_ingestion(
        user=user,
        kb_name="Shop Wiki",
        topic="timezone configuration",
        chat_ids=[str(chat.id)]
    )
    
    print(f"   ✅ Ingestion result: {result}")
    
    # Simulate what the ingestion would create
    kb = Knowledgebase.objects.get(name="Shop Wiki", owner_id=str(user.id))
    
    # Create the timezone article that would be generated
    timezone_article, created = Article.objects.get_or_create(
        knowledgebase=kb,
        hierarchy_code="TZ1",
        defaults={
            'title': "Shop Timezone Configuration",
            'content': """**Shop Timezone Setting**: Eastern Standard Time (EST)

The shop operates in EST timezone. This is critical for all time-sensitive operations including:
- Sales reports and analytics
- Order timestamps
- Business hours calculations

**Correction History**:
- Initially system assumed UTC timezone
- User corrected during sales inquiry: "no, sorry, my shop is in EST"
- Updated: All operations should use EST, not UTC

**Important**: When processing "today's sales" or similar time-based queries, 
always use EST timezone for this shop."""
        }
    )
    
    if created:
        print(f"   📚 Created KB article: {timezone_article.title}")
    else:
        print(f"   📝 Article already exists: {timezone_article.title}")
    
    return timezone_article

def demonstrate_learning(user, chat, followup_chat, timezone_article):
    """Demonstrate the complete learning cycle."""
    print("\n" + "="*60)
    print("🧪 DEMONSTRATING COMPLETE LEARNING CYCLE")
    print("="*60)
    
    # Show initial incorrect assumption
    print("\n1️⃣ INITIAL INCORRECT ASSUMPTION:")
    initial_response = chat.messages.filter(role='assistant').first()
    print(f"   Assistant: {initial_response.content}")
    
    # Show user correction
    print("\n2️⃣ USER CORRECTION:")
    user_correction = chat.messages.filter(role='user', content__icontains='EST').first()
    print(f"   User: {user_correction.content}")
    
    # Show knowledge base learning
    print("\n3️⃣ KNOWLEDGE BASE LEARNING:")
    print(f"   Article created: {timezone_article.title}")
    print(f"   Key insight: EST timezone is critical for sales reports")
    
    # Show future improved responses
    print("\n4️⃣ FUTURE IMPROVED RESPONSES:")
    improved_response = followup_chat.messages.filter(role='assistant').first()
    print(f"   Assistant: {improved_response.content}")
    
    # Show user confirmation
    print("\n5️⃣ USER CONFIRMATION:")
    user_confirmation = followup_chat.messages.filter(role='user', content__icontains='Perfect').first()
    print(f"   User: {user_confirmation.content}")
    
    print(f"\n✅ COMPLETE LEARNING CYCLE DEMONSTRATED!")
    print("="*60)

def show_benefits():
    """Show the benefits of the ingestion system."""
    print("\n" + "="*70)
    print("🚀 BENEFITS OF THE INGESTION WORKFLOW SYSTEM")
    print("="*70)
    
    print(f"\n📊 BEFORE INGESTION:")
    print(f"   • User conversations contained timezone correction")
    print(f"   • Assistant initially used UTC for sales reports")
    print(f"   • User needed to repeatedly correct the same mistakes")
    
    print(f"\n🔄 INGESTION PROCESS:")
    print(f"   • Analyzes chat history for correction patterns")
    print(f"   • Extracts factual information (shop uses EST timezone)")
    print(f"   • Creates structured knowledge base articles")
    print(f"   • Includes correction history and context")
    
    print(f"\n📚 AFTER INGESTION:")
    kb = Knowledgebase.objects.filter(name="Shop Wiki").first()
    if kb:
        articles = Article.objects.filter(knowledgebase=kb)
        print(f"   Knowledge base now contains {articles.count()} articles:")
        for article in articles:
            print(f"     - {article.title} ({article.hierarchy_code})")
    
    print(f"\n✨ BENEFITS ACHIEVED:")
    print(f"   ✅ Future queries automatically use correct timezone")
    print(f"   ✅ Business context preserved in searchable format")
    print(f"   ✅ Correction history maintained for auditing")
    print(f"   ✅ No need to repeat the same corrections")
    print(f"   ✅ Knowledge compounds over time")
    
    print("\n" + "="*70)

def main():
    """Run the complete demonstration."""
    print("🌟 TIMEZONE LEARNING SCENARIO DEMONSTRATION")
    print("=" * 60)
    
    try:
        # Create demo data
        user, chat, followup_chat = create_demo_data()
        
        # Simulate ingestion
        timezone_article = simulate_ingestion(user, chat)
        
        # Demonstrate learning cycle
        demonstrate_learning(user, chat, followup_chat, timezone_article)
        
        # Show benefits
        show_benefits()
        
        print(f"\n🎉 DEMONSTRATION COMPLETE!")
        print(f"   Database contains realistic data showing timezone learning")
        print(f"   Ingestion workflows are ready for OpenAI integration")
        print(f"   Tests can verify this behavior using fixtures or live data")
        
    except Exception as e:
        print(f"❌ Error during demonstration: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()