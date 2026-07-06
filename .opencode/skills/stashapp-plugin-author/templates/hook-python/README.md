# __PLUGIN_NAME__

Guarded Stash hook starter. It listens to `Scene.Update.Post`, skips updates that did not supply `title`, preserves the hook cookie on GraphQL callbacks, and performs no mutation by default.

Test hook execution and recursion behavior on the exact target Stash build before enabling in a production library.

The starter uses string `defaultArgs` for `enabled` and `dryRun`. Add stock settings only after wiring an explicit plugin-configuration query.
