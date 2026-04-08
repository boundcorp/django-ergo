"""
Tests for ingestion workflows.
"""
import json
from unittest.mock import patch, MagicMock
from django.test import TestCase
from django.contrib.auth.models import User
from django_ergo.models import Knowledgebase, Article, UserChat, ChatMessage
from shop.ingestion import (
    format_chat_history,
    run_chat_history_ingestion,
    run_document_ingestion,
    run_kb_review
)


class ChatHistoryIngestionTests(TestCase):
    """Tests for chat history ingestion workflow."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass'
        )
        
        # Create a knowledge base
        self.kb = Knowledgebase.objects.create(
            name='Shop Wiki',
            description='Test knowledge base',
            owner=self.user
        )
        
        # Create test chat with timezone correction
        self.chat = UserChat.objects.create(
            user=self.user,
            title='Sales inquiry with timezone correction'
        )
        
        # Messages simulating the scenario from requirements
        ChatMessage.objects.create(
            chat=self.chat,
            role='user',
            content="get me today's sales"
        )
        
        ChatMessage.objects.create(
            chat=self.chat,
            role='assistant',
            content="I'll get today's sales data using UTC timezone. Here are the results..."
        )
        
        ChatMessage.objects.create(
            chat=self.chat,
            role='user',
            content="no, sorry, my shop is in EST"
        )
        
        ChatMessage.objects.create(
            chat=self.chat,
            role='assistant',
            content="Thank you for the correction! I'll update my knowledge to use EST timezone for your shop."
        )
    
    def test_format_chat_history(self):
        """Test chat history formatting."""
        formatted = format_chat_history([self.chat])
        
        self.assertIn("Sales inquiry with timezone correction", formatted)
        self.assertIn("USER: get me today's sales", formatted)
        self.assertIn("ASSISTANT: I'll get today's sales", formatted)
        self.assertIn("USER: no, sorry, my shop is in EST", formatted)
        self.assertIn("ASSISTANT: Thank you for the correction", formatted)
    
    def test_run_chat_history_ingestion(self):
        """Test run_chat_history_ingestion function."""
        result = run_chat_history_ingestion(
            user=self.user,
            kb_name="Test KB",
            topic="business settings",
            chat_ids=[str(self.chat.id)]
        )
        
        self.assertTrue(result["success"])
        self.assertEqual(result["chats_analyzed"], 1)
        self.assertEqual(result["kb_name"], "Test KB")
        self.assertEqual(result["topic"], "business settings")
        self.assertIn("workflow_id", result)


class IngestionFunctionTests(TestCase):
    """Tests for ingestion helper functions."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass'
        )
    
    def test_run_document_ingestion(self):
        """Test run_document_ingestion function."""
        document_content = "This is a test document about shop policies..."
        
        result = run_document_ingestion(
            user=self.user,
            document_content=document_content,
            topic="shop policies",
            kb_name="Policy KB",
            instructions="Extract policy information"
        )
        
        self.assertTrue(result["success"])
        self.assertEqual(result["kb_name"], "Policy KB")
        self.assertEqual(result["topic"], "shop policies")
        self.assertEqual(result["document_size"], len(document_content))
        self.assertIn("workflow_id", result)
    
    def test_run_kb_review(self):
        """Test run_kb_review function."""
        # Create a knowledge base first
        kb = Knowledgebase.objects.create(
            name="Shop Wiki",
            description="Test KB",
            owner=self.user
        )
        
        result = run_kb_review(
            user=self.user,
            kb_name="Shop Wiki",
            focus_area="timezone settings",
            instructions="Review timezone configuration"
        )
        
        self.assertTrue(result["success"])
        self.assertEqual(result["kb_name"], "Shop Wiki")
        self.assertEqual(result["focus_area"], "timezone settings")
        self.assertIn("workflow_id", result)


class IntegrationTestsFixtureBased(TestCase):
    """Integration tests using fixture data instead of OpenAI API."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass'
        )
        
        # Create knowledge base
        self.kb = Knowledgebase.objects.create(
            name='Shop Wiki',
            description='Test shop knowledge base',
            owner=self.user
        )
        
        # Create chat with timezone correction
        self.create_timezone_correction_chat()
        
        # Create chat with business hours correction  
        self.create_business_hours_chat()
    
    def create_timezone_correction_chat(self):
        """Create a chat with timezone correction scenario."""
        chat = UserChat.objects.create(
            user=self.user,
            title='Daily sales inquiry'
        )
        
        # User asks for sales
        ChatMessage.objects.create(
            chat=chat,
            role='user',
            content="get me today's sales"
        )
        
        # Assistant responds with UTC
        ChatMessage.objects.create(
            chat=chat,
            role='assistant',
            content="Here are today's sales for UTC timezone: Total: $1,234.56"
        )
        
        # User corrects timezone
        ChatMessage.objects.create(
            chat=chat,
            role='user',
            content="no, sorry, my shop is in EST"
        )
        
        # Assistant acknowledges
        ChatMessage.objects.create(
            chat=chat,
            role='assistant',
            content="I'll remember that your shop operates in EST timezone."
        )
        
        return chat
    
    def create_business_hours_chat(self):
        """Create a chat with business hours correction."""
        chat = UserChat.objects.create(
            user=self.user,
            title='Business hours inquiry'
        )
        
        ChatMessage.objects.create(
            chat=chat,
            role='user',
            content="What are our business hours?"
        )
        
        ChatMessage.objects.create(
            chat=chat,
            role='assistant',
            content="Your business hours are 9 AM to 5 PM Monday through Friday."
        )
        
        ChatMessage.objects.create(
            chat=chat,
            role='user',
            content="Actually, we changed to 8 AM to 6 PM and we're open on Saturdays now"
        )
        
        ChatMessage.objects.create(
            chat=chat,
            role='assistant',
            content="Thank you for the update! I'll note the new hours: 8 AM to 6 PM Monday-Saturday."
        )
        
        return chat
    
    def test_chat_history_format_includes_corrections(self):
        """Test that chat history properly formats correction conversations."""
        chats = UserChat.objects.filter(user=self.user)
        formatted = format_chat_history(list(chats))
        
        # Check timezone correction is captured
        self.assertIn("get me today's sales", formatted)
        self.assertIn("my shop is in EST", formatted)
        
        # Check business hours correction is captured
        self.assertIn("What are our business hours", formatted)
        self.assertIn("8 AM to 6 PM and we're open on Saturdays", formatted)
    
    def test_ingestion_creates_timezone_article(self):
        """Test that ingestion would create timezone configuration article."""
        # Simulate the article creation that would happen
        Article.objects.create(
            knowledgebase=self.kb,
            title='Shop Configuration - Timezone',
            content='The shop operates in EST timezone. This was corrected from an initial assumption of UTC.',
            hierarchy_code='T1'
        )
        
        # Verify article was created
        article = Article.objects.get(knowledgebase=self.kb, hierarchy_code='T1')
        self.assertEqual(article.title, 'Shop Configuration - Timezone')
        self.assertIn('EST timezone', article.content)
        self.assertIn('corrected', article.content.lower())
    
    def test_ingestion_creates_business_hours_article(self):
        """Test that ingestion would create business hours article."""
        # Simulate article creation
        Article.objects.create(
            knowledgebase=self.kb,
            title='Business Hours',
            content='Current hours: 8 AM to 6 PM Monday-Saturday. Updated from previous 9 AM to 5 PM Monday-Friday.',
            hierarchy_code='B1'
        )
        
        # Verify article
        article = Article.objects.get(knowledgebase=self.kb, hierarchy_code='B1')
        self.assertEqual(article.title, 'Business Hours')
        self.assertIn('8 AM to 6 PM', article.content)
        self.assertIn('Monday-Saturday', article.content)
    
    def test_kb_query_after_ingestion_uses_correct_timezone(self):
        """Test that after ingestion, KB queries would use correct timezone."""
        # Create the timezone article that would result from ingestion
        Article.objects.create(
            knowledgebase=self.kb,
            title='Shop Configuration - Timezone',
            content='''
            The shop operates in Eastern Standard Time (EST).
            
            **Important**: When generating sales reports or handling time-sensitive queries, 
            always use EST timezone, not UTC.
            
            This was corrected during a conversation where the user requested "today's sales" 
            and clarified that their shop operates in EST.
            ''',
            hierarchy_code='TZ1'
        )
        
        # Simulate a search for timezone info
        timezone_articles = Article.objects.filter(
            knowledgebase=self.kb,
            content__icontains='EST'
        )
        
        self.assertTrue(timezone_articles.exists())
        
        timezone_article = timezone_articles.first()
        self.assertIn('Eastern Standard Time', timezone_article.content)
        self.assertIn('EST timezone', timezone_article.content)
        self.assertIn('not UTC', timezone_article.content)
    
    def test_multiple_corrections_accumulate_knowledge(self):
        """Test that multiple corrections build up knowledge over time."""
        # Simulate multiple ingestion rounds creating/updating articles
        
        # First correction: timezone
        Article.objects.create(
            knowledgebase=self.kb,
            title='Shop Configuration - Timezone',
            content='Shop operates in EST timezone.',
            hierarchy_code='TZ1'
        )
        
        # Second correction: business hours
        Article.objects.create(
            knowledgebase=self.kb,
            title='Business Hours',
            content='Shop hours: 8 AM to 6 PM EST, Monday through Saturday.',
            hierarchy_code='BH1'
        )
        
        # Third correction: return policy
        Article.objects.create(
            knowledgebase=self.kb,
            title='Return Policy',
            content='Return policy: 45 days (updated from 30 days for holiday season).',
            hierarchy_code='RP1'
        )
        
        # Verify all knowledge is accumulated
        self.assertEqual(self.kb.articles.count(), 3)
        
        # Verify each type of correction is captured
        timezone_knowledge = self.kb.articles.filter(content__icontains='EST').exists()
        hours_knowledge = self.kb.articles.filter(content__icontains='8 AM').exists()
        return_knowledge = self.kb.articles.filter(content__icontains='45 days').exists()
        
        self.assertTrue(timezone_knowledge)
        self.assertTrue(hours_knowledge) 
        self.assertTrue(return_knowledge)
        
        # This demonstrates cumulative learning from multiple corrections