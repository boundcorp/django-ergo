"""
Tests for knowledge base tools.

Tests all the kb_tools functions including search_user_kb, search_garden_kb,
get_kb_table_of_contents, get_article_by_hierarchy, list_user_knowledgebases,
create_article, delete_user_article, and send_email_notification.
"""

import json
import pytest
from unittest.mock import patch, Mock
from django.test import TestCase
from django.contrib.auth import get_user_model

from django_ergo.models import Knowledgebase, Article, UserChat, Workflow
from django_ergo.kb_tools import (
    search_user_kb, search_garden_kb, get_kb_table_of_contents,
    get_article_by_hierarchy, list_user_knowledgebases, create_article,
    delete_user_article, send_email_notification
)
from django_ergo.tools import tool_registry

User = get_user_model()


class TestSearchUserKB(TestCase):
    """Test search_user_kb function."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123"
        )
        
        self.kb = Knowledgebase.objects.create(
            name="Test Knowledge Base",
            description="A test knowledge base",
            owner_id=str(self.user.id)
        )
        
        # Create test articles
        self.article1 = Article.objects.create(
            knowledgebase=self.kb,
            title="Python Programming",
            content="Python is a high-level programming language.",
            hierarchy_code="A",
            summary="Introduction to Python programming"
        )
        
        self.article2 = Article.objects.create(
            knowledgebase=self.kb,
            title="Django Framework",
            content="Django is a web framework for Python.",
            hierarchy_code="B",
            summary="Django web framework overview"
        )
    
    @patch('django_ergo.models.Article.objects')
    def test_search_user_kb_success(self, mock_article_objects):
        """Test successful search in user's knowledge base."""
        # Mock the hybrid search
        mock_queryset = Mock()
        mock_article_objects.filter.return_value = mock_queryset
        mock_queryset.hybrid_search.return_value = [self.article1, self.article2]
        
        results = search_user_kb(
            user=self.user,
            query="Python programming",
            top_k=5
        )
        
        # Verify results structure
        self.assertEqual(len(results), 2)
        
        result1 = results[0]
        self.assertEqual(result1["title"], "Python Programming")
        self.assertEqual(result1["content"], "Python is a high-level programming language.")
        self.assertEqual(result1["hierarchy_code"], "A")
        self.assertEqual(result1["knowledgebase"], "Test Knowledge Base")
        self.assertEqual(result1["summary"], "Introduction to Python programming")
    
    def test_search_user_kb_no_results(self):
        """Test search with no results."""
        # Create a user with no knowledge bases
        empty_user = User.objects.create_user(
            username="emptyuser",
            email="empty@example.com",
            password="testpass123"
        )
        
        results = search_user_kb(
            user=empty_user,
            query="nonexistent",
            top_k=5
        )
        
        self.assertEqual(results, [])
    
    @patch('django_ergo.models.Article.objects')
    def test_search_user_kb_with_kb_name_filter(self, mock_article_objects):
        """Test search with specific knowledge base name filter."""
        mock_queryset = Mock()
        mock_article_objects.filter.return_value = mock_queryset
        mock_queryset.hybrid_search.return_value = [self.article1]
        
        results = search_user_kb(
            user=self.user,
            query="Python",
            kb_name="Test Knowledge",
            top_k=3
        )
        
        self.assertEqual(len(results), 1)


class TestSearchGardenKB(TestCase):
    """Test search_garden_kb function."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123"
        )
        
        # Create garden-related knowledge bases
        self.garden_kb = Knowledgebase.objects.create(
            name="Garden Management",
            description="Tips for garden management",
            owner_id=str(self.user.id)
        )
        
        self.plant_kb = Knowledgebase.objects.create(
            name="Plant Care Guide",
            description="How to care for plants",
            owner_id=str(self.user.id)
        )
        
        # Create test articles
        self.garden_article = Article.objects.create(
            knowledgebase=self.garden_kb,
            title="Tomato Growing",
            content="How to grow tomatoes in your garden.",
            hierarchy_code="A",
            summary="Tomato cultivation guide"
        )
        
        self.plant_article = Article.objects.create(
            knowledgebase=self.plant_kb,
            title="Watering Schedule",
            content="Best practices for watering plants.",
            hierarchy_code="B"
        )
    
    @patch('django_ergo.models.Article.objects')
    def test_search_garden_kb_success(self, mock_article_objects):
        """Test successful search in garden knowledge bases."""
        mock_queryset = Mock()
        mock_article_objects.filter.return_value = mock_queryset
        mock_queryset.hybrid_search.return_value = [self.garden_article, self.plant_article]
        
        results = search_garden_kb(
            user=self.user,
            query="plant care",
            top_k=5
        )
        
        self.assertEqual(len(results), 2)
        
        # Check first result
        result1 = results[0]
        self.assertEqual(result1["title"], "Tomato Growing")
        self.assertEqual(result1["knowledgebase"], "Garden Management")
        self.assertEqual(result1["summary"], "Tomato cultivation guide")
    
    def test_search_garden_kb_no_garden_kbs(self):
        """Test search when no garden-related knowledge bases exist."""
        # Remove all garden-related KBs
        Knowledgebase.objects.filter(
            name__icontains="garden"
        ).delete()
        Knowledgebase.objects.filter(
            name__icontains="plant"
        ).delete()
        
        results = search_garden_kb(
            user=self.user,
            query="garden tips",
            top_k=5
        )
        
        self.assertEqual(results, [])


class TestGetKBTableOfContents(TestCase):
    """Test get_kb_table_of_contents function."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123"
        )
        
        self.kb = Knowledgebase.objects.create(
            name="Test Knowledge Base",
            description="A test knowledge base",
            owner_id=str(self.user.id)
        )
        
        # Create top-level articles (single character hierarchy codes)
        self.article_a = Article.objects.create(
            knowledgebase=self.kb,
            title="Introduction",
            content="Introduction content",
            hierarchy_code="A"
        )
        
        self.article_b = Article.objects.create(
            knowledgebase=self.kb,
            title="Getting Started",
            content="Getting started content",
            hierarchy_code="B"
        )
        
        # Create sub-level article (should not appear in TOC)
        self.article_aa = Article.objects.create(
            knowledgebase=self.kb,
            title="Detailed Introduction",
            content="Detailed introduction content",
            hierarchy_code="AA"
        )
    
    def test_get_kb_table_of_contents_success(self):
        """Test successful table of contents retrieval."""
        result = get_kb_table_of_contents(
            user=self.user,
            kb_name="Test Knowledge Base"
        )
        
        # Verify basic structure
        self.assertIn("knowledgebase", result)
        self.assertIn("table_of_contents", result)
        self.assertEqual(result["knowledgebase"], "Test Knowledge Base")
        
        # Verify table of contents
        toc = result["table_of_contents"]
        self.assertEqual(len(toc), 2)  # Only top-level articles
        
        # Check articles are sorted by hierarchy code
        self.assertEqual(toc[0]["hierarchy_code"], "A")
        self.assertEqual(toc[0]["title"], "Introduction")
        self.assertEqual(toc[1]["hierarchy_code"], "B")
        self.assertEqual(toc[1]["title"], "Getting Started")
    
    def test_get_kb_table_of_contents_not_found(self):
        """Test table of contents for non-existent knowledge base."""
        result = get_kb_table_of_contents(
            user=self.user,
            kb_name="Nonexistent KB"
        )
        
        self.assertIn("error", result)
        self.assertIn("not found", result["error"])


class TestGetArticleByHierarchy(TestCase):
    """Test get_article_by_hierarchy function."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123"
        )
        
        self.kb = Knowledgebase.objects.create(
            name="Test Knowledge Base",
            description="A test knowledge base",
            owner_id=str(self.user.id)
        )
        
        self.article = Article.objects.create(
            knowledgebase=self.kb,
            title="Test Article",
            content="This is test content for the article.",
            hierarchy_code="A1",
            summary="Test article summary"
        )
    
    def test_get_article_by_hierarchy_success(self):
        """Test successful article retrieval by hierarchy code."""
        result = get_article_by_hierarchy(
            user=self.user,
            kb_name="Test Knowledge Base",
            hierarchy_code="A1"
        )
        
        # Verify article data
        self.assertEqual(result["title"], "Test Article")
        self.assertEqual(result["content"], "This is test content for the article.")
        self.assertEqual(result["hierarchy_code"], "A1")
        self.assertEqual(result["summary"], "Test article summary")
        self.assertEqual(result["knowledgebase"], "Test Knowledge Base")
        self.assertEqual(result["id"], str(self.article.id))
    
    def test_get_article_by_hierarchy_not_found(self):
        """Test article retrieval with non-existent knowledge base or article."""
        # Test non-existent KB
        result = get_article_by_hierarchy(
            user=self.user,
            kb_name="Nonexistent KB",
            hierarchy_code="A1"
        )
        
        self.assertIn("error", result)
        self.assertIn("not found", result["error"])
        
        # Test non-existent article
        result = get_article_by_hierarchy(
            user=self.user,
            kb_name="Test Knowledge Base",
            hierarchy_code="Z9"
        )
        
        self.assertIn("error", result)
        self.assertIn("not found", result["error"])


class TestListUserKnowledgebases(TestCase):
    """Test list_user_knowledgebases function."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123"
        )
        
        self.other_user = User.objects.create_user(
            username="otheruser",
            email="other@example.com",
            password="testpass123"
        )
        
        # Create knowledge bases for test user
        self.kb1 = Knowledgebase.objects.create(
            name="First KB",
            description="First knowledge base",
            owner_id=str(self.user.id)
        )
        
        self.kb2 = Knowledgebase.objects.create(
            name="Second KB",
            description="Second knowledge base",
            owner_id=str(self.user.id)
        )
        
        # Create knowledge base for other user (should not appear)
        self.other_kb = Knowledgebase.objects.create(
            name="Other KB",
            description="Other user's knowledge base",
            owner_id=str(self.other_user.id)
        )
        
        # Create some articles to test article count
        Article.objects.create(
            knowledgebase=self.kb1,
            title="Article 1",
            content="Content 1",
            hierarchy_code="A"
        )
        
        Article.objects.create(
            knowledgebase=self.kb1,
            title="Article 2",
            content="Content 2",
            hierarchy_code="B"
        )
    
    def test_list_user_knowledgebases_success(self):
        """Test successful listing of user's knowledge bases."""
        results = list_user_knowledgebases(user=self.user)
        
        # Should return 2 knowledge bases for this user
        self.assertEqual(len(results), 2)
        
        # Check knowledge base data
        kb_names = [kb["name"] for kb in results]
        self.assertIn("First KB", kb_names)
        self.assertIn("Second KB", kb_names)
        
        # Verify knowledge base with articles has correct count
        first_kb = next(kb for kb in results if kb["name"] == "First KB")
        self.assertEqual(first_kb["article_count"], 2)
        self.assertEqual(first_kb["description"], "First knowledge base")
    
    def test_list_user_knowledgebases_empty(self):
        """Test listing knowledge bases for user with none."""
        empty_user = User.objects.create_user(
            username="emptyuser",
            email="empty@example.com",
            password="testpass123"
        )
        
        results = list_user_knowledgebases(user=empty_user)
        
        self.assertEqual(results, [])


class TestCreateArticle(TestCase):
    """Test create_article function."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123"
        )
        
        self.kb = Knowledgebase.objects.create(
            name="Test Knowledge Base",
            description="A test knowledge base",
            owner_id=str(self.user.id)
        )
    
    def test_create_article_success(self):
        """Test successful article creation."""
        result = create_article(
            user=self.user,
            kb_name="Test Knowledge Base",
            title="New Article",
            content="This is new article content.",
            hierarchy_code="A"
        )
        
        # Verify success response
        self.assertIn("id", result)
        self.assertEqual(result["title"], "New Article")
        self.assertEqual(result["hierarchy_code"], "A")
        self.assertEqual(result["knowledgebase"], "Test Knowledge Base")
        self.assertIn("successfully", result["message"])
        
        # Verify article was created in database
        article = Article.objects.get(id=result["id"])
        self.assertEqual(article.title, "New Article")
        self.assertEqual(article.content, "This is new article content.")
        self.assertEqual(article.hierarchy_code, "A")
        self.assertEqual(article.knowledgebase, self.kb)
    
    def test_create_article_auto_hierarchy(self):
        """Test article creation with auto-generated hierarchy code."""
        result = create_article(
            user=self.user,
            kb_name="Test Knowledge Base",
            title="Auto Hierarchy Article",
            content="Content with auto hierarchy."
        )
        
        # Should auto-generate hierarchy code
        self.assertIn("hierarchy_code", result)
        self.assertEqual(len(result["hierarchy_code"]), 1)  # Single character
        self.assertIn(result["hierarchy_code"], "0123456789ABCDEF")  # Valid hex
    
    def test_create_article_errors(self):
        """Test article creation error scenarios."""
        # Test hierarchy conflict
        Article.objects.create(
            knowledgebase=self.kb,
            title="Existing Article",
            content="Existing content",
            hierarchy_code="A"
        )
        
        result = create_article(
            user=self.user,
            kb_name="Test Knowledge Base",
            title="Conflicting Article",
            content="Conflicting content",
            hierarchy_code="A"
        )
        
        self.assertIn("error", result)
        self.assertIn("already exists", result["error"])
        
        # Test non-existent KB
        result = create_article(
            user=self.user,
            kb_name="Nonexistent KB",
            title="Test Article",
            content="Test content"
        )
        
        self.assertIn("error", result)
        self.assertIn("not found", result["error"])


class TestDeleteUserArticle(TestCase):
    """Test delete_user_article function."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123"
        )
        
        self.other_user = User.objects.create_user(
            username="otheruser",
            email="other@example.com",
            password="testpass123"
        )
        
        self.kb = Knowledgebase.objects.create(
            name="Test Knowledge Base",
            description="A test knowledge base",
            owner_id=str(self.user.id)
        )
        
        self.article = Article.objects.create(
            knowledgebase=self.kb,
            title="Article to Delete",
            content="This article will be deleted.",
            hierarchy_code="A"
        )
        
        # Create article for other user
        self.other_kb = Knowledgebase.objects.create(
            name="Other KB",
            description="Other user's KB",
            owner_id=str(self.other_user.id)
        )
        
        self.other_article = Article.objects.create(
            knowledgebase=self.other_kb,
            title="Protected Article",
            content="This article should not be deletable.",
            hierarchy_code="B"
        )
    
    def test_delete_user_article_success(self):
        """Test successful article deletion."""
        article_id = str(self.article.id)
        
        result = delete_user_article(
            user=self.user,
            article_id=article_id
        )
        
        # Verify success response
        self.assertTrue(result["success"])
        self.assertIn("Successfully deleted", result["message"])
        self.assertEqual(result["deleted_article"]["title"], "Article to Delete")
        self.assertEqual(result["deleted_article"]["knowledgebase"], "Test Knowledge Base")
        
        # Verify article was deleted from database
        self.assertFalse(Article.objects.filter(id=article_id).exists())
    
    def test_delete_user_article_errors(self):
        """Test article deletion error scenarios."""
        # Test non-existent article
        result = delete_user_article(
            user=self.user,
            article_id="00000000-0000-0000-0000-000000000000"
        )
        
        self.assertFalse(result["success"])
        self.assertIn("not found", result["error"])
        
        # Test deleting article owned by different user
        result = delete_user_article(
            user=self.user,
            article_id=str(self.other_article.id)
        )
        
        self.assertFalse(result["success"])
        self.assertIn("not found", result["error"])
        
        # Verify article was not deleted
        self.assertTrue(Article.objects.filter(id=self.other_article.id).exists())


class TestSendEmailNotification(TestCase):
    """Test send_email_notification function."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123"
        )
    
    def test_send_email_notification_success(self):
        """Test successful email notification sending (simulated)."""
        result = send_email_notification(
            user=self.user,
            recipient="recipient@example.com",
            subject="Test Subject",
            message="Test message content"
        )
        
        # Verify success response
        self.assertTrue(result["success"])
        self.assertIn("successfully", result["message"])
        
        # Verify email details
        email_details = result["email_details"]
        self.assertEqual(email_details["recipient"], "recipient@example.com")
        self.assertEqual(email_details["subject"], "Test Subject")
        self.assertEqual(email_details["message"], "Test message content")
        self.assertEqual(email_details["sender"], "test@example.com")
        self.assertEqual(email_details["status"], "simulated_sent")


class TestToolRegistration(TestCase):
    """Test that all kb_tools are properly registered."""
    
    def test_kb_tools_registered(self):
        """Test that all knowledge base tools are registered in tool registry."""
        # Test read-only tools
        readonly_tools = [
            "search_user_kb",
            "search_garden_kb", 
            "get_kb_table_of_contents",
            "get_article_by_hierarchy",
            "list_user_knowledgebases"
        ]
        
        for tool_name in readonly_tools:
            tool_config = tool_registry.get_tool(tool_name)
            self.assertIsNotNone(tool_config, f"Tool {tool_name} should be registered")
            self.assertTrue(tool_config.readonly, f"Tool {tool_name} should be readonly")
        
        # Test action tools
        action_tools = [
            "create_article",
            "delete_user_article",
            "send_email_notification"
        ]
        
        for tool_name in action_tools:
            tool_config = tool_registry.get_tool(tool_name)
            self.assertIsNotNone(tool_config, f"Tool {tool_name} should be registered")
            self.assertTrue(tool_config.requires_approval, f"Tool {tool_name} should require approval")
    
    def test_tool_execution_through_registry(self):
        """Test executing tools through the tool registry."""
        user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123"
        )
        
        # Test read-only tool execution
        result = tool_registry.execute_tool(
            name="list_user_knowledgebases",
            user=user,
            arguments={},
            approved=True
        )
        
        # Should return empty list for new user
        self.assertEqual(result, [])
        
        # Test action tool execution
        kb = Knowledgebase.objects.create(
            name="Test KB",
            description="Test",
            owner_id=str(user.id)
        )
        
        article = Article.objects.create(
            knowledgebase=kb,
            title="Test Article",
            content="Test content",
            hierarchy_code="A"
        )
        
        result = tool_registry.execute_tool(
            name="delete_user_article",
            user=user,
            arguments={"article_id": str(article.id)},
            approved=True
        )
        
        self.assertTrue(result["success"])
        self.assertFalse(Article.objects.filter(id=article.id).exists())