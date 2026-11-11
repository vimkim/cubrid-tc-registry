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

# Reinstall the user-level cubrid-tcr command from the primary main checkout.
sync:
    @test -d .git || { echo "Run just sync from the primary checkout, not a task worktree." >&2; exit 1; }
    @test "$(git branch --show-current)" = main || { echo "Run just sync from main." >&2; exit 1; }
    uv tool install --reinstall .
    @"$(uv tool dir --bin)/cubrid-tcr" --version
