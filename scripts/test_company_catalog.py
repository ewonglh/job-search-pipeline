"""Synthetic persistence, ambiguity, correction, and safety checks."""
import os
import tempfile

import company_catalog


with tempfile.TemporaryDirectory() as temp:
    path = os.path.join(temp, "private", "catalog.json")
    company_catalog.upsert({"key": "acme", "aliases": ["Acme Ltd"],
                             "size": "SME", "footprint": "multinational",
                             "primary_industry": "software",
                             "source_url": "https://example.invalid/company",
                             "classification_date": "2026-01-01",
                             "confidence": "high", "review": "reviewed"}, path)
    company_catalog.upsert({"key": "twin", "aliases": ["Twin Co"]}, path)
    company_catalog.upsert({"key": "other", "aliases": ["Twin Co"]}, path)
    catalog = company_catalog.load(path)
    assert company_catalog.resolve(catalog, "Acme LTD")["footprint"] == "multinational"
    assert company_catalog.resolve(catalog, "Twin Co") is None
    company_catalog.upsert({"key": "other", "aliases": []}, path)
    company_catalog.upsert({"key": "acme", "footprint": "domestic",
                             "classification_date": "2026-02-01"}, path)
    corrected = company_catalog.load(path)
    acme = company_catalog.resolve(corrected, "Acme Ltd")
    assert acme["size"] == "SME" and acme["footprint"] == "domestic"
    assert acme["source_url"] == "https://example.invalid/company"
    assert acme["classification_date"] == "2026-02-01"

for unsafe in (
        {"key": "x", "aliases": ["person@example.invalid"]},
        {"key": "x", "source_url": "https://user:pass@example.invalid/company"},
        {"key": "x", "source_url": "https://example.invalid/company?token=abc"},
        {"key": "x", "size": "medium"},
        {"key": "x", "review": "approve<script>"},
):
    try:
        company_catalog._record(unsafe)
    except company_catalog.CatalogError:
        pass
    else:
        raise AssertionError("unsafe catalog record accepted")

print("test_company_catalog: ok")
