# builder

The build itself. A standalone Python runner that builds a project's
documentation end-to-end — clone, install tools and dependencies, run
Sphinx / MkDocs, upload the artifacts, and report state back to the RTD API.

It runs on the host, in-process in the `worker` Celery task, and runs every
build command inside the `readthedocs/build` container the worker started, with
`docker exec` — the same way the builder in `readthedocs.org` does. It has no
Django. The worker calls it through `builder.entrypoint.run_build`.

## What happens

1. **Fetch.** Given a `--build-pk`, fetch the Build → Version → Project from the
   API and reset the build to clear anything left from earlier attempts.

2. **Clone & configure.** Clone the git repository and parse `.readthedocs.yaml`
   to learn what tools, dependencies, and doctype to build. Then swap the clone
   container for one running the project's `build.os` image.

3. **Install.** Install the `build.tools` versions via `asdf` (with an S3-cached
   fast path), set up the language environment, and install the user's
   dependencies.

4. **Build.** Run the doctype builder (Sphinx HTML / PDF / EPUB / HTMLZip or
   MkDocs) to produce the output.

5. **Upload & finalize.** Validate the artifacts, upload them to S3, and PATCH
   the build to `finished`. On failure, attach a notification with the canonical
   `message_id` so the dashboard can render a user-facing message.

Throughout, the build is walked through its states (`cloning` → `installing` →
`building` → `uploading` → `finished`) so the dashboard reflects progress.
