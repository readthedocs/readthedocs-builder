"""
Minimal Celery worker that runs Read the Docs builds.

This package is installed on a Packer-baked AMI and run as a systemd
service (``readthedocs-celery-worker.service``). One worker process per
EC2 instance consumes the ``build:isolated`` queue. On receiving its first
``run_build`` task the worker cancels the queue consumer so it never takes
another; when that task completes (or fails) it calls
``autoscaling:TerminateInstanceInAutoScalingGroup`` on the host so the
instance is removed from the ``build-isolated`` ASG (the ASG then
launches a fresh instance to take its place).
"""
