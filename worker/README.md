# worker

The host-side half of an isolated build. A minimal Celery worker that runs
**one** Read the Docs build and then throws the instance away.

It's installed on a Packer-baked AMI and runs as a systemd service, one worker
process per EC2 instance in the `build-isolated` ASG. The worker consumes the
`build:isolated` queue with `--max-tasks-per-child=1`, so it handles a single
`run_build` task and exits.

It never imports the runner and never touches user code — the host orchestrates,
the container builds.

## What happens

1. **Receive.** `trigger_build` (in readthedocs.org) sends a `run_build` task
   with a `build_pk`, a 24h-scoped `build_api_key`, and some env vars. The
   worker enables scale-in protection so the ASG won't kill the instance
   mid-build.

2. **Prepare.** Using the API key, fetch Build → Version → Project, sparse-clone
   `.readthedocs.yaml` to read `build.os`, sync the repo's tags/branches into the
   database, and resolve the docker image, memory, and time limit.

3. **Run.** `docker run` the `readthedocs/build:<os>` container and wait for it
   to finish. The container is what actually clones the repo and builds the docs.
   If anything fails *before* the container starts, the worker marks the build
   failed itself.

4. **Terminate.** When the task finishes — success or failure — release scale-in
   protection and self-terminate the EC2 instance. The ASG launches a fresh one
   to take its place.
