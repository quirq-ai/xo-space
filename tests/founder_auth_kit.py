"""A synthetic browser credential isolated by each test's state directory."""

from services import founder_auth

SECRET = "a" * 64


def connect(client):
    founder_auth.enroll(founder_auth.digest(SECRET))
    client.cookies.set(founder_auth.COOKIE, SECRET)
    return client
