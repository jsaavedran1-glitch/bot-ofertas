from __future__ import annotations

from pathlib import Path
import re

import requests


class FacebookPublishError(RuntimeError):
    def __init__(self, message: str, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous


class FacebookPublisher:
    def __init__(
        self,
        page_id: str,
        page_token: str,
        api_version: str,
        session: requests.Session | None = None,
    ) -> None:
        if not page_id or not page_token:
            raise FacebookPublishError("Faltan FB_PAGE_ID o FB_PAGE_TOKEN.")
        if not re.fullmatch(r"v\d+\.\d+", api_version):
            raise FacebookPublishError("META_GRAPH_API_VERSION no es válida.")
        self.page_id = page_id
        self.page_token = page_token
        self.api_version = api_version
        self.session = session or requests.Session()

    def publish_photo(self, image_path: Path, caption: str) -> str:
        if not image_path.is_file():
            raise FacebookPublishError("No existe la imagen que se intentó publicar.")
        if image_path.stat().st_size > 10 * 1024 * 1024:
            raise FacebookPublishError("La imagen supera el límite de 10 MB de Meta.")
        url = f"https://graph.facebook.com/{self.api_version}/{self.page_id}/photos"
        try:
            with image_path.open("rb") as handle:
                response = self.session.post(
                    url,
                    headers={"Authorization": f"Bearer {self.page_token}"},
                    data={"caption": caption, "published": "true"},
                    files={"source": (image_path.name, handle, "image/png")},
                    timeout=(8, 45),
                )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise FacebookPublishError(
                "Resultado ambiguo: se perdió la conexión durante la publicación; requiere conciliación manual.",
                ambiguous=True,
            ) from exc
        except requests.RequestException as exc:
            raise FacebookPublishError("No se pudo contactar la API de Meta.") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not response.ok:
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            message = f"Meta rechazó la publicación (HTTP {response.status_code}"
            if code is not None:
                message += f", código {code}"
            message += ")."
            raise FacebookPublishError(message, ambiguous=response.status_code >= 500)
        post_id = payload.get("post_id") or payload.get("id") if isinstance(payload, dict) else None
        if not post_id:
            raise FacebookPublishError("Meta respondió sin identificador de publicación.", ambiguous=True)
        return str(post_id)
