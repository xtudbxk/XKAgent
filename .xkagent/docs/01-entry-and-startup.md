# 01 · Entry and Startup

[简体中文](01-入口与启动.md) | English

This guide starts with installation, then explains how to launch the REPL or Web interface and what `--workdir`, session options, and the startup directory actually affect.

## Install the capabilities you need

XKAgent's REPL and LLM layer require only Python and `requests`:

```bash
python -m pip install requests
```

To use the Web interface, semantic search, and Git inside the sandbox, install all dependencies at once:

```bash
python -m pip install -r requirements.txt
```

These dependencies are grouped by capability. Missing an optional group does not prevent the core REPL from starting:

- `faiss-cpu`, `onnxruntime`, `transformers`, and `numpy` support semantic Skill search; if the group is incomplete, search falls back to ngram/keyword matching.
- `fastapi` and `uvicorn` provide the Web interface.
- `dulwich` provides the Git extension in `pythonrt`.
- `pythonrt` is included in the source tree and itself uses only the standard library.

At startup, XKAgent reports the availability of the core, semantic-search, and Web components. The current check does not list `dulwich` separately.

## Start the REPL

Run this from the repository root:

```bash
python -m codes --workdir /path/to/project
```

Passing `--workdir` explicitly is recommended so that the project scope and the location of `.xkagent` runtime data are unambiguous. Common session workflows include:

```bash
python -m codes --workdir /path/to/project --session demo
python -m codes --workdir /path/to/project --resume
python -m codes --workdir /path/to/project --prompt "解释这个仓库"
```

If no session is specified, XKAgent opens the most recently used session when one exists; otherwise it creates a timestamp-named session. `--resume` requires an existing session, while `--prompt` processes one request and then exits.

## Start the Web interface

The unified entry point switches to Web mode with `--mode web`:

```bash
python -m codes --workdir /path/to/project --mode web
python -m codes --workdir /path/to/project --mode web --host 127.0.0.1 --port 7860
python -m codes --workdir /path/to/project --mode web --user admin --password "your-password"
```

By default, the server listens on `127.0.0.1:7860` and uses `admin` as the username. Authentication is enabled only when `--password` is set.

You can also use the dedicated entry point:

```bash
python -m codes.web_main --workdir /path/to/project
```

`codes.web_main` reuses the same arguments and startup flow but always selects Web mode; even `--mode repl` will not make it start the REPL.

## Option reference

- `--mode {repl,web}`: runtime mode; defaults to `repl`.
- `--workdir PATH`: base directory for the project scope and runtime data.
- `-s, --session NAME`: select a REPL session.
- `--resume`: resume an existing session; without a name, selects the most recent session.
- `-p, --prompt TEXT`: send one REPL input and exit.
- `--host HOST`, `--port PORT`: Web listen address and port; defaults to `127.0.0.1:7860`.
- `--user USER`, `--password PASSWORD`: Web login credentials; the username defaults to `admin`.

The entry point uses `argparse.parse_args()` for strict argument parsing. Positional arguments, unknown options, and missing values all fail immediately. As a result, `./run.sh /some/path` is not a valid way to set the workdir; use `--workdir /some/path`.

## How `run.sh` handles paths

On Linux and macOS, you can also run:

```bash
./run.sh --workdir /path/to/project
./run.sh --workdir /path/to/project --mode web --port 9090
```

`run.sh` locates the repository directory but does not change the caller's current directory. It checks whether `requests` is available in the current Python installation; if not, it installs the full `requirements.txt`, then runs `codes/main.py` by absolute path and forwards all arguments unchanged.

The script neither changes the cwd nor sets `PYTHONPATH`. The Python entry point adds the project root to `sys.path` based on its own location, so imports do not depend on where the script was invoked. Accordingly:

- `~` in `--workdir` is expanded by the Python entry point.
- A relative `--workdir` is resolved against the process cwd at startup, not the directory containing `run.sh`.

The entry point determines the workdir before importing logging, history, and related modules, ensuring that every subsequently created `.xkagent` path is based on the correct directory. See [Configuration](02-configuration.md) and [Infrastructure](03-infrastructure.md) for the resulting directory layout.

## Continue reading in the source

Unified argument handling and mode dispatch live in `codes/main.py`; `codes/__main__.py` and `codes/web_main.py` are the package entry point and Web-specific delegation layer, respectively. For shell startup and dependency groups, see `run.sh` and `requirements.txt`.

## Related documentation

- [Configuration](02-configuration.md)
- [Infrastructure](03-infrastructure.md)
- [REPL and Web](10-frontends.md)
