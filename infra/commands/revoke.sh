#!/bin/sh
: Stop sharing a project with a Space, for example qq revoke my-project SPACE_ID
. infra/qq-lib.sh
qq_ops revoke "$@"
