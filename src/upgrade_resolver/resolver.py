from dataclasses import asdict
from datetime import datetime, timezone

from .catalogs import CatalogReader
from .discovery import Discoverer, rank_identities
from .http import FetchError, HttpClient
from .source_policy import source_preference, authority_priority, annotate_agreement, catalog_coverage
from .versions import endpoint_present, milestones, parse_endpoint, select_releases, validate_interval


def resolve(client: HttpClient, project: str, current: str, target: str, *, seed_urls=None,
            mode="family", web_search="auto", max_pages=30, max_html_pages=8,
            scorer="heuristic", decision_backend=None) -> dict:
    if scorer not in ("heuristic", "decision"):
        raise ValueError("scorer must be heuristic or decision")
    if (scorer == "decision") != (decision_backend is not None):
        raise ValueError("Provide a decision_backend exactly when scorer='decision'")
    validate_interval(current, target, mode)
    if not project.strip() or len(project) > 150:
        raise ValueError("Project name must contain 1–150 characters.")
    discovery = Discoverer(client, web_search)
    candidates = discovery.discover(project.strip(), current, target, seed_urls or [])
    groups = rank_identities(candidates)
    result = {
        "schema_version": "1.0", "generated_at": datetime.now(timezone.utc).isoformat(),
        "input": {"project": project, "current_version": current, "target_version": target,
                  "source_urls": seed_urls or [], "version_mode": mode},
        "status": "unresolved", "identity": None,
        "version_policy": {
            "stable_numeric_versions_only": True,
            "interval": "exclude current family; include all target-family releases" if mode == "family" else "current < version <= target",
            "milestone_precision": len(parse_endpoint(target).release),
            "note": "Milestones group discovered versions for documentation collection; they are not installation steps. Family mode does not assess changes within the current family.",
        },
        "candidates": [c.public() for c in candidates], "catalogs": [],
        "versions": [], "documentation_milestones": [], "endpoint_checks": {},
        "coverage": {"identity": "unresolved", "catalog": "unavailable", "discovery": "bounded_search"},
        "warnings": list(discovery.warnings), "open_questions": [],
        "source_inventory": [], "llm_calls": 0,
    }

    def finish():
        result["source_inventory"] = sorted(client.sources.values(), key=lambda s: s["url"])
        result["network_requests"] = client.request_count
        return result

    if scorer == "decision":
        from .source_scoring import resolve_with_decisions
        resolve_with_decisions(result, candidates, client, decision_backend,
                               project, current, target, mode, max_pages, max_html_pages)
        return finish()

    if not groups or groups[0]["score"] < 6:
        result["open_questions"].append("No sufficiently supported project identity was found. Supply an official project URL or inspect discovery errors.")
        return finish()
    top = groups[0]
    if len(groups) > 1 and top["score"] - groups[1]["score"] < 3 and not (
            top["score"] == groups[1]["score"] and top["preference"] < groups[1]["preference"]):
        result["status"] = "ambiguous"
        result["coverage"]["identity"] = "ambiguous"
        result["open_questions"].append("Multiple project identities have similar evidence. Review candidates and provide a more specific project name or source URL.")
        return finish()

    members = sorted([c for c in top["members"] if c.kind not in ("npm", "pypi") or c.metadata.get("component_match")],
                     key=lambda c: (-c.score, source_preference(c), c.url))
    best = max(top["members"], key=lambda c: c.score)
    result["identity"] = {"id": top["identity"], "name": best.name, "repository": best.repository or None,
                          "homepage": best.homepage or None, "score": top["score"],
                          "confidence": "heuristic", "evidence": [asdict(e) for c in top["members"] for e in c.evidence]}
    result["coverage"]["identity"] = "resolved_by_heuristics"
    reader = CatalogReader(client, max_pages, max_html_pages)
    catalogs = []
    owners = {}
    # Prefer registry inventories. Do not mix their versions with unrelated tags.
    for member in members:
        try:
            attempt = reader.read(member, current, target, mode)
        except (FetchError, ValueError, KeyError, TypeError) as exc:
            result["warnings"].append(f"Catalog retrieval failed for {member.url}: {exc}")
            continue
        catalogs.extend(attempt)
        owners.update({c.source_url: member for c in attempt})
    if not any(c.releases for c in catalogs) and best.homepage:
        catalogs.append(reader.html(best.homepage, project, current, target, mode))
    result["catalogs"] = [{**asdict(c), "releases": [asdict(r) for r in select_releases(c.releases, current, target, mode)]} for c in catalogs]
    result["catalog_coverage"] = annotate_agreement(catalogs, current, target, mode)
    result["scoring"] = {"mode": "heuristic", "tie_break_order": ["web_search", "github", "registry", "other"],
                         "note": "Source preference breaks equal scores; it is not ownership evidence."}
    for catalog in catalogs:
        result["warnings"].extend(catalog.warnings)
    usable = [c for c in catalogs if select_releases(c.releases, current, target, mode)]
    if not usable:
        result["status"] = "partial"
        result["open_questions"].append("Project identity was found, but no usable release catalog was retrieved.")
        return finish()
    # Use one inventory for selection; preserve alternate inventories for inspection.
    # Coverage outranks source preference: a truncated list (e.g. GitHub's release listing stops near 1,000 entries)
    # must not beat a complete catalog of equal authority just because its source is preferred on ties.
    usable.sort(key=lambda c: (-authority_priority(owners.get(c.source_url, best), c),
                               not all(endpoint_present(c.releases, v, mode) for v in (current, target)),
                               not c.enumeration_complete,
                               source_preference(owners.get(c.source_url, best)),
                               catalog_coverage(c, current, target, mode)["granularity"] != "detailed_releases",
                               c.source_url))
    selected_catalog = usable[0]
    result["coverage"]["source_selection"] = "selected"
    result["selected_catalog"] = selected_catalog.source_url
    checks = {"current_found": endpoint_present(selected_catalog.releases, current, mode),
              "target_found": endpoint_present(selected_catalog.releases, target, mode)}
    result["endpoint_checks"] = checks
    selected = select_releases(selected_catalog.releases, current, target, mode)
    result["versions"] = [asdict(r) for r in selected]
    result["documentation_milestones"] = milestones(selected, len(parse_endpoint(target).release))
    result["coverage"]["catalog"] = "enumerated" if selected_catalog.enumeration_complete else "partial"
    result["status"] = "resolved" if all(checks.values()) and selected_catalog.enumeration_complete else "partial"
    if not all(checks.values()):
        result["open_questions"].append("One or both requested endpoints were not found in the selected catalog; do not treat this as a complete upgrade interval.")
    if selected_catalog.source_type == "github_tags":
        result["coverage"]["publication"] = "tags_only_unverified"
    else:
        result["coverage"]["publication"] = "catalog_records"
    if selected_catalog.skipped_labels:
        result["warnings"].append("Non-stable, unsupported, or unavailable version labels were skipped; inspect catalogs[].skipped_labels.")
    return finish()
