from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

import requests

from app.config import Settings
from app.database import Database


class MercadoLibreAuthError(RuntimeError):
    pass


class MercadoLibreTokenManager:
    TOKEN_URL = "https://api.mercadolibre.com/oauth/token"

    def __init__(
        self,
        settings: Settings,
        database: Database,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.session = session or requests.Session()

    def access_token(self) -> str:
        if not self.settings.ml_refresh_token:
            if self.settings.ml_access_token:
                return self.settings.ml_access_token
            raise MercadoLibreAuthError("Faltan ML_ACCESS_TOKEN y ML_REFRESH_TOKEN.")
        if not all((self.settings.ml_client_id, self.settings.ml_client_secret, self.settings.token_encryption_key)):
            raise MercadoLibreAuthError(
                "La renovación segura requiere ML_CLIENT_ID, ML_CLIENT_SECRET y TOKEN_ENCRYPTION_KEY."
            )
        try:
            from cryptography.fernet import Fernet, InvalidToken
        except ImportError as exc:
            raise MercadoLibreAuthError("Instala cryptography para cifrar los tokens rotatorios.") from exc
        try:
            cipher = Fernet(self.settings.token_encryption_key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise MercadoLibreAuthError("TOKEN_ENCRYPTION_KEY no es una clave Fernet válida.") from exc

        def decrypt(value: str) -> str:
            try:
                return cipher.decrypt(value.encode("ascii")).decode("utf-8")
            except (InvalidToken, ValueError) as exc:
                raise MercadoLibreAuthError("No se pudieron descifrar las credenciales de Mercado Libre.") from exc

        def encrypt(value: str) -> str:
            return cipher.encrypt(value.encode("utf-8")).decode("ascii")

        def rotate(row):
            now = datetime.now(timezone.utc)
            if row:
                expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
                if expires > now + timedelta(minutes=5):
                    return {
                        "access_token_enc": row["access_token_enc"],
                        "refresh_token_enc": row["refresh_token_enc"],
                        "expires_at": expires.isoformat(),
                        "access_token": decrypt(row["access_token_enc"]),
                    }
                refresh_token = decrypt(row["refresh_token_enc"])
            else:
                refresh_token = self.settings.ml_refresh_token
            payload = self._refresh(refresh_token)
            access = str(payload["access_token"])
            new_refresh = str(payload["refresh_token"])
            expires = now + timedelta(seconds=max(int(payload.get("expires_in", 21600)) - 60, 60))
            return {
                "access_token_enc": encrypt(access),
                "refresh_token_enc": encrypt(new_refresh),
                "expires_at": expires.isoformat(),
                "access_token": access,
            }

        state = self.database.rotate_integration_credentials("mercadolibre", rotate)
        return state["access_token"]

    def _refresh(self, refresh_token: str) -> dict:
        for attempt in range(3):
            try:
                response = self.session.post(
                    self.TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": self.settings.ml_client_id,
                        "client_secret": self.settings.ml_client_secret,
                        "refresh_token": refresh_token,
                    },
                    headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                    timeout=(5, 20),
                )
            except requests.RequestException as exc:
                if attempt == 2:
                    raise MercadoLibreAuthError("No se pudo renovar el token de Mercado Libre.") from exc
                time.sleep(0.5 * (attempt + 1))
                continue
            if response.status_code == 200:
                try:
                    payload = response.json()
                    if not payload.get("access_token") or not payload.get("refresh_token"):
                        raise ValueError("respuesta incompleta")
                    return payload
                except ValueError as exc:
                    raise MercadoLibreAuthError("Mercado Libre devolvió una respuesta de token inválida.") from exc
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise MercadoLibreAuthError(f"Mercado Libre rechazó la renovación (HTTP {response.status_code}).")
            time.sleep(0.5 * (attempt + 1))
        raise MercadoLibreAuthError("No se pudo renovar el token de Mercado Libre.")
