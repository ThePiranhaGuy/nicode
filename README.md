# Nicode

An interactive Python TUI coding agent. You type natural-language requests and
the agent calls an LLM to drive a tool loop against the local filesystem and
shell.

- Backends: Azure OpenAI, NVIDIA NIM (switch at runtime with `/brain`)
- Tools: read/write/edit files, search the codebase, run shell commands,
  search the web (DuckDuckGo), export the conversation
- Modes: PLAN (read-only, default) and ACT (write tools enabled)
- Persistent scratchpad at `.nicode/memory.md`

Single-file implementation (~1,300 lines). See `CLAUDE.md` for the
architecture overview.

## Install

```bash
pip install -r requirements.txt
```

Requires Python 3.10+.

## Configure

Copy the template and fill in credentials for the backend you want to use:

```bash
cp nicode/.env.example nicode/.env
```

`nicode/.env` is git-ignored. Keys needed:

- **Azure OpenAI**: `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`,
  `AZURE_OPENAI_DEPLOYMENT` (and optionally `AZURE_OPENAI_API_VERSION`,
  `AZURE_THINKING_ENABLED`)
- **NVIDIA NIM**: `NVIDIA_NIM_API_KEY` (and optionally `NVIDIA_NIM_MODEL`,
  `NVIDIA_NIM_URL`)

Switch the default backend with `NICODE_BRAIN=azure` or `NICODE_BRAIN=nvidia`
in `.env`, or at runtime with `/brain <azure|nvidia>`.

## Run

```bash
python nicode/nicode.py
```

Type `/help` for the full list of slash commands. `/mode act` enables write
tools; `Ctrl+C` exits.

## License

MIT — see `LICENSE`.
