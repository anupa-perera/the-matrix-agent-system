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

## Local Development

```powershell
cd F:\sideProjects\the-matrix-agent-system
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\the-matrix init
.\.venv\Scripts\the-matrix providers list
.\.venv\Scripts\the-matrix ask "Create a reusable research agent"
```

If `uv` is installed later, the project metadata is compatible with it.

