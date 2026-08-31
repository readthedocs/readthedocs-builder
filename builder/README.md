# builder

The container-side half of a build. A standalone Python runner that builds a
project's documentation end-to-end — clone, install tools and dependencies, run
Sphinx / MkDocs, upload the artifacts, and report state back to the RTD API.

It runs inside a `readthedocs/build` container, started by the `worker`. It has
no Django and no host-side `docker exec` orchestration — it's the only piece
that touches user code. Invoked as `python -m builder --build-pk <id>`.

## What happens

1. **Fetch.** Given a `--build-pk`, fetch the Build → Version → Project from the
   API and reset the build to clear anything left from earlier attempts.

2. **Clone & configure.** Clone the git repository and parse `.readthedocs.yaml`
   to learn what tools, dependencies, and doctype to build.

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
