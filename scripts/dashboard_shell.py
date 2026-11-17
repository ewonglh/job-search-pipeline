"""Shared navigation and document shell for cached dashboard pages."""
import html


PAGES = (
    ("home", "Home", "/jobs/"),
    ("applications", "Applications", "/jobs/applications/"),
    ("analytics", "Analytics", "/jobs/analytics/"),
    ("runs", "Runs", "/jobs/runs/"),
    ("tailoring", "Tailoring", "/jobs/tailoring/"),
    ("system-health", "System health", "/jobs/system-health/"),
)
PAGE_LABELS = {key: label for key, label, _path in PAGES}


def render(page, content, as_of, metadata, css):
    """Wrap one page view in the common responsive shell."""
    if page not in PAGE_LABELS:
        raise ValueError("unknown dashboard page")
    label = PAGE_LABELS[page]
    links = []
    for key, nav_label, path in PAGES:
        current = " aria-current='page'" if key == page else ""
        links.append("<li><a href='{}'{}>{}</a></li>".format(
            path, current, html.escape(nav_label)))
    details = "".join("<div><dt>{}</dt><dd>{}</dd></div>".format(
        html.escape(str(name)), html.escape(str(value))) for name, value in metadata)
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta name='color-scheme' content='light'>"
            "<meta name='referrer' content='no-referrer'>"
            "<title>{} | Private jobs dashboard</title><style>{}</style></head><body>"
            "<a class='skip' href='#main'>Skip to dashboard content</a><div class='shell'>"
            "<header class='masthead'><h1>Private jobs dashboard</h1>"
            "<p class='snapshot-asof'>Snapshot as of {}</p>"
            "<details class='provenance'><summary>Snapshot provenance</summary>"
            "<dl>{}</dl></details></header>"
            "<div class='workspace'><nav class='nav' aria-label='Dashboard pages'><ul>{}</ul></nav>"
            "<main id='main' aria-labelledby='page-heading'><h2 id='page-heading'>{}</h2>{}</main>"
            "</div><footer>Private local dashboard · read-only cached snapshot · viewing does not collect source data.</footer>"
            "</div></body></html>").format(
                html.escape(label), css,
                html.escape(str(as_of)),
                details, "".join(links), html.escape(label), content)
