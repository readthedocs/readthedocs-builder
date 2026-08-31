"""
Manual test of the build-isolated worker WITHOUT deploying the
readthedocs.org dispatcher code.

Load a real Build from the production DB, mint a fresh per-build API key,
construct the task kwargs ``trigger_build`` *would* send, and fire
``app.send_task("worker.tasks.run_build", queue="build:isolated", ...)``
directly against whatever broker ``readthedocs.worker.app`` is
configured for.

The worker on a fresh build-isolated EC2 instance (booted from the
new Packer AMI) picks it up, fetches build/project data from the API
using ``build_api_key``, sparse-clones ``.readthedocs.yaml`` to figure
out ``build.os``, and then ``docker run``s the build container.

Run with:
    cat test_build_isolated_manual.py | uv run django-admin shell 

Iterate by editing the CONFIG block below and re-running. Each run
mints a fresh BuildAPIKey, so re-running is safe.

Prereqs (one-time, currently set up by hand):
  1. Packer-baked AMI exists (tagged ``Purpose=build-isolated``) —
     see ``builder.pkr.hcl`` in readthedocs-ops
     (salt/base/utils/ops/build-isolated/). The AMI has
     /usr/src/builder/runner-venv + /usr/src/builder/uv-python pre-built by the boot-time
     setup unit, and /usr/src/builder/worker-venv populated with the celery
     worker + boto3 + Sentry + New Relic.
  2. Launch template re-pointed at the new AMI.
  3. At least one instance running in the ``build-isolated`` ASG
     from the new AMI. Verify with::
        ssh readthedocs@<ip> 'systemctl status readthedocs-celery-worker'
  4. A real ``Build`` record exists in the DB whose pk you'll put in
     ``BUILD_PK`` below. Easiest path: trigger a normal build through
     the dashboard for any project, grab the build pk from the URL,
     and (optionally) cancel it on the legacy fleet so we don't get
     double-execution. The runner inside the isolated container will
     ``Build.reset()`` at startup so any prior state from the legacy
     attempt is wiped.
"""

from django.conf import settings

from readthedocs.api.v2.models import BuildAPIKey
from readthedocs.builds.models import Build
from readthedocs.worker import app


BUILD_PK = 33580637

# Task name + queue MUST match what the worker registers. See
# ``readthedocs-builder/worker/tasks.py`` (``@app.task(name=...)``)
# and ``worker/celery.py`` (``task_default_queue``).
TASK_NAME = "worker.tasks.run_build"
QUEUE = "build:isolated"

# Per-project debug flag. Set to True to ask the worker to skip
# autoscaling:TerminateInstanceInAutoScalingGroup after the task,
# leaving the instance alive for inspection. Set to False to test the
# self-terminate path (instance disappears from the ASG ~30s after the
# task ends; the ASG launches a replacement).
NO_SELF_TERMINATE = True


build = Build.objects.select_related("version__project").get(pk=BUILD_PK)
project = build.version.project
version = build.version
print(
    f"Build {build.pk} on project {project.slug} "
    f"(version {version.slug} / {version.identifier})"
)


_, build_api_key = BuildAPIKey.objects.create_key(project=project)
print(f"Minted build API key: {build_api_key[:8]}...")


# The worker also fetches project + build data from the API using
# ``build_api_key`` and picks the docker image + resource caps itself,
# so we don't need to pre-compute build.os / memory / time here.
environment = {
    "RTD_API_URL": getattr(settings, "RTD_API_URL", settings.PUBLIC_API_URL),
    "RTD_PRODUCTION_DOMAIN": settings.PRODUCTION_DOMAIN,
}


# ``app`` here is ``readthedocs.worker.app`` — the existing Celery app
# already wired to the production broker. Sending by NAME (rather than
# importing run_build) means we don't need the worker package
# installed on the readthedocs.org side.
print(
    f"Sending {TASK_NAME} to queue '{QUEUE}' "
    f"no_self_terminate={NO_SELF_TERMINATE}"
)
result = app.send_task(
    TASK_NAME,
    kwargs={
        "build_pk": build.pk,
        "build_api_key": build_api_key,
        "environment": environment,
        "no_self_terminate": NO_SELF_TERMINATE,
    },
    queue=QUEUE,
)

# Store the worker task id on the Build so ``cancel_build`` can revoke
# it via ``app.control.revoke(build.task_id, signal="SIGINT",
# terminate=True)``. Matches ``submit_to_build_isolated`` in
# ``readthedocs.org``'s ``core/utils/__init__.py``.
build.task_id = result.id
build.save()

print()
print("=" * 60)
print(f" Worker task id:     {result.id}")
print(f" Build pk:           {build.pk}")
print(f" Project slug:       {project.slug}")
print("=" * 60)
print()
print(" Watch the worker pick it up:")
print(f"   ssh readthedocs@<build-isolated-ip>")
print(f"   sudo journalctl -u readthedocs-celery-worker -f")
print(f"   docker logs -f build-{build.pk}")
print()
print(" Build state in the dashboard:")
print(f"   https://{settings.PRODUCTION_DOMAIN}/projects/{project.slug}/builds/{build.pk}/")
