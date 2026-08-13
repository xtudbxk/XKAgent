# 02 · Configuration

[简体中文](02-配置管理.md) | English

XKAgent's configuration centers on three questions: where it works, which model service it uses, and which additional paths remain accessible in restricted modes.

## workdir: the shared base for the project and runtime data

`--workdir` points to the project directory that XKAgent operates on and also serves as the root for runtime data. For example:

```bash
python -m codes --workdir /path/to/project
```

When the option is omitted, the process cwd is used. Relative paths are likewise resolved against the process cwd and then converted to absolute paths. Logging, session, and related modules are imported only after the workdir has been determined, preventing runtime data from being created in an unintended location.

Each directory is created automatically when its associated feature is first accessed:

```text
<workdir>/.xkagent/
├── historys/    # One SQLite database per session
├── logs/        # Logs for the current process and previous runs
├── docs/        # Project documentation and per-session summary/compact data
└── skills/      # User Skills, which take precedence over built-ins with the same name
```

When the repository root is also the workdir, repository documentation and runtime memory share `.xkagent/docs/`. Runtime memory is written under `docs/<session>/`, however, so it is not mixed with the Markdown files in the documentation root.

## Provider configuration

Starting from the template is recommended:

```bash
cp provider.config.example .xkagent/provider.config
```

The configuration uses a compact INI-style format. Key-value pairs may use either `key: value` or `key = value`, and lines beginning with `#` or `;` are comments. A minimal configuration looks like this:

```ini
[default]
provider: openai

[openai]
type: openai
api_key_env: OPENAI_API_KEY
base_url: https://api.openai.com/v1
default_model: gpt-4o
models.flash: gpt-4o-mini
```

`[default].provider` selects the default Provider for new sessions, and `[default].model` can further override the default model. Each Provider section supports:

- `type`: calling convention; supported values are `openai` and `anthropic`.
- `api_key` or `api_key_env`: either provide the key directly or name the environment variable that stores it.
- `base_url`: the address of a compatible service instead of the official endpoint.
- `default_model`: the default model for this Provider.
- `models.<alias>`: an alias used by `/model`, such as `models.flash`.

When `type` is omitted, a `default_model` beginning with `claude-` or `anthropic/` is inferred as `anthropic`; other models use `openai`. The project has no built-in Provider fallback. A Provider must have a corresponding non-empty section in the file, or queries fail with an explicit error.

A model value may also include a reasoning effort, such as `gpt-5:high`. The configuration layer only separates the model name from the suffix and does not restrict effort values; support is determined by the actual API.

## File locations and hot reload

Whenever configuration is queried, the program selects the first existing file in this order:

1. `<workdir>/.xkagent/provider.config`
2. `<XKAgent repository root>/provider.config`

This makes the workdir configuration suitable for project-specific overrides, while the repository-root configuration serves as a local fallback. Environment variables only supply API keys to Providers that are already configured; they do not participate in configuration-file discovery or create Providers on their own.

Parsed results are cached by file path and nanosecond-resolution mtime. After the file is saved, the next Provider query reparses it automatically without requiring a restart; if the file is unchanged, the cached result is reused. Unrecognized non-empty lines are reported with their line numbers, while empty Provider sections are ignored and recorded as warnings in the log.

## Store API keys safely

API keys are resolved in this order:

1. `api_key` in the current Provider section
2. `api_key` in `[default]`
3. The environment variable named by `api_key_env` in the Provider section or `[default]`
4. `<PROVIDER uppercase>_API_KEY`, derived from the Provider name

If none is set, the configuration layer returns an empty value and the LLM call later reports a configuration error. Storing keys in environment variables is recommended:

```bash
export OPENAI_API_KEY="..."
```

```powershell
$env:OPENAI_API_KEY = "..."
```

Never commit real keys to the repository. In normal UI and log output, longer keys are displayed as their first three characters, `***`, and their last four characters. This masking is not a substitute for file permissions, environment variables, and proper secrets management.

## Extend path access with `permission.txt`

When a restricted mode needs access to directories outside the workdir, edit `<workdir>/.xkagent/permission.txt`:

```text
/path/to/reference ro
/path/to/output ro/rw
```

`ro` makes the path read-only in both `plan` and `build`; `ro/rw` makes it read-only in `plan` and writable in `build`. The legacy values `read` and `write` remain supported and are equivalent to `ro` and `ro/rw`, respectively.

The parser ignores blank lines and `#` comments. Entries with invalid syntax, unknown permission values, or nonexistent paths are skipped. `~` is expanded, and relative paths are converted to absolute paths against the process cwd rather than the workdir. If the same path appears more than once, the last entry wins.

Run `/mount refresh` after making changes to reload the file. `permission.txt` only extends the restricted path scope of `plan` and `build`: `plan` remains read-only, while `build-unsafe` does not use this file as its access boundary and still retains a small number of runtime protections for locations such as the `.xkagent` data directory. See [Sandbox and Tool Execution](05-sandbox-and-tools.md) for dynamic mounts and the complete sandbox semantics.

## Continue reading in the source

`codes/config.py` manages the workdir and runtime directories; `codes/provider_config.py` implements configuration discovery, parsing, caching, and model selection; `codes/agent.py` parses `permission.txt` and maintains dynamic mounts. Refer to `provider.config.example` for the supported fields and examples.

## Related documentation

- [Entry and Startup](01-entry-and-startup.md)
- [Infrastructure](03-infrastructure.md)
- [Sandbox and Tool Execution](05-sandbox-and-tools.md)
- [Command System](09-commands.md)
