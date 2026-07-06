"""Deterministic synthetic scene fixtures for Tier-A tests.

Every scene is fully synthetic -- no real performer, title, or fingerprint is
recorded.  The factory builds a stable, reviewable population of ~50 scenes
that covers the enrichment edge cases the plan calls out (decision D9 +
planning-handoff L1147-1159):

* zero performers;
* unknown gender (null / empty);
* multi-ethnicity string ("Asian / Caucasian");
* trans performers (``TRANSGENDER_MALE`` / ``TRANSGENDER_FEMALE`` / ``NON_BINARY``);
* under-18 *computed* age (scene date close to performer birthdate);
* missing fingerprints (files empty or fingerprint-less);
* married-IRL performer (performer carries the ``Married IRL`` tag id).

The shape mirrors the field list documented for ``FIND_SCENES_PAGE`` in T5 so
the same dicts can be returned verbatim by the mock ``findScenes`` cassette.
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["build_scenes", "scene_by_id", "scene_fixtures_path"]

# A single, deterministic "Married IRL" tag id referenced by married performers.
MARRIED_IRL_TAG_ID = "9001"
MARRIED_IRL_TAG_NAME = "Married IRL"

# Deterministic synthetic endpoints (no real provider URLs -- D15 documents the
# real ones; tests use these stand-ins so cassettes never encode real data).
STASHDB_ENDPOINT = "https://stashdb.example/graphql"
TPDB_ENDPOINT = "https://theporndb.example/graphql"


def _performer(pid: str, *, name: str, gender: str | None, birthdate: str | None,
                ethnicity: str | None, country: str | None, height_cm: int | None,
                weight: int | None, measurements: str | None = None,
                fake_tits: str | None = None, tattoos: str | None = None,
                piercings: str | None = None,
                tags: list[dict] | None = None) -> dict:
    return {
        "id": pid,
        "name": name,
        "gender": gender,
        "birthdate": birthdate,
        "ethnicity": ethnicity,
        "country": country,
        "height_cm": height_cm,
        "weight": weight,
        "measurements": measurements,
        "fake_tits": fake_tits,
        "tattoos": tattoos,
        "piercings": piercings,
        "tags": tags or [],
    }


def _file(fingerprints: list[dict] | None = None) -> dict:
    return {"path": "synthetic/clip.mp4", "fingerprints": fingerprints or []}


def _fp(ptype: str, value: str) -> dict:
    return {"type": ptype, "value": value}


def _scene(sid: int, *, title: str, date: str, performers: list[dict],
           tags: list[dict] | None = None, files: list[dict] | None = None,
           stash_ids: list[dict] | None = None, code: str | None = None,
           studio: dict | None = None) -> dict:
    return {
        "id": str(sid),
        "title": title,
        "date": date,
        "code": code,
        "details": None,
        "files": files if files is not None else [_file([_fp("phash", f"phash-{sid:08x}")])],
        "tags": tags or [],
        "performers": performers,
        "studio": studio,
        "stash_ids": stash_ids or [],
    }


def _tag(tid: int, name: str) -> dict:
    return {"id": str(tid), "name": name}


def build_scenes() -> list[dict]:
    """Build the deterministic ~50-scene fixture set.

    Scene ids are stable; tests may hard-code them.  Edge cases are clustered
    into documented bands so a failing assertion localises quickly:

    * 1-10  : baseline single-performer scenes (known gender + ethnicity);
    * 11-15 : zero-performer scenes (Needs Review cast);
    * 16-20 : unknown-gender performers (known ethnicity);
    * 21-25 : multi-ethnicity string performers;
    * 26-30 : trans / non-binary performers;
    * 31-35 : under-18 *computed* age (data-quality failures);
    * 36-40 : missing fingerprints (PRESERVE / NO_IDENTIFIERS);
    * 41-45 : married-IRL performers (tag identity, not name);
    * 46-50 : ambiguous + null-field stress (missing height/weight/ethnicity).
    """
    scenes: list[dict] = []

    # -- 1-10: baseline known performers --------------------------------
    for i in range(1, 11):
        scenes.append(_scene(
            i,
            title=f"Baseline Scene {i}",
            date="2024-06-15",
            performers=[_performer(
                f"p{i:03d}", name=f"Performer {i}", gender="FEMALE",
                birthdate="1996-03-10", ethnicity="Caucasian", country="US",
                height_cm=165, weight=55, measurements="34B-24-36",
            )],
            tags=[_tag(100 + i, "Blowjob")],
            stash_ids=[{"endpoint": STASHDB_ENDPOINT, "stash_id": f"stashdb-{i:04d}"}],
        ))

    # -- 11-15: zero performers -----------------------------------------
    for i in range(11, 16):
        scenes.append(_scene(
            i,
            title=f"No Cast Scene {i}",
            date="2024-07-01",
            performers=[],
            tags=[],
            files=[_file([_fp("phash", f"zerocast-{i:08x}"),
                         _fp("oshash", f"oshash-{i:08x}")])],
        ))

    # -- 16-20: unknown gender, known ethnicity -------------------------
    for i in range(16, 21):
        scenes.append(_scene(
            i,
            title=f"Unknown Gender {i}",
            date="2024-05-20",
            performers=[_performer(
                f"p{i:03d}", name=f"Performer {i}", gender=None,
                birthdate="1993-11-02", ethnicity="Asian", country="JP",
                height_cm=158, weight=48,
            )],
        ))

    # -- 21-25: multi-ethnicity string ----------------------------------
    multi = [
        ("Asian / Caucasian", "US"),
        ("Latin / Caucasian", "BR"),
        ("Black / Caucasian", "GB"),
        ("Asian / Black", "CA"),
        ("Caucasian / Latin", "MX"),
    ]
    for idx, (eth, country) in enumerate(multi):
        sid = 21 + idx
        scenes.append(_scene(
            sid,
            title=f"Multi-Ethnic Scene {sid}",
            date="2024-08-11",
            performers=[_performer(
                f"p{sid:03d}", name=f"Performer {sid}", gender="FEMALE",
                birthdate="1994-02-14", ethnicity=eth, country=country,
                height_cm=168, weight=58,
            )],
        ))

    # -- 26-30: trans / non-binary performers ---------------------------
    trans = [
        ("TRANSGENDER_FEMALE", "FEMALE"),  # transitioned, counts as F in cast
        ("TRANSGENDER_MALE", "MALE"),
        ("TRANSGENDER_FEMALE", None),
        ("NON_BINARY", None),
        ("TRANSGENDER_MALE", "MALE"),
    ]
    for idx, (stored_gender, _) in enumerate(trans):
        sid = 26 + idx
        scenes.append(_scene(
            sid,
            title=f"Trans Performer Scene {sid}",
            date="2024-09-03",
            performers=[_performer(
                f"p{sid:03d}", name=f"Performer {sid}", gender=stored_gender,
                birthdate="1995-07-22", ethnicity="Caucasian", country="US",
                height_cm=172, weight=64,
            )],
        ))

    # -- 31-35: under-18 *computed* age (data-quality failure) ----------
    # scene_date 2024-06-15 with birthdate 2010-06-14 => 14 (and the boundary
    # case birthdate == scene_date - exactly-18-minus-one-day checks).
    under18 = [
        ("2010-06-14", "2024-06-15"),   # 14y0m1d
        ("2009-06-16", "2024-06-15"),   # 14y11m30d
        ("2007-06-16", "2024-06-15"),   # 16y11m30d
        ("2006-06-15", "2024-06-15"),   # exactly 18 (boundary -- OK but grouped)
        ("2012-01-01", "2024-06-15"),   # 12y (clearly underage)
    ]
    for idx, (bd, scene_date) in enumerate(under18):
        sid = 31 + idx
        scenes.append(_scene(
            sid,
            title=f"Young Performer Scene {sid}",
            date=scene_date,
            performers=[_performer(
                f"p{sid:03d}", name=f"Performer {sid}", gender="FEMALE",
                birthdate=bd, ethnicity="Caucasian", country="US",
                height_cm=160, weight=52,
            )],
        ))

    # -- 36-40: missing fingerprints ------------------------------------
    for i in range(36, 41):
        scenes.append(_scene(
            i,
            title=f"No Fingerprints Scene {i}",
            date="2024-04-04",
            performers=[_performer(
                f"p{i:03d}", name=f"Performer {i}", gender="MALE",
                birthdate="1990-01-01", ethnicity="Black", country="US",
                height_cm=180, weight=80,
            )],
            files=[_file([])],  # present file, no fingerprints
        ))
    # 40: no files at all
    scenes[-1]["files"] = []

    # -- 41-45: married-IRL performer (tag identity) --------------------
    for i in range(41, 46):
        scenes.append(_scene(
            i,
            title=f"Married IRL Scene {i}",
            date="2024-10-10",
            performers=[_performer(
                f"p{i:03d}", name=f"Performer {i}", gender="FEMALE",
                birthdate="1992-12-25", ethnicity="Latin", country="US",
                height_cm=163, weight=57,
                tags=[{"id": MARRIED_IRL_TAG_ID, "name": MARRIED_IRL_TAG_NAME}],
            )],
            tags=[_tag(200 + i, "Vaginal Sex")],
        ))

    # -- 46-50: null-field stress ---------------------------------------
    null_field = [
        {"height_cm": None, "weight": None, "ethnicity": None, "country": None},
        {"height_cm": None, "weight": 60, "ethnicity": "Asian", "country": None},
        {"height_cm": 170, "weight": None, "ethnicity": None, "country": "DE"},
        {"height_cm": None, "weight": None, "ethnicity": "", "country": ""},
        {"height_cm": 999, "weight": 9999, "ethnicity": "Caucasian", "country": "US"},
    ]
    for idx, overrides in enumerate(null_field):
        sid = 46 + idx
        scenes.append(_scene(
            sid,
            title=f"Null-Field Scene {sid}",
            date="2024-03-17",
            performers=[_performer(
                f"p{sid:03d}", name=f"Performer {sid}", gender="FEMALE",
                birthdate="1991-09-09",
                ethnicity=overrides["ethnicity"], country=overrides["country"],
                height_cm=overrides["height_cm"], weight=overrides["weight"],
            )],
        ))

    # 50 also exercises a multi-performer interracial scene (2 known ethnicities)
    scenes[49] = _scene(
        50,
        title="Interracial Duo Scene 50",
        date="2024-03-17",
        performers=[
            _performer("p050a", name="Performer 50A", gender="FEMALE",
                       birthdate="1991-09-09", ethnicity="Asian", country="JP",
                       height_cm=160, weight=50),
            _performer("p050b", name="Performer 50B", gender="MALE",
                       birthdate="1988-01-15", ethnicity="Black", country="US",
                       height_cm=185, weight=88),
        ],
        tags=[_tag(250, "Threesome")],
    )

    assert len(scenes) == 50, f"expected 50 scenes, got {len(scenes)}"
    return scenes


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

_SCENES_CACHE: list[dict] | None = None


def _all_scenes() -> list[dict]:
    global _SCENES_CACHE
    if _SCENES_CACHE is None:
        _SCENES_CACHE = build_scenes()
    return _SCENES_CACHE


def scene_by_id(scene_id: int | str) -> dict | None:
    """Return the synthetic scene with the given id, or ``None``."""
    sid = str(scene_id)
    for s in _all_scenes():
        if s["id"] == sid:
            return s
    return None


def scene_fixtures_path() -> Path:
    """Path to the on-disk ``scenes_db.json`` mirror (built lazily)."""
    return Path(__file__).resolve().parent.parent / "fixtures" / "scenes_db.json"


def dump_scenes_db(path: Path | None = None) -> Path:
    """Serialise the scene set to ``fixtures/scenes_db.json`` (idempotent).

    Called by the repo bootstrap; tests should read it through the
    ``scene_db`` fixture instead.
    """
    target = path or scene_fixtures_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "scenes": build_scenes(),
        "stashdb_endpoint": STASHDB_ENDPOINT,
        "tpdb_endpoint": TPDB_ENDPOINT,
        "married_irl_tag_id": MARRIED_IRL_TAG_ID,
        "married_irl_tag_name": MARRIED_IRL_TAG_NAME,
        "note": "Fully synthetic fixture data; no real performers or fingerprints.",
    }
    with target.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
        fh.write("\n")
    return target
