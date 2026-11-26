"""Provider-wide UTC monthly accounting. All identities are explicit, non-secret assertions.

Lock order: provider (vendor), owner envelope, child envelope. Mapping/consent
writes take the vendor lock too. A reservation's month never changes at settlement;
unknown exposure from *any* earlier month continues to consume availability.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from uuid import UUID

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from django_ergo.conversation.models import InferenceOutcome
from django_ergo.conversation.models import InferenceRequest
from django_ergo.conversation.models import ProviderAccountMapping
from django_ergo.conversation.models import ProviderBudgetFamily
from django_ergo.conversation.models import ProviderMonthlyConsent
from django_ergo.conversation.models import SpendControlAudit

CUMULATIVE = "cumulative"
MONTHLY = "calendar_month_utc"
MAX_MONTHLY_USD = Decimal("100")
MAX_NOTE_LENGTH = 200
IDENTITY_PARTS = 6
# Wire type -> the vendor that type natively speaks to.
FAMILIES = {"openai": "openai", "google": "google", "claude": "anthropic"}
# The first-party API host of each vendor. An OpenAI-compatible endpoint is
# billed by whoever owns its host, whatever wire type it speaks.
VENDOR_HOSTS = {
    "api.openai.com": "openai",
    "generativelanguage.googleapis.com": "google",
    "api.anthropic.com": "anthropic",
}
LEDGER_PREFIX = "grant:"  # InferenceRequest.pool of metered (spend_guard) attempts


def utc(value):
    # The library's historical test/application settings can use naive UTC.
    return (
        value.replace(tzinfo=dt.UTC)
        if timezone.is_naive(value)
        else value.astimezone(dt.UTC)
    )


def month_of(value=None):
    return utc(value or timezone.now()).date().replace(day=1)


def month_start(value=None):
    found = month_of(value)
    result = dt.datetime.combine(found, dt.time(), tzinfo=dt.UTC)
    from django.conf import settings

    return result if settings.USE_TZ else result.replace(tzinfo=None)


def account_identity(identity):
    """Canonicalize metadata only; exclude model and provider alias from identity."""
    parts = json.loads(identity)
    if (
        not isinstance(parts, list)
        or len(parts) != IDENTITY_PARTS
        or not all(isinstance(p, str) for p in parts)
        or parts[0] not in FAMILIES
        or parts[1] != "api"
        or not parts[2]
        or parts[2] == "unidentified-inline-credential"
    ):
        msg = "A supported named API account identity is required"
        raise ValueError(msg)
    return json.dumps(parts[:5], separators=(",", ":"))


def family_of(identity):
    """The vendor bucket of an account identity.

    The audited admin mapping decides. An unmapped identity that shares a
    credential source and account with a mapped one belongs to the same vendor
    (so an endpoint or alias can't open a second cap for one account). Failing
    both, the vendor is only what the endpoint itself proves (see
    ``_host_vendor``); anything else is unknown (``""``), which fails closed for
    recurring sends and counts in every vendor's history gate. There is no
    wire-type guess for an OpenAI-compatible endpoint.
    """
    account = json.loads(account_identity(identity))
    canonical = json.dumps(account, separators=(",", ":"))
    mapped = ProviderAccountMapping.objects.filter(identity=canonical).first()
    if mapped is not None:
        return mapped.family_id
    for other, family_id in ProviderAccountMapping.objects.values_list(
        "identity", "family_id"
    ):
        parts = json.loads(other)
        if parts[2] == account[2] and parts[3] == account[3]:
            return family_id
    return _host_vendor(account)


def _host_vendor(account):
    """The vendor an unmapped account provably bills through, else ``""``."""
    from urllib.parse import urlsplit

    if account[0] != "openai":
        return FAMILIES[account[0]]  # native wires only speak to their own vendor
    host = (urlsplit(account[4]).hostname or "").lower()
    return VENDOR_HOSTS.get(host, "")


def _validated_family(account, family):
    """The admin-asserted vendor, checked against the wire type and endpoint host."""
    from urllib.parse import urlsplit

    wire = FAMILIES[account[0]]
    family = family or wire
    if family not in FAMILIES.values():
        msg = "Choose a supported provider family"
        raise ValueError(msg)
    host = (urlsplit(account[4]).hostname or "").lower()
    owner_of_host = VENDOR_HOSTS.get(host)
    if account[0] != "openai":
        # Native google/claude wires only speak to their own vendor.
        if family != wire:
            msg = f"A {account[0]} account belongs to the {wire} family"
            raise ValueError(msg)
    elif owner_of_host is not None:
        if family != owner_of_host:
            msg = f"The endpoint {host} is billed by {owner_of_host}, not {family}"
            raise ValueError(msg)
    else:
        msg = (
            "An OpenAI-compatible endpoint that is not a supported vendor's "
            "first-party API can't join a provider family"
        )
        raise ValueError(msg)
    return family


def lock_family(family):
    from django_ergo.conversation.spend_guard import lock

    lock(f"provider-monthly:{family}")


def _actor(actor_id, *, admin=False):
    actor = get_user_model().objects.filter(pk=actor_id, is_active=True).first()
    if actor is None or (admin and not actor.is_superuser):
        msg = "An authenticated admin must assert account mappings"
        raise PermissionError(msg)
    return actor


def _note(note):
    if not isinstance(note, str) or not note.strip() or len(note) > MAX_NOTE_LENGTH:
        msg = "An explicit assertion/note of 1 to 200 characters is required"
        raise ValueError(msg)
    return note.strip()


def assert_mapping(  # noqa: PLR0913 -- explicit audited account assertion
    *, actor_id, owner_id, identity, account_label, assertion, attempt_ids=(), family=""
):
    """Admin attestation, never credential inspection. History is mapped explicitly.

    Mapping a historical attempt does not alter its cost or reservation. Missing
    bounds must be reconciled separately before recurring authority can activate.
    """
    actor = _actor(actor_id, admin=True)
    owner = _actor(owner_id)
    canonical = account_identity(identity)
    family_id = _validated_family(json.loads(canonical), family)
    assertion = _note(assertion)
    account_label = _note(account_label)
    attempt_ids = [UUID(str(pk)) for pk in attempt_ids]
    with transaction.atomic():
        lock_family(family_id)
        family, _ = ProviderBudgetFamily.objects.get_or_create(
            pk=family_id,
            defaults={"owner": owner, "actor": actor, "assertion": assertion},
        )
        if family.owner_id != owner.pk:
            msg = "The provider family's owner is immutable"
            raise ValueError(msg)
        mapping, _ = ProviderAccountMapping.objects.get_or_create(
            identity=canonical,
            defaults={
                "family": family,
                "actor": actor,
                "account_label": account_label,
                "assertion": assertion,
            },
        )
        if mapping.family_id != family_id:
            msg = "The account's provider family is immutable"
            raise ValueError(msg)
        # One credential source and account is one vendor account: never two caps.
        parts = json.loads(canonical)
        for other, other_family in ProviderAccountMapping.objects.exclude(
            pk=mapping.pk
        ).values_list("identity", "family_id"):
            found = json.loads(other)
            if found[2:4] == parts[2:4] and other_family != family_id:
                msg = (
                    "This credential source and account is already mapped to the "
                    f"{other_family} family"
                )
                raise ValueError(msg)
        attempts = list(
            InferenceRequest.objects.filter(pk__in=attempt_ids).select_for_update()
        )
        if len(attempts) != len({str(pk) for pk in attempt_ids}):
            msg = "Every historical attempt must exist"
            raise ValueError(msg)
        for attempt in attempts:
            if attempt.billing_mode not in ("api", "metered") or is_private(attempt):
                msg = "Only paid provider API attempts can be mapped"
                raise ValueError(msg)
            if attempt.vendor and attempt.vendor != family_id:
                msg = f"Attempt {attempt.pk} was recorded against {attempt.vendor}"
                raise ValueError(msg)
            if attempt.account_mapping_id and attempt.account_mapping_id != mapping.pk:
                msg = "A historical attempt's account mapping is immutable"
                raise ValueError(msg)
            InferenceRequest.objects.filter(pk=attempt.pk).update(
                provider_family=family,
                account_mapping=mapping,
                vendor=family_id,
                budget_month=month_of(attempt.created_at),
            )
        SpendControlAudit.objects.create(
            actor=actor,
            action="provider_mapping",
            details={
                "family": family_id,
                "mapping": mapping.pk,
                "owner": str(owner.pk),
                "assertion": assertion,
                "attempt_ids": [str(a.pk) for a in attempts],
            },
        )
    return mapping


def is_private(row):
    """A private-pool ledger row (``inference_ledger``): its own pool and cap,
    never a shared provider-family attempt."""
    return row.billing_mode == "api" and not row.pool.startswith(LEDGER_PREFIX)


def historical_problem(family_id):
    """Unmapped or unbounded history that could belong to ``family_id``.

    Scoped per vendor: rows recorded against another vendor and private-pool
    ledger rows never block this one. A row of unknown vendor could be anyone's,
    so it still fails closed for every family, as does this vendor's own.
    """
    paid = (
        InferenceRequest.objects.filter(billing_mode__in=["api", "metered"])
        .exclude(outcome=InferenceOutcome.NOT_SENT)
        .exclude(Q(billing_mode="api") & ~Q(pool__startswith=LEDGER_PREFIX))
        .filter(
            Q(provider_family_id=family_id)
            | Q(provider_family__isnull=True, vendor__in=[family_id, ""])
        )
    )
    if paid.filter(
        Q(provider_family__isnull=True)
        | Q(account_mapping__isnull=True)
        | Q(budget_month__isnull=True)
    ).exists():
        return "Ambiguous historical paid attempts require explicit account mapping and reconciliation"
    if (
        paid.exclude(cost_status="priced", cost_usd__isnull=False)
        .filter(Q(reserved_usd__isnull=True) | Q(reserved_usd__lte=0))
        .exists()
    ):
        return "Historical unknown exposure has no positive bound; reconcile it before activation"
    return ""


def set_consent(  # noqa: PLR0913 -- explicit recurring consent terms
    *,
    actor_id,
    family_id,
    version,
    monthly_usd,
    enabled,
    valid_from,
    expires_at,
    note,
    timezone_name="UTC",
):
    from django_ergo.conversation.funding import VersionConflict
    from django_ergo.conversation.funding import usd

    actor = _actor(actor_id)
    note = _note(note)
    amount = usd(monthly_usd, "Monthly consent")
    if amount > MAX_MONTHLY_USD or type(enabled) is not bool or timezone_name != "UTC":
        msg = "Monthly consent requires UTC, a boolean switch and at most $100 per provider"
        raise ValueError(msg)
    if (
        not isinstance(valid_from, dt.datetime)
        or timezone.is_naive(valid_from)
        or not isinstance(expires_at, dt.datetime)
        or timezone.is_naive(expires_at)
        or expires_at <= valid_from
    ):
        msg = "Consent requires an explicit timezone-aware validity window"
        raise ValueError(msg)
    with transaction.atomic():
        lock_family(family_id)
        family = ProviderBudgetFamily.objects.get(pk=family_id)
        if actor.pk != family.owner_id and not actor.is_superuser:
            msg = (
                "Only the provider family owner or admin can approve recurring consent"
            )
            raise PermissionError(msg)
        latest = family.consents.order_by("-version").first()
        expected = latest.version if latest else 0
        if type(version) is not int or version != expected:
            msg = "Recurring consent changed; refresh before writing"
            raise VersionConflict(msg)
        if enabled and (problem := historical_problem(family_id)):
            raise ValueError(problem)
        consent = ProviderMonthlyConsent.objects.create(
            family=family,
            actor=actor,
            version=expected + 1,
            monthly_usd=amount,
            enabled=enabled,
            valid_from=valid_from,
            expires_at=expires_at,
            note=note,
        )
        SpendControlAudit.objects.create(
            actor=actor,
            action="monthly_consent",
            details={
                "family": family_id,
                "version": consent.version,
                "monthly_usd": str(amount),
                "enabled": enabled,
                "timezone": "UTC",
                "note": note,
            },
        )
    return consent


def totals(rows, *, period=CUMULATIVE, now=None):
    """Exact priced usage plus conservative pending/unknown exposure.

    Monthly spend belongs to the attempt's original month. Outstanding holds
    from prior months carry forward, including attempts predating enrollment.
    """
    spent = held = Decimal(0)
    start = month_start(now)
    for row in rows.exclude(outcome=InferenceOutcome.NOT_SENT):
        priced = row.cost_status == "priced" and row.cost_usd is not None
        in_month = (row.budget_month or month_of(row.created_at)) == month_of(start)
        if priced:
            if period == CUMULATIVE or in_month:
                spent += row.cost_usd
        else:
            held += row.reserved_usd or Decimal(0)
    return spent, held


def validate_pool_period(budget_period, version, accounts):
    if budget_period not in (CUMULATIVE, MONTHLY):
        msg = "Funding period must be cumulative or calendar_month_utc"
        raise ValueError(msg)
    if budget_period != MONTHLY:
        return
    if type(version) is not int:
        msg = "Recurring funding requires the version last inspected"
        raise ValueError(msg)
    for identity in accounts:
        if not ProviderAccountMapping.objects.filter(
            identity=account_identity(identity)
        ).exists():
            msg = "Recurring funding requires explicit admin account mapping"
            raise ValueError(msg)


def reservation_context(identity, amount, *, required=False, now=None):  # noqa: C901 -- fail-closed identity and consent checks
    """Under the vendor lock: verify consent and aggregate all vendor attempts."""
    from django_ergo.conversation.spend_guard import SpendDenied

    if not identity:
        if required:
            msg = "Recurring funding requires explicit provider/account mapping"
            raise SpendDenied(msg)
        return {}
    try:
        family_id = family_of(identity)
    except (ValueError, TypeError, json.JSONDecodeError):
        if required:
            msg = "Recurring funding requires a supported API account"
            raise SpendDenied(msg) from None
        return {}
    family = ProviderBudgetFamily.objects.filter(pk=family_id).first()
    if family is None:
        if required:
            msg = "Recurring funding requires explicit provider/account mapping"
            raise SpendDenied(msg)
        return {}
    # Once enrolled, even cumulative grants on this vendor use the same cap.
    consent = family.consents.order_by("-version").first()
    if consent is None:
        msg = "Provider recurring consent is pending"
        raise SpendDenied(msg)
    mapping = family.accounts.filter(identity=account_identity(identity)).first()
    if mapping is None:
        msg = "Provider account requires an explicit admin mapping"
        raise SpendDenied(msg)
    now = utc(now or timezone.now())
    if not consent.enabled or not (
        utc(consent.valid_from) <= now < utc(consent.expires_at)
    ):
        msg = "Provider recurring consent is disabled, revoked or outside its validity window"
        raise SpendDenied(msg)
    if problem := historical_problem(family_id):
        raise SpendDenied(problem)
    spent, held = totals(family.attempts.all(), period=MONTHLY, now=now)
    if spent + held + amount > consent.monthly_usd:
        msg = f"Provider {family_id} UTC monthly budget is used: ${spent} spent and ${held} reserved"
        raise SpendDenied(msg)
    return {
        "provider_family": family,
        "account_mapping": mapping,
        "budget_month": month_of(now),
    }


def inspect_family(family_id):
    family = ProviderBudgetFamily.objects.get(pk=family_id)
    consent = family.consents.order_by("-version").first()
    spent, held = totals(family.attempts.all(), period=MONTHLY)
    return {
        "family": family.pk,
        "owner_id": str(family.owner_id),
        "timezone": "UTC",
        "month": str(month_of()),
        "version": consent.version if consent else 0,
        "monthly_usd": str(consent.monthly_usd) if consent else None,
        "enabled": bool(consent and consent.enabled),
        "spent_usd": str(spent),
        "reserved_usd": str(held),
        "valid_from": consent.valid_from.isoformat() if consent else None,
        "expires_at": consent.expires_at.isoformat() if consent else None,
        "actor_id": str(consent.actor_id) if consent else None,
        "history_problem": historical_problem(family_id),
        "accounts": list(
            family.accounts.values(
                "id", "identity", "account_label", "actor_id", "assertion"
            )
        ),
    }
