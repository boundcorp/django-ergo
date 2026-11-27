"""Bounded, line-oriented text windows for model-facing reads."""


def read_window(  # noqa: C901, PLR0913
    text,
    label,
    *,
    start_line=None,
    end_line=None,
    max_chars=50_000,
    sha256="",
    version=None,
):
    """Return unchanged short text or a numbered window with explicit coverage."""
    if start_line is not None and start_line < 1:
        msg = "start_line must be at least 1"
        raise ValueError(msg)
    if end_line is not None and end_line < (start_line or 1):
        msg = "end_line must be at least start_line"
        raise ValueError(msg)
    if max_chars < 1:
        msg = "max_chars must be positive"
        raise ValueError(msg)
    if start_line is None and end_line is None and len(text) <= max_chars:
        return text
    lines = text.splitlines()
    start = start_line or 1
    if start > len(lines):
        return f"{label} has {len(lines)} lines"
    end = min(end_line or len(lines), len(lines))
    rendered = []
    remaining = max_chars
    last = start - 1
    for number in range(start, end + 1):
        line = lines[number - 1]
        prefix = f"{number}: "
        rendered_line = prefix + line
        if len(rendered_line) > remaining:
            if not rendered:
                rendered.append(prefix + line[: max(0, remaining - len(prefix))])
                rendered.append(
                    f"[line {number} cut at {max(0, remaining - len(prefix))} chars]"
                )
                last = number
            break
        rendered.append(rendered_line)
        remaining -= len(rendered_line) + 1
        last = number
    marker = f" · sha256 {sha256[:12]}" if sha256 else ""
    if version is not None:
        marker += f" · version {version}"
    header = f"{label} lines {start}-{last} of {len(lines)}{marker}"
    if last < end:
        rendered.append(
            f"[truncated: {len(lines)} total lines; read on with start_line={last + 1}]"
        )
    return "\n".join([header, *rendered])
