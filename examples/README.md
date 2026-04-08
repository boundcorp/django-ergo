# Django Ergo Examples

This directory contains example applications demonstrating various features and use cases of Django Ergo.

## 🎯 Purpose

These examples serve multiple purposes:
1. **Learning**: Understand how to use Django Ergo features in real applications
2. **Reference**: Copy patterns and code for your own projects
3. **Testing**: Validate framework functionality with real-world scenarios
4. **Showcase**: Demonstrate the power and flexibility of semantic search and AI workflows

## 📚 Available Examples

### 🛍️ [EcomDBAdmin](./ecom_db_admin/)
**Database Admin Assistant for E-commerce**
- Natural language database queries
- SQL generation with approval workflows
- Knowledge base learning from user corrections
- Demonstrates tool approval and KB ingestion

### 🎧 CustomerSupport (Coming Soon)
**Multi-tier Knowledge Management System**
- Hierarchical knowledge bases
- Role-based access control
- Workflow escalation
- Auto-categorization

### 💻 ProjectAssistant (Coming Soon)
**Developer Workflow Automation**
- Code review workflows
- Documentation generation
- Semantic task search
- External tool integration

### 💬 VanillaChat (Deprecated)
**Basic Chat Interface**
- Simple chat UI
- Basic KB integration
- Will be replaced by features in other examples

## 🚀 Getting Started

Each example is a complete Django application that can be run independently:

1. **Navigate to example directory**:
   ```bash
   cd examples/ecom_db_admin
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Run migrations**:
   ```bash
   python manage.py migrate
   ```

4. **Load sample data**:
   ```bash
   python manage.py loaddata fixtures/*.json
   ```

5. **Create superuser** (if needed):
   ```bash
   python manage.py createsuperuser
   ```

6. **Run the server**:
   ```bash
   python manage.py runserver
   ```

## 🏗️ Architecture Patterns

### Common Patterns Across Examples

1. **Tool Registration**:
   - Each app registers its tools in `tools.py`
   - Tools are discovered automatically by Django Ergo
   - Approval requirements are configured per tool

2. **Knowledge Base Structure**:
   - Apps define their KB hierarchy in `models.py`
   - Semantic fields are used for searchable content
   - Embeddings are generated automatically

3. **Workflow Definition**:
   - Workflows are defined in `workflows.py`
   - State persistence handled by Django Ergo
   - Context serialization for pause/resume

4. **API Integration**:
   - Django Ninja for REST APIs
   - JWT authentication where needed
   - Semantic search endpoints

## 📝 Creating Your Own Example

To add a new example:

1. Create a new directory under `examples/`
2. Set up a standard Django app structure
3. Add Django Ergo to `INSTALLED_APPS`
4. Define your models with semantic fields
5. Create tools and workflows
6. Add sample data fixtures
7. Document your example with a README

## 🧪 Testing Examples

Each example includes its own test suite:

```bash
cd examples/ecom_db_admin
python manage.py test
```

## 📖 Learning Path

We recommend exploring the examples in this order:

1. **EcomDBAdmin**: Start here to understand basic concepts
2. **CustomerSupport**: Learn about complex KB structures
3. **ProjectAssistant**: Advanced workflow integration

## 🤝 Contributing

We welcome new example contributions! Please:
- Follow the existing patterns
- Include comprehensive documentation
- Add test coverage
- Provide sample data
- Update this README

## 📄 License

All examples are licensed under the same license as Django Ergo.