# Contributing to AFL-api

Thank you for contributing to AFL-api.

This project is developed using GitHub Issues, Pull Requests and OpenAI Codex. The aim is to keep changes small, easy to review and easy to test.

## Development workflow

Each GitHub Issue represents a single unit of work.

For every Issue:

1. Start from the current `main` branch.
2. Create a dedicated feature branch.
3. Implement only the requested scope.
4. Open a Pull Request.
5. Include `Closes #<issue>` in the PR description.
6. After review and testing, merge the PR and delete the feature branch.

Do not combine unrelated work into a single Pull Request.

---

## Scope

Changes should remain focused on the Issue being implemented.

If additional improvements are discovered:

- mention them in the PR discussion, or
- create a new GitHub Issue.

Avoid "while I'm here" changes.

---

## Pull Requests

A good Pull Request should:

- have a clear title
- explain what changed
- explain why it changed
- reference the relevant GitHub Issue
- remain as small as practical

---

## Testing

Before requesting a merge:

- ensure the project still builds successfully
- run any tests relevant to the change
- add new tests where appropriate
- avoid reducing existing test coverage

Infrastructure-only changes (such as Docker configuration) should at least confirm the Docker image builds successfully.

---

## Code style

When modifying existing code:

- follow the existing project style
- keep functions focused
- avoid unnecessary refactoring
- prefer readability over cleverness

---

## Documentation

If behaviour changes, update the documentation as part of the same Pull Request.

---

## Guidance for AI contributors

When implementing an Issue:

Before writing code:

1. Read the GitHub Issue.
2. Confirm the Issue title.
3. Summarise the expected scope.
4. Identify which files are expected to change.

If the required work appears to extend beyond the Issue, stop and explain why before making changes.

Do not infer requirements that are not described by the Issue.

If the requested Issue cannot be accessed or is ambiguous, stop and ask for clarification rather than implementing a different feature.

---

## Branch naming

Feature branches should clearly describe the work being undertaken.

Examples:
codex/add-root-dockerignore
codex/github-actions-ci
codex/hash-api-keys

---

## Commit messages

Use concise, descriptive commit messages.

Examples:
Add root .dockerignore
Hash API keys before storage
Add GitHub Actions CI workflow

---

## Project philosophy

Small Pull Requests are preferred over large ones.

The goal is to keep every change:

- understandable
- reviewable
- testable
- reversible
