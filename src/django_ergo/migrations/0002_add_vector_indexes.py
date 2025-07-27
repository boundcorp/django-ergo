# Generated migration for adding pgvector indexes

from django.db import migrations
from django.contrib.postgres.operations import AddIndexConcurrently


class Migration(migrations.Migration):

    dependencies = [
        ('django_ergo', '0001_initial'),
    ]

    # Allow non-atomic migration for concurrent index creation
    atomic = False

    operations = [
        # Add HNSW index for content_embedding field
        # HNSW (Hierarchical Navigable Small World) is optimal for cosine distance queries
        migrations.RunSQL(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS django_ergo_article_content_embedding_hnsw_idx "
            "ON django_ergo_article USING hnsw (content_embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64);",
            reverse_sql="DROP INDEX IF EXISTS django_ergo_article_content_embedding_hnsw_idx;"
        ),
        
        # Add HNSW index for summary_embedding field
        migrations.RunSQL(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS django_ergo_article_summary_embedding_hnsw_idx "
            "ON django_ergo_article USING hnsw (summary_embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64);",
            reverse_sql="DROP INDEX IF EXISTS django_ergo_article_summary_embedding_hnsw_idx;"
        ),
        
        # Add IVFFlat index for content_embedding as alternative
        # IVFFlat can be better for smaller datasets or different query patterns
        migrations.RunSQL(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS django_ergo_article_content_embedding_ivfflat_idx "
            "ON django_ergo_article USING ivfflat (content_embedding vector_cosine_ops) "
            "WITH (lists = 100);",
            reverse_sql="DROP INDEX IF EXISTS django_ergo_article_content_embedding_ivfflat_idx;"
        ),
        
        # Add IVFFlat index for summary_embedding
        migrations.RunSQL(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS django_ergo_article_summary_embedding_ivfflat_idx "
            "ON django_ergo_article USING ivfflat (summary_embedding vector_cosine_ops) "
            "WITH (lists = 100);",
            reverse_sql="DROP INDEX IF EXISTS django_ergo_article_summary_embedding_ivfflat_idx;"
        ),
        
        # Add composite index for knowledgebase filtering with vector search
        migrations.RunSQL(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS django_ergo_article_kb_content_hnsw_idx "
            "ON django_ergo_article (knowledgebase_id, content_embedding) "
            "WHERE content_embedding IS NOT NULL;",
            reverse_sql="DROP INDEX IF EXISTS django_ergo_article_kb_content_hnsw_idx;"
        ),
        
        # Add composite index for knowledgebase filtering with summary search
        migrations.RunSQL(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS django_ergo_article_kb_summary_hnsw_idx "
            "ON django_ergo_article (knowledgebase_id, summary_embedding) "
            "WHERE summary_embedding IS NOT NULL;",
            reverse_sql="DROP INDEX IF EXISTS django_ergo_article_kb_summary_hnsw_idx;"
        ),
    ]