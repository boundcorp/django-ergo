# OpenAI Testing Rules and Constraints

## Overview

This codebase uses a **two-tier testing system** for OpenAI integrations to balance thorough testing with cost management and development speed.

## Two-Tier Testing System

### Tier 1: Real API Tests (`openai_real`)
- **Purpose**: Generate fixtures by calling real OpenAI APIs
- **When to run**: Only when necessary, costs real money 
- **Environment**: `TEST_OPENAI=true`
- **Markers**: `@pytest.mark.openai_real`
- **Cost**: 💰 Credits used per test run
- **Speed**: 🐌 Slow (network calls)

### Tier 2: Mocked Tests (`openai_mocked`) 
- **Purpose**: Fast unit testing using saved fixtures
- **When to run**: Always, during regular development
- **Environment**: Any (uses fixtures)
- **Markers**: `@pytest.mark.openai_mocked`
- **Cost**: 🆓 Free
- **Speed**: ⚡ Fast (no network calls)

## Testing Workflow

### For New OpenAI Integrations:
1. Write tests with both `openai_real` and `openai_mocked` variants
2. Run `make tests_openai_real` ONCE to generate fixtures 
3. Commit the generated fixtures to git
4. Use `make tests_openai_mocked` for daily development

### For Existing Integrations:
- Always run mocked tests first: `make tests_openai_mocked`
- Only run real API tests if you suspect fixture data is stale
- Real API tests should regenerate fixtures automatically

## Important Constraints

### 🚨 COST MANAGEMENT
- **Real API tests cost money** - each call to OpenAI APIs uses credits
- **Run real tests sparingly** - only when you need fresh fixtures
- **Always check fixtures first** - mocked tests should work for 99% of development

### ⚡ DEVELOPMENT SPEED
- **Use mocked tests by default** - they run in milliseconds
- **Real tests are for fixture generation** - not daily development
- **CI/CD should primarily use mocked tests** - keeps builds fast and free

### 🔧 MAINTENANCE
- **Keep fixtures up to date** - regenerate if OpenAI responses change significantly
- **Test both tiers** - ensure real and mocked tests have equivalent coverage
- **Document API changes** - update fixtures when OpenAI APIs evolve

## Makefile Commands

```bash
# Fast mocked testing (daily development)
make tests_openai_mocked

# Costly real API testing (fixture generation)
make tests_openai_real

# Run all OpenAI tests (both real and mocked)
make tests_openai_all

# Container-based testing with real APIs
make tests_with_costs_in_devcontainer

# Container-based testing with mocked APIs
make tests_fast_in_devcontainer

# Fixture management
make list_openai_fixtures
make clean_openai_fixtures
```

## Test Structure

### Real API Test Example:
```python
@pytest.mark.openai_real
def test_generate_summary_real_api(self):
    if not openai_test_manager.should_use_real_api():
        pytest.skip("TEST_OPENAI not set - skipping costly API test")
    
    result = generate_summary("Test text")
    assert isinstance(result, str)
    
    # Save fixture for mocked test
    save_openai_fixture("test_name", input_data, response, "chat.completions")
```

### Mocked Test Example:
```python
@pytest.mark.openai_mocked
def test_generate_summary_mocked(self):
    fixture = openai_test_manager.load_fixture("test_name")
    if not fixture:
        pytest.skip("No fixture found - run with TEST_OPENAI=true first")
    
    mock_response = openai_test_manager.create_mock_response(fixture)
    with patch('openai.chat.completions.create', return_value=mock_response):
        result = generate_summary(fixture.input_data["text"])
    
    assert result == fixture.response_data["content"]
```

## Current OpenAI Integration Points

1. **`django_ergo.fields.generate_summary()`**
   - API: `openai.chat.completions.create`
   - Model: `gpt-4o-mini`
   - Purpose: Text summarization

2. **`django_ergo.fields.generate_embedding()`**
   - API: `openai.embeddings.create`
   - Model: `text-embedding-3-small`
   - Purpose: Vector embeddings

3. **`django_ergo.fields.SummarizedVectorField`**
   - Uses both summary and embedding functions
   - Django model field integration

4. **`django_ergo.workflow_engine.WorkflowEngine`** (commented out)
   - API: `openai.chat.completions.create`
   - Model: `gpt-4o-mini`  
   - Purpose: Chat completions with tools

## Environment Variables

- `TEST_OPENAI=true` - Enable real API tests
- `OPENAI_API_KEY` - Required for real API tests
- No special environment needed for mocked tests

## Fixture Storage

- Location: `tests/fixtures/openai/`
- Format: JSON files with input/output data
- Naming: `{test_name}.json`
- Version control: **Include fixtures in git**

## Best Practices

### ✅ DO:
- Write both real and mocked variants of every OpenAI test
- Use descriptive test names for fixtures
- Run mocked tests in CI/CD pipelines
- Regenerate fixtures when APIs change
- Include fixtures in version control

### ❌ DON'T:
- Run real API tests in CI/CD (wastes money)
- Skip writing mocked test variants
- Forget to save fixtures from real tests  
- Run real tests repeatedly during development
- Leave stale fixtures that don't match current API responses

## When to Run Real API Tests

### 🟢 Good reasons:
- Adding new OpenAI integration
- OpenAI API responses have changed
- Fixture data seems outdated
- Testing against latest OpenAI models

### 🔴 Bad reasons:
- Regular development testing
- CI/CD pipeline runs
- "Just to be sure" checks
- Debugging non-OpenAI code

## Integration with Cursor Rules

When working on OpenAI integrations:

1. **Always prefer mocked tests** for development
2. **Only suggest real API tests** when fixtures are missing or updating integrations
3. **Warn about costs** before running real API tests
4. **Verify fixtures exist** before running mocked tests
5. **Use both test tiers** for comprehensive coverage

Remember: The goal is to have comprehensive OpenAI testing without breaking the bank! 💰⚡