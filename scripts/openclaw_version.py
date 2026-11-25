"""Exact OpenClaw builds reviewed for the queue runner interfaces."""


SUPPORTED_OPENCLAW_BUILDS = frozenset((
    "OpenClaw 2026.10.1 (e178c39)",
    "OpenClaw 2026.11.8 (2d4ab29)",
))


def supports_version_output(output):
    """Accept an exact version line with at most one terminal LF or CRLF."""
    if not isinstance(output, str):
        return False
    if output.endswith("\r\n"):
        output = output[:-2]
    elif output.endswith("\n"):
        output = output[:-1]
    return output in SUPPORTED_OPENCLAW_BUILDS
