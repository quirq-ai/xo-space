#!/bin/sh
: Restore a project from its newest backup, for example qq restore my-project, or --all, --snapshot ID, --force
. infra/qq-lib.sh
qq_ops restore "$@"
