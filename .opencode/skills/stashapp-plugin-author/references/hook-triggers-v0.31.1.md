# Hook triggers — v0.31.1 source audit

## Public documentation list

The public plugin manual documents:

- Object types: `Scene`, `SceneMarker`, `Image`, `Gallery`, `Group`, `Performer`, `Studio`, `Tag`
- Operations: `Create`, `Update`, `Destroy`; `Merge` for Tag
- Hook phase: `Post` only

## Constants declared in v0.31.1 source

```text
SceneMarker.Create.Post
SceneMarker.Update.Post
SceneMarker.Destroy.Post
Scene.Create.Post
Scene.Update.Post
Scene.Destroy.Post
Image.Create.Post
Image.Update.Post
Image.Destroy.Post
Gallery.Create.Post
Gallery.Update.Post
Gallery.Destroy.Post
GalleryChapter.Create.Post
GalleryChapter.Update.Post
GalleryChapter.Destroy.Post
Movie.Create.Post        (deprecated; prefer Group)
Movie.Update.Post        (deprecated; prefer Group)
Movie.Destroy.Post       (deprecated; prefer Group)
Group.Create.Post
Group.Update.Post
Group.Destroy.Post
Performer.Create.Post
Performer.Update.Post
Performer.Destroy.Post
Studio.Create.Post
Studio.Update.Post
Studio.Destroy.Post
Tag.Create.Post
Tag.Update.Post
Tag.Merge.Post
Tag.Destroy.Post
```

## Important source inconsistency

In the v0.31.1 `pkg/plugin/hook/hooks.go` file:

- `Group.*` and `Tag.Merge.Post` are declared as constants.
- `AllHookTriggerEnum` appears to omit `Group.*`.
- `IsValid()` appears to omit both `Group.*` and `Tag.Merge.Post`.
- `GalleryChapter.*` and deprecated `Movie.*` are declared, although the public object-type list omits them.

This may reflect a defect, generation artifact, or code path nuance. An agent must not silently choose one source over the other.

## Required validation procedure

1. Put the candidate trigger in a minimal manifest.
2. Reload plugins and inspect Stash logs for YAML/enum errors.
3. Trigger the operation in a disposable library.
4. Confirm exactly one execution.
5. For mutation callbacks, preserve the hook session cookie.
6. Document target Stash build/hash in the test record.

## Hook implementation guard

```python
hook = args.get("hookContext") or {}
if hook.get("type") != "Scene.Update.Post":
    return {"ok": True, "skipped": "unexpected hook"}

changed = set(hook.get("inputFields") or [])
if "title" not in changed:
    return {"ok": True, "skipped": "title not supplied"}
```

Do not use an in-memory recursion flag as the only guard; plugin processes may be short-lived or concurrent. Prefer no-op detection, targeted fields, persisted markers where appropriate, and Stash's cookie context.
