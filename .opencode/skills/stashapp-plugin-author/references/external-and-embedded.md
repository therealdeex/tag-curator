# External and embedded task design

## Raw external process

Stash sends JSON on stdin and reads stdout. Stopping a raw task kills the process without a graceful signal contract.

Use raw for Python, Node, shell wrappers, and standalone binaries unless persistent asynchronous RPC is genuinely required.

### Strict stdout discipline

Bad:

```python
print("starting")
print(json.dumps({"output": result}))
```

Good:

```python
print("starting", file=sys.stderr)
print(json.dumps({"output": result}, separators=(",", ":")))
```

### Error handling

- Catch top-level exceptions.
- Print traceback to stderr only.
- Emit a concise JSON `error` to stdout.
- Exit non-zero.
- Detect GraphQL's `errors` field.
- Set network timeouts.

### Progress

Stash external plugins can encode log levels/progress using control characters from the Go logging helper. Do not reproduce that protocol from memory. For a portable plugin, use normal stderr logging unless progress integration is specifically needed and verified against source.

## RPC

RPC uses JSON-RPC and is intended for a process implementing Stash's expected runner interface. It must accept asynchronous requests and stop itself when asked. It adds lifecycle complexity and should not be selected merely because a task is long.

## Embedded JavaScript

Stash v0.31.1 uses Goja. Available documented globals include:

- `input`
- `log.Trace`, `log.Debug`, `log.Info`, `log.Warn`, `log.Error`
- `log.Progress(0..1)`
- `gql.Do(query, variables)`
- `util.Sleep(milliseconds)`

Do not assume availability of Node built-ins, npm packages, browser DOM, or browser `fetch`.

### Output ambiguity

The generic plugin contract documents lowercase `output`, but embedded examples display `Output`. Test the exact target version. Keep return objects simple and inspect Stash debug logs.

## Dependency strategy

- Do not install packages automatically at runtime.
- Pin dependencies in `requirements.txt`, lockfile, or release build.
- For Docker, install dependencies into the image or ship a self-contained executable.
- Explain whether Python/Node must exist inside the Stash container.
- Avoid host-only absolute paths.
