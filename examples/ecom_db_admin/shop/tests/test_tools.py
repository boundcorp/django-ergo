"""
Tests for database tools.
"""
from unittest.mock import patch, MagicMock
from django.test import TestCase
from django.contrib.auth.models import User
from django.db import connection
from shop.tools import QueryDatabaseTool, ModifyDatabaseTool, get_database_schema
from shop.models import Product, Customer, Order


class QueryDatabaseToolTests(TestCase):
    """Tests for the QueryDatabaseTool."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass'
        )
        self.tool = QueryDatabaseTool()
    
    def test_tool_properties(self):
        """Test tool properties are correct."""
        self.assertEqual(self.tool.name, "query_database")
        self.assertIn("SELECT query", self.tool.description)
        self.assertEqual(len(self.tool.parameters), 2)
        
        # Check parameter names
        param_names = [p.name for p in self.tool.parameters]
        self.assertIn("query", param_names)
        self.assertIn("explanation", param_names)
    
    def test_valid_select_query(self):
        """Test that valid SELECT queries are executed."""
        query = "SELECT COUNT(*) as total FROM auth_user"
        explanation = "Count total users"
        
        result = self.tool.execute(
            query=query,
            explanation=explanation
        )
        
        self.assertTrue(result["success"])
        self.assertEqual(result["explanation"], explanation)
        self.assertEqual(result["query"], query)
        self.assertIn("results", result)
        self.assertIn("row_count", result)
        self.assertIn("columns", result)
    
    def test_rejects_non_select_queries(self):
        """Test that non-SELECT queries are rejected."""
        invalid_queries = [
            "INSERT INTO auth_user (username) VALUES ('test')",
            "UPDATE auth_user SET username='test'",
            "DELETE FROM auth_user",
            "DROP TABLE auth_user",
            "CREATE TABLE test (id int)"
        ]
        
        for query in invalid_queries:
            result = self.tool.execute(
                query=query,
                explanation="Invalid query test"
            )
            
            self.assertFalse(result["success"])
            self.assertIn("Only SELECT queries are allowed", result["error"])
    
    def test_rejects_queries_with_dangerous_keywords(self):
        """Test that SELECT queries with dangerous keywords are rejected."""
        dangerous_queries = [
            "SELECT * FROM auth_user; DROP TABLE auth_user;",
            "SELECT * FROM auth_user WHERE username='test' AND (UPDATE auth_user SET username='hacked')",
            "SELECT * FROM auth_user UNION ALL SELECT * FROM (INSERT INTO auth_user VALUES (1,'hack'))"
        ]
        
        for query in dangerous_queries:
            result = self.tool.execute(
                query=query,
                explanation="Dangerous query test"
            )
            
            self.assertFalse(result["success"])
            self.assertIn("forbidden keyword", result["error"])
    
    def test_handles_database_errors(self):
        """Test that database errors are handled gracefully."""
        query = "SELECT * FROM nonexistent_table"
        explanation = "Query non-existent table"
        
        result = self.tool.execute(
            query=query,
            explanation=explanation
        )
        
        self.assertFalse(result["success"])
        self.assertIn("error", result)
        self.assertEqual(result["query"], query)
    
    def test_limits_result_rows(self):
        """Test that results are limited to 100 rows."""
        # Create more than 100 users to test truncation
        users = []
        for i in range(105):
            users.append(User(username=f'user{i}', email=f'user{i}@test.com'))
        User.objects.bulk_create(users)
        
        query = "SELECT id, username FROM auth_user ORDER BY id"
        explanation = "Get all users"
        
        result = self.tool.execute(
            query=query,
            explanation=explanation
        )
        
        self.assertTrue(result["success"])
        self.assertEqual(len(result["results"]), 100)
        self.assertTrue(result["truncated"])
        self.assertGreater(result["row_count"], 100)


class ModifyDatabaseToolTests(TestCase):
    """Tests for the ModifyDatabaseTool."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass'
        )
        self.tool = ModifyDatabaseTool()
    
    def test_tool_properties(self):
        """Test tool properties are correct."""
        self.assertEqual(self.tool.name, "modify_database")
        self.assertTrue(self.tool.requires_approval)
        self.assertEqual(len(self.tool.parameters), 3)
        
        # Check parameter names
        param_names = [p.name for p in self.tool.parameters]
        self.assertIn("query", param_names)
        self.assertIn("explanation", param_names)
        self.assertIn("affected_table", param_names)
    
    def test_rejects_non_modification_queries(self):
        """Test that non-modification queries are rejected."""
        invalid_queries = [
            "SELECT * FROM auth_user",
            "SHOW TABLES",
            "DESCRIBE auth_user"
        ]
        
        for query in invalid_queries:
            result = self.tool.execute(
                query=query,
                explanation="Invalid query test",
                affected_table="auth_user"
            )
            
            self.assertFalse(result["success"])
            self.assertIn("Only INSERT, UPDATE, or DELETE", result["error"])
    
    def test_rejects_system_table_modifications(self):
        """Test that system table modifications are rejected."""
        system_tables = ['auth_user', 'django_migrations', 'django_content_type', 'django_session']
        
        for table in system_tables:
            result = self.tool.execute(
                query=f"DELETE FROM {table}",
                explanation="Dangerous system table modification",
                affected_table=table
            )
            
            self.assertFalse(result["success"])
            self.assertIn("Cannot modify system table", result["error"])
    
    @patch('django.db.connection.cursor')
    def test_successful_insert(self, mock_cursor):
        """Test successful INSERT operation."""
        mock_cursor_obj = MagicMock()
        mock_cursor_obj.rowcount = 1
        mock_cursor.return_value.__enter__.return_value = mock_cursor_obj
        
        result = self.tool.execute(
            query="INSERT INTO shop_product (name, sku, price) VALUES ('Test Product', 'TEST001', 29.99)",
            explanation="Insert test product",
            affected_table="shop_product"
        )
        
        self.assertTrue(result["success"])
        self.assertEqual(result["operation"], "INSERT")
        self.assertEqual(result["affected_table"], "shop_product")
        self.assertEqual(result["affected_rows"], 1)
        self.assertIn("Successfully executed", result["message"])
    
    @patch('django.db.connection.cursor')
    def test_successful_update(self, mock_cursor):
        """Test successful UPDATE operation."""
        mock_cursor_obj = MagicMock()
        mock_cursor_obj.rowcount = 5
        mock_cursor.return_value.__enter__.return_value = mock_cursor_obj
        
        result = self.tool.execute(
            query="UPDATE shop_product SET price = price * 1.1 WHERE category = 'Electronics'",
            explanation="Increase electronics prices by 10%",
            affected_table="shop_product"
        )
        
        self.assertTrue(result["success"])
        self.assertEqual(result["operation"], "UPDATE")
        self.assertEqual(result["affected_table"], "shop_product")
        self.assertEqual(result["affected_rows"], 5)
    
    @patch('django.db.connection.cursor')
    def test_successful_delete(self, mock_cursor):
        """Test successful DELETE operation."""
        mock_cursor_obj = MagicMock()
        mock_cursor_obj.rowcount = 3
        mock_cursor.return_value.__enter__.return_value = mock_cursor_obj
        
        result = self.tool.execute(
            query="DELETE FROM shop_product WHERE stock_quantity = 0 AND is_active = false",
            explanation="Remove inactive products with no stock",
            affected_table="shop_product"
        )
        
        self.assertTrue(result["success"])
        self.assertEqual(result["operation"], "DELETE")
        self.assertEqual(result["affected_table"], "shop_product")
        self.assertEqual(result["affected_rows"], 3)
    
    @patch('django.db.connection.cursor')
    def test_database_error_handling(self, mock_cursor):
        """Test database error handling."""
        mock_cursor_obj = MagicMock()
        mock_cursor_obj.execute.side_effect = Exception("Database constraint violation")
        mock_cursor.return_value.__enter__.return_value = mock_cursor_obj
        
        result = self.tool.execute(
            query="INSERT INTO shop_product (name) VALUES (NULL)",
            explanation="Insert invalid product",
            affected_table="shop_product"
        )
        
        self.assertFalse(result["success"])
        self.assertIn("Database constraint violation", result["error"])
        self.assertEqual(result["query"], "INSERT INTO shop_product (name) VALUES (NULL)")


class DatabaseSchemaTests(TestCase):
    """Tests for database schema inspection."""
    
    def test_get_database_schema(self):
        """Test schema inspection returns expected structure."""
        schema = get_database_schema()
        
        self.assertIsInstance(schema, list)
        self.assertGreater(len(schema), 0)
        
        # Check that each table has expected structure
        for table_info in schema:
            self.assertIn('table', table_info)
            self.assertIn('columns', table_info)
            self.assertIsInstance(table_info['columns'], list)
            
            # Check column structure
            for column in table_info['columns']:
                self.assertIn('name', column)
                self.assertIn('type', column)
                self.assertIn('nullable', column)
                self.assertIn('default', column)
                self.assertIsInstance(column['nullable'], bool)
    
    def test_schema_includes_shop_tables(self):
        """Test that schema includes our shop tables."""
        schema = get_database_schema()
        table_names = [table['table'] for table in schema]
        
        # These tables should exist after migrations
        expected_tables = ['shop_product', 'shop_customer', 'shop_order', 'shop_orderitem']
        
        for table in expected_tables:
            self.assertIn(table, table_names)
    
    def test_product_table_schema(self):
        """Test product table has expected columns."""
        schema = get_database_schema()
        
        # Find the product table
        product_table = None
        for table in schema:
            if table['table'] == 'shop_product':
                product_table = table
                break
        
        self.assertIsNotNone(product_table, "shop_product table should exist")
        
        # Check for expected columns
        column_names = [col['name'] for col in product_table['columns']]
        expected_columns = ['id', 'name', 'sku', 'description', 'price', 'stock_quantity', 'category']
        
        for col in expected_columns:
            self.assertIn(col, column_names, f"Column {col} should exist in shop_product")


class ToolIntegrationTests(TestCase):
    """Integration tests for tools with actual data."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass'
        )
        
        # Create test customer
        self.customer = Customer.objects.create(
            user=self.user,
            phone='555-0123',
            address_line1='123 Test St',
            city='Test City',
            state='TC',
            postal_code='12345'
        )
        
        # Create test products
        self.products = []
        for i in range(3):
            product = Product.objects.create(
                name=f'Test Product {i}',
                sku=f'TEST00{i}',
                description=f'Description for test product {i}',
                price=f'{10 + i}.99',
                stock_quantity=50 + i,
                category='Test'
            )
            self.products.append(product)
        
        self.query_tool = QueryDatabaseTool()
        self.modify_tool = ModifyDatabaseTool()
    
    def test_query_products(self):
        """Test querying products with actual data."""
        query = "SELECT name, sku, price FROM shop_product WHERE category = 'Test' ORDER BY name"
        explanation = "Get all test products"
        
        result = self.query_tool.execute(
            query=query,
            explanation=explanation
        )
        
        self.assertTrue(result["success"])
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(len(result["results"]), 3)
        
        # Check that results contain expected data
        for i, row in enumerate(result["results"]):
            self.assertEqual(row['name'], f'Test Product {i}')
            self.assertEqual(row['sku'], f'TEST00{i}')
            self.assertEqual(float(row['price']), 10 + i + 0.99)
    
    def test_query_with_joins(self):
        """Test complex query with joins."""
        # Create an order for testing
        order = Order.objects.create(
            order_number='TEST001',
            customer=self.customer,
            status='pending'
        )
        
        query = """
        SELECT 
            o.order_number,
            u.username,
            c.city,
            o.status
        FROM shop_order o
        JOIN shop_customer c ON o.customer_id = c.id
        JOIN auth_user u ON c.user_id = u.id
        WHERE o.order_number = 'TEST001'
        """
        explanation = "Get order details with customer information"
        
        result = self.query_tool.execute(
            query=query,
            explanation=explanation
        )
        
        self.assertTrue(result["success"])
        self.assertEqual(result["row_count"], 1)
        
        row = result["results"][0]
        self.assertEqual(row['order_number'], 'TEST001')
        self.assertEqual(row['username'], 'testuser')
        self.assertEqual(row['city'], 'Test City')
        self.assertEqual(row['status'], 'pending')
    
    def test_aggregate_queries(self):
        """Test aggregate queries work correctly."""
        query = """
        SELECT 
            category,
            COUNT(*) as product_count,
            AVG(price) as avg_price,
            SUM(stock_quantity) as total_stock
        FROM shop_product 
        WHERE category = 'Test'
        GROUP BY category
        """
        explanation = "Get product statistics by category"
        
        result = self.query_tool.execute(
            query=query,
            explanation=explanation
        )
        
        self.assertTrue(result["success"])
        self.assertEqual(result["row_count"], 1)
        
        row = result["results"][0]
        self.assertEqual(row['category'], 'Test')
        self.assertEqual(row['product_count'], 3)
        self.assertAlmostEqual(float(row['avg_price']), 11.99, places=2)
        self.assertEqual(row['total_stock'], 153)  # 50 + 51 + 52
    
    def test_timezone_scenario_query(self):
        """Test a query that would benefit from timezone knowledge."""
        # This simulates a query that would use timezone information from KB
        query = """
        SELECT 
            DATE(created_at) as order_date,
            COUNT(*) as order_count,
            SUM(total_amount) as daily_total
        FROM shop_order 
        WHERE created_at >= CURRENT_DATE
        GROUP BY DATE(created_at)
        """
        explanation = "Get today's sales (would use EST timezone from KB)"
        
        result = self.query_tool.execute(
            query=query,
            explanation=explanation
        )
        
        # Query should succeed even without data
        self.assertTrue(result["success"])
        self.assertIn("timezone", explanation.lower())
        
        # This demonstrates where KB timezone info would be crucial
        # In a real scenario, the workflow would:
        # 1. Search KB for timezone configuration
        # 2. Use EST/EDT instead of server timezone
        # 3. Adjust the query accordingly