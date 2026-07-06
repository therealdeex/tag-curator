# __PLUGIN_NAME__

Hybrid plugin with a server-side Python task and a browser route. These runtimes are separate: UI JavaScript cannot directly call the local Python process. Connect them through supported Stash GraphQL operations or an explicitly designed authenticated backend.

The task defaults to dry-run through `defaultArgs`. A displayed plugin setting is not automatically injected into either runtime.
