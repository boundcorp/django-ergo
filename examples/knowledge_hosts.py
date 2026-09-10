"""Two host-owned authorization examples, independent of storage and providers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectMember:
    username: str
    projects: tuple[str, ...]
    reviewer: bool = False


class ProjectPolicy:
    def __init__(self, collection_id, project, approved_digests):
        self.collection_id = collection_id
        self.project = project
        self.approved_digests = frozenset(approved_digests)
        self.reviewed_proposals = set()
        self.events = []

    def allows(self, principal, collection_id, scope, action):
        if (
            collection_id != self.collection_id
            or scope != self.project
            or self.project not in principal.projects
        ):
            return False
        return action in {
            "read",
            "search",
            "evidence",
            "validate",
            "propose",
            "intake",
            "semantic",
        } or (
            principal.reviewer
            and action in {"history", "export", "review", "apply", "index", "usage"}
        )

    def actor(self, principal):
        return principal.username

    def accepts_proposal(self, principal, proposal):
        return proposal.revision in self.reviewed_proposals

    def accepts_review(self, principal, collection_id, scope, review):
        return (
            review.reviewer == "engineering-review"
            and review.policy_version == "project/v1"
            and review.reviewed_digest in self.approved_digests
        )

    def audit(self, principal, event):
        self.events.append((principal.username, event))


@dataclass(frozen=True)
class Technician:
    username: str
    company: str
    sites: tuple[str, ...]
    supervisor: bool = False


class MaintenancePolicy:
    def __init__(self, collection_id, company, site, approved_digests):
        self.collection_id = collection_id
        self.company = company
        self.site = site
        self.approved_digests = frozenset(approved_digests)
        self.reviewed_proposals = set()
        self.events = []

    def allows(self, principal, collection_id, scope, action):
        if (
            collection_id != self.collection_id
            or scope != f"{self.company}:{self.site}"
            or principal.company != self.company
            or self.site not in principal.sites
        ):
            return False
        return action in {
            "read",
            "search",
            "evidence",
            "validate",
            "propose",
            "intake",
            "semantic",
        } or (
            principal.supervisor
            and action in {"history", "export", "review", "apply", "index", "usage"}
        )

    def actor(self, principal):
        return principal.username

    def accepts_proposal(self, principal, proposal):
        return proposal.revision in self.reviewed_proposals

    def accepts_review(self, principal, collection_id, scope, review):
        return (
            review.reviewer == "maintenance-review"
            and review.policy_version == "site/v1"
            and review.reviewed_digest in self.approved_digests
        )

    def audit(self, principal, event):
        self.events.append((principal.username, event))
