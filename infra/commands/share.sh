#!/bin/sh
: Share a project with another Space, for example qq share my-project SPACE_ID
. infra/qq-lib.sh
qq_ops share "$@"
