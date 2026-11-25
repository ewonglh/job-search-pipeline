"""Exact OpenClaw builds reviewed for the queue runner interfaces."""


SUPPORTED_OPENCLAW_BUILDS = frozenset((
    "OpenClaw 2026.10.1 (e178c39)",
    "OpenClaw 2026.11.8 (2d4ab29)",
))


def supports_version_output(output):
    """Accept only a complete CLI version line for a reviewed exact build."""
    return isinstance(output, str) and output.strip() in SUPPORTED_OPENCLAW_BUILDS
