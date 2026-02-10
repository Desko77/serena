# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

**Essential Commands (use these exact commands):**
- `uv run poe format` - Format code (BLACK + RUFF) - ONLY allowed formatting command
- `uv run poe type-check` - Run mypy type checking (covers `src/serena` and `src/solidlsp`) - ONLY allowed type checking command
- `uv run poe test` - Run tests with default markers (excludes rust and erlang by default)
- `uv run poe test -m "python or go"` - Run specific language tests
- `uv run poe lint` - Check code style without fixing (black --check + ruff check)

**Default test marker:** `not rust and not erlang` (configurable via `PYTEST_MARKERS` env var).
A second `-m` option on the command line overrides the default.

**Test Markers:**
Available pytest markers for selective testing:
- **Languages:** `python`, `go`, `java`, `kotlin`, `rust`, `typescript`, `php`, `perl`, `csharp`, `elixir`, `elm`, `terraform`, `clojure`, `swift`, `bash`, `ruby`, `zig`, `lua`, `nix`, `dart`, `erlang`, `scala`, `al`, `rego`, `markdown`, `julia`, `fortran`, `haskell`, `yaml`, `r`
- **Other:** `snapshot` - snapshot tests for symbolic editing operations

**Project Management:**
- `uv run serena-mcp-server` - Start MCP server from project root
- `uv run serena` - Main CLI entry point
- `uv run index-project` - Index project for faster tool performance (deprecated)

**Documentation:**
- `uv run poe doc-build` - Build Sphinx documentation (clean + generate + build)

**Always run format, type-check, and test before completing any task.**

## Architecture Overview

Serena is a dual-layer coding agent toolkit:

### Core Components

**1. SerenaAgent (`src/serena/agent.py`)**
- Central orchestrator managing projects, tools, and user interactions
- Coordinates language servers, memory persistence, and MCP server interface
- Manages tool registry and context/mode configurations

**2. SolidLanguageServer (`src/solidlsp/ls.py`)**  
- Unified wrapper around Language Server Protocol (LSP) implementations
- Provides language-agnostic interface for symbol operations
- Handles caching, error recovery, and multiple language server lifecycle

**3. Tool System (`src/serena/tools/`)**
- **file_tools.py** - File system operations, search, regex replacements
- **symbol_tools.py** - Language-aware symbol finding, navigation, editing
- **memory_tools.py** - Project knowledge persistence and retrieval
- **config_tools.py** - Project activation, mode switching
- **workflow_tools.py** - Onboarding and meta-operations
- **cmd_tools.py** - Shell command execution tools
- **jetbrains_tools.py** / **jetbrains_plugin_client.py** - JetBrains IDE integration

**4. Configuration System (`src/serena/config/`)**
- **Contexts** - Define tool sets for different environments (desktop-app, agent, ide-assistant)
- **Modes** - Operational patterns (planning, editing, interactive, one-shot)
- **Projects** - Per-project settings and language server configs

**5. Additional Modules (`src/serena/`)**
- **mcp.py** - MCP (Model Context Protocol) server implementation
- **cli.py** - Command-line interface entry points (`serena`, `serena-mcp-server`, `index-project`)
- **agent.py** - Main SerenaAgent class
- **project.py** - Project model and management
- **ls_manager.py** - Language server lifecycle management
- **symbol.py** - Symbol representation and operations
- **prompt_factory.py** - System prompt generation from contexts/modes
- **code_editor.py** - Code editing abstractions
- **task_executor.py** - Task execution coordination
- **text_utils.py** - Text processing utilities
- **analytics.py** - Usage analytics
- **dashboard.py** / **gui_log_viewer.py** - GUI components
- **agno.py** - Agno framework integration (optional dependency)
- **constants.py** - Shared constants

**6. Interprompt (`src/interprompt/`)**
- Multi-language prompt template system with Jinja2-based templating and language fallbacks

### Language Support Architecture

Each supported language has:
1. **Language Server Implementation** in `src/solidlsp/language_servers/`
2. **Runtime Dependencies** - Automatic language server downloads when needed
3. **Test Repository** in `test/resources/repos/<language>/`
4. **Test Suite** in `test/solidlsp/<language>/`

### Supported Languages (30+)

| Language | Server | File |
|----------|--------|------|
| Python | Pyright / Jedi | `pyright_server.py` / `jedi_server.py` |
| TypeScript/JS | TypeScript LS | `typescript_language_server.py` |
| C# | MS CodeAnalysis / OmniSharp | `csharp_language_server.py` / `omnisharp.py` |
| Go | gopls | `gopls.py` |
| Rust | rust-analyzer | `rust_analyzer.py` |
| Java | Eclipse JDT LS | `eclipse_jdtls.py` |
| Kotlin | Kotlin LS | `kotlin_language_server.py` |
| PHP | Intelephense | `intelephense.py` |
| Ruby | ruby-lsp / Solargraph | `ruby_lsp.py` / `solargraph.py` |
| Dart | Dart LS | `dart_language_server.py` |
| Elixir | ElixirLS | `elixir_tools/` |
| Clojure | clojure-lsp | `clojure_lsp.py` |
| Scala | Metals | `scala_language_server.py` |
| Haskell | HLS | `haskell_language_server.py` |
| Elm | elm-language-server | `elm_language_server.py` |
| Erlang | erlang_ls | `erlang_language_server.py` |
| Swift | SourceKit-LSP | `sourcekit_lsp.py` |
| C/C++ | clangd | `clangd_language_server.py` |
| Zig | ZLS | `zls.py` |
| Lua | lua-language-server | `lua_ls.py` |
| Perl | PerlNavigator | `perl_language_server.py` |
| Bash | bash-language-server | `bash_language_server.py` |
| Terraform | terraform-ls | `terraform_ls.py` |
| Nix | nixd | `nixd_ls.py` |
| R | R language server | `r_language_server.py` |
| Julia | Julia LS | `julia_server.py` |
| Fortran | fortls | `fortran_language_server.py` |
| YAML | yaml-language-server | `yaml_language_server.py` |
| Markdown | Marksman | `marksman.py` |
| AL (BC) | AL LS | `al_language_server.py` |
| Rego | Regal | `regal_server.py` |
| BSL (1C) | BSL LS | `bsl_language_server.py` |
| Vue | VTS | `vts_language_server.py` |

### Memory & Knowledge System

- **Markdown-based storage** in `.serena/memories/` directories
- **Project-specific knowledge** persistence across sessions
- **Contextual retrieval** based on relevance
- **Onboarding support** for new projects

## Development Patterns

### Adding New Languages
1. Create language server class in `src/solidlsp/language_servers/`
   - Reference implementations: `intelephense.py` (auto-downloads deps), `gopls.py` (needs preinstalled deps), `pyright_server.py` (python-only deps)
2. Add to `Language` enum in `src/solidlsp/ls_config.py` with file extension matchers
3. Update factory method `create()` in `src/solidlsp/ls.py`
4. Create test repository in `test/resources/repos/<language>/test_repo/`
5. Write test suite in `test/solidlsp/<language>/` (must test symbol finding, within-file references, cross-file references)
6. Add pytest marker to `pyproject.toml` under `[tool.pytest.ini_options]`
7. Update README.md and CHANGELOG.md

### Adding New Tools
1. Inherit from `Tool` base class in `src/serena/tools/tools_base.py`
2. Implement required methods and parameter validation
3. Register in appropriate tool registry
4. Add to context/mode configurations

### Testing Strategy
- Language-specific tests use pytest markers
- Symbolic editing operations have snapshot tests (`syrupy` library)
- Integration tests in `test_serena_agent.py`
- Test repositories provide realistic symbol structures in `test/resources/repos/<lang>/test_repo/`
- **Test quality requirements:**
  - Tests must assert that expected symbol names/references were actually found (not just that a list was returned)
  - Tests should never be skipped (exception: missing dependencies or unsupported OS)
  - Tests should run in CI

## Configuration Hierarchy

Configuration is loaded from (in order of precedence):
1. Command-line arguments to `serena-mcp-server`
2. Project-specific `.serena/project.yml`
3. User config `~/.serena/serena_config.yml`
4. Active modes and contexts

## Key Implementation Notes

- **Symbol-based editing** - Uses LSP for precise code manipulation
- **Caching strategy** - Reduces language server overhead with in-memory symbol cache
- **Error recovery** - Automatic language server restart on crashes, timeout management
- **Multi-language support** - 30+ languages with LSP integration
- **MCP protocol** - Exposes tools to AI agents via Model Context Protocol
- **Language server lifecycle** - Discovery → Initialization → LSP handshake → Operation → Shutdown
- **Language server pooling** - Reuse servers across projects when possible

## Working with the Codebase

- **Python version:** `>=3.11, <3.12` (strict) with `uv` for dependency management
- **Formatting:** black (line-length 140) + ruff, target `py311`
- **Type checking:** mypy with strict settings (`disallow_untyped_defs`, `strict_optional`, etc.)
- **Build system:** hatchling; packages `src/serena`, `src/solidlsp`, `src/interprompt`
- Language servers run as separate processes communicating via LSP stdio
- Memory system enables persistent project knowledge in `.serena/memories/`
- Context/mode system allows workflow customization

## Project Structure Quick Reference

```
src/
├── serena/           # Agent framework, tools, MCP server, CLI
│   ├── tools/        # MCP tools (file, symbol, memory, config, workflow, cmd, jetbrains)
│   ├── config/       # Configuration (serena_config.py, context_mode.py)
│   ├── util/         # Utility modules
│   ├── resources/    # Bundled resources (contexts, modes YAML)
│   └── generated/    # Auto-generated code
├── solidlsp/         # LSP wrapper library
│   ├── language_servers/  # 30+ language server implementations
│   ├── lsp_protocol_handler/  # Low-level LSP protocol handling
│   └── ls.py, ls_config.py, ls_types.py, ls_utils.py
└── interprompt/      # Multi-language prompt templates (Jinja2)

test/
├── serena/           # Agent integration tests
├── solidlsp/         # Per-language test suites (30+ directories)
└── resources/repos/  # Minimal test projects per language
```

## Optional Dependencies

- `dev` - Development tools (black, mypy, pytest, ruff, sphinx, etc.)
- `agno` - Agno framework integration (agno, sqlalchemy)
- `google` - Google GenAI integration (google-genai)