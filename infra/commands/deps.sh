#!/bin/sh
: Rebuild .qq/venv with the pinned Python and requirements.txt
exec qq build server
