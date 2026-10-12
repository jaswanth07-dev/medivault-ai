# AGENTS.md

## Project state

This workspace is currently empty or uninitialized. There is no detected application code, framework, or project documentation yet.

Treat this folder as a blank slate unless the user explicitly asks to scaffold a specific stack or feature.

## Working conventions for AI coding agents

- Start by checking the repository state before making assumptions.
- Do not invent a framework, package manager, or build pipeline if none is present.
- Prefer small, targeted changes and avoid creating unrelated files or boilerplate.
- If the project is not yet initialized, ask which stack or app type the user wants before generating code.
- When a project does exist later, follow the repository's real conventions, docs, and configs instead of generic defaults.

## When a project is introduced

- Read any existing documentation before coding.
- Prefer the existing project structure and naming conventions.
- Use the repository's actual test, build, lint, and run commands instead of guessing.
- Keep modifications scoped to the user request and avoid opportunistic cleanup.

## Default behavior for this workspace

- Use a minimal, explicit workflow.
- Prefer clarifying questions over assumptions in an empty workspace.
- Confirm the intended language, framework, and runtime before scaffolding or implementing features.

## Example of good behavior

- "I don't see a project yet. Do you want a Node/React app, a Python service, a static site, or a different stack?"

This keeps future agents aligned with the user's intent instead of producing incorrect boilerplate.
