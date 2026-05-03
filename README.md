# The Matrix Agent System

The Matrix Agent System is a local-first agent framework inspired by the role boundaries in *The Matrix*.

The theme is useful only when it clarifies the architecture:

- **Oracle** understands intent, ethics, and the human side of an agent.
- **Architect** designs the agent spec, memory scope, prompt cache metadata, provider choice, and reuse plan.
- **Neo** protects the system by reviewing permissions, risks, memory writes, and output.
- **Nebuchadnezzar** is the runtime that carries a request through the mission flow.

The first version is intentionally conservative: CLI first, global vault first, internal tools only, and no free-form multi-agent chatter.

## Architecture

```text
User
  -> Operator CLI
  -> Oracle intent and ethics pass
  -> Architect technical agent spec
  -> Oracle human nature pass
  -> Neo preflight security review
  -> Nebuchadnezzar runtime
  -> Neo output review
  -> Oracle final response
  -> Obsidian vault and SQLite runtime index
```

## Memory Model

The memory system follows the Karpathy-style wiki pattern:

- `raw/` stores immutable request, run, tool, and security records.
- `wiki/` stores synthesized knowledge, reusable agent notes, workflows, decisions, and risks.
- `schema/` stores the rules for how memory is written and maintained.
- `index.md` maps the vault.
- `log.md` gives the user-visible timeline.

SQLite is not the long-term memory. It is the runtime index for exact lookups:

- agent registry
- provider catalog
- prompt block hashes
- run metadata
- security events
- user preferences

## Package Identity

```text
Product name:      The Matrix
PyPI package:      the-matrix-agent-system
Python module:     thematrix
CLI command:       the-matrix
```

## Usage

```text
uv tool install the-matrix-agent-system
the-matrix init
the-matrix setup
the-matrix providers list
the-matrix providers test
the-matrix ask "Create a reusable research agent"
```

For development, install the project in editable mode and run:

```text
python -m ruff check .
python -m pytest
```

## Provider Setup

Providers are user-selected. The framework does not force a default provider.

```powershell
the-matrix setup
the-matrix providers configure
the-matrix providers current
the-matrix providers test
```

Secrets are handled by **Keymaker**. Keymaker never stores raw secrets in Obsidian, SQLite,
or logs. The default secure backend uses the operating system credential store through `keyring`.

If no writable OS secret backend is available, Keymaker can read provider keys from environment
variables such as:

```text
THE_MATRIX_OPENROUTER_API_KEY
THE_MATRIX_OPENAI_API_KEY
```

Normal questions do not trigger cloud-use prompts after a provider is configured. Consent is
reserved for meaningful actions such as file changes, shell commands, sensitive memory writes,
or privacy-mode conflicts.

`the-matrix init` offers the onboarding wizard on first run. Use `--no-onboarding` for scripts
or tests that only need the filesystem initialized.

## Model Gateway

Providers are called through a single `ModelGateway` boundary. Oracle, Architect, Neo, and
spawned agents should not know provider-specific HTTP details.

The first gateway supports:

- OpenAI-compatible APIs: OpenRouter, OpenAI, Mistral, Ollama, LM Studio, and custom endpoints.
- Anthropic Messages API.
- Gemini `generateContent` API.

`the-matrix providers test` sends a small readiness prompt through the configured provider and
records only metadata such as provider, model, status, and character counts. It does not write
prompt text or secret values to Obsidian or SQLite.

## Model-Backed Oracle

Oracle can use the configured `ModelGateway` to produce a structured `OracleBrief` from a
markdown prompt. If no provider is configured, credentials are missing, or the model returns
invalid JSON, Oracle falls back to deterministic local rules.

The Oracle prompt is installed into the Matrix home prompt directory during bootstrap, so users
can inspect and later customize it while SQLite keeps only prompt hash metadata.

## Model-Backed Architect

Architect can also use the configured `ModelGateway` to draft an `AgentSpec`. The model is only
allowed to suggest the agent shape: type, purpose, capabilities, memory scope, constraints, and
interaction points. Local code still owns provider selection, privacy mode, stable agent IDs,
tool filtering, prompt hash tracking, and reuse lookup.

Agent blueprint prompt text is written to markdown under the Matrix prompt directory. SQLite keeps
the prompt block hash and reusable agent index, while Obsidian gets a user-visible agent page under
`wiki/agents/`.

After execution, the runtime updates reusable agent success/failure counters in SQLite. Dry-run
planning without a configured provider does not count as success, because the spawned agent did not
actually complete the request.

Multi-agent cooperation starts sequentially: Architect creates an ordered mission plan, each task
gets its own agent spec, and Nebuchadnezzar runs one task at a time while passing previous task
results forward. SQLite stores the task ledger and Obsidian gets a readable workflow page.

## Neo Security Reviews

Neo reviews the final `AgentSpec` before execution and the final response before user delivery.
It checks local-only privacy boundaries, unknown tools, unsafe memory scopes, missing prompt-cache
references, missing user checkpoints for file/shell actions, prompt-injection-like language, and
credential-like output. Each review is written to Obsidian under `raw/neo_reviews/`.

## Agent Execution

When a provider is configured and Neo approves the spec, the runtime spawns the selected agent by
calling the configured model with the agent blueprint markdown and the current Oracle brief. If no
provider is configured, the run stays in safe planning mode and tells the user to configure a
provider before execution.

Spawned agents can request guarded shell commands through structured JSON. Low-risk read/check
commands such as `git status`, `git diff`, `pytest`, `ruff check`, and directory listing can run
automatically. Commands that can change the machine, install packages, make network calls, or push
code require explicit CLI approval. Dangerous patterns are blocked before approval. Tool outputs
are written to Obsidian under `raw/tool_outputs/`.

Spawned agents can also request guarded file reads and writes. Safe reads inside the current
workspace are allowed automatically. File writes require approval unless onboarding/provider
settings allow file changes. Paths outside the workspace and secret-looking paths are blocked.

## Inspection Commands

- `the-matrix agents list` shows reusable agents tracked by Architect.
- `the-matrix agents show <agent-id>` shows one agent spec without printing prompt text.
- `the-matrix memory prompt-blocks` shows prompt-cache hashes.
- `the-matrix memory security` shows recent Neo events.
- `the-matrix memory model-calls` shows model-call metadata without prompt or response text.
- `the-matrix memory tasks` shows recent sequential mission tasks.
- `the-matrix doctor` shows local setup health without exposing secrets.
