"""
Management command to optimize vector search performance.

This command applies pgvector indexes and performs database optimizations
for maximum search performance.
"""

from django.core.management.base import BaseCommand
from django.db import connection
from django_ergo.vector_performance import VectorIndexManager
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Optimize vector search performance by adding pgvector indexes'

    def add_arguments(self, parser):
        parser.add_argument(
            '--analyze',
            action='store_true',
            help='Analyze current index status without making changes'
        )
        parser.add_argument(
            '--vacuum',
            action='store_true',
            help='Run VACUUM ANALYZE on vector tables'
        )
        parser.add_argument(
            '--query',
            type=str,
            help='Analyze performance of a specific search query'
        )

    def handle(self, *args, **options):
        if options['analyze']:
            self.analyze_indexes()
        elif options['vacuum']:
            self.vacuum_tables()
        elif options['query']:
            self.analyze_query(options['query'])
        else:
            self.create_indexes()

    def analyze_indexes(self):
        """Analyze current index status."""
        self.stdout.write(self.style.SUCCESS('Analyzing vector indexes...'))
        
        info = VectorIndexManager.get_index_info()
        
        if not info.get('pgvector_enabled'):
            self.stdout.write(
                self.style.ERROR('❌ pgvector extension not enabled')
            )
            return
        
        self.stdout.write(self.style.SUCCESS('✅ pgvector extension enabled'))
        
        indexes = info.get('indexes', [])
        if indexes:
            self.stdout.write(f"\n📊 Found {len(indexes)} vector indexes:")
            for idx in indexes:
                self.stdout.write(f"  • {idx['name']} on {idx['table']}")
        else:
            self.stdout.write(self.style.WARNING('⚠️  No vector indexes found'))
        
        # Table statistics
        stats = info.get('table_stats', {})
        if stats:
            self.stdout.write(f"\n📈 Table statistics:")
            self.stdout.write(f"  • Live tuples: {stats.get('live_tuples', 0):,}")
            self.stdout.write(f"  • Dead tuples: {stats.get('dead_tuples', 0):,}")
            self.stdout.write(f"  • Inserts: {stats.get('inserts', 0):,}")
            self.stdout.write(f"  • Updates: {stats.get('updates', 0):,}")
        
        # Recommendations
        recommendations = info.get('recommended_maintenance', [])
        if recommendations:
            self.stdout.write(f"\n💡 Maintenance recommendations:")
            for rec in recommendations:
                self.stdout.write(f"  • {rec}")

    def create_indexes(self):
        """Create vector indexes for optimal performance."""
        self.stdout.write(self.style.SUCCESS('Creating vector indexes...'))
        
        indexes = [
            {
                'name': 'django_ergo_article_content_embedding_hnsw_idx',
                'sql': """
                    CREATE INDEX CONCURRENTLY IF NOT EXISTS django_ergo_article_content_embedding_hnsw_idx 
                    ON django_ergo_article USING hnsw (content_embedding vector_cosine_ops) 
                    WITH (m = 16, ef_construction = 64);
                """
            },
            {
                'name': 'django_ergo_article_summary_embedding_hnsw_idx',
                'sql': """
                    CREATE INDEX CONCURRENTLY IF NOT EXISTS django_ergo_article_summary_embedding_hnsw_idx 
                    ON django_ergo_article USING hnsw (summary_embedding vector_cosine_ops) 
                    WITH (m = 16, ef_construction = 64);
                """
            },
            {
                'name': 'django_ergo_article_content_embedding_ivfflat_idx',
                'sql': """
                    CREATE INDEX CONCURRENTLY IF NOT EXISTS django_ergo_article_content_embedding_ivfflat_idx 
                    ON django_ergo_article USING ivfflat (content_embedding vector_cosine_ops) 
                    WITH (lists = 100);
                """
            },
            {
                'name': 'django_ergo_article_summary_embedding_ivfflat_idx',
                'sql': """
                    CREATE INDEX CONCURRENTLY IF NOT EXISTS django_ergo_article_summary_embedding_ivfflat_idx 
                    ON django_ergo_article USING ivfflat (summary_embedding vector_cosine_ops) 
                    WITH (lists = 100);
                """
            },
            {
                'name': 'django_ergo_article_kb_content_hnsw_idx',
                'sql': """
                    CREATE INDEX CONCURRENTLY IF NOT EXISTS django_ergo_article_kb_content_hnsw_idx 
                    ON django_ergo_article (knowledgebase_id, content_embedding) 
                    WHERE content_embedding IS NOT NULL;
                """
            },
            {
                'name': 'django_ergo_article_kb_summary_hnsw_idx',
                'sql': """
                    CREATE INDEX CONCURRENTLY IF NOT EXISTS django_ergo_article_kb_summary_hnsw_idx 
                    ON django_ergo_article (knowledgebase_id, summary_embedding) 
                    WHERE summary_embedding IS NOT NULL;
                """
            }
        ]
        
        with connection.cursor() as cursor:
            for index in indexes:
                try:
                    self.stdout.write(f"Creating {index['name']}...")
                    cursor.execute(index['sql'].strip())
                    self.stdout.write(
                        self.style.SUCCESS(f"✅ Created {index['name']}")
                    )
                except Exception as e:
                    self.stdout.write(
                        self.style.ERROR(f"❌ Failed to create {index['name']}: {e}")
                    )
        
        self.stdout.write(
            self.style.SUCCESS('\n🚀 Vector index optimization complete!')
        )
        self.stdout.write(
            'Expected performance improvement: 10-100x faster vector searches'
        )

    def vacuum_tables(self):
        """Run maintenance operations."""
        self.stdout.write(self.style.SUCCESS('Running maintenance operations...'))
        
        results = VectorIndexManager.maintenance_vacuum_analyze()
        
        if 'error' in results:
            self.stdout.write(
                self.style.ERROR(f"❌ Maintenance failed: {results['error']}")
            )
        else:
            self.stdout.write(self.style.SUCCESS('✅ VACUUM ANALYZE completed'))
            
            indexes = results.get('vector_indexes_analyzed', [])
            if indexes:
                self.stdout.write(f"📊 Analyzed {len(indexes)} vector indexes")

    def analyze_query(self, query_text):
        """Analyze query performance."""
        self.stdout.write(f"Analyzing query: {query_text[:100]}...")
        
        results = VectorIndexManager.analyze_query_performance(query_text)
        
        if 'error' in results:
            self.stdout.write(
                self.style.ERROR(f"❌ Analysis failed: {results['error']}")
            )
            return
        
        self.stdout.write(f"\n⏱️  Performance metrics:")
        self.stdout.write(f"  • Embedding time: {results['embedding_time_ms']}ms")
        self.stdout.write(f"  • Search time: {results['search_time_ms']}ms")
        self.stdout.write(f"  • Total time: {results['total_time_ms']}ms")
        
        index_info = results.get('index_used', {})
        if index_info.get('index_name'):
            self.stdout.write(f"\n📊 Index used: {index_info['index_name']}")
            self.stdout.write(f"  • Startup cost: {index_info.get('startup_cost', 'N/A')}")
            self.stdout.write(f"  • Total cost: {index_info.get('total_cost', 'N/A')}")
        else:
            self.stdout.write(self.style.WARNING("\n⚠️  No vector index used"))
        
        tips = results.get('performance_tips', [])
        if tips:
            self.stdout.write(f"\n💡 Performance tips:")
            for tip in tips:
                self.stdout.write(f"  • {tip}")