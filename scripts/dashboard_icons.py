"""Selected Tabler Icons v3.52.0, https://github.com/tabler/tabler-icons.

See THIRD_PARTY_NOTICES.md for attribution and the MIT license.
"""

_PATHS = {
    "home": (
        "M5 4h4a1 1 0 0 1 1 1v6a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1v-6a1 1 0 0 1 1 -1",
        "M5 16h4a1 1 0 0 1 1 1v2a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1v-2a1 1 0 0 1 1 -1",
        "M15 12h4a1 1 0 0 1 1 1v6a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1v-6a1 1 0 0 1 1 -1",
        "M15 4h4a1 1 0 0 1 1 1v2a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1v-2a1 1 0 0 1 1 -1",
    ),
    "applications": (
        "M3 9a2 2 0 0 1 2 -2h14a2 2 0 0 1 2 2v9a2 2 0 0 1 -2 2h-14a2 2 0 0 1 -2 -2l0 -9",
        "M8 7v-2a2 2 0 0 1 2 -2h4a2 2 0 0 1 2 2v2", "M12 12l0 .01", "M3 13a20 20 0 0 0 18 0",
    ),
    "analytics": (
        "M3 13a1 1 0 0 1 1 -1h4a1 1 0 0 1 1 1v6a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1l0 -6",
        "M15 9a1 1 0 0 1 1 -1h4a1 1 0 0 1 1 1v10a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1l0 -10",
        "M9 5a1 1 0 0 1 1 -1h4a1 1 0 0 1 1 1v14a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1l0 -14",
        "M4 20h14",
    ),
    "runs": ("M3 12h4l3 8l4 -16l3 8h4",),
    "tailoring": (
        "M14 3v4a1 1 0 0 0 1 1h4",
        "M17 21h-10a2 2 0 0 1 -2 -2v-14a2 2 0 0 1 2 -2h7l5 5v11a2 2 0 0 1 -2 2",
        "M10 18l5 -5a1.414 1.414 0 0 0 -2 -2l-5 5v2h2",
    ),
    "system-health": (
        "M3 5a1 1 0 0 1 1 -1h16a1 1 0 0 1 1 1v10a1 1 0 0 1 -1 1h-16a1 1 0 0 1 -1 -1l0 -10",
        "M7 20h10", "M9 16v4", "M15 16v4", "M7 10h2l2 3l2 -6l1 3h3",
    ),
    "snapshot": (
        "M4 6a8 3 0 1 0 16 0a8 3 0 1 0 -16 0",
        "M4 6v6a8 3 0 0 0 16 0v-6",
        "M4 12v6a8 3 0 0 0 16 0v-6",
    ),
}


def icon(name):
    """Decorative inline SVG. The surrounding UI supplies the text label."""
    paths = "".join("<path d='{}'/>".format(path) for path in _PATHS[name])
    return ("<svg class='ui-icon' width='20' height='20' viewBox='0 0 24 24' "
            "fill='none' stroke='currentColor' stroke-width='1.75' stroke-linecap='round' "
            "stroke-linejoin='round' aria-hidden='true' focusable='false'>{}</svg>").format(paths)
