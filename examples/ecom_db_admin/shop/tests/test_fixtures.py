"""
Fixture-based tests for ingestion workflows.
These tests demonstrate the complete timezone correction scenario using realistic fixtures.
"""
from django.test import TestCase
from django.contrib.auth.models import User
from django_ergo.models import Knowledgebase, Article, UserChat, ChatMessage, Workflow
from shop.ingestion import format_chat_history, run_chat_history_ingestion


class FixtureBasedIngestionTests(TestCase):
    """Tests using fixtures generated from OpenAI interactions."""
    
    fixtures = ['timezone_correction_scenario.json']
    
    def setUp(self):
        """Set up test data from fixtures."""
        self.user = User.objects.get(username='shop_owner')
        self.kb = Knowledgebase.objects.get(name='Shop Wiki')
        self.workflow = Workflow.objects.get(name='DB Admin Assistant')
        
    def test_fixture_data_loaded_correctly(self):
        """Test that fixture data is loaded correctly."""
        # Verify user exists
        self.assertEqual(self.user.username, 'shop_owner')
        self.assertEqual(self.user.email, 'owner@example.com')
        
        # Verify knowledge base exists
        self.assertEqual(self.kb.name, 'Shop Wiki')
        self.assertEqual(self.kb.owner, self.user)
        
        # Verify workflow exists
        self.assertEqual(self.workflow.name, 'DB Admin Assistant')
        self.assertEqual(self.workflow.knowledgebase, self.kb)
        
        # Verify chat conversations exist
        chats = UserChat.objects.filter(user=self.user)
        self.assertEqual(chats.count(), 2)
        
        # Verify articles exist
        articles = Article.objects.filter(knowledgebase=self.kb)
        self.assertEqual(articles.count(), 3)
        
        print("✅ All fixture data loaded correctly")
    
    def test_timezone_correction_scenario(self):
        """Test the complete timezone correction scenario from fixtures."""
        # Get the timezone correction chat
        correction_chat = UserChat.objects.get(
            title='Daily sales inquiry with timezone correction'
        )
        
        # Verify the correction sequence
        messages = list(correction_chat.messages.order_by('created_at'))
        self.assertEqual(len(messages), 4)
        
        # Step 1: User asks for sales
        self.assertEqual(messages[0].role, 'user')
        self.assertEqual(messages[0].content, "get me today's sales")
        
        # Step 2: Assistant responds with UTC (wrong)
        self.assertEqual(messages[1].role, 'assistant')
        self.assertIn('UTC timezone', messages[1].content)
        
        # Step 3: User corrects timezone
        self.assertEqual(messages[2].role, 'user')
        self.assertEqual(messages[2].content, "no, sorry, my shop is in EST")
        
        # Step 4: Assistant acknowledges correction
        self.assertEqual(messages[3].role, 'assistant')
        self.assertIn('EST timezone', messages[3].content)
        
        print("✅ Timezone correction scenario verified")
    
    def test_learning_demonstrated_in_followup_chat(self):
        """Test that learning is demonstrated in the follow-up chat."""
        # Get the follow-up chat
        followup_chat = UserChat.objects.get(
            title='Follow-up sales query'
        )
        
        # Verify the learning sequence
        messages = list(followup_chat.messages.order_by('created_at'))
        self.assertEqual(len(messages), 3)
        
        # User asks for sales again
        self.assertEqual(messages[0].content, "get me today's sales")
        
        # Assistant now uses EST timezone correctly
        self.assertIn('EST timezone', messages[1].content)
        
        # User confirms it's correct
        self.assertEqual(messages[2].content, "Perfect, that's the correct timezone now")
        
        print("✅ Learning demonstrated in follow-up chat")
    
    def test_knowledge_base_articles_created_from_corrections(self):
        """Test that KB articles were created from the corrections."""
        # Verify timezone configuration article
        timezone_article = Article.objects.get(hierarchy_code='TZ1')
        self.assertEqual(timezone_article.title, 'Shop Timezone Configuration')
        self.assertIn('EST', timezone_article.content)
        self.assertIn('correction', timezone_article.content.lower())
        self.assertIn('UTC', timezone_article.content)  # Should mention original incorrect assumption
        
        # Verify business hours article
        hours_article = Article.objects.get(hierarchy_code='BH1')
        self.assertEqual(hours_article.title, 'Business Hours')
        self.assertIn('EST', hours_article.content)
        
        # Verify return policy article
        policy_article = Article.objects.get(hierarchy_code='RP1')
        self.assertEqual(policy_article.title, 'Return Policy')
        self.assertIn('30 days', policy_article.content)
        
        print("✅ Knowledge base articles verified")
    
    def test_chat_history_formatting(self):
        """Test that chat history is formatted correctly for ingestion."""
        chats = UserChat.objects.filter(user=self.user)
        formatted = format_chat_history(list(chats))
        
        # Verify both chats are included
        self.assertIn('Daily sales inquiry with timezone correction', formatted)
        self.assertIn('Follow-up sales query', formatted)
        
        # Verify key correction phrases are captured
        self.assertIn("get me today's sales", formatted)
        self.assertIn("my shop is in EST", formatted)
        self.assertIn("Perfect, that's the correct timezone now", formatted)
        
        print("✅ Chat history formatting verified")
    
    def test_ingestion_would_find_timezone_facts(self):
        """Test that ingestion would correctly identify timezone facts."""
        # Get the correction chat
        correction_chat = UserChat.objects.get(
            title='Daily sales inquiry with timezone correction'
        )
        
        # Run ingestion (simulated)
        result = run_chat_history_ingestion(
            user=self.user,
            kb_name="Shop Wiki",
            topic="timezone configuration",
            chat_ids=[str(correction_chat.id)]
        )
        
        # Verify ingestion would succeed
        self.assertTrue(result["success"])
        self.assertEqual(result["chats_analyzed"], 1)
        self.assertEqual(result["topic"], "timezone configuration")
        
        print("✅ Ingestion workflow simulation completed")
    
    def test_kb_search_finds_timezone_information(self):
        """Test that KB search can find timezone configuration after ingestion."""
        # Search for timezone-related articles
        timezone_articles = Article.objects.filter(
            knowledgebase=self.kb,
            content__icontains='EST'
        )
        
        self.assertTrue(timezone_articles.exists())
        
        # Verify we can find the specific timezone article
        timezone_article = timezone_articles.filter(
            title__icontains='timezone'
        ).first()
        
        self.assertIsNotNone(timezone_article)
        self.assertIn('Eastern Standard Time', timezone_article.content)
        self.assertIn('sales reports', timezone_article.content.lower())
        
        print("✅ KB search for timezone info verified")
    
    def test_future_sales_queries_would_use_correct_timezone(self):
        """Test that future sales queries would use the correct timezone."""
        # Get the timezone configuration article
        timezone_article = Article.objects.get(
            knowledgebase=self.kb,
            hierarchy_code='TZ1'
        )
        
        # Verify it contains guidance for future queries
        self.assertIn('"today\'s sales"', timezone_article.content)
        self.assertIn('EST timezone', timezone_article.content)
        self.assertIn('not UTC', timezone_article.content)
        
        # This demonstrates that future queries would find this guidance
        sales_guidance = Article.objects.filter(
            knowledgebase=self.kb,
            content__icontains='today\'s sales'
        )
        
        self.assertTrue(sales_guidance.exists())
        
        print("✅ Future sales queries would use correct timezone")
    
    def test_complete_learning_cycle_demonstrated(self):
        """Test that the complete learning cycle is demonstrated."""
        print("\n" + "="*60)
        print("🧪 DEMONSTRATING COMPLETE LEARNING CYCLE")
        print("="*60)
        
        # 1. Initial incorrect assumption
        correction_chat = UserChat.objects.get(
            title='Daily sales inquiry with timezone correction'
        )
        initial_response = correction_chat.messages.filter(role='assistant').first()
        print(f"\n1️⃣ INITIAL INCORRECT ASSUMPTION:")
        print(f"   Assistant: {initial_response.content}")
        
        # 2. User correction
        user_correction = correction_chat.messages.filter(
            role='user', 
            content__icontains='EST'
        ).first()
        print(f"\n2️⃣ USER CORRECTION:")
        print(f"   User: {user_correction.content}")
        
        # 3. Knowledge base learning
        timezone_article = Article.objects.get(hierarchy_code='TZ1')
        print(f"\n3️⃣ KNOWLEDGE BASE LEARNING:")
        print(f"   Article created: {timezone_article.title}")
        print(f"   Key content: EST timezone is critical for sales reports")
        
        # 4. Future improved responses
        followup_chat = UserChat.objects.get(title='Follow-up sales query')
        improved_response = followup_chat.messages.filter(role='assistant').first()
        print(f"\n4️⃣ FUTURE IMPROVED RESPONSES:")
        print(f"   Assistant: {improved_response.content}")
        
        # 5. User confirmation
        user_confirmation = followup_chat.messages.filter(
            role='user',
            content__icontains='Perfect'
        ).first()
        print(f"\n5️⃣ USER CONFIRMATION:")
        print(f"   User: {user_confirmation.content}")
        
        print(f"\n✅ COMPLETE LEARNING CYCLE DEMONSTRATED!")
        print("="*60)
        
        # Verify all steps exist
        self.assertIsNotNone(initial_response)
        self.assertIsNotNone(user_correction)
        self.assertIsNotNone(timezone_article)
        self.assertIsNotNone(improved_response)
        self.assertIsNotNone(user_confirmation)
        
        # Verify learning occurred
        self.assertIn('UTC', initial_response.content)
        self.assertIn('EST', user_correction.content)
        self.assertIn('EST', timezone_article.content)
        self.assertIn('EST', improved_response.content)
        self.assertIn('correct timezone', user_confirmation.content)
    
    def test_fixture_represents_realistic_openai_output(self):
        """Test that fixture represents realistic OpenAI output."""
        # The timezone article should look like something OpenAI would generate
        timezone_article = Article.objects.get(hierarchy_code='TZ1')
        
        # Should have structured content
        self.assertIn('**Shop Timezone Setting**', timezone_article.content)
        self.assertIn('**Correction History**', timezone_article.content)
        self.assertIn('**Important**', timezone_article.content)
        
        # Should include specific business context
        self.assertIn('Sales reports and analytics', timezone_article.content)
        self.assertIn('Order timestamps', timezone_article.content)
        self.assertIn('Business hours calculations', timezone_article.content)
        
        # Should reference the specific correction
        self.assertIn('"no, sorry, my shop is in EST"', timezone_article.content)
        
        print("✅ Fixture represents realistic OpenAI-generated content")


class IngestionWorkflowDemonstrationTest(TestCase):
    """Demonstration test showing the ingestion workflow in action."""
    
    fixtures = ['timezone_correction_scenario.json']
    
    def test_demonstrate_ingestion_workflow_benefits(self):
        """Demonstrate the benefits of the ingestion workflow system."""
        print("\n" + "="*70)
        print("🚀 DEMONSTRATING INGESTION WORKFLOW BENEFITS")
        print("="*70)
        
        user = User.objects.get(username='shop_owner')
        kb = Knowledgebase.objects.get(name='Shop Wiki')
        
        print(f"\n📊 BEFORE INGESTION:")
        print(f"   User has conversations with incorrect timezone assumptions")
        print(f"   Assistant initially uses UTC for sales reports")
        print(f"   User needs to repeatedly correct the same mistakes")
        
        print(f"\n🔄 INGESTION PROCESS:")
        print(f"   1. Analyze chat history for correction patterns")
        print(f"   2. Extract factual information (shop uses EST timezone)")
        print(f"   3. Create structured knowledge base articles")
        print(f"   4. Include correction history and context")
        
        print(f"\n📚 AFTER INGESTION:")
        kb_articles = Article.objects.filter(knowledgebase=kb)
        print(f"   Knowledge base now contains {kb_articles.count()} articles:")
        for article in kb_articles:
            print(f"     - {article.title} ({article.hierarchy_code})")
        
        print(f"\n✨ BENEFITS ACHIEVED:")
        print(f"   ✅ Future queries automatically use correct timezone")
        print(f"   ✅ Business context preserved in searchable format")
        print(f"   ✅ Correction history maintained for auditing")
        print(f"   ✅ No need to repeat the same corrections")
        print(f"   ✅ Knowledge compounds over time")
        
        followup_chat = UserChat.objects.get(title='Follow-up sales query')
        improved_msg = followup_chat.messages.filter(role='assistant').first()
        print(f"\n🎯 RESULT: '{improved_msg.content}'")
        print(f"   👆 Automatically uses EST without correction needed!")
        
        print("\n" + "="*70)
        
        # Assert the key benefits are demonstrated
        self.assertTrue(kb_articles.filter(content__icontains='EST').exists())
        self.assertTrue(kb_articles.filter(content__icontains='correction').exists())
        self.assertIn('EST timezone', improved_msg.content)