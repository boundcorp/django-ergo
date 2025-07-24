from papa.apps.ergo.models.kb import Knowledgebase
from papa.utils.test_openai import skip_unless_openai_test


@skip_unless_openai_test
def test_kb_search(project_fixture_common):
    kb = Knowledgebase.objects.create(name="Test KB", description="KB about dogs")
    kb.articles.create(
        title="Article One", content="My favorite dog is a golden retriever"
    )
    kb.articles.create(title="Article Two", content="My sister owns a labrador")
    kb.articles.create(title="Article Three", content="My brother owns a poodle")

    results = kb.articles.hybrid_search("favorite dog", top_k=1)
    assert len(results) == 1
    assert results[0].title == "Article One"
