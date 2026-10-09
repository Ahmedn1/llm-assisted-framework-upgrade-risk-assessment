from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Evidence:
    url: str
    field: str
    value: str


@dataclass
class Candidate:
    kind: str
    name: str
    url: str
    description: str = ""
    repository: str = ""
    homepage: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    rejected: bool = False
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)

    def public(self) -> dict:
        result = asdict(self)
        result.pop("metadata")
        return result


@dataclass
class Release:
    version: str
    raw_version: str
    source_url: str
    source_type: str
    published_at: str | None = None
    yanked: bool = False
    record_url: str | None = None


@dataclass
class Catalog:
    source_url: str
    source_type: str
    releases: list[Release] = field(default_factory=list)
    enumeration_complete: bool = True
    warnings: list[str] = field(default_factory=list)
    skipped_labels: list[str] = field(default_factory=list)
