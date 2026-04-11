"""
Django Ergo - Vector Search Performance Optimization

This module provides pgvector-specific performance optimizations:
- Vector index configuration and management
- Performance monitoring and query analysis
- Database-level optimization settings
- Search query optimization hints

Performance improvements with proper vector indexes:
🚀 10-100x faster vector searches (HNSW indexes)
🚀 Reduced memory usage (efficient index structures)
🚀 Better query planning (PostgreSQL optimization)
🚀 Scalable to millions of vectors
"""

import logging
import time
from typing import Any

from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)


class VectorIndexManager:
    """
    Manages pgvector indexes and performance optimization.
    """

    # Index configuration
    HNSW_M = getattr(
        settings, "DJANGO_ERGO_HNSW_M", 16
    )  # Number of connections for HNSW
    HNSW_EF_CONSTRUCTION = getattr(
        settings, "DJANGO_ERGO_HNSW_EF_CONSTRUCTION", 64
    )  # Search effort during construction
    IVFFLAT_LISTS = getattr(
        settings, "DJANGO_ERGO_IVFFLAT_LISTS", 100
    )  # Number of clusters for IVFFlat

    @classmethod
    def get_index_info(cls) -> dict[str, Any]:
        """
        Get information about vector indexes in the database.

        Returns:
            Dict: Index information and status
        """
        with connection.cursor() as cursor:
            # Check if pgvector extension is installed
            cursor.execute("SELECT extname FROM pg_extension WHERE extname = 'vector';")
            vector_enabled = cursor.fetchone() is not None

            if not vector_enabled:
                return {
                    "pgvector_enabled": False,
                    "error": "pgvector extension not installed",
                }

            # Get vector indexes
            cursor.execute(
                """
                SELECT
                    indexname,
                    tablename,
                    indexdef
                FROM pg_indexes
                WHERE indexdef LIKE '%vector_%'
                ORDER BY indexname;
            """
            )

            indexes = [
                {"name": row[0], "table": row[1], "definition": row[2]}
                for row in cursor.fetchall()
            ]

            # Get table statistics
            cursor.execute(
                """
                SELECT
                    schemaname,
                    tablename,
                    n_tup_ins as inserts,
                    n_tup_upd as updates,
                    n_tup_del as deletes,
                    n_live_tup as live_tuples,
                    n_dead_tup as dead_tuples
                FROM pg_stat_user_tables
                WHERE tablename = 'django_ergo_article';
            """
            )

            table_stats = cursor.fetchone()

            return {
                "pgvector_enabled": True,
                "indexes": indexes,
                "table_stats": {
                    "inserts": table_stats[2] if table_stats else 0,
                    "updates": table_stats[3] if table_stats else 0,
                    "deletes": table_stats[4] if table_stats else 0,
                    "live_tuples": table_stats[5] if table_stats else 0,
                    "dead_tuples": table_stats[6] if table_stats else 0,
                }
                if table_stats
                else {},
                "recommended_maintenance": cls._get_maintenance_recommendations(
                    table_stats
                ),
            }

    @classmethod
    def _get_maintenance_recommendations(cls, table_stats) -> list[str]:
        """Get maintenance recommendations based on table statistics."""
        recommendations = []

        if not table_stats:
            return recommendations

        live_tuples = table_stats[5] or 0
        dead_tuples = table_stats[6] or 0

        if dead_tuples > live_tuples * 0.1:  # More than 10% dead tuples
            recommendations.append(
                "Consider running VACUUM on django_ergo_article table"
            )

        if dead_tuples > live_tuples * 0.2:  # More than 20% dead tuples
            recommendations.append("Consider running REINDEX on vector indexes")

        return recommendations

    @classmethod
    def analyze_query_performance(cls, query_text: str) -> dict[str, Any]:
        """
        Analyze the performance of a vector search query.

        Args:
            query_text: The search query to analyze

        Returns:
            Dict: Performance analysis results
        """
        try:
            from django_ergo.fields import generate_embedding

            # Generate embedding
            start_time = time.time()
            query_vector = generate_embedding(query_text)
            embedding_time = time.time() - start_time

            # Test search performance
            start_time = time.time()
            with connection.cursor() as cursor:
                # Analyze query plan for vector search
                cursor.execute(
                    """
                    EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                    SELECT id, title, content_embedding <=> %s as distance
                    FROM django_ergo_article
                    WHERE content_embedding IS NOT NULL
                    ORDER BY content_embedding <=> %s
                    LIMIT 10;
                """,
                    [query_vector, query_vector],
                )

                query_plan = cursor.fetchone()[0][0]

            search_time = time.time() - start_time

            max_query_display_len = 100
            return {
                "query_text": query_text[:max_query_display_len] + "..."
                if len(query_text) > max_query_display_len
                else query_text,
                "embedding_time_ms": round(embedding_time * 1000, 2),
                "search_time_ms": round(search_time * 1000, 2),
                "total_time_ms": round((embedding_time + search_time) * 1000, 2),
                "query_plan": query_plan,
                "index_used": cls._extract_index_usage(query_plan),
                "performance_tips": cls._get_performance_tips(query_plan),
            }

        except Exception as e:
            logger.exception("Error analyzing query performance")
            return {"error": str(e)}

    @classmethod
    def _extract_index_usage(cls, query_plan: dict) -> dict[str, Any]:
        """Extract index usage information from query plan."""

        def find_index_scan(node):
            if node.get("Node Type") == "Index Scan":
                return {
                    "index_name": node.get("Index Name"),
                    "startup_cost": node.get("Startup Cost"),
                    "total_cost": node.get("Total Cost"),
                    "actual_time": node.get("Actual Total Time"),
                }

            for child in node.get("Plans", []):
                result = find_index_scan(child)
                if result:
                    return result
            return None

        return find_index_scan(query_plan) or {"index_used": False}

    @classmethod
    def _get_performance_tips(cls, query_plan: dict) -> list[str]:
        """Generate performance tips based on query plan."""
        tips = []
        high_cost_threshold = 1000
        high_memory_threshold_kb = 100000  # 100MB in KB

        def analyze_node(node):
            # Check for sequential scans
            if node.get("Node Type") == "Seq Scan":
                tips.append(
                    "Sequential scan detected - ensure vector indexes are created and used"
                )

            # Check for expensive operations
            if node.get("Total Cost", 0) > high_cost_threshold:
                tips.append(
                    "High-cost query detected - consider optimizing search parameters"
                )

            # Check memory usage
            if node.get("Peak Memory Usage", 0) > high_memory_threshold_kb:
                tips.append(
                    "High memory usage - consider reducing top_k or adding more selective filters"
                )

            for child in node.get("Plans", []):
                analyze_node(child)

        analyze_node(query_plan)

        if not tips:
            tips.append("Query performance looks good!")

        return tips

    @classmethod
    def optimize_database_settings(cls) -> dict[str, str]:
        """
        Get recommended PostgreSQL settings for vector search optimization.

        Returns:
            Dict: Recommended settings
        """
        return {
            "shared_preload_libraries": "vector",
            "max_connections": "100",
            "shared_buffers": "256MB",  # Adjust based on available RAM
            "effective_cache_size": "1GB",  # Adjust based on available RAM
            "random_page_cost": "1.1",  # SSD optimization
            "cpu_tuple_cost": "0.01",
            "cpu_index_tuple_cost": "0.005",
            "cpu_operator_cost": "0.0025",
            # Vector-specific settings
            "hnsw.ef_search": "40",  # Runtime search effort for HNSW
            "ivfflat.probes": "10",  # Number of probes for IVFFlat
            # Memory settings for large vector operations
            "work_mem": "64MB",  # For sorting and hash operations
            "maintenance_work_mem": "512MB",  # For index creation
            # Logging for performance monitoring
            "log_min_duration_statement": "1000",  # Log slow queries
            "log_statement": "none",  # Reduce logging overhead
        }

    @classmethod
    def maintenance_vacuum_analyze(cls) -> dict[str, Any]:
        """
        Run maintenance operations on vector tables and indexes.

        Returns:
            Dict: Results of maintenance operations
        """
        results = {}

        try:
            with connection.cursor() as cursor:
                # VACUUM ANALYZE the main table
                logger.info("Running VACUUM ANALYZE on django_ergo_article...")
                cursor.execute("VACUUM ANALYZE django_ergo_article;")
                results["vacuum_analyze"] = "completed"

                # Update index statistics
                logger.info("Updating index statistics...")
                cursor.execute(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE tablename = 'django_ergo_article'
                    AND indexdef LIKE '%vector_%';
                """
                )

                vector_indexes = [row[0] for row in cursor.fetchall()]
                results["vector_indexes_analyzed"] = vector_indexes

                logger.info("Maintenance operations completed successfully")

        except Exception as e:
            logger.exception("Error during maintenance")
            results["error"] = str(e)

        return results
