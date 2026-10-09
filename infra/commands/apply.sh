#!/bin/sh
: Fast-forward a project to the commits fetched from its sharers, for example qq apply my-project
. infra/qq-lib.sh
qq_ops apply "$@"
