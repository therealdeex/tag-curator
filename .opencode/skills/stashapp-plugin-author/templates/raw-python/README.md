# __PLUGIN_NAME__

External raw Python Stash plugin.

## Requirements

- Stash v0.31.1 or a tested compatible version.
- `python3` available inside the Stash runtime/container.

## Install

Copy this directory under the Stash `plugins` directory, reload plugins, run the task from **Tasks**.

## Safety

The starter performs a read-only scene-count query. Keep dry-run enabled while implementing mutations.

`defaultArgs` are passed to the runtime. Stock plugin settings are display/configuration values and require an explicit configuration query before code can use them.
