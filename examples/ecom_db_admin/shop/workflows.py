"""
Database Admin workflow for natural language SQL queries.
"""
import logging
from typing import Dict, Any, Optional
from django.contrib.auth.models import User
from django_ergo.models import Workflow, Knowledgebase
from django_ergo.workflow_engine import workflow_engine
from .tools import get_database_schema

logger = logging.getLogger(__name__)


def get_db_admin_system_prompt() -> str:
    """Get the system prompt with database schema information."""
    # Get current database schema
    schema = get_database_schema()
    
    # Format schema for the prompt
    schema_text = "DATABASE SCHEMA:\n\n"
    for table_info in schema:
        schema_text += f"Table: {table_info['table']}\n"
        schema_text += "Columns:\n"
        for col in table_info['columns']:
            nullable = "NULL" if col['nullable'] else "NOT NULL"
            default = f" DEFAULT {col['default']}" if col['default'] else ""
            schema_text += f"  - {col['name']} ({col['type']}) {nullable}{default}\n"
        schema_text += "\n"
    
    return f"""You are a database administration assistant for an e-commerce system.
Your role is to help users query and manage their database using natural language.

{schema_text}

IMPORTANT GUIDELINES:
1. For read operations (SELECT), use the 'query_database' tool
2. For write operations (INSERT, UPDATE, DELETE), use the 'modify_database' tool
3. Always explain what the query does in plain language
4. Be careful with DELETE and UPDATE operations - confirm the WHERE clause
5. When showing results, format them clearly and highlight important information
6. If a query might affect many rows, warn the user
7. For complex queries, break down what each part does

The Shop Wiki knowledge base contains information about:
- Store policies (return policy, shipping, etc.)
- Business rules and procedures
- Product categories and classifications
- Common customer questions and answers

Use this knowledge to provide context when answering questions."""


def create_db_admin_workflow(user: User, name: str = "DB Admin Assistant") -> Workflow:
    """Create a database admin workflow for a user."""
    # Get or create the Shop Wiki knowledge base
    shop_wiki, _ = Knowledgebase.objects.get_or_create(
        name="Shop Wiki",
        defaults={
            'description': "E-commerce store policies, procedures, and business knowledge",
            'owner': user
        }
    )
    
    workflow = Workflow.objects.create(
        name=name,
        description="Natural language database administration assistant",
        instructions=get_db_admin_system_prompt(),
        owner=user,
        tools_config={
            "available_tools": [
                'shop.tools.query_database',
                'shop.tools.modify_database',
                'search_user_kb',
            ],
            "approved_tools": [
                'shop.tools.query_database',  # Read-only queries don't need approval
                'search_user_kb',
            ]
        },
        knowledgebase=shop_wiki
    )
    
    return workflow


def format_query_results(results: Dict[str, Any]) -> str:
    """Format database query results for display."""
    if not results.get('success'):
        return f"❌ Query failed: {results.get('error', 'Unknown error')}"
    
    if results.get('row_count', 0) == 0:
        return "No results found."
    
    # Format as a simple table
    output = [f"Found {results['row_count']} results:\n"]
    
    if results.get('truncated'):
        output.append("(Showing first 100 rows)\n")
    
    # Get results
    rows = results.get('results', [])
    if not rows:
        return "No data to display."
    
    # Get column names
    columns = list(rows[0].keys())
    
    # Simple text table
    output.append(" | ".join(columns))
    output.append("-" * (len(" | ".join(columns))))
    
    for row in rows[:10]:  # Show first 10 rows in formatted output
        values = [str(row.get(col, '')) for col in columns]
        output.append(" | ".join(values))
    
    if len(rows) > 10:
        output.append(f"\n... and {len(rows) - 10} more rows")
    
    return "\n".join(output)


# Register the workflow
def register_workflows():
    """Register all workflows for this app."""
    # For now, workflows are created on-demand rather than pre-registered
    # This matches the existing Django Ergo pattern
    pass