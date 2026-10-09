"""Numeric stable-release ordering. Unsupported schemes are never guessed."""
import re
from packaging.version import Version
from .models import Release


def numeric_version(label: str, project_names: tuple[str, ...] = ()) -> Version | None:
    text = label.strip()
    # Recognize conventional tag wrappers, not arbitrary embedded digits/dates.
    text = re.sub(r"^(?:refs/tags/)", "", text, flags=re.I)
    for name in sorted(project_names, key=len, reverse=True):
        text = re.sub(r"^" + re.escape(name) + r"[-_@](?=v?\d)", "", text, flags=re.I)
    text = re.sub(r"^(?:version[-_ ]?|release[-_ ]?|rel[-_]|v)(?=\d)", "", text, flags=re.I)
    text = text.replace("_", ".")
    if not re.fullmatch(r"\d+(?:\.\d+){0,3}", text):
        return None
    try:
        return Version(text)
    except ValueError:
        return None


def parse_endpoint(value: str) -> Version:
    if not re.fullmatch(r"v?\d+(?:\.\d+){0,3}", value):
        raise ValueError("Endpoints must be numeric stable versions, e.g. 3.2 or 3.2.1; prereleases and ranges are not supported yet.")
    parsed = numeric_version(value)
    assert parsed is not None
    return parsed


def family_upper(version: Version) -> Version:
    parts = list(version.release)
    parts[-1] += 1
    return Version(".".join(map(str, parts)))


def validate_interval(current: str, target: str, mode: str) -> tuple[Version, Version]:
    start, end = parse_endpoint(current), parse_endpoint(target)
    if mode not in ("family", "exact"):
        raise ValueError("version mode must be family or exact")
    if end <= start:
        raise ValueError("Target version must be newer than current version.")
    if mode == "family":
        if len(start.release) != len(end.release):
            raise ValueError("Family endpoints must have equal precision; use --version-mode exact for mixed precision.")
        if family_upper(start) > end:
            raise ValueError("Current and target release families overlap.")
    return start, end


def endpoint_present(releases: list[Release], endpoint: str, mode: str) -> bool:
    parsed = parse_endpoint(endpoint)
    return any(
        (parsed <= Version(r.version) < family_upper(parsed)) if mode == "family"
        else Version(r.version) == parsed for r in releases
    )


def select_releases(releases: list[Release], current: str, target: str, mode: str) -> list[Release]:
    start, end = validate_interval(current, target, mode)
    selected = {}
    for release in releases:
        version = Version(release.version)
        applies = (family_upper(start) <= version < family_upper(end)) if mode == "family" else (start < version <= end)
        if applies:
            # Preserve all provenance separately in catalogs; selection is deduplicated.
            selected.setdefault(version, release)
    return [selected[v] for v in sorted(selected)]


def milestones(releases: list[Release], precision: int) -> list[str]:
    result = set()
    for release in releases:
        parts = Version(release.version).release
        parts = (parts + (0,) * precision)[:precision]
        result.add(parts)
    return [".".join(map(str, parts)) for parts in sorted(result)]
