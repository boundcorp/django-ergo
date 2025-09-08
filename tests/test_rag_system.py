"""
Comprehensive Test Suite for RAG System
Tests common use cases and ensures robust functionality
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from django.test import TestCase
from django.contrib.auth import get_user_model

from django_ergo.models import Knowledgebase, Article
from examples.rag_examples import (
    SmartDocumentChunker,
    ChunkConfig,
    HybridSearchEngine,
    ContextWindowOptimizer,
    FeedbackLearningSystem
)

User = get_user_model()


# =============================================================================
# TEST FIXTURES
# =============================================================================

@pytest.fixture
def sample_documents():
    """Provide various document types for testing"""
    return {
        'technical': """
        # API Documentation
        
        ## Authentication
        Our API uses OAuth 2.0 for authentication. You'll need to obtain
        an access token before making requests.
        
        ### Getting Started
        1. Register your application
        2. Obtain client credentials
        3. Request an access token
        
        ## Endpoints
        
        ### GET /api/users
        Returns a list of users. Requires admin privileges.
        
        ### POST /api/users
        Creates a new user. Required fields: username, email, password.
        """,
        
        'faq': """
        Q: How do I reset my password?
        A: Click on "Forgot Password" on the login page and follow the instructions.
        
        Q: What payment methods do you accept?
        A: We accept credit cards, PayPal, and bank transfers.
        
        Q: How can I cancel my subscription?
        A: Go to Account Settings and click "Cancel Subscription".
        """,
        
        'narrative': """
        Once upon a time in a land of continuous integration, there lived
        a developer who dreamed of perfect test coverage. Every morning,
        they would write unit tests before their coffee, and integration
        tests before lunch. Their code was beautiful, their commits atomic,
        and their pull requests a joy to review.
        """,
        
        'edge_cases': {
            'empty': '',
            'single_word': 'Hello',
            'unicode': '数学 εψιλον 🔬 测试',
            'very_long': 'x' * 10000,
            'malformed': '{"incomplete": ',
        }
    }


@pytest.fixture
def knowledge_base():
    """Create test knowledge base"""
    kb = Knowledgebase.objects.create(
        name="test_kb",
        description="Test knowledge base"
    )
    return kb


@pytest.fixture
def populated_kb(knowledge_base):
    """Knowledge base with sample articles"""
    articles = [
        Article.objects.create(
            knowledgebase=knowledge_base,
            title=f"Article {i}",
            content=f"This is test content for article {i}. It contains information about topic {i}.",
            hierarchy_code=f"{i:04d}"
        )
        for i in range(10)
    ]
    return knowledge_base, articles


# =============================================================================
# CHUNKING TESTS
# =============================================================================

class TestSmartDocumentChunker(TestCase):
    """Test intelligent document chunking"""
    
    def setUp(self):
        self.chunker = SmartDocumentChunker(
            ChunkConfig(max_tokens=256, overlap_tokens=20)
        )
    
    def test_basic_chunking(self):
        """Test basic document chunking"""
        content = "This is paragraph one.\n\nThis is paragraph two.\n\nThis is paragraph three."
        chunks = self.chunker.chunk_document(content)
        
        self.assertGreater(len(chunks), 0)
        self.assertTrue(all('content' in chunk for chunk in chunks))
        self.assertTrue(all('id' in chunk for chunk in chunks))
        self.assertTrue(all('index' in chunk for chunk in chunks))
    
    def test_respects_token_limits(self):
        """Test that chunks respect token limits"""
        content = "Long content. " * 500  # Very long content
        chunks = self.chunker.chunk_document(content)
        
        for chunk in chunks:
            self.assertLessEqual(
                chunk['token_count'],
                self.chunker.config.max_tokens
            )
    
    def test_overlap_handling(self):
        """Test that chunks have proper overlap"""
        content = "Sentence one. Sentence two. Sentence three. Sentence four."
        config = ChunkConfig(max_tokens=10, overlap_tokens=5)
        chunker = SmartDocumentChunker(config)
        
        chunks = chunker.chunk_document(content)
        
        if len(chunks) > 1:
            # Check that consecutive chunks have overlap
            for i in range(len(chunks) - 1):
                chunk1_end = chunks[i]['content'].split()[-5:]
                chunk2_start = chunks[i+1]['content'].split()[:5]
                # There should be some overlap
                self.assertTrue(
                    any(word in chunk2_start for word in chunk1_end)
                )
    
    def test_metadata_preservation(self):
        """Test that metadata is preserved in chunks"""
        content = "Test content"
        metadata = {"source": "test.pdf", "author": "Test Author"}
        
        chunks = self.chunker.chunk_document(content, metadata)
        
        for chunk in chunks:
            self.assertEqual(chunk['metadata'], metadata)
    
    def test_edge_cases(self):
        """Test edge cases in chunking"""
        # Empty content
        chunks = self.chunker.chunk_document("")
        self.assertEqual(len(chunks), 0)
        
        # Single word
        chunks = self.chunker.chunk_document("Hello")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]['content'], "Hello")
        
        # Unicode content
        unicode_content = "数学 εψιλον 🔬 测试"
        chunks = self.chunker.chunk_document(unicode_content)
        self.assertGreater(len(chunks), 0)
        self.assertIn("数学", chunks[0]['content'])


# =============================================================================
# HYBRID SEARCH TESTS
# =============================================================================

class TestHybridSearchEngine(TestCase):
    """Test hybrid search functionality"""
    
    def setUp(self):
        self.kb = Knowledgebase.objects.create(
            name="test_kb",
            description="Test KB"
        )
        self.engine = HybridSearchEngine(self.kb)
    
    @patch('examples.rag_examples.semantic_search')
    def test_semantic_search_integration(self, mock_semantic_search):
        """Test semantic search component"""
        mock_semantic_search.return_value = [
            (Mock(id=1, title="Result 1", content="Content 1"), 0.9),
            (Mock(id=2, title="Result 2", content="Content 2"), 0.8),
        ]
        
        results = self.engine._semantic_search("test query", 10)
        
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['score'], 0.9)
        self.assertEqual(results[0]['type'], 'semantic')
    
    def test_result_combination(self):
        """Test combining semantic and keyword results"""
        semantic_results = [
            {'id': 1, 'title': 'Doc 1', 'content': 'Content', 'score': 0.9, 'type': 'semantic'},
            {'id': 2, 'title': 'Doc 2', 'content': 'Content', 'score': 0.7, 'type': 'semantic'},
        ]
        
        keyword_results = [
            {'id': 2, 'title': 'Doc 2', 'content': 'Content', 'score': 0.8, 'type': 'keyword'},
            {'id': 3, 'title': 'Doc 3', 'content': 'Content', 'score': 0.6, 'type': 'keyword'},
        ]
        
        combined = self.engine._combine_results(
            semantic_results,
            keyword_results,
            semantic_weight=0.7,
            keyword_weight=0.3
        )
        
        # Doc 2 should have highest score (appears in both)
        self.assertEqual(combined[0]['id'], 2)
        
        # Check combined scoring
        doc2_score = 0.7 * 0.7 + 0.8 * 0.3  # semantic + keyword
        self.assertAlmostEqual(combined[0]['combined_score'], doc2_score, places=2)
    
    def test_reranking(self):
        """Test result reranking"""
        results = [
            {'id': 1, 'title': 'Installation Guide', 'content': 'How to install', 'combined_score': 0.8},
            {'id': 2, 'title': 'User Manual', 'content': 'Installing software', 'combined_score': 0.75},
        ]
        
        reranked = self.engine._rerank_results(results, "install")
        
        # Both should have final scores
        self.assertTrue(all('final_score' in r for r in reranked))
        
        # Title match should boost first result
        title_match_1 = self.engine._calculate_title_match('Installation Guide', 'install')
        title_match_2 = self.engine._calculate_title_match('User Manual', 'install')
        self.assertGreater(title_match_1, title_match_2)


# =============================================================================
# CONTEXT WINDOW OPTIMIZATION TESTS
# =============================================================================

class TestContextWindowOptimizer(TestCase):
    """Test context window optimization"""
    
    def setUp(self):
        self.optimizer = ContextWindowOptimizer(max_context_tokens=1000)
    
    def test_basic_optimization(self):
        """Test basic context optimization"""
        chunks = [
            {'content': 'Chunk 1 content', 'token_count': 100, 'score': 0.9, 'index': 0},
            {'content': 'Chunk 2 content', 'token_count': 200, 'score': 0.8, 'index': 1},
            {'content': 'Chunk 3 content', 'token_count': 150, 'score': 0.7, 'index': 2},
        ]
        
        result = self.optimizer.optimize_context(
            query="test query",
            retrieved_chunks=chunks,
            required_tokens=200
        )
        
        self.assertIn('chunks', result)
        self.assertIn('total_tokens', result)
        self.assertIn('context', result)
        self.assertLessEqual(result['total_tokens'], 800)  # 1000 - 200 reserved
    
    def test_respects_token_limits(self):
        """Test that optimization respects token limits"""
        chunks = [
            {'content': 'x' * 1000, 'token_count': 500, 'score': 0.9, 'index': 0},
            {'content': 'y' * 1000, 'token_count': 500, 'score': 0.8, 'index': 1},
            {'content': 'z' * 1000, 'token_count': 500, 'score': 0.7, 'index': 2},
        ]
        
        result = self.optimizer.optimize_context(
            query="test",
            retrieved_chunks=chunks,
            required_tokens=200
        )
        
        # Should only fit 1 chunk (500 tokens) with 200 reserved = 800 total < 1000
        self.assertEqual(result['num_chunks'], 1)
        self.assertEqual(result['total_tokens'], 500)
    
    def test_chunk_truncation(self):
        """Test chunk truncation when needed"""
        chunks = [
            {'content': 'a' * 2000, 'token_count': 400, 'score': 0.9, 'index': 0},
            {'content': 'b' * 4000, 'token_count': 800, 'score': 0.8, 'index': 1},
        ]
        
        result = self.optimizer.optimize_context(
            query="test",
            retrieved_chunks=chunks,
            required_tokens=100
        )
        
        # First chunk fits, second should be truncated
        self.assertEqual(result['num_chunks'], 2)
        self.assertTrue(result['chunks'][1].get('truncated', False))
    
    def test_chunk_reordering(self):
        """Test that chunks are reordered by document order"""
        chunks = [
            {'content': 'Third', 'token_count': 100, 'score': 0.9, 'index': 2},
            {'content': 'First', 'token_count': 100, 'score': 0.8, 'index': 0},
            {'content': 'Second', 'token_count': 100, 'score': 0.7, 'index': 1},
        ]
        
        result = self.optimizer.optimize_context(
            query="test",
            retrieved_chunks=chunks,
            required_tokens=100
        )
        
        # Should be reordered by index
        self.assertEqual(result['chunks'][0]['content'], 'First')
        self.assertEqual(result['chunks'][1]['content'], 'Second')
        self.assertEqual(result['chunks'][2]['content'], 'Third')


# =============================================================================
# FEEDBACK LEARNING TESTS
# =============================================================================

class TestFeedbackLearningSystem(TestCase):
    """Test self-learning feedback system"""
    
    def setUp(self):
        self.kb = Knowledgebase.objects.create(
            name="test_kb",
            description="Test KB"
        )
        self.feedback_system = FeedbackLearningSystem(self.kb)
    
    def test_interaction_recording(self):
        """Test recording interactions"""
        chunks = [
            {'id': 1, 'content': 'Test content', 'score': 0.9}
        ]
        
        self.feedback_system.record_interaction(
            query="test query",
            retrieved_chunks=chunks,
            llm_response="Test response",
            user_feedback={'helpful': True, 'rating': 5}
        )
        
        self.assertEqual(len(self.feedback_system.feedback_history), 1)
        interaction = self.feedback_system.feedback_history[0]
        self.assertEqual(interaction['query'], "test query")
        self.assertTrue(interaction['feedback']['helpful'])
    
    @patch('examples.rag_examples.Article.objects.get')
    def test_relevance_boosting(self, mock_get):
        """Test boosting relevance for helpful content"""
        mock_article = Mock()
        mock_article.relevance_score = 1.0
        mock_get.return_value = mock_article
        
        chunks = [{'id': 1, 'content': 'Helpful content'}]
        
        self.feedback_system._boost_chunk_relevance(chunks, boost_factor=1.1)
        
        mock_get.assert_called_once_with(id=1)
        self.assertEqual(mock_article.relevance_score, 1.1)
        mock_article.save.assert_called_once()
    
    def test_query_expansion_learning(self):
        """Test learning query expansions"""
        chunks = [
            {'content': 'installation setup configure deploy'},
            {'content': 'setup installation process steps'},
        ]
        
        key_terms = self.feedback_system._extract_key_terms(chunks)
        
        # Should extract common meaningful terms
        self.assertIn('installation', key_terms)
        self.assertIn('setup', key_terms)
        
        # Should filter out short/common words
        self.assertNotIn('the', key_terms)
        self.assertNotIn('a', key_terms)
    
    def test_feedback_pattern_analysis(self):
        """Test analyzing feedback patterns"""
        # Add some test interactions
        for i in range(5):
            self.feedback_system.record_interaction(
                query=f"query {i}",
                retrieved_chunks=[{'id': i, 'content': f'content {i}'}],
                llm_response=f"response {i}",
                user_feedback={'helpful': i % 2 == 0}  # Alternate helpful/unhelpful
            )
        
        helpful_patterns = self.feedback_system._analyze_helpful_patterns()
        unhelpful_patterns = self.feedback_system._analyze_unhelpful_patterns()
        
        # Should identify patterns
        self.assertIn('avg_chunk_count', helpful_patterns)
        self.assertIn('problematic_queries', unhelpful_patterns)
        
        # Check counts
        self.assertEqual(helpful_patterns['avg_chunk_count'], 1.0)
        self.assertEqual(len(unhelpful_patterns['problematic_queries']), 2)


# =============================================================================
# INTEGRATION TESTS
# =============================================================================

class TestRAGPipelineIntegration(TestCase):
    """Test complete RAG pipeline integration"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            password='testpass'
        )
        self.kb = Knowledgebase.objects.create(
            name="integration_test_kb",
            description="Integration test KB",
            owner_id=str(self.user.id)
        )
    
    def test_end_to_end_pipeline(self):
        """Test complete pipeline from document to response"""
        # 1. Chunk document
        chunker = SmartDocumentChunker(ChunkConfig(max_tokens=256))
        content = """
        # User Guide
        
        ## Getting Started
        This guide will help you get started with our application.
        
        ## Installation
        Follow these steps to install the software.
        """
        
        chunks = chunker.chunk_document(content, {"source": "guide.md"})
        self.assertGreater(len(chunks), 0)
        
        # 2. Store in knowledge base
        for chunk in chunks:
            Article.objects.create(
                knowledgebase=self.kb,
                title=f"Chunk {chunk['index']}",
                content=chunk['content'],
                hierarchy_code=f"{chunk['index']:04d}"
            )
        
        # 3. Search
        engine = HybridSearchEngine(self.kb)
        with patch.object(engine, '_semantic_search') as mock_semantic:
            mock_semantic.return_value = [
                {
                    'id': 1,
                    'title': 'Installation',
                    'content': 'Installation steps',
                    'score': 0.9,
                    'type': 'semantic'
                }
            ]
            
            with patch.object(engine, '_keyword_search') as mock_keyword:
                mock_keyword.return_value = []
                
                results = engine.search("How to install?")
                self.assertGreater(len(results), 0)
        
        # 4. Optimize context
        optimizer = ContextWindowOptimizer(max_context_tokens=1000)
        context = optimizer.optimize_context(
            query="How to install?",
            retrieved_chunks=results,
            required_tokens=200
        )
        
        self.assertIn('context', context)
        self.assertLessEqual(context['total_tokens'], 800)
        
        # 5. Record feedback
        feedback = FeedbackLearningSystem(self.kb)
        feedback.record_interaction(
            query="How to install?",
            retrieved_chunks=context['chunks'],
            llm_response="To install, follow these steps...",
            user_feedback={'helpful': True}
        )
        
        self.assertEqual(len(feedback.feedback_history), 1)


# =============================================================================
# PERFORMANCE TESTS
# =============================================================================

class TestPerformance(TestCase):
    """Test system performance"""
    
    def test_chunking_performance(self):
        """Test chunking performance with large documents"""
        import time
        
        chunker = SmartDocumentChunker()
        large_content = "This is a test sentence. " * 1000  # ~5000 words
        
        start_time = time.time()
        chunks = chunker.chunk_document(large_content)
        elapsed = time.time() - start_time
        
        self.assertLess(elapsed, 1.0)  # Should chunk in under 1 second
        self.assertGreater(len(chunks), 0)
    
    def test_search_performance(self):
        """Test search performance with many documents"""
        kb = Knowledgebase.objects.create(name="perf_test")
        
        # Create many articles
        articles = [
            Article(
                knowledgebase=kb,
                title=f"Article {i}",
                content=f"Content for article {i} with various keywords",
                hierarchy_code=f"{i:06d}"
            )
            for i in range(100)
        ]
        Article.objects.bulk_create(articles)
        
        engine = HybridSearchEngine(kb)
        
        import time
        start_time = time.time()
        
        # Mock the search methods to avoid actual embedding calls
        with patch.object(engine, '_semantic_search') as mock_semantic:
            mock_semantic.return_value = []
            with patch.object(engine, '_keyword_search') as mock_keyword:
                mock_keyword.return_value = []
                
                results = engine.search("test query")
                elapsed = time.time() - start_time
        
        self.assertLess(elapsed, 0.5)  # Should search in under 500ms


# =============================================================================
# RUN TESTS
# =============================================================================

if __name__ == '__main__':
    pytest.main([__file__, '-v'])