# UI Plugin API guidance

`window.PluginApi` is experimental and may change without notice. Check the target Stash UI source for patchable component names and generated GraphQL symbols.

## Documented surface

- `React`
- `ReactDOM`
- `GQL` (low-level generated GraphQL client)
- `libraries.ReactRouterDOM`
- `libraries.Bootstrap`
- `libraries.Apollo`
- `libraries.Intl`
- FontAwesome namespaces/component
- `libraries.Mousetrap`
- `libraries.ReactSelect`
- `register.route(path, component)`
- `register.component(name, component)`
- `components`
- `utils` and `StashService`-related helpers
- `hooks.useLoadComponents`
- `loadableComponents`
- `patch.before`, `patch.after`, `patch.instead`
- `Event`

## Stability hierarchy

Prefer, in order:

1. Standalone registered route.
2. Registered reusable component.
3. Existing documented utility/service.
4. `patch.after` or `patch.before` with a graceful fallback.
5. `patch.instead` only when there is no viable alternative.

## Safe boot wrapper

```javascript
(() => {
  const api = window.PluginApi;
  if (!api?.React || !api?.register?.route) {
    console.warn("[my-plugin] compatible PluginApi not available");
    return;
  }

  try {
    // register route or component
  } catch (error) {
    console.error("[my-plugin] initialization failed", error);
  }
})();
```

## React rules

- Use `api.React`; do not ship another React instance.
- Use `api.libraries.Bootstrap` where appropriate.
- Use stable keys for injected children.
- Clean up event listeners and timers.
- Avoid assumptions about internal component tree shape.

## CSS rules

- Prefix classes with the plugin ID.
- Scope under a plugin root where practical.
- Avoid generic selectors such as `.card`, `button`, or `body *`.
- Respect Stash themes and reduced-motion preferences.
- Do not hide core destructive-action warnings.

## GraphQL in UI code

Generated symbols can change. Prefer a current `StashService` helper or Apollo client from the target build. Use variables and surface errors in the UI. Never expose an API key in browser code.

## CSP

A browser request to a local service may require exact `connect-src` entries for both HTTP and WebSocket schemes. Avoid wildcards. Remember that `localhost` resolves on the browser's machine, not necessarily the Stash server's machine.

## Testing matrix

- hard refresh and normal navigation;
- direct route load;
- browser back/forward;
- missing API/patch target;
- light/dark theme;
- mobile/narrow layout;
- plugin enabled/disabled;
- coexistence with common UI plugins;
- browser console clean of uncaught errors.
