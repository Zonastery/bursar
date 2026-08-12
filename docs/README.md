# Bursar documentation

This directory contains the public Bursar documentation site. Docusaurus renders maintained Markdown/MDX pages and combines them with API, database, notebook, sitemap, search, and LLM-oriented artifacts generated from their canonical sources.

## Information architecture

The site follows the [Diátaxis](https://diataxis.fr/) content model. Each page serves one reader need:

| Content type  | Reader need                                                        | Location                                            |
| ------------- | ------------------------------------------------------------------ | --------------------------------------------------- |
| Get started   | Understand the product boundary and complete the first integration | `docs/intro.mdx`, `docs/quickstart.mdx`             |
| Tutorials     | Learn through a complete, reproducible exercise                    | `../samples/python/notebooks/`                      |
| How-to guides | Complete a production task                                         | `docs/guides/`                                      |
| Concepts      | Understand design decisions and system behavior                    | `docs/concepts/`                                    |
| Reference     | Look up exact commands, configuration, types, and schema details   | `docs/cli.mdx`, `docs/*-api/`, generated references |

The sidebar exposes these categories in that order. Authored category landing pages define the recommended reading and production sequences, while Docusaurus renders their child pages with native document cards. Do not organize pages around internal package names unless the page is API reference.

## Canonical sources

Do not maintain the same technical fact in multiple prose documents. Follow this ownership map:

| Subject                       | Canonical source                       | Site artifact                                                             |
| ----------------------------- | -------------------------------------- | ------------------------------------------------------------------------- |
| Python signatures             | Public `bursar` exports and docstrings | Sphinx Autosummary in `docs/python-api/reference/`                        |
| TypeScript signatures         | TypeScript source and TSDoc            | TypeDoc Markdown in `docs/javascript-api/reference/`                      |
| Database structure            | Ordered SQL migrations                 | Mermaid entity-relationship diagram in `docs/concepts/database-schema.md` |
| Configuration shape           | Pydantic models and generated schema   | `pricing-config.schema.json`, copied into the built site at the same path |
| Executable tutorials          | Jupyter notebooks                      | MDX pages in `docs/notebooks/`                                            |
| Navigation and page summaries | Docusaurus sidebar and front matter    | Search, sitemap, `llms.txt`, and `llms-full.txt`                          |
| Agent procedure               | `../skills/bursar/SKILL.md`            | Linked from `docs/agent-skills.mdx`                                       |

Generated paths are build artifacts. Edit their source instead.

## Requirements

Install these runtimes:

- Node.js 22 or newer and npm
- Bun 1.3.14 (the JavaScript SDK's pinned `packageManager`; TypeDoc runs from this directory's own dependencies)
- Python 3.12 or 3.13 with the Bursar development group, Sphinx, and `sphinx-markdown-builder`

Install dependencies from the repository root and package directories:

```bash
cd python
uv sync --group dev
uv pip install --python .venv sphinx sphinx-markdown-builder
cd ../javascript
bun ci
cd ../docs
npm ci
```

The documentation scripts detect `../python/.venv/bin/python` automatically.
Set `BURSAR_DOCS_PYTHON` only when using a different Python environment.

## Local development

Run the Docusaurus development server from this directory:

```bash
npm start
```

The `prestart` hook regenerates API, notebook, and database artifacts before Docusaurus starts. Edit a source notebook or SDK source, then restart generation when that source changes.

## Verification

Run the complete documentation gate before opening a pull request:

```bash
npm run check
```

The command checks formatting and TypeScript, regenerates derived content, builds every static route, and fails on broken links, anchors, images, or duplicate routes. The build plugin creates `llms.txt`, `llms-full.txt`, and per-page Markdown in `build/`; do not commit those files.

Run individual checks while editing:

```bash
npm run format:check
npm run typecheck
npm run smoke
npm run generate:notebooks
npm run build
```

`npm run smoke` executes one Python provider/webhook path and compiles one
TypeScript provider/webhook path. These two checks cover the public facade,
provider factory, exact amount, nullable commerce, event-constant, and raw
webhook contracts without duplicating the SDK integration suites.

## Authoring standard

Every maintained page must include a specific `title`, `sidebar_label`, and `description` in front matter. Add `keywords` when search terms differ from the page title.

Apply these rules:

- Write sentence-case page headings and task-oriented navigation labels
- Open with one paragraph that states the page outcome or scope
- Keep tutorials, how-to steps, concepts, and reference material on separate pages
- Define an acronym on first use
- Use active voice and direct address
- Put Python and TypeScript equivalents in synchronized Docusaurus tabs
- Pass stable idempotency keys in every replayable monetary example
- Use `Decimal` or decimal strings for exact amounts, never floating-point examples
- Link internal documents by their `.md` or `.mdx` source path so Docusaurus validates and rewrites the route
- Link to generated reference instead of copying exhaustive signatures into prose

Code snippets must be runnable in the stated context. Keep setup variables explicit and explain the observable result after each block.

## Notebook standard

Each notebook must be executable from top to bottom against an isolated environment. Keep committed notebooks free of outputs and execution counts. Store its documentation metadata under `metadata.bursar_docs`; the converter validates that metadata before generating MDX.

Notebook prose should state learning objectives and prerequisites near the beginning. End database-backed tutorials with cleanup. Shared setup belongs in `../samples/python/notebooks/shared.py`, not in copied cells.

## Deployment

The `Deploy Docs` GitHub Actions workflow builds the site and publishes `build/` to GitHub Pages after a push to `main`. No manual `docusaurus deploy` step is required.
