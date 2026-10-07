"""Pantry actions for the Pantry page."""

from django_ergo.bots import page_action


@page_action(
    requires_approval=True,
    approval_preview=lambda ctx, item, qty: f"Order {qty} x {item}",
)
def restock(ctx, item: str, qty: int = 1) -> dict:
    """Order more of an item."""
    rows = ctx.table("Pantry").objects.filter(name=item)
    if not rows.exists():
        msg = f"No item called {item}"
        raise ValueError(msg)
    rows.update(on_order=qty)
    ctx.table("Pantry").touch()  # .update() skips model signals; tell open pages
    return {"message": f"Ordered {qty} {item}"}
