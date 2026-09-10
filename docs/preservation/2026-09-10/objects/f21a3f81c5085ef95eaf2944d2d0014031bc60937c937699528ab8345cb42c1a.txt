import subprocess
import tempfile
from pathlib import Path

import pytest
from django.test import TestCase
from django.test import override_settings

from django_ergo.embedding_providers import DeterministicEmbeddingProvider
from django_ergo.models import Knowledgebase
from django_ergo.models import KnowledgeSource
from django_ergo.models import SourceFile
from django_ergo.models import SourceUnit
from django_ergo.repository_index import index_repository_commit


class RepositoryIndexReconciliationTests(TestCase):
    def git(self, repository, *args):
        subprocess.run(
            ["git", "-C", str(repository), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_identical_rerun_reuses_checkpoint_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            (repository / ".ergo").mkdir(parents=True)
            (repository / "src").mkdir()
            self.git(repository.parent, "init", "repo")
            self.git(repository, "config", "user.email", "test@example.com")
            self.git(repository, "config", "user.name", "Test")
            (repository / ".ergo" / "index.yaml").write_text(
                "embedding_dimensions: 1536\nmax_unit_characters: 6000\nroles:\n  - role: runtime\n    include: [src/**/*.py]\n"
            )
            (repository / "src" / "sample.py").write_text(
                "def example():\n    return 1\n"
            )
            self.git(repository, "add", ".")
            self.git(repository, "commit", "-m", "initial")
            with override_settings(
                DJANGO_ERGO={"KNOWLEDGE_REPOSITORIES": {"repo": str(repository)}}
            ):
                kb = Knowledgebase.objects.create(name="Test", owner_id="owner")
                source = KnowledgeSource.objects.create(
                    knowledgebase=kb,
                    repository_alias="repo",
                    repository_subdirectory="",
                    allowed_ref="HEAD",
                )
                provider = DeterministicEmbeddingProvider()
                first = index_repository_commit(source, provider=provider)
                source.refresh_from_db()
                second = index_repository_commit(source, provider=provider)
                (repository / "src" / "sample.py").write_text(
                    "def example():\n    return 2\n"
                )
                self.git(repository, "add", ".")
                self.git(repository, "commit", "-m", "change implementation")
                source.refresh_from_db()
                third = index_repository_commit(source, provider=provider)
            self.assertGreater(first["writes"], 0)
            self.assertEqual(second["writes"], 0)
            self.assertEqual(SourceFile.objects.count(), 1)
            self.assertGreater(SourceUnit.objects.count(), 0)
            self.assertGreater(third["writes"], 0)
            self.assertIn(
                "return 2",
                SourceUnit.objects.get(qualified_name="example").evidence_text,
            )

    def test_git_rename_preserves_file_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            (repository / ".ergo").mkdir(parents=True)
            (repository / "src").mkdir()
            self.git(repository.parent, "init", "repo")
            self.git(repository, "config", "user.email", "test@example.com")
            self.git(repository, "config", "user.name", "Test")
            (repository / ".ergo" / "index.yaml").write_text(
                "embedding_dimensions: 1536\nmax_unit_characters: 6000\nroles:\n  - role: runtime\n    include: [src/**/*.py]\n"
            )
            (repository / "src" / "old.py").write_text("def example():\n    return 1\n")
            self.git(repository, "add", ".")
            self.git(repository, "commit", "-m", "initial")
            with override_settings(
                DJANGO_ERGO={"KNOWLEDGE_REPOSITORIES": {"repo": str(repository)}}
            ):
                kb = Knowledgebase.objects.create(name="Rename", owner_id="owner")
                source = KnowledgeSource.objects.create(
                    knowledgebase=kb,
                    repository_alias="repo",
                    repository_subdirectory="",
                    allowed_ref="HEAD",
                )
                index_repository_commit(
                    source, provider=DeterministicEmbeddingProvider()
                )
                original_id = SourceFile.objects.get(relative_path="src/old.py").id
                self.git(repository, "mv", "src/old.py", "src/new.py")
                self.git(repository, "commit", "-m", "rename")
                source.refresh_from_db()
                index_repository_commit(
                    source, provider=DeterministicEmbeddingProvider()
                )
            renamed = SourceFile.objects.get(relative_path="src/new.py")
            self.assertEqual(renamed.id, original_id)
            self.assertIn("src/old.py", renamed.prior_paths)

    def test_provider_failure_does_not_advance_checkpoint(self):
        class FailingProvider(DeterministicEmbeddingProvider):
            def generate_embedding(self, text):
                message = "embedding unavailable"
                raise RuntimeError(message)

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            (repository / ".ergo").mkdir(parents=True)
            (repository / "src").mkdir()
            self.git(repository.parent, "init", "repo")
            self.git(repository, "config", "user.email", "test@example.com")
            self.git(repository, "config", "user.name", "Test")
            (repository / ".ergo" / "index.yaml").write_text(
                "embedding_dimensions: 1536\nmax_unit_characters: 6000\nroles:\n  - role: runtime\n    include: [src/**/*.py]\n"
            )
            (repository / "src" / "sample.py").write_text(
                "def example():\n    return 1\n"
            )
            self.git(repository, "add", ".")
            self.git(repository, "commit", "-m", "initial")
            with override_settings(
                DJANGO_ERGO={"KNOWLEDGE_REPOSITORIES": {"repo": str(repository)}}
            ):
                kb = Knowledgebase.objects.create(name="Failure", owner_id="owner")
                source = KnowledgeSource.objects.create(
                    knowledgebase=kb,
                    repository_alias="repo",
                    repository_subdirectory="",
                    allowed_ref="HEAD",
                )
                with pytest.raises(RuntimeError, match="embedding unavailable"):
                    index_repository_commit(source, provider=FailingProvider())
                source.refresh_from_db()
            self.assertIsNone(source.last_indexed_commit)
            self.assertEqual(SourceFile.objects.count(), 0)
            self.assertEqual(SourceUnit.objects.count(), 0)
