<p align="center" style="text-align:center">
  <img src="resources/serena-logo.svg#gh-light-mode-only" style="width:500px">
  <img src="resources/serena-logo-dark-mode.svg#gh-dark-mode-only" style="width:500px">
</p>

# Serena (Desko77 Fork)

> Fork of [oraios/serena](https://github.com/oraios/serena) with extended BSL (1C:Enterprise) support, new agent tools, and Docker-ready deployment.

* :rocket: Serena is a powerful **coding agent toolkit** capable of turning an LLM into a fully-featured agent that works **directly on your codebase**.
  Unlike most other tools, it is not tied to an LLM, framework or an interface, making it easy to use it in a variety of ways.
* :wrench: Serena provides essential **semantic code retrieval and editing tools** that are akin to an IDE's capabilities, extracting code entities at the symbol level and exploiting relational structure. When combined with an existing coding agent, these tools greatly enhance (token) efficiency.
* :free: Serena is **free & open-source**, enhancing the capabilities of LLMs you already have access to free of charge.

## What's New in This Fork

### BSL (1C:Enterprise) Local Cache Mode

Full rewrite of the BSL language server — from a thin wrapper around Java `bsl-language-server.jar` to a **pure Python local cache mode** that requires no external dependencies:

| Component | Description |
|-----------|-------------|
| `bsl_parser.py` | Regex-based parser extracting procedures, functions, module variables, and call positions from BSL code |
| `bsl_cache.py` | Thread-safe in-memory symbol cache with indexed lookups (by name, module, export status) |
| `bsl_language_server.py` | Complete local cache mode: parallel indexing via `ThreadPoolExecutor`, fingerprint-based (MD5) cache invalidation, call graph for references and rename |

**Key advantages over the original:**
- No Java dependency — pure Python implementation
- Parallel file indexing (up to 500 workers, 30s timeout per file)
- Fingerprint system for incremental re-indexing (only changed files are re-parsed)
- Pickle-persistent `DocumentSymbols` cache between sessions
- Full call graph: find references and rename across the entire project
- Thread-safe cache operations via `threading.Lock`

### New Agent Tools

| Tool | Description |
|------|-------------|
| **DiagnosticsTool** | Retrieves LSP diagnostics (errors, warnings, information, hints) for any file. Converts severity codes to human-readable labels. |
| **TypeHierarchyTool** | Recursive traversal of type hierarchy (supertypes/subtypes) for classes and interfaces. Configurable direction and depth. |

Both tools are auto-discovered by `ToolRegistry` and available to all MCP clients.

### Type Hierarchy LSP Methods

Three new methods in `SolidLanguageServer`:
- `request_prepare_type_hierarchy(file, line, character)` — initiates type hierarchy at a position
- `request_type_hierarchy_supertypes(item)` — resolves parent types
- `request_type_hierarchy_subtypes(item)` — resolves child types

### Admin Panel

Web-based admin UI for managing multiple Serena instances running in Docker:

- Real-time server list with status, port, transport, PID, memory, uptime
- System stats: RAM usage bar, CPU count, load average
- Per-project actions: Stop / Start / Restart / Logs / Stats / Remove
- Add projects from dropdown (auto-discovers mounted directories)
- BSL cache statistics preservation across restarts

Access at `http://localhost:9000` when `SERENA_ADMIN_PORT=9000` is set.

### Docker Compose Generator

CLI tool to generate `docker-compose.yml` with **per-project bind mounts** (rw) from a config file — no more mounting entire disks:

```bash
# 1. Create config
serena docker init-config          # creates ~/.serena/docker.yml

# 2. Edit config — add your project paths
#    ~/.serena/docker.yml:
#    projects:
#      - C:\Projets
#      - /home/user/my-project

# 3. Generate docker-compose.yml
serena docker generate-compose     # outputs docker-compose.yml

# 4. Start
docker compose up -d
```

Each project path is mounted as `/projects/<basename>` (read-write) inside the container. Cyrillic directory names are automatically transliterated to ASCII for container paths.

### Docker-Ready Deployment

The Dockerfile includes all major runtimes for full language server support:

| Runtime | Version | Supports |
|---------|---------|----------|
| Python | 3.11 | Pyright, Jedi |
| Node.js | 22.x | TypeScript LS, Bash LS, Intelephense (PHP) |
| Java JDK | 21 | Eclipse JDTLS (Java), Kotlin LS |
| .NET SDK | 8.0 | CSharpLanguageServer, OmniSharp |
| Go | 1.23.x | gopls |
| Rust | stable | rust-analyzer |

## Supported Languages (36)

| Language | Server | Language | Server |
|----------|--------|----------|--------|
| AL | AL LS | Lua | lua-language-server |
| Bash | bash-language-server | Markdown | Marksman |
| **BSL (1C)** | **Local Cache (Python)** | Nix | nixd |
| C# | MS CodeAnalysis / OmniSharp | Perl | PerlNavigator |
| C/C++ | clangd | PHP | Intelephense |
| Clojure | clojure-lsp | Python | Pyright / Jedi |
| Dart | Dart LS | R | R language server |
| Elixir | ElixirLS | Rego | Regal |
| Elm | elm-language-server | Ruby | ruby-lsp / Solargraph |
| Erlang | erlang_ls | Rust | rust-analyzer |
| Fortran | fortls | Scala | Metals |
| Go | gopls | Swift | SourceKit-LSP |
| Haskell | HLS | Terraform | terraform-ls |
| Java | Eclipse JDT LS | TypeScript/JS | TypeScript LS / VTS |
| Julia | Julia LS | YAML | yaml-language-server |
| Kotlin | Kotlin LS | Zig | ZLS |

## LLM Integration

Serena can be integrated with an LLM in several ways:

* **MCP (Model Context Protocol)** — integrates with Claude Code, Claude Desktop, Cursor, VSCode, Cline, Roo Code, Codex, Gemini-CLI, and others
* **OpenAPI** via [mcpo](docs/03-special-guides/serena_on_chatgpt.md) — connects to ChatGPT and other clients
* **Custom agent frameworks** — Serena's tool implementation is decoupled from framework-specific code

## Quick Start

**Prerequisites**: Install [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
# Clone this fork
git clone https://github.com/Desko77/serena.git
cd serena

# Run MCP server
uvx --from . serena start-mcp-server --help
```

**Docker (recommended):**

```bash
# 1. Init config and add your project paths
serena docker init-config
# Edit ~/.serena/docker.yml — add paths under 'projects:'

# 2. Generate docker-compose.yml with per-project mounts
serena docker generate-compose

# 3. Start
docker compose up -d

# Admin panel at http://localhost:9000
# Projects at http://localhost:9200, :9201, ...
```

**Docker (manual):**

```bash
docker build --target production -t serena .
docker run -v /path/to/project:/projects/my-project serena \
  "serena start-mcp-server --project /projects/my-project --transport stdio"
```

## Running Multiple Projects

Serena supports parallel work with multiple projects via process-level isolation — each project runs as its own MCP server process with auto-restart.

### stdio mode (Claude Code / Cursor)

Each MCP client connects to its own `docker exec` session:

```bash
docker compose up -d

# In your MCP client config, add per project:
# "command": "docker", "args": ["exec", "-i", "serena-multi", "serena-mcp-server", "--project", "/projects/my-project"]
```

### HTTP mode — SSE or streamable-http (web clients, parallel access)

All projects start automatically on sequential ports:

```bash
# Using generated compose (recommended):
serena docker generate-compose
docker compose up -d

# Or manually with env vars:
SERENA_MULTI_SERVER=1 SERENA_TRANSPORT=streamable-http docker compose up -d

# Projects available at localhost:9200, :9201, ...
# Admin panel at localhost:9000
```

### Multi-server CLI

```bash
# Start multi-server locally (without Docker)
serena start-multi-server --transport sse --base-port 9200 --projects-dir /path/to/projects

# Manage individual servers
serena multi-server status                    # table: project | port | status | pid | uptime
serena multi-server stop <project_name>       # stop a specific project
serena multi-server start <project_name>      # start a previously stopped project
serena multi-server restart <project_name>    # restart a project
```

### Docker Compose Generator CLI

```bash
serena docker init-config                     # create ~/.serena/docker.yml template
serena docker generate-compose                # generate docker-compose.yml from config
serena docker generate-compose --dry-run      # preview without writing
serena docker generate-compose --config /path/to/config.yml --output /path/to/output.yml
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SERENA_MULTI_SERVER` | `0` | Set to `1` to enable HTTP multi-server mode |
| `SERENA_TRANSPORT` | `sse` | `sse` or `streamable-http` |
| `SERENA_PROJECTS_DIR` | — | Directory containing project subdirectories |
| `SERENA_BASE_PORT` | `9200` | Starting port (each project gets +1) |
| `SERENA_HOST` | `0.0.0.0` | Bind address |
| `SERENA_ADMIN_PORT` | — | Admin API port (disabled by default) |
| `SERENA_CONFIG_PATH` | — | Override path to `serena_config.yml` |

## Development

```bash
# Install dependencies
uv sync --extra dev

# Format code
uv run poe format

# Type-check
uv run poe type-check

# Run tests (excludes rust and erlang by default)
uv run poe test

# Run specific language tests
uv run poe test -m "python or go"
```

### Project Structure

```
src/
├── serena/                    # Agent framework, tools, MCP server, CLI
│   ├── tools/                 # MCP tools
│   │   ├── file_tools.py      #   File operations, search, regex
│   │   ├── symbol_tools.py    #   Symbol finding, diagnostics, type hierarchy
│   │   ├── memory_tools.py    #   Project knowledge persistence
│   │   ├── config_tools.py    #   Project activation, mode switching
│   │   ├── workflow_tools.py  #   Onboarding and meta-operations
│   │   └── cmd_tools.py       #   Shell command execution
│   ├── config/                # Configuration (contexts, modes, projects)
│   ├── resources/
│   │   ├── admin/admin.html   # Admin panel web UI (single-file)
│   │   └── docker.template.yml # Docker config template
│   ├── mcp.py                 # MCP server implementation
│   ├── multi_server.py        # Multi-project process manager + admin API
│   ├── docker_compose.py      # Docker Compose generator from config
│   ├── agent.py               # Main SerenaAgent class
│   └── cli.py                 # CLI entry points (+ docker subcommands)
├── solidlsp/                  # LSP wrapper library
│   ├── language_servers/      # 30+ language server implementations
│   │   ├── bsl_language_server.py  # BSL with local cache mode
│   │   ├── pyright_server.py       # Python (Pyright)
│   │   ├── eclipse_jdtls.py        # Java
│   │   └── ...
│   ├── bsl_parser.py          # BSL regex parser
│   ├── bsl_cache.py           # BSL in-memory cache
│   ├── ls.py                  # SolidLanguageServer (+ type hierarchy)
│   └── ls_config.py           # Language enum and config
└── interprompt/               # Multi-language prompt templates
```

## Acknowledgements

This is a fork of [oraios/serena](https://github.com/oraios/serena). All credit to the original authors and the open-source community.

**Upstream technologies:**
1. [multilspy](https://github.com/microsoft/multilspy) — the basis for Solid-LSP
2. [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
3. All language servers used through Solid-LSP

BSL local cache mode was ported and improved from the [asweetand-a11y/serena](https://github.com/asweetand-a11y/serena) fork.
