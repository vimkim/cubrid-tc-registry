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

# Install bundled skills (copy to ~/.agents/skills and link for Claude, Codex, and Antigravity).
install-skills:
    #!/usr/bin/env bash
    set -euo pipefail
    skill_root="{{justfile_directory()}}/skills"
    agents_dir="$HOME/.agents/skills"
    claude_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
    codex_dir="${CODEX_HOME:-$HOME/.codex}/skills"
    agy_dir="$HOME/.gemini/config/skills"
    mkdir -p "$agents_dir" "$claude_dir" "$codex_dir" "$agy_dir"

    link_agent() {
        local target="$1"
        local dir="$2"
        local name="$3"
        local link_path="$dir/$name"
        if [ -L "$link_path" ] || [ -e "$link_path" ]; then
            rm -rf "${dir:?}/${name:?}"
        fi
        ln -s "$target" "$link_path"
    }

    for src in "$skill_root"/*; do
        [ -d "$src" ] && [ -f "$src/SKILL.md" ] || continue
        name="$(basename "$src")"
        staging="$(mktemp -d "${agents_dir}/.${name}.XXXXXX")"
        cp -R "$src/." "$staging/"
        find "$staging" -type f -name '*.pyc' -delete
        find "$staging" -type d -name '__pycache__' -empty -delete
        if [ -L "$agents_dir/$name" ] || [ -e "$agents_dir/$name" ]; then
            rm -rf "${agents_dir:?}/${name:?}"
        fi
        mv "$staging" "$agents_dir/$name"
        if [ "$claude_dir" = "$HOME/.claude/skills" ]; then
            link_agent "../../.agents/skills/$name" "$claude_dir" "$name"
        else
            link_agent "$agents_dir/$name" "$claude_dir" "$name"
        fi
        link_agent "$agents_dir/$name" "$codex_dir" "$name"
        link_agent "$agents_dir/$name" "$agy_dir" "$name"
        printf 'Installed skill %s -> %s\n' "$name" "$agents_dir/$name"
    done
