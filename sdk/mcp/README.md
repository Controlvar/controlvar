# @controlvar/mcp-server

The Controlvar MCP server: a read-only [Model Context Protocol](https://modelcontextprotocol.io)
server for one Controlvar instance. An AI assistant such as Claude Code, Claude Desktop or Cursor
starts it on your computer and talks to it over stdio. It calls your instance's API with your own
API key, so it sees what your role can see.

It offers six tools:

| Tool | What it answers | API route |
|---|---|---|
| `list_feature_flags` | The feature flags, a page at a time | `GET /api/v1/feature-flags/` |
| `get_feature_flag` | One feature flag, by its key | `GET /api/v1/feature-flags/` |
| `evaluate_feature_flag` | What a flag serves one user | `POST /api/v1/feature-flags/evaluate/{flag_key}` |
| `list_experiments` | The experiments, a page at a time | `GET /api/v1/experiments/` |
| `get_experiment` | One experiment, by its id | `GET /api/v1/experiments/{experiment_id}` |
| `get_experiment_results` | An experiment's statistical results | `GET /api/v1/experiments/{experiment_id}/results` |

It changes no flag, experiment or setting. Each `evaluate_feature_flag` call is recorded as one
evaluation of the flag, as an SDK's is.

## Install

To install it and add it to your MCP client, follow
[the MCP server page](https://github.com/Controlvar/controlvar/blob/main/docs/mcp-server.md).

It needs Node.js 22 or later, and a Controlvar instance whose read operations accept an API key
that carries the `mcp:read` scope.

## Configuration

The server reads three environment variables, which your MCP client sets for it. It takes no
command-line arguments, and refuses any.

| Variable | Required | Value |
|---|---|---|
| `CONTROLVAR_API_URL` | yes | Your instance's address, with no path: for example `https://controlvar.example.com` |
| `CONTROLVAR_API_KEY` | yes | An API key that carries the `mcp:read` scope ([creating an API key](https://github.com/Controlvar/controlvar/blob/main/docs/security/api-keys.md#creating-an-api-key)) |
| `CONTROLVAR_ALLOW_INSECURE_HTTP` | no | `1` to allow a plain `http://` address on another computer |

A plain `http://` address is accepted only for `localhost`, `127.0.0.1` or `[::1]`, because the
API key would otherwise cross the network unencrypted. If your instance runs on a private network
you trust and has no HTTPS, set `CONTROLVAR_ALLOW_INSECURE_HTTP=1`; the server then prints a
warning each time it starts.

A setting that can never work stops the server at start-up, with exit code 2 and one line on
stderr. The key is never printed. Each request is logged to stderr as one line: the tool, the
method, the path, the status, the time it took and its request id.

## Development

In a clone of this repository, in `sdk/mcp/`, run `npm ci` and then `npm test`. The tests build
`dist/` and run the built server as a child process against a stub API on `127.0.0.1`; one of them
packs the package and installs it into an empty directory, so it needs the npm registry.

`tools.json` is what the server answers to `tools/list`, with its `instructions` and the
environment variables it reads. The tests compare the two exactly; after changing a tool, run
`npm run tools-json` and commit the result.

## License

MIT.
