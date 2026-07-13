"""GraphQL operation strings for the Stash Tag Curator (Stash v0.31.1).

Every constant in this module is a standalone GraphQL document that targets the
schema shipped with Stash v0.31.1. Each operation is parametrised with GraphQL
``$variables`` only; no title, endpoint, id, or any other runtime value is ever
interpolated into the source text. Callers must supply a ``variables`` mapping
alongside the query when submitting it through :mod:`curator.graphql_client`.

The selection sets intentionally mirror the fields documented in the planning
handoff (L1078-1096): scenes, scene files/fingerprints, stash ids, tags,
performers (with demographic + body + decoration fields), performer tags, job
state, tag association counts, and tag create/destroy plus scene update
operations.

Note on the ``Map`` scalar: Stash defines a custom ``Map`` scalar (an arbitrary
JSON object). ``graphql-core`` parses it as an opaque named type; only Stash
validates it, so these documents parse client-side without a schema.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# System / config
# ---------------------------------------------------------------------------

GET_APP_VERSION = """
query GetAppVersion {
  version {
    version
    hash
    build_time
  }
}
"""

GET_CONFIGURATION_STASHBOXES = """
query GetConfigurationStashBoxes {
  configuration {
    general {
      stashBoxes {
        endpoint
        name
      }
    }
  }
}
"""

# ---------------------------------------------------------------------------
# Scene / performer / tag queries
# ---------------------------------------------------------------------------

# Field selection required by the curator pipeline (handoff L1078-1096):
# scene id/title/date/code, file fingerprints, scene tags, full performer
# demographic + body decoration fields plus performer tags, studio, and stash
# ids. The same selection is reused for the single-scene lookup so the engine
# can share one row-to-model mapper.
_SCENE_FIELDS = """
      id
      title
      date
      code
      details
      director
      urls
      files {
        fingerprints {
          type
          value
        }
      }
      tags {
        id
        name
      }
      performers {
        id
        name
        gender
        birthdate
        ethnicity
        country
        height_cm
        weight
        measurements
        fake_tits
        tattoos
        piercings
        tags {
          id
          name
        }
      }
      studio {
        id
        name
      }
      stash_ids {
        endpoint
        stash_id
      }
"""

FIND_SCENES_PAGE = """
query FindScenesPage(
  $filter: FindFilterType
  $scene_filter: SceneFilterType
  $ids: [ID!]
) {
  findScenes(filter: $filter, scene_filter: $scene_filter, ids: $ids) {
    count
    scenes {
""" + _SCENE_FIELDS + """
    }
  }
}
"""

FIND_SCENE_BY_ID = """
query FindSceneById($id: ID!) {
  findScene(id: $id) {
""" + _SCENE_FIELDS + """
  }
}
"""

FIND_PERFORMERS_PAGE = """
query FindPerformersPage(
  $filter: FindFilterType
  $performer_filter: PerformerFilterType
  $ids: [ID!]
) {
  findPerformers(
    filter: $filter
    performer_filter: $performer_filter
    ids: $ids
  ) {
    count
    performers {
      id
      name
      gender
      birthdate
      ethnicity
      country
      height_cm
      weight
      measurements
      fake_tits
      tattoos
      piercings
      tags {
        id
        name
      }
    }
  }
}
"""

# Every per-object count is requested at depth:0 (direct associations only);
# parent_count and child_count are scalar fields with no depth argument. The
# deprecated movie_count is intentionally omitted in favour of group_count.
FIND_TAGS_WITH_COUNTS = """
query FindTagsWithCounts(
  $filter: FindFilterType
  $tag_filter: TagFilterType
  $ids: [ID!]
) {
  findTags(filter: $filter, tag_filter: $tag_filter, ids: $ids) {
    count
    tags {
      id
      name
      scene_count(depth: 0)
      scene_marker_count(depth: 0)
      image_count(depth: 0)
      gallery_count(depth: 0)
      performer_count(depth: 0)
      studio_count(depth: 0)
      group_count(depth: 0)
      parent_count
      child_count
    }
  }
}
"""

# ---------------------------------------------------------------------------
# Scraper queries (stash-box fingerprint lookup)
# ---------------------------------------------------------------------------

# scrapeMultiScenes returns [[ScrapedScene!]!]! (list per input scene). Per the
# planning handoff the curator only ever uses the stash-box source via
# fingerprint, so the source and the scene ids are exposed as explicit
# variables; no endpoint is ever hardcoded into the document. The same scraped
# field set is reused for the single-scene lookup.
_SCRAPED_SCENE_FIELDS = """
    title
    code
    date
    details
    director
    duration
    urls
    remote_site_id
    studio {
      stored_id
      name
      remote_site_id
    }
    tags {
      stored_id
      name
      remote_site_id
    }
    performers {
      stored_id
      name
      remote_site_id
    }
    fingerprints {
      algorithm
      hash
      duration
    }
"""

SCRAPE_MULTI_SCENES = """
query ScrapeMultiScenes(
  $endpoint: String!
  $scene_ids: [ID!]!
) {
  scrapeMultiScenes(
    source: { stash_box_endpoint: $endpoint }
    input: { scene_ids: $scene_ids }
  ) {
""" + _SCRAPED_SCENE_FIELDS + """
  }
}
"""

SCRAPE_SINGLE_SCENE = """
query ScrapeSingleScene(
  $endpoint: String!
  $scene_id: ID!
) {
  scrapeSingleScene(
    source: { stash_box_endpoint: $endpoint }
    input: { scene_id: $scene_id }
  ) {
""" + _SCRAPED_SCENE_FIELDS + """
  }
}
"""

# ---------------------------------------------------------------------------
# Tag mutations
# ---------------------------------------------------------------------------

TAG_CREATE = """
mutation TagCreate($input: TagCreateInput!) {
  tagCreate(input: $input) {
    id
    name
  }
}
"""

# Stash's tagsDestroy takes `ids: [ID!]!` directly (no input wrapper).
TAG_DESTROY_BULK = """
mutation TagDestroyBulk($ids: [ID!]!) {
  tagsDestroy(ids: $ids)
}
"""

# ---------------------------------------------------------------------------
# Scene mutations
# ---------------------------------------------------------------------------

# sceneUpdate performs a FULL REPLACEMENT of tag_ids: the caller must supply
# the complete desired tag id list inside $input.tag_ids (handoff: sceneUpdate
# tag_ids = full replacement).
SCENE_UPDATE = """
mutation SceneUpdate($input: SceneUpdateInput!) {
  sceneUpdate(input: $input) {
    id
  }
}
"""

# bulkSceneUpdate adds tags without replacing existing ones: mode: ADD inside
# the tag_ids BulkUpdateIds object appends $tag_ids to every scene in $ids.
BULK_SCENE_UPDATE_ADD_TAGS = """
mutation BulkSceneUpdateAddTags(
  $ids: [ID!]
  $tag_ids: [ID!]
) {
  bulkSceneUpdate(
    input: { ids: $ids, tag_ids: { ids: $tag_ids, mode: ADD } }
  ) {
    id
  }
}
"""

# ---------------------------------------------------------------------------
# Plugin task + job lifecycle
# ---------------------------------------------------------------------------

# runPluginTask returns the job id as a scalar ID! (no selection set). Stash
# v0.31.1 deprecates `args` in favour of `args_map: Map`; the curator uses the
# non-deprecated argument.
RUN_PLUGIN_TASK = """
mutation RunPluginTask(
  $plugin_id: ID!
  $task_name: String
  $description: String
  $args_map: Map
) {
  runPluginTask(
    plugin_id: $plugin_id
    task_name: $task_name
    description: $description
    args_map: $args_map
  )
}
"""

FIND_JOB = """
query FindJob($id: ID!) {
  findJob(input: { id: $id }) {
    id
    status
    description
    progress
    startTime
    endTime
    addTime
    error
    subTasks
  }
}
"""

JOB_QUEUE = """
query JobQueue {
  jobQueue {
    id
    status
    description
    progress
    startTime
    endTime
    addTime
    error
    subTasks
  }
}
"""

# stopJob(job_id: ID!): Boolean! — Stash SIGKILLs the raw task; the curator
# treats this as non-graceful cancellation (handoff: cancellation has no
# graceful path in v1).
STOP_JOB = """
mutation StopJob($job_id: ID!) {
  stopJob(job_id: $job_id)
"""

# ---------------------------------------------------------------------------
# Entity (performer/studio) queries + mutations — Milestone 3 (Workstream B)
# ---------------------------------------------------------------------------

FIND_PERFORMER_BY_ID = """
query FindPerformerById($id: ID!) {
  findPerformer(id: $id) {
    id
    name
    stash_ids { endpoint stash_id }
  }
}
"""

FIND_STUDIO_BY_ID = """
query FindStudioById($id: ID!) {
  findStudio(id: $id) {
    id
    name
    stash_ids { endpoint stash_id }
  }
}
"""

FIND_PERFORMERS_BY_NAME = """
query FindPerformersByName($filter: PerformerFilterType, $ff: FindFilterType) {
  findPerformers(performer_filter: $filter, filter: $ff) {
    count
    performers { id name disambiguation stash_ids { endpoint stash_id } }
  }
}
"""

FIND_STUDIOS_BY_NAME = """
query FindStudiosByName($filter: StudioFilterType, $ff: FindFilterType) {
  findStudios(studio_filter: $filter, filter: $ff) {
    count
    studios { id name stash_ids { endpoint stash_id } }
  }
}
"""

FIND_PERFORMERS_BY_STASH_ID = """
query FindPerformersByStashId($filter: PerformerFilterType) {
  findPerformers(performer_filter: $filter) {
    count
    performers { id name stash_ids { endpoint stash_id } }
  }
}
"""

FIND_STUDIOS_BY_STASH_ID = """
query FindStudiosByStashId($filter: StudioFilterType) {
  findStudios(studio_filter: $filter) {
    count
    studios { id name stash_ids { endpoint stash_id } }
  }
}
"""

PERFORMER_CREATE = """
mutation PerformerCreate($input: PerformerCreateInput!) {
  performerCreate(input: $input) {
    id
    name
  }
}
"""

STUDIO_CREATE = """
mutation StudioCreate($input: StudioCreateInput!) {
  studioCreate(input: $input) {
    id
    name
  }
}
"""

PERFORMER_UPDATE = """
mutation PerformerUpdate($input: PerformerUpdateInput!) {
  performerUpdate(input: $input) {
    id
    name
  }
}
"""

STUDIO_UPDATE = """
mutation StudioUpdate($input: StudioUpdateInput!) {
  studioUpdate(input: $input) {
    id
    name
  }
}
"""

PERFORMER_DESTROY = """
mutation PerformerDestroy($input: PerformerDestroyInput!) {
  performerDestroy(input: $input)
}
"""

STUDIO_DESTROY = """
mutation StudioDestroy($input: StudioDestroyInput!) {
  studioDestroy(input: $input)
}
"""

__all__ = [
    "GET_APP_VERSION",
    "GET_CONFIGURATION_STASHBOXES",
    "FIND_SCENES_PAGE",
    "FIND_SCENE_BY_ID",
    "FIND_PERFORMERS_PAGE",
    "FIND_TAGS_WITH_COUNTS",
    "SCRAPE_MULTI_SCENES",
    "SCRAPE_SINGLE_SCENE",
    "TAG_CREATE",
    "TAG_DESTROY_BULK",
    "SCENE_UPDATE",
    "BULK_SCENE_UPDATE_ADD_TAGS",
    "RUN_PLUGIN_TASK",
    "FIND_JOB",
    "JOB_QUEUE",
    "STOP_JOB",
    "FIND_PERFORMER_BY_ID",
    "FIND_STUDIO_BY_ID",
    "FIND_PERFORMERS_BY_NAME",
    "FIND_STUDIOS_BY_NAME",
    "FIND_PERFORMERS_BY_STASH_ID",
    "FIND_STUDIOS_BY_STASH_ID",
    "PERFORMER_CREATE",
    "STUDIO_CREATE",
    "PERFORMER_UPDATE",
    "STUDIO_UPDATE",
    "PERFORMER_DESTROY",
    "STUDIO_DESTROY",
]
