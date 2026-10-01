# mdformat sorts and lowercases link definitions, which breaks the Keep a Changelog layout, and it
# rewraps the entries in changelog.d. scripts/changelog.py checks both instead, so neither
# CHANGELOG.md nor changelog.d is passed to format or check/format.
MARKDOWN_ROOT_FILES := $(filter-out CHANGELOG.md,$(wildcard *.md))

.PHONY: version/get
version/get: ## Get version.
	@uv version | awk '{print $$2}'
	
.PHONY: version/set
version/set: ## Set version.
	@uv version $(v)
	@make version/commit

.PHONY: version/patch
version/patch: ## Bump patch version.
	@uv version --bump patch
	@make version/commit

.PHONY: version/minor
version/minor: ## Bump minor version.
	@uv version --bump minor
	@make version/commit

.PHONY: version/major
version/major: ## Bump major version.
	@uv version --bump major
	@make version/commit

.PHONY: version/commit
version/commit: ## Commit version.
	@uv lock
	@git add pyproject.toml uv.lock
	@git commit -m "chore: bump v$$(make version/get)"

# Commits the roll on a detached HEAD so the local branch still matches its remote. On main, the
# bump-next PR repeats the roll. Pushes only the release tags, since GitHub fires no events for a
# push of more than 3 tags.
.PHONY: version/publish
version/publish: ## Roll the changelog, then create and push git tags. Pass allow_empty=1 to release with no entries.
	@git fetch --tags --force
	@# The roll rewrites CHANGELOG.md and deletes the entry files it folds in, and this target then
	@# commits, tags, and pushes both. An uncommitted edit to either rides into that tag unreviewed,
	@# and an untracked entry file is gone for good.
	@git diff --quiet HEAD -- CHANGELOG.md changelog.d \
		&& test -z "$$(git ls-files --others --exclude-standard -- changelog.d)" \
		|| { echo "CHANGELOG.md or changelog.d holds changes that are not committed, and the roll would overwrite them."; \
		     echo "Commit them, then release. Nothing has been rolled, tagged, or pushed."; \
		     exit 1; }
	@uv run python scripts/changelog.py roll $$(make version/get) $(if $(allow_empty),--allow-empty)
	@git checkout --quiet --detach
	@# Named paths only, so nothing a maintainer happens to have staged rides into a commit this
	@# target tags and pushes. git treats a pathspec it knows nothing about as fatal, and a release
	@# branch cut before changelog.d existed has no entry files in it, so ask git rather than the
	@# filesystem: an ignored .DS_Store is enough to keep the directory itself present.
	@tracked_entries=$$(git ls-files changelog.d); \
		git commit --quiet -m "chore: release v$$(make version/get)" -- CHANGELOG.md $${tracked_entries:+changelog.d}
	@make version/assert-rolled
	@git tag v$$(make version/get)
	@git tag stable -f
	@git push -f origin refs/tags/v$$(make version/get) refs/tags/stable
	@git push origin HEAD:refs/heads/release/v$$(make version/get | awk -F. '{print $$1 "." $$2}')
	@git checkout --quiet -
	
# The roll deletes the entry files it folds in. A commit that staged CHANGELOG.md alone would leave
# them in the tree, and the next release would publish them a second time.
.PHONY: version/assert-rolled
version/assert-rolled: ## Fail if HEAD still carries changelog.d entries.
	@# Exempts what the roll never folds, so this must match _entry_paths in scripts/changelog.py: a
	@# tracked file the roll leaves behind on purpose would block every release with no way out.
	@# Names the files rather than prescribing a remedy, since the three callers recover differently:
	@# version/publish is on a detached HEAD and restarts the step, the manual recipe amends, and CI
	@# can only report. CONTRIBUTING.md, under Making a Release, carries each one.
	@unfolded=$$(git ls-tree -r --name-only HEAD -- changelog.d | grep -v -e '^changelog\.d/README\.md$$' -e '/\.'); \
		test -z "$$unfolded" \
		|| { echo "HEAD still carries these changelog.d entries, so the roll did not reach this commit:"; \
		     echo "$$unfolded" | sed 's/^/  /'; \
		     echo "Nothing has been tagged or pushed. See Making a Release in CONTRIBUTING.md to recover."; \
		     exit 1; }

.PHONY: install
install: ## Install all dependencies.
	@make install/all

.PHONY: install/core
install/core: ## Install core dependencies.
	@uv sync

.PHONY: install/all
install/all: ## Install all dependencies.
	@uv sync --all-groups --all-extras

.PHONY: install/dev
install/dev: ## Install dev dependencies.
	@uv sync --group dev

.PHONY: install/test
install/test: ## Install test dependencies.
	@uv sync --group test

.PHONY: run
run: ## Run the engine from this checkout (published app + local editable engine).
	@uvx --python 3.12 --from griptape-nodes --with-editable . gtn $(ARGS)

.PHONY: run/refresh
run/refresh: ## Run the engine, pulling the latest published app first.
	@uvx --refresh --python 3.12 --from griptape-nodes --with-editable . gtn $(ARGS)

.PHONY: lint
lint: ## Lint project.
	@uv run ruff check --fix

.PHONY: format
format: ## Format project.
	@uv run ruff format
	@uv run mdformat .github docs src tests $(MARKDOWN_ROOT_FILES)

.PHONY: fix
fix: ## Fix project.
	@make format
	@uv run ruff check --fix --unsafe-fixes

.PHONY: check
check: check/format check/lint check/types check/spell check/changelog ## Run all checks.

.PHONY: check/format
check/format:
	@uv run ruff format --check
	@uv run mdformat --check .github docs src tests $(MARKDOWN_ROOT_FILES)

.PHONY: check/lint
check/lint:
	@uv run ruff check .

.PHONY: check/types
check/types:
	@uv run pyright .
	
.PHONY: check/spell
check/spell:
	@uv run typos 

.PHONY: check/changelog
check/changelog: ## Check CHANGELOG.md and the changelog.d entries follow Keep a Changelog.
	@uv run python scripts/changelog.py check

.PHONY: changelog
changelog: ## Print [Unreleased] as the next release will show it, changelog.d entries folded in.
	@uv run python scripts/changelog.py render

.PHONY: test  ## Run all tests.
test: test/unit test/integration test/e2e

.PHONY: test/unit
test/unit: ## Run unit tests.
	@uv run pytest -n auto tests/unit

.PHONY: test/unit/coverage
test/unit/coverage: ## Run unit tests with coverage.
	@uv run pytest -n auto --cov=src/griptape_nodes --cov-report=xml --cov-report=term tests/unit

.PHONY: test/coverage
test/coverage: ## Run all tests with coverage.
	@uv run pytest -n auto --cov=src/griptape_nodes --cov-report=xml --cov-report=term tests/unit

.PHONY: test/integration
test/integration: ## Run integration tests.
	@uv run pytest -n auto tests/integration

.PHONY: test/e2e
test/e2e: ## Run end-to-end tests (spawns subprocesses; slower than unit/integration).
	@uv run pytest tests/e2e

.PHONY: docs/settings-reference
docs/settings-reference: ## Generate the configuration reference doc from the Settings model.
	@uv run python scripts/generate_settings_reference.py

.PHONY: docs
docs: docs/settings-reference ## Build documentation.
	@uv run python -m mkdocs build --clean --strict

.PHONY: docs/serve
docs/serve: docs/settings-reference ## Serve documentation.
	@uv run python -m mkdocs serve
	
.DEFAULT_GOAL := help
.PHONY: help
help: ## Print Makefile help text.
	@# Matches targets with a comment in the format <target>: ## <comment>
	@# then formats help output using these values.
	@grep -E '^[a-zA-Z_\/-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	| awk 'BEGIN {FS = ":.*?## "}; \
		{printf "\033[36m%-12s\033[0m%s\n", $$1, $$2}'
