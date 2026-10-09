#!/bin/sh
: Back up a project to its encrypted GitHub repo, for example qq backup my-project, or --all, or --list
. infra/qq-lib.sh
qq_ops backup "$@"
