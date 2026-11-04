#!/usr/bin/env python3
"""Offline regression test for tex2txt.py.

No network. Run: python3 test_tex2txt.py

Regression pinned here (Lumen AI variant, `app/lumen-ai`):
    The export is a *wrapped* plain-text form.  A bullet's wrapped continuation
    lines are indented two spaces.  Non-bullet logical lines (entry headings,
    tech-stack lines) wrap with the *same* indent.  `_txt_bullets` used to fold
    any two-space-indented line into the most recent `- ` bullet, so the tail of
    a following tech-stack line (e.g. "API, GeoJSON") was appended to an earlier
    bullet and `--check` failed on a correct export.

The fixture is a synthetic variant body with the same shape, so the test fails if the folding
bug returns and passes once continuation attribution is scoped to the bullet.
"""
from pathlib import Path

import tex2txt as t

FIXTURE = (Path(__file__).parent / "fixtures" /
           "lumen-ai-backend-intern" / "resume.tex")
TEX = FIXTURE.read_text(encoding="utf-8")

# --------------------------------------------------------------- the regression
# The exported text is faithful to the .tex: no mismatch, no residue.
problems = t._check(TEX, t.render(TEX))
assert problems == [], problems

# The Harbourline bullet must not swallow the following tech-stack tail.
bullets = t._extract_items(TEX)
exposed = [b for b in bullets if b.startswith("Exposed the pipeline")]
assert len(exposed) == 1, exposed
assert exposed[0] == (
    "Exposed the pipeline through a FastAPI REST endpoint and a CLI for batch "
    "runs; containerized with Docker for reproducible deployment."
), exposed[0]
assert "GeoJSON" not in exposed[0]

# ...but the tech-stack line still wraps onto its own indented continuation, so
# we fixed attribution rather than stripping the fragment from the export.
txt = t.render(TEX)
assert "  API, GeoJSON" in txt.splitlines(), "tech-stack continuation missing"

# ------------------------------------------------- unit: continuation scoping
# A bullet's continuation is joined; a *later* non-bullet line's continuation
# must not be attributed to the bullet.
snippet = "\n".join([
    "- First bullet line that is long,",
    "  continued here.",
    "An entry header line",
    "  API, GeoJSON",
    "- Second bullet.",
])
assert t._txt_bullets(snippet) == [
    "First bullet line that is long, continued here.",
    "Second bullet.",
], t._txt_bullets(snippet)

# A blank line also closes a bullet's continuation window.
snippet2 = "- Bullet.\n\n  stray indent\n"
assert t._txt_bullets(snippet2) == ["Bullet."], t._txt_bullets(snippet2)

print("test_tex2txt: OK (synthetic fixture + continuation scoping)")
