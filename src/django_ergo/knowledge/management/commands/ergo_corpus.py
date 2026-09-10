import json
import sys

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.utils.module_loading import import_string

from django_ergo.knowledge.changes import Proposal
from django_ergo.knowledge.schema import MAX_CORPUS_BYTES
from django_ergo.knowledge.schema import CorpusError
from django_ergo.knowledge.schema import Provenance
from django_ergo.knowledge.schema import Reference
from django_ergo.knowledge.schema import Review
from django_ergo.knowledge.schema import require


class Command(BaseCommand):
    help = "Validate or retrieve a host-authorized logical corpus (no filesystem required)."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("alias")
        parser.add_argument(
            "action",
            choices=[
                "validate",
                "search",
                "get",
                "resolve",
                "export",
                "table_of_contents",
                "intake",
                "review",
                "apply",
                "operations",
                "rebuild_index",
                "get_strategy",
                "get_tree_status",
                "usage",
                "propose_strategy",
                "hierarchy",
            ],
        )
        parser.add_argument("--query")
        parser.add_argument("--document")
        parser.add_argument("--revision")
        parser.add_argument("--digest")
        parser.add_argument(
            "--mode", choices=["lexical", "semantic", "hybrid"], default="lexical"
        )
        parser.add_argument("--weights")
        parser.add_argument("--prefix", default="")
        parser.add_argument("--context")
        parser.add_argument("--rebuild-index", action="store_true")

    def handle(self, *args, **options):
        factory = getattr(settings, "ERGO_CORPORA", {}).get(options["alias"])
        if factory is None:
            message = "Unknown corpus alias"
            raise CommandError(message)
        if isinstance(factory, str):
            factory = import_string(factory)
        try:
            service = factory()
            action = options["action"]
            if action in {"intake", "review", "apply", "propose_strategy"}:
                result = self._write(service, action, self._input())
            elif action == "search":
                result = self._search(service, options)
            elif action in {"hierarchy", "usage"}:
                result = self._lookup(service, action, options)
            elif action == "get":
                result = service.get_document(
                    options["document"], revision=options["revision"]
                )
            elif action == "resolve":
                require(
                    all(options[name] for name in ("document", "revision", "digest")),
                    "resolve requires --document, --revision and --digest",
                )
                result = service.resolve(
                    Reference(
                        options["document"], options["revision"], options["digest"]
                    )
                )
            else:
                result = getattr(service, action)()
        except (CorpusError, PermissionError) as exc:
            raise CommandError(str(exc)) from exc
        except (TypeError, KeyError, json.JSONDecodeError) as exc:
            message = "Invalid command input"
            raise CommandError(message) from exc
        return json.dumps(result, sort_keys=True)

    @staticmethod
    def _lookup(service, action, options):
        if action == "hierarchy":
            return service.by_hierarchy_prefix(options["prefix"])
        return service.usage(context_id=options["context"])

    @staticmethod
    def _search(service, options):
        if options["rebuild_index"]:
            service.rebuild_index()
        return service.search(
            options["query"],
            mode=options["mode"],
            weights=json.loads(options["weights"]) if options["weights"] else None,
        )

    @staticmethod
    def _write(service, action, payload):
        if action in {"intake", "propose_strategy"}:
            require(isinstance(payload, dict), "Intake must be an object")
            payload["provenance"] = Provenance(**payload["provenance"])
            return getattr(service, action)(**payload).to_dict()
        if action == "review":
            return service.review(
                Proposal.from_dict(payload["proposal"]),
                [Review(**record) for record in payload["reviews"]],
            ).to_dict()
        return service.apply(Proposal.from_dict(payload))

    @staticmethod
    def _input():
        raw = sys.stdin.read(2 * MAX_CORPUS_BYTES + 1)
        require(
            len(raw.encode("utf-8")) <= 2 * MAX_CORPUS_BYTES, "Input exceeds size limit"
        )
        return json.loads(raw)
