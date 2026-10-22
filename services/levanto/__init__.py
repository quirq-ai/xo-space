"""The one client for Levanto Sage, the decision model (https://sage.levanto.ai).

``client.py`` is the only module that knows the base URL or sends a request
there. Its key is the router's (``services/decision_models/keys.py``,
``LEVANTO_API_KEY`` in ``router.env``). Without a key nothing is sent.
"""
