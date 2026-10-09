import pytest

from upgrade_resolver.models import Release
from upgrade_resolver.versions import endpoint_present, milestones, numeric_version, select_releases, validate_interval


def releases(*labels):
    return [Release(v, v, "https://example.org/releases", "test") for v in labels]


def test_numeric_sorting_and_exact_bounds():
    found = select_releases(releases("1.10.0", "1.9.0", "1.8.0", "2.0.0"), "1.8.0", "1.10.0", "exact")
    assert [r.version for r in found] == ["1.9.0", "1.10.0"]


def test_family_mode_preserves_input_precision_and_includes_target_patches():
    found = select_releases(releases("3.2.99", "4.0.0", "4.1.7", "4.2.0", "4.2.9", "4.3.0"), "3.2", "4.2", "family")
    assert [r.version for r in found] == ["4.0.0", "4.1.7", "4.2.0", "4.2.9"]
    assert milestones(found, 2) == ["4.0", "4.1", "4.2"]


def test_exact_mode_includes_current_family_patch_changes():
    found = select_releases(releases("3.2.1", "3.2.9", "4.0.0", "4.2.0", "4.2.1"), "3.2.0", "4.2.0", "exact")
    assert [r.version for r in found] == ["3.2.1", "3.2.9", "4.0.0", "4.2.0"]


def test_no_invented_versions_and_duplicate_normalization():
    found = select_releases(releases("1.1", "1.1.0", "1.4", "1.7"), "1.0", "1.7", "exact")
    assert [r.version for r in found] == ["1.1", "1.4", "1.7"]


@pytest.mark.parametrize("label,expected", [("v1.2.3", "1.2.3"), ("REL_16_0", "16.0"), ("release-2.1", "2.1"), ("orbit@2.0.0", "2.0.0")])
def test_tag_normalization(label, expected):
    assert str(numeric_version(label, ("orbit",))) == expected


@pytest.mark.parametrize("label", ["v2.0.0-rc.1", "2.0b1", "nightly", "unrelated@2.0.0", "release-2.0-dev", "2025-01-01"])
def test_prereleases_and_unknown_labels_are_not_guessed(label):
    assert numeric_version(label, ("orbit",)) is None


@pytest.mark.parametrize("current,target,mode", [("2", "1", "family"), ("1", "1", "exact"), ("1.0rc1", "2", "family"), ("1.2", "2", "family"), ("1", "2", "unknown")])
def test_invalid_or_ambiguous_intervals(current, target, mode):
    with pytest.raises(ValueError):
        validate_interval(current, target, mode)


def test_family_endpoint_is_not_silently_an_exact_version():
    assert endpoint_present(releases("18.2.0"), "18", "family")
    assert not endpoint_present(releases("18.2.0"), "18", "exact")
