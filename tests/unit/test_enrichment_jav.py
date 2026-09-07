"""Unit tests for the JAV identification subsystem (derive_jav_tag).

Covers the four identification signals and their interactions:

* studio-list match (case-insensitive, stripped);
* URL-substring match (case-insensitive, substring-in-URL);
* scene-code pattern (canonical ``ABP-987`` notation incl. lowercase and
  trailing-letter variants);
* file-basename fallback when the code is absent or non-matching
  (unscraped scenes whose code lives only in the filename);
* ``code_exempt_studios`` suppresses ONLY the code-shaped signals —
  studio-list and URL evidence still win;
* quality tokens like ``[WEBDL-1080p]`` never match (anchoring);
* disabled subsystem, custom tag names, malformed config -> TypeError,
  uncompilable pattern -> ValueError;
* engine wiring: ``_derive_enrichment_tags`` includes the tag and
  ``_finite_tag_candidates`` registers it for the tagCreate pre-pass.

Tier-A tests: no live Stash, no network, no third-party services.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from curator.enrichment import derive_jav_tag

# Mirrors the jav_detection block shipped in config/default-tag-rules.yaml.
CONFIG: dict[str, Any] = {
    "enabled": True,
    "tag_name": "JAV",
    "studio_names": ["Madonna", "MOODYZ", "Otona No Drama"],
    "url_substrings": ["javdatabase", "r18.dev", "dmm.co.jp"],
    "code_pattern": r"^[A-Za-z]{1,7}-\d{2,5}[A-Za-z]?$",
    "path_code_pattern": r"^[A-Za-z]{1,7}-\d{2,5}[A-Za-z]?\b",
    "code_exempt_studios": ["Evil Angel", "Up Her Asshole"],
}


def _scene(
    *,
    code: "str | None" = None,
    studio: "str | None" = None,
    urls: "list[str] | None" = None,
    path: "str | None" = None,
) -> dict[str, Any]:
    scene: dict[str, Any] = {
        "id": "1",
        "title": "Scene",
        "code": code,
        "urls": urls or [],
        "performers": [],
        "studio": {"id": "5", "name": studio} if studio else None,
        "tags": [],
        "files": [{"path": path}] if path else [],
    }
    return scene


class TestStudioSignal:
    def test_studio_list_match_is_case_insensitive(self) -> None:
        assert derive_jav_tag(_scene(studio="madonna"), CONFIG) == "JAV"

    def test_studio_list_match_strips_whitespace(self) -> None:
        assert derive_jav_tag(_scene(studio="  MOODYZ  "), CONFIG) == "JAV"

    def test_unlisted_studio_alone_never_fires(self) -> None:
        assert derive_jav_tag(_scene(studio="Brazzers"), CONFIG) is None

    def test_missing_studio_contributes_nothing(self) -> None:
        assert derive_jav_tag(_scene(), CONFIG) is None


class TestUrlSignal:
    def test_url_substring_match(self) -> None:
        scene = _scene(urls=["https://www.javdatabase.com/movies/ABP-987"])
        assert derive_jav_tag(scene, CONFIG) == "JAV"

    def test_url_substring_case_insensitive(self) -> None:
        scene = _scene(urls=["https://R18.DEV/titles/xyz"])
        assert derive_jav_tag(scene, CONFIG) == "JAV"

    def test_unrelated_url_never_fires(self) -> None:
        assert derive_jav_tag(_scene(urls=["https://theporndb.net/1"]), CONFIG) is None

    def test_url_wins_even_for_exempt_studio(self) -> None:
        # The exemption gates only the code-shaped signals.
        scene = _scene(
            studio="Evil Angel",
            code="OO-0087",
            urls=["https://www.javdatabase.com/x"],
        )
        assert derive_jav_tag(scene, CONFIG) == "JAV"


class TestCodeSignal:
    def test_canonical_code_fires(self) -> None:
        assert derive_jav_tag(_scene(code="ABP-987"), CONFIG) == "JAV"

    def test_lowercase_code_fires(self) -> None:
        assert derive_jav_tag(_scene(code="jufd-520"), CONFIG) == "JAV"

    def test_trailing_letter_suffix_fires(self) -> None:
        assert derive_jav_tag(_scene(code="HUNTA-137B"), CONFIG) == "JAV"

    def test_single_letter_prefix_fires(self) -> None:
        assert derive_jav_tag(_scene(code="C-2861"), CONFIG) == "JAV"

    def test_western_catalog_code_suppressed_by_exempt_studio(self) -> None:
        assert derive_jav_tag(_scene(studio="Evil Angel", code="OO-0087"), CONFIG) is None

    def test_same_code_from_unknown_studio_fires(self) -> None:
        # Unknown studios default to recall: the code notation is JAV-shaped.
        assert derive_jav_tag(_scene(studio="New Label", code="SSPD-137"), CONFIG) == "JAV"

    def test_non_code_strings_never_fires(self) -> None:
        assert derive_jav_tag(
            _scene(code="altered-states-of-consciousness"), CONFIG
        ) is None

    def test_no_hyphen_codes_never_fires(self) -> None:
        # Bang Bros-style codes (PWG13467) must NOT match.
        assert derive_jav_tag(_scene(code="PWG13467"), CONFIG) is None


class TestPathFallback:
    def test_basename_prefix_fires_when_code_missing(self) -> None:
        scene = _scene(path="/media/JAV/VKO-209 Japanese wife swap.mp4")
        assert derive_jav_tag(scene, CONFIG) == "JAV"

    def test_basename_prefix_fires_when_code_junk(self) -> None:
        scene = _scene(
            code="nbe002", path="/media/JAV/NBE-002-Momotaro Eizo.mp4"
        )
        assert derive_jav_tag(scene, CONFIG) == "JAV"

    def test_quality_token_at_end_never_fires(self) -> None:
        # 'WEBDL-1080p' has the code shape but is not at the basename start.
        scene = _scene(path="/media/Evil Angel/Some Scene [WEBDL-1080p].mp4")
        assert derive_jav_tag(scene, CONFIG) is None

    def test_code_in_mid_basename_never_fires(self) -> None:
        scene = _scene(path="/media/x/All About ABP-987.mp4")
        assert derive_jav_tag(scene, CONFIG) is None

    def test_windows_separator_handled(self) -> None:
        scene = _scene(path="C:\\media\\JAV\\HUNTA-429.mp4")
        assert derive_jav_tag(scene, CONFIG) == "JAV"

    def test_exempt_studio_blocks_path_fallback(self) -> None:
        scene = _scene(studio="Up Her Asshole", path="/media/x/UHA-0264.mp4")
        assert derive_jav_tag(scene, CONFIG) is None

    def test_matching_code_beats_path_evaluation(self) -> None:
        # Code signal is checked first; a junk path cannot veto it.
        scene = _scene(code="IPX-287", path="/media/x/renamed file.mp4")
        assert derive_jav_tag(scene, CONFIG) == "JAV"


class TestConfigHandling:
    def test_disabled_returns_none(self) -> None:
        cfg = dict(CONFIG, enabled=False)
        assert derive_jav_tag(_scene(studio="Madonna"), cfg) is None

    def test_custom_tag_name(self) -> None:
        cfg = dict(CONFIG, tag_name="PROD: JAV")
        assert derive_jav_tag(_scene(studio="Madonna"), cfg) == "PROD: JAV"

    def test_empty_config_uses_defaults_with_code_signal(self) -> None:
        # Empty studio/url lists + default patterns: only the code-shaped
        # signals remain active.
        assert derive_jav_tag(_scene(code="START-100"), {}) == "JAV"
        assert derive_jav_tag(_scene(studio="Madonna"), {}) is None

    def test_custom_code_pattern(self) -> None:
        cfg = dict(CONFIG, code_pattern=r"^\d{6}_\d{3}$")
        assert derive_jav_tag(_scene(code="060421_001"), cfg) == "JAV"
        assert derive_jav_tag(_scene(code="ABP-987"), cfg) is None

    def test_non_mapping_config_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            derive_jav_tag(_scene(), ["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_non_mapping_scene_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            derive_jav_tag(["not", "a", "scene"], CONFIG)  # type: ignore[arg-type]

    def test_malformed_studio_list_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            derive_jav_tag(_scene(), dict(CONFIG, studio_names="Madonna"))

    def test_malformed_url_substring_entry_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            derive_jav_tag(_scene(), dict(CONFIG, url_substrings=[123]))

    def test_empty_tag_name_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            derive_jav_tag(_scene(), dict(CONFIG, tag_name="  "))

    def test_uncompilable_code_pattern_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="does not compile"):
            derive_jav_tag(_scene(), dict(CONFIG, code_pattern="([unclosed"))

    def test_uncompilable_path_pattern_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="does not compile"):
            derive_jav_tag(_scene(), dict(CONFIG, path_code_pattern="*bad"))

    def test_malformed_files_list_is_ignored(self) -> None:
        scene = _scene()
        scene["files"] = "not-a-list"
        assert derive_jav_tag(scene, CONFIG) is None


class TestEngineWiring:
    """derive_jav_tag is reachable through the engine's enrichment pass."""

    def test_derive_enrichment_tags_includes_jav(self, tmp_path: Path) -> None:
        import sys

        repo = Path(__file__).resolve().parents[2]
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))

        from curator.journal import Journal
        from curator.processing import RebuildEngine
        from curator.rules import Rules
        from curator.state import StateDB

        raw = {
            "version": 3,
            "prefixes": {"ACT": "ACT:", "BODY": "BODY:", "AGE": "AGE:",
                          "DEMO": "DEMO:", "THEME": "THEME:", "CAST": "CAST:",
                          "SET": "SET:", "WARD": "WARD:", "KINK": "KINK:",
                          "PROD": "PROD:", "ERA": "ERA:", "STUDIO": "STUDIO:"},
            "canonical_tags": {},
            "mappings": {},
            "derived": {
                "age_buckets": [], "height_buckets": [], "weight_buckets": [],
                "era_buckets": [], "studio_passthrough": False,
                "ethnicity_aliases": {}, "ethnicity_owned_prefixes": [],
                "country_aliases": {}, "cast_taxonomy": {},
                "jav_detection": copy.deepcopy(CONFIG),
            },
            "protected": {"prefixes": ["MANUAL:"], "tag_names": []},
            "legacy": {"prefixes": [], "checkpoint_tags": [], "artifact_suffixes": []},
        }
        rules = Rules._build(raw, Path("<test>"))
        state = StateDB(str(tmp_path / "state.db"))
        try:
            engine = RebuildEngine(
                client=None, state=state, journal=Journal(state),
                rules=rules, providers=None,
            )
            tags, failures = engine._derive_enrichment_tags(
                _scene(studio="Madonna", code="JUQ-535")
            )
            assert "JAV" in tags
            assert not [f for f in failures if f.get("subsystem") == "jav"]
        finally:
            state.close()

    def test_jav_failure_recorded_not_raised(self, tmp_path: Path) -> None:
        import sys

        repo = Path(__file__).resolve().parents[2]
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))

        from curator.journal import Journal
        from curator.processing import RebuildEngine
        from curator.rules import Rules
        from curator.state import StateDB

        bad = copy.deepcopy(CONFIG)
        bad["code_pattern"] = "([unclosed"
        raw = {
            "version": 3,
            "prefixes": {"ACT": "ACT:"},
            "canonical_tags": {},
            "mappings": {},
            "derived": {"jav_detection": bad},
            "protected": {"prefixes": [], "tag_names": []},
            "legacy": {"prefixes": [], "checkpoint_tags": [], "artifact_suffixes": []},
        }
        rules = Rules._build(raw, Path("<test>"))
        state = StateDB(str(tmp_path / "state.db"))
        try:
            engine = RebuildEngine(
                client=None, state=state, journal=Journal(state),
                rules=rules, providers=None,
            )
            tags, failures = engine._derive_enrichment_tags(_scene(code="ABP-1"))
            assert "JAV" not in tags
            assert [f for f in failures if f.get("subsystem") == "jav"]
        finally:
            state.close()

    def test_finite_tag_candidates_include_jav(self) -> None:
        from curator.main import _finite_tag_candidates
        from curator.rules import Rules

        rules = Rules.load()  # bundled default rules
        candidates = _finite_tag_candidates(rules)
        assert "JAV" in candidates
