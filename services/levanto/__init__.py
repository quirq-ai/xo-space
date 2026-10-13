"""The one client for Levanto Sage, the decision model (https://sage.levanto.ai).

``client.py`` is the only module that knows the base URL, reads
``LEVANTO_API_KEY`` or sends a request there; ``tests/test_levanto_client.py``
enforces it. Without a key nothing is sent.
"""
