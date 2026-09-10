from django_ergo.repository_index import _bounded_parts
from django_ergo.repository_index import _markdown_units
from django_ergo.repository_index import _python_units


def test_python_units_capture_structural_relation_metadata():
    units = _python_units(
        "src/example.py",
        "import django_ergo.models\n\nclass Child(Base):\n    def run(self):\n        return helper()\n",
        6000,
    )
    module, child, run = units
    assert module.metadata["imports"] == ["django_ergo.models"]
    assert child.metadata["inherits"] == ["Base"]
    assert run.metadata["calls"] == ["helper"]


def test_markdown_units_keep_heading_identity_and_internal_links():
    units = _markdown_units(
        "wiki/guide.md", "# Guide\nSee [Other](other.md).\n# Guide\n", 6000
    )
    assert [unit.key for unit in units[1:]] == ["heading:1:Guide", "heading:3:Guide"]
    assert units[1].metadata["links"] == ["other.md"]


def test_markdown_splitter_keeps_fenced_lists_together():
    from django_ergo.repository_index import _markdown_parts

    text = "intro\n\n```\n- not a list boundary\n\nstill fence\n```\n\noutro\n"
    parts = _markdown_parts(text, 40)
    assert any(
        "still fence" in part and "not a list boundary" in part for part in parts
    )


def test_bounded_parts_never_exceed_the_ceiling():
    assert all(len(part) <= 7 for part in _bounded_parts("abcdefghijk", 7))
