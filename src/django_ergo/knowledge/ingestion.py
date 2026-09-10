"""Host-extracted text intake; providers, source access and execution remain host-owned."""

from .toolkit import CorpusToolkit


def prepare_absorption(service, *, content, title, provenance, reason):
    """Capture evidence in a pending proposal and bind a curator's cited tools.

    Pass the returned toolkit to any existing extra_tools runner or use it directly.
    Nothing is published until a host reviewer approves and applies its proposal.
    """
    proposal = service.intake(
        content=content, title=title, provenance=provenance, reason=reason
    )
    return CorpusToolkit(
        service,
        provenance=provenance,
        sources=(proposal.changes[0].reference,),
        proposal=proposal,
        reason=reason,
    )
