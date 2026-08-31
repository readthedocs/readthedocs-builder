# Read the Docs builder

A standalone Python runner that builds Read the Docs documentation end-to-end
— clone, install dependencies, run Sphinx / MkDocs, upload artifacts to S3,
report build state and notifications back to the RTD API — **without
depending on Django or the host-side `docker exec` orchestration**.

The goal is to isolate the entire build process into one self-contained
script that runs inside a Read the Docs build container (`readthedocs/build` image).
The production orchestration layer is the ``worker/`` package, which runs on an ASG
of ephemeral EC2 instances built from a Packer-baked AMI.

## Packages

A uv workspace with three members:

| Package | Runs | Responsibility |
|---|---|---|
| `builder/` | inside the build container | The runner: clone, install tools and dependencies, build the docs, upload the artifacts, report state back to the API. Most of the code lives here. |
| `worker/` | on the host (an EC2 instance, one per build) | A single Celery task. Takes a build off the `build:isolated` queue, reads `build.os` out of `.readthedocs.yaml`, starts the container, then terminates the instance. |
| `shared/` | both | `readthedocsext.builds`: the API v2 client and the `build.os` / `build.tools` tables. |

`builder` and `worker` never import each other — the host orchestrates, the
container builds. Only the container touches user code, which is why the worker
keeps the AWS credentials and the broker to itself.

## What it does

Given an existing Build PK in the database via `--build-pk` argument, the runner:

1. Fetches the build, version and project from the RTD API.
2. Resets the build to clear prior commands / notifications from earlier attempts.
3. Walks the build through state transitions (`cloning` → `installing` →
   `building` → `uploading` → `finished`) with `PATCH /api/v2/build/<id>/`.
4. Clones the git repository and parses `.readthedocs.yaml`.
5. Installs `build.tools` versions via `asdf` — with an S3-cached tarball
   fast path when available.
6. Sets up the language environment (`Virtualenv` / `UvEnv` / `Conda`).
7. Installs the user's Python dependencies.
8. Runs the doctype builder (Sphinx HTML / PDF / EPUB / HTMLZip or MkDocs).
9. Validates the artifacts and uploads them to S3.
10. Finalizes the build (`success`, `length`).
11. On failure, attaches a notification with the canonical `message_id` so
    the dashboard can render the user-facing message.

## Running a build in development

The builder isn't invoked standalone — it runs as part of the Read the
Docs dev environment, on the exact same code path as production. Local
`docker-compose` dispatches every build to the `build:isolated` queue,
where the `build-isolated` compose service (the dev emulator for the
production AMI) picks it up, spawns a build container, and runs the
runner (`builder/`) inside it.

### Setup

1. Check this repository out next to `readthedocs.org`. The
   `build-isolated` service bind-mounts `../readthedocs-builder` by
   default (override with `RTDDEV_PATH_BUILDER`)
2. Bring up the Read the Docs dev environment (http://devthedocs.org/).

### Trigger a build

Trigger a build the normal way — from the project's dashboard.
Note the project needs `Feature.USE_BUILD_ISOLATED`.

## Architecture

```
Runner — lifecycle (reset → state transitions → validate → upload → finalize → notify)
   └── BuildDirector — orchestration (VCS → environment → install → build)
         ├── BuildEnvironment — subprocess, $VAR expansion, runuser drop
         ├── vcs.Backend       — git clone / fetch / checkout / submodules
         ├── PythonEnvironment — Virtualenv / UvEnv / Conda
         ├── backends.sphinx / .mkdocs — sphinx-build, mkdocs build, latexmk
         ├── config.BuildConfigV2      — .readthedocs.yaml parser
         └── storage — boto3 wrapper for the build-tools cache + artifact upload
```

The lifecycle (`Runner`) is intentionally separate from the orchestration
(`BuildDirector`). The director focuses on doing-the-build; the runner
owns "is the build object in the right state for the world to see it" —
state transitions, validation, upload, finalize, notification.

## Limitations

Known gaps against upstream include build concurrency limits and features that are
not implemented yet.
