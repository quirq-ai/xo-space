"""XO Cowork API tests."""

import os

# Decision-model routing stays off in tests, whatever the checkout's .env says.
# A developer's .env may set a router mode and real decision-model keys, and
# importing server.py loads it. load_dotenv() never overrides a non-empty
# export, and server.py refills a blank one from .env, so both are pinned to
# non-empty values here, before any test imports it. Tests that need a mode or
# a key patch them in.
os.environ["XO_ROUTER_MODE"] = "off"
os.environ["LEVANTO_API_KEY"] = "test-placeholder-not-a-key"
os.environ["TYPESAFE_API_KEY"] = "test-placeholder-not-a-key"
