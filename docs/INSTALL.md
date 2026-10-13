# Install Envelope

Download and extract the release ZIP, which includes `envelope.pyz`, `SHA256SUMS`,
this guide, the license and release notes. Alternatively, download those files
individually from the same release. Use CPython 3.12 or newer. Envelope's runtime
needs no third-party packages.

## Verify and start

On Linux, from the directory containing the downloaded release files:

```sh
sha256sum -c SHA256SUMS
python3 envelope.pyz --version
python3 envelope.pyz doctor
```

On Windows, compare this hash with the `envelope.pyz` line in `SHA256SUMS`:

```powershell
Get-FileHash .\envelope.pyz -Algorithm SHA256
python .\envelope.pyz --version
python .\envelope.pyz doctor
```

Running `python3 envelope.pyz` (or `python envelope.pyz` on Windows) serves MCP over
stdio. Clients must use absolute paths to both the interpreter and the artifact.
If you keep an older version, install the RC in a separate directory so switching
the client's configured path restores the previous version.

## OpenCode

Add this to the project's `opencode.json` or your user-level OpenCode configuration:

```json
{
  "mcp": {
    "envelope": {
      "type": "local",
      "command": ["/absolute/path/to/python3", "/absolute/path/to/envelope.pyz"],
      "enabled": true
    }
  }
}
```

On Windows, the command can be
`["C:\\Python312\\python.exe", "C:\\tools\\envelope\\envelope.pyz"]`.
Check the connection with `opencode mcp list`.

### Qwen3.8 Flash workaround

For OpenCode using Qwen3.8 Flash, this concise build-agent prompt was tested with
all four Envelope tools. Add it alongside the `mcp` configuration above:

```json
{
  "agent": {
    "build": {
      "prompt": "You are a coding agent. Complete the user's task with the available tools. Run requested commands rather than describe them. Read the tool results and return the requested answer."
    }
  }
}
```

This replaces the build agent's default prompt for all models using that agent in
this configuration. Apply it in the project or profile where you use Qwen3.8 Flash.

## Qwen Code

Add this to `.qwen/settings.json`:

```json
{
  "mcpServers": {
    "envelope": {
      "command": "/absolute/path/to/python3",
      "args": ["/absolute/path/to/envelope.pyz"],
      "timeout": 600000
    }
  }
}
```

On Windows, replace the command with the installed Python path and use a Windows
artifact path in `args`. The client request timeout must exceed the waits you ask
Envelope to perform; cancelling a tool request cancels that command.

Approve a project-level server with `qwen mcp approve envelope`, then check the
connection with `qwen mcp list`.

## Quick checks

Ask your client to list Envelope's tools: `run`, `output`, `stop`, `processes`.
Try a short command, then a named background command with `wait_s: 0`. Inspect it
with `processes`, read its output, and stop it by name.

Commands run with your privileges. Configure working directories, environment
filters, execution deadlines and policy as appropriate for your use; `doctor`
reports the available shells and host restrictions.
