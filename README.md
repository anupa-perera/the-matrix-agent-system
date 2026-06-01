# The Matrix Agent System

The Matrix Agent System is a local agent app for your computer.

It helps you ask for work, choose an AI model connection, keep readable memory in an Obsidian-style folder, and run agent tasks with safety checks.

You do not need to know the command line to start. The normal setup opens in your browser.

## Download

Use the friendly download page:

```text
https://anupa-perera.github.io/the-matrix-agent-system/
```

Or open the GitHub Releases page:

```text
https://github.com/anupa-perera/the-matrix-agent-system/releases/latest
```

For most Windows users, download:

```text
the-matrix-agent-system-windows.zip
```

## What This App Does

The Matrix gives you a local control center for agent work.

- You type what you want done.
- The system checks the request for safety.
- It designs the right kind of agent for the job.
- It asks before risky actions such as changing files.
- It can hand recurring goals to The Operator after you review and activate them.
- It writes readable memory notes to your own computer.
- It keeps API keys out of notes, logs, and the database.

The app is inspired by *The Matrix*, but you do not need to know the movie. The names are just a clean way to separate responsibilities:

- **Oracle** understands your request and the human side of the answer.
- **Architect** designs the agent and decides what can be reused later.
- **Neo** checks for risk before and after work.
- **Nebuchadnezzar** runs the mission.
- **The Operator** keeps track of goals that need follow-up, including recurring goals.

## Before You Install

You need:

- A Windows, macOS, or Linux computer.
- Internet access for the first install.
- One AI connection.

An AI connection can be:

- OpenRouter browser sign-in.
- OpenAI Codex sign-in through the official Codex app or CLI, if you have an eligible ChatGPT/Codex subscription.
- An API key from a provider such as OpenAI, Anthropic, Gemini, Mistral, or OpenRouter.
- A local model app such as Ollama or LM Studio.

If you are not sure, start with the browser setup. It will guide you.

## Windows Setup

1. Download `the-matrix-agent-system-windows.zip`.
2. Open your Downloads folder.
3. Right-click the ZIP file.
4. Click **Extract All**.
5. Click **Extract**.
6. Open the extracted folder.
7. Open `START_HERE_WINDOWS.txt` if you want click-by-click help.
8. Double-click `Install The Matrix.cmd`.

Windows may show a security warning because this preview is not code-signed yet.

Only continue if you downloaded it from this GitHub project or from someone you trust.

## macOS / Linux Setup

1. Open the latest release page.
2. Download `the-matrix-agent-system-mac-linux.tar.gz`.
3. Extract the file.
4. Open `START_HERE_MAC_LINUX.txt`.
5. Follow the short Terminal steps.

Fast install for users who are comfortable with Terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/anupa-perera/the-matrix-agent-system/main/install.sh | sh
```

## What Happens During Install

The installer:

1. Installs a small Python tool called `uv` if it is missing.
2. Installs The Matrix for your current user account.
3. Does not require administrator permission.
4. Creates shortcuts on Windows.
5. Opens the guided browser setup.

The setup page opens on:

```text
127.0.0.1
```

That means the page is running on your own computer.

## First-Time Browser Setup

When the browser opens:

1. Choose an AI connection.
2. Sign in or paste an API key only if the page asks.
3. Leave advanced settings closed unless you know you need them.
4. Click **Start The Matrix**.
5. Wait for the dashboard.

The setup page stores secrets through **Keymaker**, which uses your operating system credential store when available.

The Matrix does not write API keys to:

- Obsidian notes
- SQLite
- logs
- dashboard pages

## How To Use It After Setup

On Windows, use one of these:

- Desktop shortcut named `The Matrix`
- Start Menu shortcut named `The Matrix`
- `Start The Matrix.cmd` in the extracted folder

On macOS or Linux, run:

```sh
the-matrix start
```

The app opens a local browser dashboard. Type what you want the agents to do, then submit the request.

## Example Requests

You can ask for things like:

```text
Create a reusable research agent for comparing AI tools.
```

```text
Review this project folder and tell me what should be improved first.
```

```text
Plan a safe step-by-step workflow for organizing my notes.
```

```text
Send me a desktop notification every 5 minutes to check the build.
```

Recurring requests are drafted by **The Operator** first. You can inspect the goal, then activate it from the same browser page. The Operator only runs scheduled goals while the local app is open.

If a request needs file changes, shell commands, or sensitive actions, The Matrix should ask before proceeding.

## Safety And Privacy

The Matrix is designed to be local-first.

- The app runs on your computer.
- The browser UI is served from `127.0.0.1`.
- Memory notes are written to a local vault folder.
- Metadata is stored in local SQLite.
- API keys are handled by Keymaker.
- Dangerous shell patterns are blocked.
- Low-risk read/check commands can run automatically.
- File changes require approval unless you explicitly allow them.

Cloud model providers still receive the prompts you send to them. If you need everything to stay on your machine, use a local model provider such as Ollama or LM Studio.

## Where Files Are Stored

App files:

```text
Windows: C:\Users\<your name>\.thematrix
macOS/Linux: ~/.thematrix
```

Readable memory vault:

```text
Windows: C:\Users\<your name>\Documents\The Matrix Vault
macOS/Linux: ~/Documents/The Matrix Vault
```

You normally do not need to open these folders.

## If Something Goes Wrong

Do not close the install window right away.

Take a screenshot or copy the error text.

Useful things to mention when asking for help:

- What step you were on.
- Whether the browser opened.
- Whether the page address started with `127.0.0.1`.
- Whether Windows showed a security warning.
- The exact error message.

If Windows says access is denied:

1. Close any open Matrix windows.
2. Close any terminal windows running The Matrix.
3. Run the installer again.

## One-Line Install Options

Windows Command Prompt:

```cmd
curl -L -o "%TEMP%\install-matrix.cmd" https://raw.githubusercontent.com/anupa-perera/the-matrix-agent-system/main/install.cmd && "%TEMP%\install-matrix.cmd"
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/anupa-perera/the-matrix-agent-system/main/install.ps1 | iex
```

macOS / Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/anupa-perera/the-matrix-agent-system/main/install.sh | sh
```

## For Technical Users

Package identity:

```text
Product name:      The Matrix
PyPI package:      the-matrix-agent-system
Python module:     thematrix
CLI command:       the-matrix
```

Useful commands:

```text
the-matrix start
the-matrix setup-ui
the-matrix providers list
the-matrix providers detect
the-matrix providers test
the-matrix ask "Create a reusable research agent"
the-matrix agents list
the-matrix operator list
the-matrix memory summary
the-matrix missions list
the-matrix doctor
```

Development setup:

```powershell
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest
```

Release publishing guide:

```text
RELEASE_GUIDE.md
```

## Architecture

The first version is intentionally conservative: local-first, sequential missions first, global vault first, and internal tools only.

Request flow:

```text
User
  -> Browser or CLI
  -> The Operator, when the request is a goal that needs tracking or recurrence
  -> Oracle intent and ethics pass
  -> Architect agent design and reuse plan
  -> Oracle human-language adjustment
  -> Neo preflight security review
  -> Nebuchadnezzar runtime
  -> Neo output review
  -> Oracle final response
  -> Obsidian-readable vault and SQLite runtime index
```

Memory model:

- `raw/` stores request, run, tool, and security records.
- `wiki/` stores synthesized knowledge, reusable agent notes, workflows, decisions, and risks.
- `schema/` stores the rules for memory.
- `index.md` maps the vault.
- `log.md` gives the user-visible timeline.

SQLite is not the long-term memory. It is the runtime index for exact lookups such as agent records, provider settings, prompt hashes, run metadata, security events, and user preferences.

## Current Provider Support

The model gateway supports:

- OpenAI-compatible APIs: OpenRouter, OpenAI, Mistral, Ollama, LM Studio, and custom endpoints.
- OpenAI Codex through the official Codex CLI using the user's existing Codex sign-in.
- Anthropic Messages API.
- Gemini `generateContent` API.

OpenRouter supports browser sign-in during onboarding. OpenAI API access still uses API keys. ChatGPT/Codex subscription access is handled through the separate OpenAI Codex provider so The Matrix does not copy or store Codex OAuth tokens.

## Project Status

This is an early preview.

The goal is to become a self-sustaining local agent ecosystem that a non-technical user can install, open, and use when a requirement arises.

The current release is a working foundation, not a polished app-store product yet.
