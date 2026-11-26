set positional-arguments

# List available commands.
default:
    @just --list

# Run the tests.
test *args:
    uv run --locked python -m unittest discover -s tests "$@"

# Lint and check formatting rules.
lint:
    uv run --locked ruff check src tests

# Run tests and lint.
verify: test lint

# Run the CLI from this checkout.
tcr +args:
    uv run --locked cubrid-tcr "$@"

# Install the user-level cubrid-tcr CLI and AI agent skill from the primary main checkout.
install: sync

# Reinstall the user-level cubrid-tcr command and skill from the primary main checkout.
sync:
    @test -d .git || { echo "Run just sync from the primary checkout, not a task worktree." >&2; exit 1; }
    @test "$(git branch --show-current)" = main || { echo "Run just sync from main." >&2; exit 1; }
    uv tool install --reinstall .
    @"$(uv tool dir --bin)/cubrid-tcr" --version
    "{{just_executable()}}" install-skills

# Install the bundled cubrid-tcr skill for Codex, Claude Code, and generic agents.
install-skills:
    install -Dm644 skills/cubrid-tcr/SKILL.md "$HOME/.agents/skills/cubrid-tcr/SKILL.md"
    install -Dm644 skills/cubrid-tcr/SKILL.md "${CODEX_HOME:-$HOME/.codex}/skills/cubrid-tcr/SKILL.md"
    install -Dm644 skills/cubrid-tcr/SKILL.md "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/cubrid-tcr/SKILL.md"
