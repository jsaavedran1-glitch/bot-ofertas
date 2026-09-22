from dataclasses import replace
import tempfile
import unittest

from cryptography.fernet import Fernet

from app.database import Database
from app.ml_auth import MercadoLibreTokenManager
from tests.helpers import settings_for


class TokenResponse:
    status_code = 200

    def json(self):
        return {"access_token": "rotated-access", "refresh_token": "rotated-refresh", "expires_in": 21600}


class TokenSession:
    def __init__(self):
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return TokenResponse()


class MercadoLibreAuthTests(unittest.TestCase):
    def test_refresh_pair_is_rotated_once_and_persisted_encrypted(self):
        with tempfile.TemporaryDirectory() as directory:
            base = settings_for(directory)
            settings = replace(
                base,
                ml_client_id="client",
                ml_client_secret="secret",
                ml_refresh_token="bootstrap-refresh",
                token_encryption_key=Fernet.generate_key().decode("ascii"),
            )
            database = Database(settings.database_url)
            database.initialize()
            session = TokenSession()
            manager = MercadoLibreTokenManager(settings, database, session)
            self.assertEqual(manager.access_token(), "rotated-access")
            self.assertEqual(manager.access_token(), "rotated-access")
            self.assertEqual(session.calls, 1)
