"""Cross-posting to the Instagram professional account linked to the Facebook Page.

Same Page token, once it carries instagram_basic and instagram_content_publish. Reels go up with Meta's
resumable upload (rupload.facebook.com); photos are published from the copy Facebook already hosts (Instagram
only takes images by public URL). Instagram is a bonus channel: callers only warn on failure.
"""
from __future__ import annotations

from pathlib import Path
import re
import time

import requests


class InstagramPublishError(RuntimeError):
    pass


def instagram_caption(caption: str) -> str:
    """Links aren't clickable in Instagram captions or comments: point to the bio instead."""
    caption = caption.replace("👇 Link en el primer comentario", "🔗 Link en nuestra bio")
    caption = re.sub(r"👉 https?://\S+", "🔗 Link en nuestra bio", caption)
    return caption[:2200]


class InstagramPublisher:
    def __init__(self, ig_user_id: str, token: str, api_version: str, session: requests.Session | None = None,
                 poll_seconds: float = 8, max_wait: float = 180) -> None:
        self.ig_user_id, self.token, self.api_version = ig_user_id, token, api_version
        self.session = session or requests.Session()
        self.poll_seconds, self.max_wait = poll_seconds, max_wait

    @classmethod
    def for_page(cls, page_id: str, token: str, api_version: str, session: requests.Session | None = None):
        """The publisher for the Instagram account linked to the Page, or None when there isn't one
        (or the token can't see it)."""
        session = session or requests.Session()
        try:
            response = session.get(f"https://graph.facebook.com/{api_version}/{page_id}",
                                   params={"fields": "instagram_business_account"},
                                   headers={"Authorization": f"Bearer {token}"}, timeout=(8, 20))
            account = (response.json() if response.ok else {}).get("instagram_business_account") or {}
        except (requests.RequestException, ValueError):
            return None
        return cls(str(account["id"]), token, api_version, session) if account.get("id") else None

    def publish_reel(self, video_path: Path, caption: str) -> str:
        container = self._post(f"{self.ig_user_id}/media", {
            "media_type": "REELS", "upload_type": "resumable", "caption": instagram_caption(caption),
            "share_to_feed": "true",
        })
        creation_id = str(container.get("id") or "")
        upload_uri = container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{self.api_version}/{creation_id}"
        if not creation_id:
            raise InstagramPublishError("Instagram no devolvió el contenedor del reel.")
        try:
            response = self.session.post(upload_uri, data=video_path.read_bytes(), timeout=(8, 180), headers={
                "Authorization": f"OAuth {self.token}", "offset": "0", "file_size": str(video_path.stat().st_size)})
        except requests.RequestException as exc:
            raise InstagramPublishError("Se perdió la conexión subiendo el reel a Instagram.") from exc
        if not response.ok:
            raise InstagramPublishError(f"Instagram rechazó el video (HTTP {response.status_code}).")
        return self._publish(creation_id)

    def publish_image(self, image_url: str, caption: str) -> str:
        container = self._post(f"{self.ig_user_id}/media", {"image_url": image_url, "caption": instagram_caption(caption)})
        creation_id = str(container.get("id") or "")
        if not creation_id:
            raise InstagramPublishError("Instagram no devolvió el contenedor de la foto.")
        return self._publish(creation_id)

    def _publish(self, creation_id: str) -> str:
        """Wait until Instagram finishes processing the container, then publish it."""
        waited = 0.0
        while True:
            status = self._get(creation_id, {"fields": "status_code"}).get("status_code")
            if status in (None, "FINISHED"):
                break
            if status in ("ERROR", "EXPIRED") or waited >= self.max_wait:
                raise InstagramPublishError(f"Instagram no terminó de procesar el contenido ({status}).")
            time.sleep(self.poll_seconds)
            waited += self.poll_seconds
        media_id = str(self._post(f"{self.ig_user_id}/media_publish", {"creation_id": creation_id}).get("id") or "")
        if not media_id:
            raise InstagramPublishError("Instagram no confirmó la publicación.")
        return media_id

    def _post(self, path: str, data: dict) -> dict:
        return self._request("post", path, data=data)

    def _get(self, path: str, params: dict) -> dict:
        return self._request("get", path, params=params)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"https://graph.facebook.com/{self.api_version}/{path}"
        try:
            response = getattr(self.session, method)(url, headers={"Authorization": f"Bearer {self.token}"},
                                                     timeout=(8, 45), **kwargs)
        except requests.RequestException as exc:
            raise InstagramPublishError("No se pudo contactar la API de Instagram.") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not response.ok:
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            raise InstagramPublishError(
                f"Instagram rechazó la solicitud (HTTP {response.status_code}" + (f", código {code}" if code else "") + ").")
        return payload if isinstance(payload, dict) else {}
