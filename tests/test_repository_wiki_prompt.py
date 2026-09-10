from django_ergo.repository_wiki_prompt import build_repository_wiki_prompt


def test_repository_wiki_prompt_is_architecture_focused_not_cataloging():
    prompt = build_repository_wiki_prompt(
        {
            "question": "Explain boundaries.",
            "required_sections": ["Overview", "Boundaries"],
            "required_path_groups": {
                "runtime": ["src/runtime.py", "src/engine.py"],
            },
            "required_symbol_groups": {
                "entrypoint": ["run", "execute"],
            },
        }
    )
    assert "Do not catalog files" in prompt
    assert "codebase is the source of truth" in prompt
    assert "Goal: Explain boundaries." in prompt
    assert "Required sections: Overview, Boundaries" in prompt
    assert "final action must be wiki_propose" in prompt
    assert "Never infer performance" in prompt
    assert "runtime=src/runtime.py|src/engine.py" in prompt
    assert "entrypoint=run|execute" in prompt


def test_repository_wiki_prompt_binds_existing_committed_page():
    prompt = build_repository_wiki_prompt(
        {"question": "Explain boundaries.", "required_sections": ["Overview"]},
        "# Existing",
    )
    assert "Existing committed page to improve" in prompt
    assert "# Existing" in prompt
