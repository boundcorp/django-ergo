import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.test import TransactionTestCase
from django.test import override_settings

from django_ergo.embedding_providers import DeterministicEmbeddingProvider
from django_ergo.models import Knowledgebase
from django_ergo.models import KnowledgeSource
from django_ergo.repository_index import index_repository_commit
from django_ergo.repository_wiki import RepositoryToolkit
from django_ergo.repository_wiki import WikiProposalToolkit
from django_ergo.repository_wiki import propose_repository_wiki


class RepositoryToolkitTests(TransactionTestCase):
    def test_read_file_and_grep_are_numbered_and_scoped(  # noqa: PLR0915
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            (repo / ".ergo").mkdir(parents=True)
            (repo / "src").mkdir()
            (repo / "notes").mkdir()
            subprocess.run(
                ["git", "-C", str(repo.parent), "init", "repo"],
                check=True,
                capture_output=True,
            )
            for args in (
                ("config", "user.email", "test@example.com"),
                ("config", "user.name", "Test"),
            ):
                subprocess.run(
                    ["git", "-C", str(repo), *args], check=True, capture_output=True
                )
            (repo / ".ergo" / "index.yaml").write_text(
                "embedding_dimensions: 1536\nmax_unit_characters: 6000\nroles:\n  - role: runtime\n    include: [src/**/*.py]\n"
            )
            (repo / "src" / "sample.py").write_text("one\nneedle\nthree\n")
            (repo / "notes" / "ignored.py").write_text("needle\n")
            subprocess.run(
                ["git", "-C", str(repo), "add", "."], check=True, capture_output=True
            )
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "initial"],
                check=True,
                capture_output=True,
            )
            with override_settings(
                DJANGO_ERGO={"KNOWLEDGE_REPOSITORIES": {"repo": str(repo)}}
            ):
                kb = Knowledgebase.objects.create(name="Toolkit", owner_id="owner")
                source = KnowledgeSource.objects.create(
                    knowledgebase=kb,
                    repository_alias="repo",
                    repository_subdirectory="",
                    allowed_ref="HEAD",
                )
                index_repository_commit(
                    source, provider=DeterministicEmbeddingProvider()
                )
                toolkit = RepositoryToolkit(source)
                outline = json.loads(
                    toolkit.execute_tool(
                        "repo_outline",
                        {"roles_json": '{"roles":["runtime"]}'},
                    )
                )
                nested_outline = json.loads(
                    toolkit.execute_tool(
                        "repo_outline",
                        {"roles_json": '[{"role":"runtime"}]'},
                    )
                )
                history = json.loads(
                    toolkit.execute_tool(
                        "repo_history",
                        {"limit": 2, "query": "initial"},
                    )
                )
                read = json.loads(
                    toolkit.execute_tool(
                        "repo_read_file",
                        {"path": "src/sample.py", "start_line": 2, "end_line": 2},
                    )
                )
                grep = json.loads(
                    toolkit.execute_tool(
                        "repo_grep",
                        {
                            "pattern": "needle",
                            "path_glob": "src/**/*.py",
                            "before": 1,
                            "after": 1,
                        },
                    )
                )
                with pytest.raises(ValueError, match="not indexed"):
                    toolkit.execute_tool("repo_read_file", {"path": "notes/ignored.py"})
                with pytest.raises(ValueError, match="cannot be negative"):
                    toolkit.execute_tool(
                        "repo_grep", {"pattern": "needle", "before": -1}
                    )
                unit_id = (
                    source.source_files.get(relative_path="src/sample.py")
                    .units.first()
                    .id
                )
                with pytest.raises(ValueError, match="use repo_read_file"):
                    toolkit.execute_tool(
                        "repo_read",
                        {"unit_id": "src/sample.py"},
                    )
                proposal = WikiProposalToolkit(
                    source,
                    {
                        "type": "architecture",
                        "required_roles": ["runtime"],
                        "required_sections": ["Overview"],
                        "minimum_source_files": 2,
                    },
                    {str(unit_id)},
                    toolkit.observed_commits,
                )
                with pytest.raises(ValueError, match="distinct source files"):
                    proposal.execute_tool(
                        "wiki_propose",
                        {
                            "summary": "Too narrow",
                            "body": "# Overview\nNarrow evidence.",
                            "source_unit_ids_json": json.dumps([str(unit_id)]),
                        },
                    )
                missing_group_proposal = WikiProposalToolkit(
                    source,
                    {
                        "type": "architecture",
                        "required_roles": ["runtime"],
                        "required_sections": ["Overview"],
                        "minimum_source_files": 1,
                        "required_path_groups": {
                            "projection": ["src/missing.py"],
                        },
                    },
                    toolkit.observed,
                )
                with pytest.raises(ValueError, match="evidence group projection"):
                    missing_group_proposal.execute_tool(
                        "wiki_propose",
                        {
                            "summary": "Wrong subsystem",
                            "body": "# Overview\nEvidence.",
                            "source_unit_ids_json": json.dumps([str(unit_id)]),
                        },
                    )
                missing_symbol_proposal = WikiProposalToolkit(
                    source,
                    {
                        "type": "architecture",
                        "required_roles": ["runtime"],
                        "required_sections": ["Overview"],
                        "minimum_source_files": 1,
                        "required_symbol_groups": {
                            "entrypoint": ["missing_symbol"],
                        },
                    },
                    toolkit.observed,
                )
                with pytest.raises(ValueError, match="symbol group entrypoint"):
                    missing_symbol_proposal.execute_tool(
                        "wiki_propose",
                        {
                            "summary": "Wrong symbol",
                            "body": "# Overview\nEvidence.",
                            "source_unit_ids_json": json.dumps([str(unit_id)]),
                        },
                    )
                cited_proposal = WikiProposalToolkit(
                    source,
                    {
                        "type": "architecture",
                        "required_roles": ["runtime"],
                        "required_sections": ["Overview"],
                        "minimum_source_files": 1,
                        "required_path_groups": {
                            "implementation": ["src/**/*.py"],
                        },
                        "required_symbol_groups": {
                            "implementation": ["src/sample.py"],
                        },
                    },
                    toolkit.observed,
                    toolkit.observed_commits,
                )
                with pytest.raises(ValueError, match="Markdown headings"):
                    cited_proposal.execute_tool(
                        "wiki_propose",
                        {
                            "summary": "Wrong heading syntax",
                            "body": "**Overview**\nEvidence.",
                            "source_unit_ids_json": json.dumps([str(unit_id)]),
                        },
                    )
                with pytest.raises(ValueError, match="evaluative language"):
                    cited_proposal.execute_tool(
                        "wiki_propose",
                        {
                            "summary": "Unsupported quality claim",
                            "body": "# Overview\nA robust and scalable system.",
                            "source_unit_ids_json": json.dumps([str(unit_id)]),
                        },
                    )
                cited_proposal.execute_tool(
                    "wiki_propose",
                    {
                        "summary": "Cited architecture",
                        "body": "# Overview\nEvidence.",
                        "source_unit_ids_json": json.dumps([str(unit_id)]),
                        "git_commit_ids_json": json.dumps([history[0]["oid"]]),
                    },
                )
                unit_read = json.loads(
                    toolkit.execute_tool("repo_read", {"unit_id": str(unit_id)})
                )
            self.assertEqual(read["lines"], ["2:needle"])
            self.assertEqual(len(grep), 1)
            self.assertEqual(grep[0]["context"], ["1:one", "2:needle", "3:three"])
            self.assertEqual(unit_read["lines"][0].split(":", 1)[0], "1")
            self.assertEqual(
                [item["path"] for item in outline],
                ["src/sample.py"],
            )
            self.assertEqual(history[0]["subject"], "initial")
            self.assertEqual(nested_outline, outline)
            self.assertIn(history[0]["oid"], toolkit.observed_commits)
            read_unit_ids = {item["unit_id"] for item in read["source_units"]}
            grep_unit_ids = {item["unit_id"] for item in grep[0]["source_units"]}
            self.assertTrue(read_unit_ids)
            self.assertTrue(grep_unit_ids)
            self.assertTrue(read_unit_ids | grep_unit_ids <= toolkit.observed)
            self.assertEqual(
                cited_proposal.proposal["git_commit_ids"],
                [history[0]["oid"]],
            )

    def test_proposal_runner_uses_architecture_fixture(self):

        captured = {}

        async def fake_runner(*, message, extra_tools, **kwargs):
            captured["message"] = message
            repository, proposal = extra_tools
            await sync_to_async(repository.execute_tool, thread_sensitive=True)(
                "repo_read", {"unit_id": captured["unit_id"]}
            )
            await sync_to_async(proposal.execute_tool, thread_sensitive=True)(
                "wiki_propose",
                {
                    "summary": "Architecture summary",
                    "body": "# Overview\n# Boundaries",
                    "source_unit_ids_json": json.dumps([captured["unit_id"]]),
                },
            )
            return SimpleNamespace(
                session=SimpleNamespace(id="session-1"), approvals=[]
            )

        async def missing_runner(**kwargs):
            return SimpleNamespace(
                session=SimpleNamespace(id="session-2"),
                approvals=[],
                text="Stopped without proposing.",
            )

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            (repo / ".ergo").mkdir(parents=True)
            (repo / "src").mkdir()
            subprocess.run(
                ["git", "-C", str(repo.parent), "init", "repo"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Test"],
                check=True,
                capture_output=True,
            )
            (repo / ".ergo" / "index.yaml").write_text(
                'embedding_dimensions: 1536\nmax_unit_characters: 6000\nroles:\n  - role: runtime\n    include: ["src/sample.py"]\n'
            )
            (repo / ".ergo" / "wiki-goals.yaml").write_text(
                "version: 1\ngoals:\n  - id: goal-1\n    path: wiki/test.md\n    title: Test\n    type: architecture\n    project: test\n    question: Explain the architecture.\n    required_roles: [runtime]\n    minimum_source_files: 1\n    required_sections: [Overview, Boundaries]\n"
            )
            (repo / "src" / "sample.py").write_text("def example():\n    return 1\n")
            subprocess.run(
                ["git", "-C", str(repo), "add", "."], check=True, capture_output=True
            )
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "fixture"],
                check=True,
                capture_output=True,
            )
            with override_settings(
                DJANGO_ERGO={"KNOWLEDGE_REPOSITORIES": {"repo": str(repo)}}
            ):
                kb = Knowledgebase.objects.create(name="Proposal", owner_id="owner")
                source = KnowledgeSource.objects.create(
                    knowledgebase=kb,
                    repository_alias="repo",
                    repository_subdirectory="",
                    allowed_ref="HEAD",
                )
                index_repository_commit(
                    source, provider=DeterministicEmbeddingProvider()
                )
                source.refresh_from_db()
                source_file = source.source_files.get(relative_path="src/sample.py")
                captured["unit_id"] = str(source_file.units.first().id)
                with pytest.raises(ValueError, match="missing requested IDs"):
                    propose_repository_wiki(
                        source,
                        user=object(),
                        output=Path(directory) / "unknown-goal",
                        goal_ids=["unknown"],
                    )
                with patch(
                    "django_ergo.repository_wiki.run_workflow_task",
                    fake_runner,
                ):
                    manifest = propose_repository_wiki(
                        source,
                        user=object(),
                        output=Path(directory) / "out",
                    )
                self.assertEqual(manifest["goals"][0]["status"], "ok")
                page = Path(directory) / "out" / "wiki" / "test.md"
                self.assertTrue(page.exists())
                self.assertIn("kind: source-unit", page.read_text())
                failed_output = Path(directory) / "failed"
                with (
                    patch(
                        "django_ergo.repository_wiki.run_workflow_task",
                        missing_runner,
                    ),
                    pytest.raises(RuntimeError),
                ):
                    propose_repository_wiki(
                        source,
                        user=object(),
                        output=failed_output,
                    )
                failed_manifest = json.loads(
                    (failed_output / "manifest.json").read_text()
                )
                self.assertEqual(
                    failed_manifest["goals"][0]["error"]["code"],
                    "missing_proposal",
                )
            self.assertIn("codebase is the source of truth", captured["message"])
