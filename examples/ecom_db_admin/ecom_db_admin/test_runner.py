"""
Custom test runner that ensures pgvector extension is available in test databases.
"""
from django.test.runner import DiscoverRunner
from django.db import connection
from django.core.management.color import no_style


class PgvectorTestRunner(DiscoverRunner):
    """Test runner that creates pgvector extension in test database."""
    
    def setup_databases(self, **kwargs):
        """Setup test databases and ensure pgvector extension is available."""
        result = super().setup_databases(**kwargs)
        
        # Create pgvector extension in test database
        with connection.cursor() as cursor:
            try:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                connection.commit()
                print("✅ pgvector extension created in test database")
            except Exception as e:
                print(f"⚠️ Could not create pgvector extension: {e}")
                
        return result