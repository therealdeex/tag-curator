# Stash GraphQL integration

## Schema authority

Open **Settings > Tools > GraphQL playground** on the target Stash instance. Use its Documentation Explorer and test every query/mutation there before adding it to a plugin.

Endpoint:

```text
<scheme>://<server>:<port>/graphql
```

Default local endpoint is `http://localhost:9999/graphql`.

## Authentication contexts

- External automation: use the `ApiKey` HTTP header.
- Username/password configurations: cookie authentication may still be required.
- Plugin hook callbacks: preserve `server_connection.SessionCookie`, even when an API key could authenticate, because the cookie carries hook execution context used for recursion control.

## Request contract

```json
{
  "query": "query FindScene($id: ID!) { findScene(id: $id) { id title } }",
  "variables": {"id":"45"}
}
```

Always use variables. Never interpolate titles, paths, or user-controlled strings into GraphQL source.

## Response contract

HTTP 200 can still contain GraphQL errors:

```json
{"data":null,"errors":[{"message":"..."}]}
```

Raise/report `errors`; do not treat presence of `data` alone as success.

## Mutation discipline

- Query current state first.
- Compute the smallest update input.
- Skip a no-op.
- Preserve unrelated fields.
- Batch carefully and respect server load.
- Include dry-run for broad changes.
- Never use direct SQLite writes for plugin behavior.

## IDs and nulls

Treat Stash IDs as opaque strings. Distinguish:

- field omitted;
- field explicitly `null`;
- field set to empty list/string.

This distinction is especially important in update hooks and mutation inputs.
