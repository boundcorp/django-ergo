# EcomDBAdmin - E-commerce Database Admin Assistant

An example Django application demonstrating how to build a natural language database admin interface using Django Ergo.

## 🎯 Overview

EcomDBAdmin showcases:
- **Natural Language Queries**: Ask questions about your data in plain English
- **SQL Generation**: Automatic conversion of questions to SQL queries
- **Approval Workflows**: Read queries execute immediately, writes require approval
- **Knowledge Base Learning**: System learns from user corrections
- **Chat History Ingestion**: Automatically extracts facts from conversations

## 🏗️ Architecture

### Key Components

1. **Models** (`models.py`):
   - `Product`: Product catalog with semantic search on descriptions
   - `Order`: E-commerce orders with customer information
   - `OrderItem`: Line items linking orders to products

2. **Tools** (`tools.py`):
   - `query_database`: Read-only SELECT queries (whitelisted)
   - `modify_database`: INSERT/UPDATE/DELETE operations (requires approval)

3. **Workflows** (`workflows.py`):
   - `DBAdminWorkflow`: Handles natural language to SQL conversion
   - Integrates with approval system for write operations

4. **Knowledge Base**:
   - "Shop Wiki" - Shared knowledge base about the store
   - Learns from user corrections and chat history

5. **Ingestion Helper** (`ingestion.py`):
   - `UserChatHistoryKBIngestion`: Extracts facts from conversations
   - Identifies corrections and updates KB automatically

## 🚀 Features

### Natural Language Database Queries
```python
# User asks: "What were our top selling products last month?"
# System generates: SELECT p.name, SUM(oi.quantity) as total_sold 
#                   FROM products p 
#                   JOIN order_items oi ON p.id = oi.product_id 
#                   JOIN orders o ON oi.order_id = o.id 
#                   WHERE o.created_at >= '2024-01-01' 
#                   GROUP BY p.id ORDER BY total_sold DESC LIMIT 10
```

### Approval Workflow for Writes
```python
# User says: "Update the price of Widget X to $29.99"
# System: "This operation requires approval. 
#         SQL: UPDATE products SET price = 29.99 WHERE name = 'Widget X'
#         Do you want to proceed?"
```

### Learning from Corrections
```python
# User: "What's our return policy?"
# Assistant: "Returns are accepted within 30 days."
# User: "Actually, we changed it to 45 days last month"
# System: [Automatically updates KB with new return policy information]
```

## 📦 Installation

1. **Clone and navigate**:
   ```bash
   cd examples/ecom_db_admin
   ```

2. **Create virtual environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment**:
   ```bash
   cp .env.example .env
   # Edit .env and add your OpenAI API key
   ```

5. **Run migrations**:
   ```bash
   python manage.py migrate
   ```

6. **Load sample data**:
   ```bash
   python manage.py loaddata fixtures/products.json
   python manage.py loaddata fixtures/orders.json
   python manage.py loaddata fixtures/shop_wiki.json
   python manage.py loaddata fixtures/chat_history.json
   ```

7. **Create admin user**:
   ```bash
   python manage.py createsuperuser
   ```

8. **Run server**:
   ```bash
   python manage.py runserver
   ```

## 🎮 Usage

### Admin Interface
Visit `http://localhost:8000/admin/` to:
- View and manage products, orders
- Browse the Shop Wiki knowledge base
- Review chat histories and corrections

### API Endpoints
- `POST /api/chat/` - Start a conversation
- `GET /api/orders/` - List orders (with semantic search)
- `GET /api/products/` - List products (with semantic search)
- `POST /api/workflow/db-admin/` - Execute DB admin workflow

### Example Queries
Try these natural language queries:
- "Show me all orders from last week"
- "What's the average order value?"
- "Which products are low in stock?"
- "Update the description of Product X"
- "How many customers do we have?"

## 🧪 Testing

Run the test suite:
```bash
python manage.py test
```

Key test cases:
- Natural language to SQL conversion
- Approval workflow for write operations
- Knowledge base learning from corrections
- Chat history ingestion

## 📊 Sample Data

The fixtures include:
- **50 products** across various categories
- **200 orders** with realistic customer data
- **Shop Wiki** with store policies and FAQs
- **Chat histories** showing correction examples

## 🔧 Configuration

### Settings
Key settings in `settings.py`:
- `DJANGO_ERGO_APPROVAL_REQUIRED`: List of tools requiring approval
- `DJANGO_ERGO_WHITELISTED_TOOLS`: Tools that run without approval
- `OPENAI_API_KEY`: Your OpenAI API key

### Customization
- Add new tools in `tools.py`
- Modify workflows in `workflows.py`
- Extend models in `models.py`
- Configure KB ingestion in `ingestion.py`

## 📚 Learning Resources

### Code Structure
```
ecom_db_admin/
├── models.py          # Django models with semantic fields
├── tools.py           # Database query tools
├── workflows.py       # DB admin workflow
├── ingestion.py       # Chat history KB ingestion
├── views.py          # API views
├── urls.py           # URL routing
├── admin.py          # Django admin config
├── fixtures/         # Sample data
│   ├── products.json
│   ├── orders.json
│   ├── shop_wiki.json
│   └── chat_history.json
└── tests/            # Test suite
```

### Key Concepts Demonstrated
1. **Tool Approval**: How to require user confirmation for sensitive operations
2. **KB Learning**: Extracting knowledge from user corrections
3. **Semantic Search**: Using Django Ergo's SemanticTextField
4. **Workflow State**: Pause/resume for approval flows
5. **Natural Language Processing**: Converting questions to SQL

## 🤝 Contributing

To extend this example:
1. Add more sophisticated SQL generation
2. Implement query optimization
3. Add visualization for query results
4. Extend the correction detection logic
5. Add more complex approval workflows

## 📄 License

This example is part of Django Ergo and follows the same license.