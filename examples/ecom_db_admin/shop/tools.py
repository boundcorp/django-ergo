"""
Database admin tools for natural language SQL queries.
"""
import logging
from typing import Dict, Any, List
from django.db import connection
from django_ergo.tools import Tool, ToolParameter, tool_registry

logger = logging.getLogger(__name__)


@tool_registry.register
class QueryDatabaseTool(Tool):
    """Execute read-only SELECT queries on the database."""
    
    name = "query_database"
    description = "Execute a SELECT query to retrieve data from the database"
    
    parameters = [
        ToolParameter(
            name="query",
            description="The SELECT SQL query to execute",
            type="string",
            required=True
        ),
        ToolParameter(
            name="explanation",
            description="Natural language explanation of what this query does",
            type="string",
            required=True
        )
    ]
    
    def execute(self, query: str, explanation: str, **kwargs) -> Dict[str, Any]:
        """Execute a read-only database query."""
        # Validate it's a SELECT query
        normalized_query = query.strip().upper()
        if not normalized_query.startswith('SELECT'):
            return {
                "success": False,
                "error": "Only SELECT queries are allowed with this tool"
            }
        
        # Check for dangerous keywords
        dangerous_keywords = ['INSERT', 'UPDATE', 'DELETE', 'DROP', 'CREATE', 'ALTER', 'TRUNCATE']
        for keyword in dangerous_keywords:
            if keyword in normalized_query:
                return {
                    "success": False,
                    "error": f"Query contains forbidden keyword: {keyword}"
                }
        
        try:
            with connection.cursor() as cursor:
                cursor.execute(query)
                columns = [col[0] for col in cursor.description]
                rows = cursor.fetchall()
                
                # Convert to list of dicts for easier consumption
                results = []
                for row in rows:
                    results.append(dict(zip(columns, row)))
                
                return {
                    "success": True,
                    "explanation": explanation,
                    "query": query,
                    "columns": columns,
                    "row_count": len(results),
                    "results": results[:100],  # Limit to 100 rows
                    "truncated": len(results) > 100
                }
                
        except Exception as e:
            logger.error(f"Database query error: {str(e)}")
            return {
                "success": False,
                "error": str(e),
                "query": query
            }


@tool_registry.register
class ModifyDatabaseTool(Tool):
    """Execute write operations (INSERT, UPDATE, DELETE) on the database."""
    
    name = "modify_database"
    description = "Execute INSERT, UPDATE, or DELETE queries to modify database data"
    requires_approval = True  # This tool requires user approval
    
    parameters = [
        ToolParameter(
            name="query",
            description="The SQL query to execute (INSERT, UPDATE, or DELETE)",
            type="string",
            required=True
        ),
        ToolParameter(
            name="explanation",
            description="Natural language explanation of what this modification does",
            type="string",
            required=True
        ),
        ToolParameter(
            name="affected_table",
            description="The primary table being modified",
            type="string",
            required=True
        )
    ]
    
    def execute(self, query: str, explanation: str, affected_table: str, **kwargs) -> Dict[str, Any]:
        """Execute a database modification query."""
        # Validate it's a modification query
        normalized_query = query.strip().upper()
        allowed_operations = ['INSERT', 'UPDATE', 'DELETE']
        
        is_allowed = False
        operation = None
        for op in allowed_operations:
            if normalized_query.startswith(op):
                is_allowed = True
                operation = op
                break
        
        if not is_allowed:
            return {
                "success": False,
                "error": "Only INSERT, UPDATE, or DELETE queries are allowed"
            }
        
        # Extra safety: prevent operations on system tables
        system_tables = ['auth_user', 'django_migrations', 'django_content_type', 'django_session']
        if affected_table.lower() in system_tables:
            return {
                "success": False,
                "error": f"Cannot modify system table: {affected_table}"
            }
        
        try:
            with connection.cursor() as cursor:
                cursor.execute(query)
                affected_rows = cursor.rowcount
                
                return {
                    "success": True,
                    "operation": operation,
                    "explanation": explanation,
                    "query": query,
                    "affected_table": affected_table,
                    "affected_rows": affected_rows,
                    "message": f"Successfully executed {operation} on {affected_table}, {affected_rows} rows affected"
                }
                
        except Exception as e:
            logger.error(f"Database modification error: {str(e)}")
            return {
                "success": False,
                "error": str(e),
                "query": query
            }


def get_database_schema() -> List[Dict[str, Any]]:
    """Get the schema information for all tables in the database."""
    with connection.cursor() as cursor:
        # Get all tables
        cursor.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public' 
            AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
        tables = [row[0] for row in cursor.fetchall()]
        
        schema_info = []
        for table in tables:
            # Get columns for each table
            cursor.execute("""
                SELECT 
                    column_name,
                    data_type,
                    is_nullable,
                    column_default
                FROM information_schema.columns
                WHERE table_schema = 'public'
                AND table_name = %s
                ORDER BY ordinal_position
            """, [table])
            
            columns = []
            for col in cursor.fetchall():
                columns.append({
                    'name': col[0],
                    'type': col[1],
                    'nullable': col[2] == 'YES',
                    'default': col[3]
                })
            
            schema_info.append({
                'table': table,
                'columns': columns
            })
        
        return schema_info