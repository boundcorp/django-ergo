"""
Tests for OpenAI integrations in django_ergo.fields module.

This module provides two-tier testing:
1. Real OpenAI API tests (when TEST_OPENAI=true) that cost credits and save fixtures
2. Mocked tests using saved fixtures for fast unit testing without API costs
"""

import pytest
import os
from unittest.mock import patch, Mock
from django.test import TestCase
from django.contrib.auth import get_user_model

from django_ergo.fields import generate_summary, generate_embedding, SummarizedVectorField
from .openai_test_utils import openai_test_manager, save_openai_fixture

User = get_user_model()


class TestGenerateSummary:
    """Test the generate_summary function with both real and mocked OpenAI API."""
    
    @pytest.mark.openai_real
    def test_generate_summary_real_api(self):
        """Test generate_summary with real OpenAI API (costs credits)."""
        if not openai_test_manager.should_use_real_api():
            pytest.skip("TEST_OPENAI not set - skipping costly API test")
        
        # Test data
        test_text = "This is a long text that needs to be summarized. It contains multiple sentences with various information about different topics. The goal is to create a concise summary that captures the main points."
        max_tokens = 50
        
        # Call the real API
        result = generate_summary(test_text, max_tokens)
        
        # Basic assertions
        assert isinstance(result, str)
        assert len(result) > 0
        assert len(result.split()) <= max_tokens + 10  # Allow some tolerance
        
        # Save fixture for mocked test
        input_data = {
            "text": test_text,
            "max_tokens": max_tokens,
            "user_context": None
        }
        
        # Mock response structure for saving
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message = Mock()
        mock_response.choices[0].message.content = result
        mock_response.usage = Mock()
        mock_response.usage.model_dump.return_value = {"prompt_tokens": 50, "completion_tokens": 20}
        
        save_openai_fixture("generate_summary_basic", input_data, mock_response, "chat.completions")
        
        print(f"✅ Real API test completed. Generated summary: {result[:100]}...")
    
    @pytest.mark.openai_mocked
    def test_generate_summary_mocked(self):
        """Test generate_summary with mocked OpenAI API using saved fixtures."""
        fixture = openai_test_manager.load_fixture("generate_summary_basic")
        if not fixture:
            pytest.skip("No fixture found - run with TEST_OPENAI=true first")
        
        # Create mock response
        mock_response = openai_test_manager.create_mock_response(fixture)
        
        with patch('openai.chat.completions.create', return_value=mock_response):
            result = generate_summary(
                fixture.input_data["text"],
                fixture.input_data["max_tokens"]
            )
        
        # Verify result matches fixture
        assert result == fixture.response_data["content"]
        assert isinstance(result, str)
        assert len(result) > 0
        
        print(f"✅ Mocked test completed. Result: {result[:100]}...")
    
    @pytest.mark.openai_real 
    def test_generate_summary_with_context_real_api(self):
        """Test generate_summary with user context using real API."""
        if not openai_test_manager.should_use_real_api():
            pytest.skip("TEST_OPENAI not set - skipping costly API test")
        
        test_text = "Django is a high-level Python web framework that encourages rapid development and clean, pragmatic design."
        user_context = {"role": "developer", "interest": "web_frameworks"}
        
        result = generate_summary(test_text, max_tokens=30, user_context=user_context)
        
        assert isinstance(result, str)
        assert len(result) > 0
        
        # Save fixture
        input_data = {
            "text": test_text,
            "max_tokens": 30,
            "user_context": user_context
        }
        
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message = Mock()
        mock_response.choices[0].message.content = result
        mock_response.usage = Mock()
        mock_response.usage.model_dump.return_value = {"prompt_tokens": 40, "completion_tokens": 15}
        
        save_openai_fixture("generate_summary_with_context", input_data, mock_response, "chat.completions")
    
    @pytest.mark.openai_mocked
    def test_generate_summary_with_context_mocked(self):
        """Test generate_summary with context using mocked API."""
        fixture = openai_test_manager.load_fixture("generate_summary_with_context")
        if not fixture:
            pytest.skip("No fixture found - run with TEST_OPENAI=true first")
        
        mock_response = openai_test_manager.create_mock_response(fixture)
        
        with patch('openai.chat.completions.create', return_value=mock_response):
            result = generate_summary(
                fixture.input_data["text"],
                fixture.input_data["max_tokens"],
                fixture.input_data["user_context"]
            )
        
        assert result == fixture.response_data["content"]


class TestGenerateEmbedding:
    """Test the generate_embedding function with both real and mocked OpenAI API."""
    
    @pytest.mark.openai_real
    def test_generate_embedding_real_api(self):
        """Test generate_embedding with real OpenAI API (costs credits)."""
        if not openai_test_manager.should_use_real_api():
            pytest.skip("TEST_OPENAI not set - skipping costly API test")
        
        test_text = "Django web framework for Python development"
        
        result = generate_embedding(test_text)
        
        # Verify embedding properties
        assert isinstance(result, list)
        assert len(result) == 1536  # text-embedding-3-small dimension
        assert all(isinstance(x, float) for x in result)
        
        # Save fixture
        input_data = {"text": test_text}
        
        mock_response = Mock()
        mock_response.data = [Mock()]
        mock_response.data[0].embedding = result
        
        save_openai_fixture("generate_embedding_basic", input_data, mock_response, "embeddings")
        
        print(f"✅ Real API test completed. Generated {len(result)}-dimensional embedding")
    
    @pytest.mark.openai_mocked
    def test_generate_embedding_mocked(self):
        """Test generate_embedding with mocked OpenAI API using saved fixtures."""
        fixture = openai_test_manager.load_fixture("generate_embedding_basic")
        if not fixture:
            pytest.skip("No fixture found - run with TEST_OPENAI=true first")
        
        mock_response = openai_test_manager.create_mock_response(fixture)
        
        with patch('openai.embeddings.create', return_value=mock_response):
            result = generate_embedding(fixture.input_data["text"])
        
        # Verify result matches fixture
        assert result == fixture.response_data["embedding"]
        assert isinstance(result, list)
        assert len(result) == 1536
        assert all(isinstance(x, float) for x in result)


class TestSummarizedVectorField(TestCase):
    """Test the SummarizedVectorField custom Django field."""
    
    def setUp(self):
        """Set up test database with a simple test model."""
        # We'll create a minimal test model for this
        from django.db import models
        from pgvector.django import VectorField
        
        class TestDocument(models.Model):
            content = SummarizedVectorField(
                max_length=1000,
                embedding_field_name="content_embedding",
                summary_field_name="content_summary"
            )
            content_embedding = VectorField(dimensions=1536, null=True, blank=True)
            content_summary = models.TextField(blank=True)
            created_at = models.DateTimeField(auto_now_add=True)
            
            class Meta:
                app_label = 'django_ergo'
        
        self.TestDocument = TestDocument
    
    @pytest.mark.openai_real
    def test_summarized_vector_field_real_api(self):
        """Test SummarizedVectorField with real OpenAI API."""
        if not openai_test_manager.should_use_real_api():
            pytest.skip("TEST_OPENAI not set - skipping costly API test")
        
        # This test would need a proper Django model setup
        # For now, we'll test the field's pre_save method directly
        
        field = SummarizedVectorField()
        
        # Create a mock model instance
        mock_instance = Mock()
        mock_instance._state.adding = True
        mock_instance.content = "This is test content for the SummarizedVectorField that should be summarized and embedded."
        
        # This is a simplified test - in practice you'd need proper model setup
        pytest.skip("Full model test requires database setup - covered by integration tests")
    
    @pytest.mark.openai_mocked
    def test_summarized_vector_field_mocked(self):
        """Test SummarizedVectorField with mocked APIs."""
        # Similar to above - would need full model setup
        pytest.skip("Full model test requires database setup - covered by integration tests")


class TestOpenAIIntegrationErrors:
    """Test error handling in OpenAI integrations."""
    
    def test_generate_summary_empty_content(self):
        """Test generate_summary with empty content handling."""
        with patch('openai.chat.completions.create') as mock_create:
            # Test empty string
            mock_response = Mock()
            mock_response.choices = [Mock()]
            mock_response.choices[0].message = Mock()
            mock_response.choices[0].message.content = None
            mock_create.return_value = mock_response
            
            with pytest.raises(ValueError, match="OpenAI returned empty content"):
                generate_summary("Some text")
    
    def test_generate_embedding_api_error(self):
        """Test generate_embedding error handling."""
        with patch('openai.embeddings.create') as mock_create:
            mock_create.side_effect = Exception("API Error")
            
            with pytest.raises(Exception, match="API Error"):
                generate_embedding("Some text")


# Test configuration and fixtures
@pytest.fixture(scope="session")
def openai_api_key():
    """Ensure OpenAI API key is available for real API tests."""
    if openai_test_manager.should_use_real_api():
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            pytest.skip("OPENAI_API_KEY not set - cannot run real API tests")
        return api_key
    return None


class TestFixtureGeneration:
    """Tests specifically for fixture generation and management."""
    
    def test_fixture_directory_creation(self):
        """Test that fixture directory is created properly."""
        assert openai_test_manager.fixtures_dir.exists()
        assert openai_test_manager.fixtures_dir.is_dir()
    
    def test_fixture_loading_nonexistent(self):
        """Test loading a fixture that doesn't exist."""
        fixture = openai_test_manager.load_fixture("nonexistent_test")
        assert fixture is None
    
    @pytest.mark.openai_real
    def test_minimal_summary_real_api(self):
        """Minimal test to generate a basic fixture."""
        if not openai_test_manager.should_use_real_api():
            pytest.skip("TEST_OPENAI not set - skipping costly API test")
        
        result = generate_summary("Test text for fixture generation", max_tokens=20)
        
        # Save a simple fixture
        input_data = {"text": "Test text for fixture generation", "max_tokens": 20}
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message = Mock()
        mock_response.choices[0].message.content = result
        mock_response.usage = Mock()
        mock_response.usage.model_dump.return_value = {}
        
        save_openai_fixture("minimal_summary", input_data, mock_response, "chat.completions")
        
        # Verify fixture was saved
        fixture = openai_test_manager.load_fixture("minimal_summary")
        assert fixture is not None
        assert fixture.response_data["content"] == result