"""
RAG System Examples - Demonstrating Effective Usage Patterns

These examples show how to build robust RAG applications using django-ergo
for LLMs with limited context windows (100k-2M tokens).
"""

import hashlib
from dataclasses import dataclass
from typing import Any

from django_ergo.fields import semantic_search
from django_ergo.models import Article
from django_ergo.models import Knowledgebase

# =============================================================================
# EXAMPLE 1: Smart Document Chunking for Context Window Management
# =============================================================================


@dataclass
class ChunkConfig:
    """Configuration for intelligent document chunking"""

    max_tokens: int = 512
    min_tokens: int = 100
    overlap_tokens: int = 50
    respect_boundaries: bool = True  # Respect paragraph/section boundaries


class SmartDocumentChunker:
    """
    Intelligently chunks documents while preserving semantic coherence
    and staying within token limits.
    """

    def __init__(self, config: ChunkConfig = None):
        self.config = config or ChunkConfig()

    def chunk_document(
        self, content: str, metadata: dict[str, Any] = None
    ) -> list[dict]:
        """
        Split document into optimal chunks for RAG.

        Example:
            >>> chunker = SmartDocumentChunker(ChunkConfig(max_tokens=256))
            >>> chunks = chunker.chunk_document(
            ...     content="Long document text...",
            ...     metadata={"source": "manual.pdf", "chapter": "Installation"}
            ... )
            >>> print(f"Created {len(chunks)} chunks")
            Created 15 chunks
        """
        chunks = []

        if not content or not content.strip():
            return chunks

        # Split by natural boundaries first (paragraphs)
        paragraphs = content.split("\n\n")

        current_chunk = []
        current_tokens = 0

        for para in paragraphs:
            para_tokens = self._estimate_tokens(para)

            # If paragraph is too large, split it
            if para_tokens > self.config.max_tokens:
                if current_chunk:
                    # Save current chunk
                    chunks.append(
                        self._create_chunk(
                            "\n\n".join(current_chunk), metadata, len(chunks)
                        )
                    )
                    current_chunk = []
                    current_tokens = 0

                # Split large paragraph
                sub_chunks = self._split_large_paragraph(para)
                for sub_chunk in sub_chunks:
                    chunks.append(self._create_chunk(sub_chunk, metadata, len(chunks)))

            # Add to current chunk if it fits
            elif current_tokens + para_tokens <= self.config.max_tokens:
                current_chunk.append(para)
                current_tokens += para_tokens

            # Start new chunk
            else:
                if current_chunk:
                    chunks.append(
                        self._create_chunk(
                            "\n\n".join(current_chunk), metadata, len(chunks)
                        )
                    )

                # Add overlap from previous chunk
                if chunks and self.config.overlap_tokens > 0:
                    overlap_text = self._get_overlap(chunks[-1]["content"])
                    current_chunk = [overlap_text, para]
                    current_tokens = self._estimate_tokens(overlap_text) + para_tokens
                else:
                    current_chunk = [para]
                    current_tokens = para_tokens

        # Don't forget the last chunk
        if current_chunk:
            chunks.append(
                self._create_chunk("\n\n".join(current_chunk), metadata, len(chunks))
            )

        return chunks

    def _estimate_tokens(self, text: str) -> int:
        """Rough token estimation (1 token ≈ 4 characters)"""
        return len(text) // 4

    def _split_large_paragraph(self, para: str) -> list[str]:
        """Split a large paragraph into smaller chunks"""
        sentences = para.split(". ")
        chunks = []
        current = []

        for sentence in sentences:
            candidate = current + [sentence]
            candidate_tokens = self._estimate_tokens(". ".join(candidate))
            if candidate_tokens <= self.config.max_tokens:
                current = candidate
            else:
                if current:
                    chunks.append(". ".join(current) + ".")
                current = [sentence]

        if current:
            chunks.append(". ".join(current) + ".")

        return chunks

    def _get_overlap(self, text: str) -> str:
        """Get overlap text from end of previous chunk"""
        tokens = text.split()[-self.config.overlap_tokens :]
        return " ".join(tokens)

    def _create_chunk(self, content: str, metadata: dict, index: int) -> dict:
        """Create a chunk with metadata"""
        chunk_id = hashlib.md5(f"{content}{index}".encode()).hexdigest()[:8]
        return {
            "id": chunk_id,
            "content": content,
            "index": index,
            "metadata": metadata or {},
            "token_count": self._estimate_tokens(content),
        }


# =============================================================================
# EXAMPLE 2: Hybrid Search with Reranking
# =============================================================================


class HybridSearchEngine:
    """
    Combines semantic search, keyword search, and metadata filtering
    for optimal retrieval quality.
    """

    def __init__(self, knowledge_base: Knowledgebase):
        self.kb = knowledge_base

    def search(
        self,
        query: str,
        semantic_weight: float = 0.7,
        keyword_weight: float = 0.3,
        top_k: int = 10,
        rerank: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Perform hybrid search combining multiple strategies.

        Example:
            >>> engine = HybridSearchEngine(kb)
            >>> results = engine.search(
            ...     "How to configure SSL certificates",
            ...     semantic_weight=0.6,
            ...     keyword_weight=0.4
            ... )
            >>> for r in results[:3]:
            ...     print(f"Score: {r['score']:.3f} - {r['title']}")
            Score: 0.923 - SSL Configuration Guide
            Score: 0.887 - Certificate Installation
            Score: 0.834 - Security Best Practices
        """
        # Semantic search using embeddings
        semantic_results = self._semantic_search(query, top_k * 2)

        # Keyword search
        keyword_results = self._keyword_search(query, top_k * 2)

        # Combine and normalize scores
        combined_results = self._combine_results(
            semantic_results, keyword_results, semantic_weight, keyword_weight
        )

        # Rerank if requested
        if rerank:
            combined_results = self._rerank_results(combined_results, query)

        return combined_results[:top_k]

    def _semantic_search(self, query: str, limit: int) -> list[dict]:
        """Perform semantic search using embeddings"""
        articles = Article.objects.filter(knowledgebase=self.kb)
        results = semantic_search(articles, "content_embedding", query, top_k=limit)

        return [
            {
                "id": article.id,
                "title": article.title,
                "content": article.content,
                "score": score,
                "type": "semantic",
            }
            for article, score in results
        ]

    def _keyword_search(self, query: str, limit: int) -> list[dict]:
        """Perform keyword search"""
        # Use PostgreSQL full-text search
        from django.contrib.postgres.search import SearchQuery
        from django.contrib.postgres.search import SearchRank

        search_query = SearchQuery(query)
        articles = (
            Article.objects.filter(knowledgebase=self.kb)
            .annotate(rank=SearchRank("search_vector", search_query))
            .order_by("-rank")[:limit]
        )

        return [
            {
                "id": article.id,
                "title": article.title,
                "content": article.content,
                "score": article.rank,
                "type": "keyword",
            }
            for article in articles
        ]

    def _combine_results(
        self,
        semantic_results: list[dict],
        keyword_results: list[dict],
        semantic_weight: float,
        keyword_weight: float,
    ) -> list[dict]:
        """Combine results from different search methods"""
        combined = {}

        # Add semantic results
        for result in semantic_results:
            combined[result["id"]] = {
                **result,
                "semantic_score": result["score"],
                "keyword_score": 0,
                "combined_score": result["score"] * semantic_weight,
            }

        # Add/update with keyword results
        for result in keyword_results:
            if result["id"] in combined:
                combined[result["id"]]["keyword_score"] = result["score"]
                combined[result["id"]]["combined_score"] += (
                    result["score"] * keyword_weight
                )
            else:
                combined[result["id"]] = {
                    **result,
                    "semantic_score": 0,
                    "keyword_score": result["score"],
                    "combined_score": result["score"] * keyword_weight,
                }

        # Sort by combined score
        sorted_results = sorted(
            combined.values(), key=lambda x: x["combined_score"], reverse=True
        )

        return sorted_results

    def _rerank_results(self, results: list[dict], query: str) -> list[dict]:
        """Rerank results using additional signals"""
        for result in results:
            # Boost recent content
            recency_score = self._calculate_recency_score(result)

            # Boost if title matches query
            title_match_score = self._calculate_title_match(result["title"], query)

            # Adjust combined score
            result["final_score"] = (
                result["combined_score"] * 0.7
                + recency_score * 0.2
                + title_match_score * 0.1
            )

        return sorted(results, key=lambda x: x["final_score"], reverse=True)

    def _calculate_recency_score(self, result: dict) -> float:
        """Calculate score based on content recency"""
        # Implementation would check creation/update date
        return 0.5  # Placeholder

    def _calculate_title_match(self, title: str, query: str) -> float:
        """Calculate title-query similarity"""
        query_words = query.lower().split()
        title_lower = title.lower()
        if not query_words:
            return 0
        matches = sum(1 for w in query_words if w in title_lower)
        return matches / len(query_words)


# =============================================================================
# EXAMPLE 3: Context Window Optimizer
# =============================================================================


class ContextWindowOptimizer:
    """
    Optimizes retrieved content to fit within LLM context windows
    while maximizing relevance.
    """

    def __init__(self, max_context_tokens: int = 4000):
        self.max_context_tokens = max_context_tokens

    def optimize_context(
        self,
        query: str,
        retrieved_chunks: list[dict],
        required_tokens: int = 1000,  # Reserve for prompt/response
    ) -> dict[str, Any]:
        """
        Select optimal chunks that fit within context window.

        Example:
            >>> optimizer = ContextWindowOptimizer(max_context_tokens=4000)
            >>> context = optimizer.optimize_context(
            ...     query="How to deploy?",
            ...     retrieved_chunks=chunks,
            ...     required_tokens=1000
            ... )
            >>> print(f"Selected {context['num_chunks']} chunks")
            >>> print(f"Total tokens: {context['total_tokens']}")
            Selected 5 chunks
            Total tokens: 2987
        """
        available_tokens = self.max_context_tokens - required_tokens

        selected_chunks = []
        total_tokens = 0

        # Sort chunks by relevance (assuming they have scores)
        sorted_chunks = sorted(
            retrieved_chunks, key=lambda x: x.get("score", 0), reverse=True
        )

        for chunk in sorted_chunks:
            chunk_tokens = chunk.get("token_count", len(chunk["content"]) // 4)

            if total_tokens + chunk_tokens <= available_tokens:
                selected_chunks.append(chunk)
                total_tokens += chunk_tokens
            else:
                # Try to fit a truncated version
                remaining_tokens = available_tokens - total_tokens
                if remaining_tokens > 100:  # Minimum useful chunk size
                    truncated = self._truncate_chunk(chunk, remaining_tokens)
                    selected_chunks.append(truncated)
                    total_tokens += truncated["token_count"]
                break

        # Reorder selected chunks by original document order
        selected_chunks.sort(key=lambda x: x.get("index", 0))

        return {
            "chunks": selected_chunks,
            "num_chunks": len(selected_chunks),
            "total_tokens": total_tokens,
            "context": self._build_context_string(selected_chunks),
            "metadata": self._extract_metadata(selected_chunks),
        }

    def _truncate_chunk(self, chunk: dict, max_tokens: int) -> dict:
        """Truncate chunk to fit within token limit"""
        content = chunk["content"]
        # Rough truncation (1 token ≈ 4 characters)
        max_chars = max_tokens * 4
        truncated_content = content[:max_chars] + "..."

        return {
            **chunk,
            "content": truncated_content,
            "token_count": max_tokens,
            "truncated": True,
        }

    def _build_context_string(self, chunks: list[dict]) -> str:
        """Build context string from selected chunks"""
        context_parts = []

        for i, chunk in enumerate(chunks):
            source = chunk.get("metadata", {}).get("source", "Unknown")
            context_parts.append(f"[Source: {source}]\n{chunk['content']}")

        return "\n\n---\n\n".join(context_parts)

    def _extract_metadata(self, chunks: list[dict]) -> dict:
        """Extract useful metadata from chunks"""
        sources = set()
        chapters = set()

        for chunk in chunks:
            metadata = chunk.get("metadata", {})
            if "source" in metadata:
                sources.add(metadata["source"])
            if "chapter" in metadata:
                chapters.add(metadata["chapter"])

        return {
            "sources": list(sources),
            "chapters": list(chapters),
            "chunk_count": len(chunks),
        }


# =============================================================================
# EXAMPLE 4: Self-Learning Feedback System
# =============================================================================


class FeedbackLearningSystem:
    """
    Implements self-learning through user feedback and LLM interactions.
    """

    def __init__(self, knowledge_base: Knowledgebase):
        self.kb = knowledge_base
        self.feedback_history = []

    def record_interaction(
        self,
        query: str,
        retrieved_chunks: list[dict],
        llm_response: str,
        user_feedback: dict | None = None,
    ):
        """
        Record an interaction for learning.

        Example:
            >>> feedback_system = FeedbackLearningSystem(kb)
            >>> feedback_system.record_interaction(
            ...     query="How to install?",
            ...     retrieved_chunks=chunks,
            ...     llm_response="To install, first...",
            ...     user_feedback={'helpful': True, 'rating': 5}
            ... )
            >>> feedback_system.optimize_based_on_feedback()
        """
        interaction = {
            "query": query,
            "chunks": retrieved_chunks,
            "response": llm_response,
            "feedback": user_feedback,
            "timestamp": self._get_timestamp(),
        }

        self.feedback_history.append(interaction)

        # Learn from this interaction
        if user_feedback:
            self._process_feedback(interaction)

    def _process_feedback(self, interaction: dict):
        """Process feedback to improve future retrievals"""
        feedback = interaction["feedback"]

        if feedback.get("helpful", False):
            # Boost relevance of helpful chunks
            self._boost_chunk_relevance(
                interaction["chunks"][:3],  # Top 3 chunks
                boost_factor=1.1,
            )
        else:
            # Decrease relevance of unhelpful chunks
            self._decrease_chunk_relevance(interaction["chunks"], decrease_factor=0.9)

        # Learn query expansions
        if feedback.get("rating", 0) >= 4:
            self._learn_query_expansion(interaction["query"], interaction["chunks"])

    def _boost_chunk_relevance(self, chunks: list[dict], boost_factor: float):
        """Increase relevance scores for helpful chunks"""
        for chunk in chunks:
            # In production, this would update a relevance score in the database
            article_id = chunk.get("id")
            if article_id:
                # Update article relevance score
                article = Article.objects.get(id=article_id)
                article.relevance_score = (
                    getattr(article, "relevance_score", 1.0) * boost_factor
                )
                article.save()

    def _decrease_chunk_relevance(self, chunks: list[dict], decrease_factor: float):
        """Decrease relevance scores for unhelpful chunks"""
        for chunk in chunks:
            article_id = chunk.get("id")
            if article_id:
                article = Article.objects.get(id=article_id)
                article.relevance_score = (
                    getattr(article, "relevance_score", 1.0) * decrease_factor
                )
                article.save()

    def _learn_query_expansion(self, query: str, successful_chunks: list[dict]):
        """Learn query expansions from successful retrievals"""
        # Extract key terms from successful chunks
        key_terms = self._extract_key_terms(successful_chunks)

        # Store query expansion mapping
        self._store_query_expansion(query, key_terms)

    def _extract_key_terms(self, chunks: list[dict]) -> list[str]:
        """Extract key terms from chunks"""
        # Simplified - in production use TF-IDF or similar
        all_words = []
        for chunk in chunks:
            words = chunk["content"].lower().split()
            all_words.extend(words)

        # Get most common meaningful words
        from collections import Counter

        word_counts = Counter(all_words)

        # Filter out common words
        stopwords = {
            "the",
            "a",
            "an",
            "and",
            "or",
            "but",
            "in",
            "on",
            "at",
            "to",
            "for",
        }
        key_terms = [
            word
            for word, count in word_counts.most_common(10)
            if word not in stopwords and len(word) > 3
        ]

        return key_terms

    def _store_query_expansion(self, query: str, expansion_terms: list[str]):
        """Store query expansion for future use"""
        # In production, store in database
        print(f"Learned expansion for '{query}': {expansion_terms}")

    def _get_timestamp(self):
        """Get current timestamp"""
        from datetime import datetime

        return datetime.now().isoformat()

    def optimize_based_on_feedback(self):
        """Periodic optimization based on accumulated feedback"""
        # Analyze feedback patterns
        helpful_patterns = self._analyze_helpful_patterns()
        unhelpful_patterns = self._analyze_unhelpful_patterns()

        # Reorganize knowledge base based on patterns
        self._reorganize_knowledge_base(helpful_patterns, unhelpful_patterns)

        # Update embeddings for frequently accessed content
        self._update_popular_embeddings()

    def _analyze_helpful_patterns(self) -> dict:
        """Analyze patterns in helpful interactions"""
        helpful = [
            i
            for i in self.feedback_history
            if i.get("feedback", {}).get("helpful", False)
        ]

        # Find common characteristics
        return {
            "avg_chunk_count": sum(len(i["chunks"]) for i in helpful) / len(helpful)
            if helpful
            else 0,
            "common_sources": self._get_common_sources(helpful),
            "query_patterns": self._extract_query_patterns(helpful),
        }

    def _analyze_unhelpful_patterns(self) -> dict:
        """Analyze patterns in unhelpful interactions"""
        unhelpful = [
            i
            for i in self.feedback_history
            if not i.get("feedback", {}).get("helpful", True)
        ]

        return {
            "problematic_queries": [i["query"] for i in unhelpful],
            "failed_sources": self._get_common_sources(unhelpful),
        }

    def _get_common_sources(self, interactions: list[dict]) -> list[str]:
        """Get most common sources from interactions"""
        sources = []
        for interaction in interactions:
            for chunk in interaction["chunks"]:
                if "metadata" in chunk and "source" in chunk["metadata"]:
                    sources.append(chunk["metadata"]["source"])

        from collections import Counter

        return [s for s, _ in Counter(sources).most_common(5)]

    def _extract_query_patterns(self, interactions: list[dict]) -> list[str]:
        """Extract common query patterns"""
        queries = [i["query"] for i in interactions]
        # In production, use more sophisticated pattern extraction
        return queries[:10]

    def _reorganize_knowledge_base(self, helpful: dict, unhelpful: dict):
        """Reorganize knowledge base based on feedback analysis"""
        print("Reorganizing based on feedback patterns...")
        print(f"Helpful patterns: {helpful}")
        print(f"Unhelpful patterns: {unhelpful}")

    def _update_popular_embeddings(self):
        """Update embeddings for frequently accessed content"""
        # In production, identify and re-embed popular content


# =============================================================================
# USAGE EXAMPLES
# =============================================================================


def demonstrate_rag_pipeline():
    """
    Complete example showing the full RAG pipeline.
    """
    # Initialize knowledge base
    kb = Knowledgebase.objects.create(
        name="technical_docs", description="Technical documentation"
    )

    # 1. Smart chunking
    chunker = SmartDocumentChunker(ChunkConfig(max_tokens=512, overlap_tokens=50))

    document_content = """
    # Installation Guide

    This guide explains how to install our software on various platforms.

    ## Prerequisites

    Before installation, ensure you have Python 3.8 or higher installed.
    You'll also need pip for package management.

    ## Installation Steps

    1. Clone the repository
    2. Install dependencies with pip install -r requirements.txt
    3. Configure your environment variables
    4. Run the setup script

    ## Verification

    After installation, verify everything works by running the test suite.
    """

    chunks = chunker.chunk_document(
        document_content, metadata={"source": "install_guide.md", "version": "1.0"}
    )

    print(f"Created {len(chunks)} chunks from document")

    # 2. Store chunks in knowledge base
    for chunk in chunks:
        Article.objects.create(
            knowledgebase=kb,
            title=f"Chunk {chunk['index']}",
            content=chunk["content"],
            hierarchy_code=f"{chunk['index']:04d}",
            metadata=chunk["metadata"],
        )

    # 3. Hybrid search
    search_engine = HybridSearchEngine(kb)
    results = search_engine.search(
        "How do I install the software?",
        semantic_weight=0.7,
        keyword_weight=0.3,
        top_k=5,
    )

    print(f"\nFound {len(results)} relevant chunks")

    # 4. Optimize for context window
    optimizer = ContextWindowOptimizer(max_context_tokens=2000)
    optimized_context = optimizer.optimize_context(
        query="How do I install the software?",
        retrieved_chunks=results,
        required_tokens=500,  # Reserve for prompt
    )

    print(f"\nOptimized context uses {optimized_context['total_tokens']} tokens")
    print(f"Selected {optimized_context['num_chunks']} chunks")

    # 5. Simulate LLM interaction with feedback
    llm_response = "To install the software, follow these steps..."

    feedback_system = FeedbackLearningSystem(kb)
    feedback_system.record_interaction(
        query="How do I install the software?",
        retrieved_chunks=optimized_context["chunks"],
        llm_response=llm_response,
        user_feedback={"helpful": True, "rating": 5},
    )

    print("\nFeedback recorded for continuous improvement")

    return {
        "chunks": chunks,
        "search_results": results,
        "optimized_context": optimized_context,
        "feedback_recorded": True,
    }


if __name__ == "__main__":
    # Run demonstration
    result = demonstrate_rag_pipeline()
    print("\nRAG Pipeline Demonstration Complete!")
    print(f"Results: {result}")
