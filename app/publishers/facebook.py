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

    def comment(self, post_id: str, message: str) -> None:
        url = f"https://graph.facebook.com/{self.api_version}/{post_id}/comments"
        try:
            response = self.session.post(
                url,
                headers={"Authorization": f"Bearer {self.page_token}"},
                data={"message": message},
                timeout=(8, 30),
            )
        except requests.RequestException as exc:
            raise FacebookPublishError("No se pudo publicar el comentario con el enlace.") from exc
        if not response.ok:
            raise FacebookPublishError(f"Meta rechazó el comentario (HTTP {response.status_code}).")

    def publish_reel(self, video_path: Path, description: str) -> str:
        if not video_path.is_file():
            raise FacebookPublishError("No existe el video que se intentó publicar.")
        endpoint = f"https://graph.facebook.com/{self.api_version}/{self.page_id}/video_reels"
        auth = {"Authorization": f"Bearer {self.page_token}"}
        started = self._call(endpoint, headers=auth, data={"upload_phase": "start"})
        video_id = str(started.get("video_id") or "")
        if not video_id:
            raise FacebookPublishError("Meta no devolvió video_id al iniciar el reel.")
        size = video_path.stat().st_size
        self._call(
            f"https://rupload.facebook.com/video-upload/{self.api_version}/{video_id}",
            headers={"Authorization": f"OAuth {self.page_token}", "offset": "0", "file_size": str(size)},
            data=video_path.read_bytes(),
            timeout=(8, 120),
        )
        # Only the finish call can leave a published reel behind, so only it is ambiguous on timeout.
        finished = self._call(
            endpoint,
            headers=auth,
            data={"upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED", "description": description},
            ambiguous=True,
        )
        if not finished.get("success"):
            raise FacebookPublishError("Meta no confirmó la publicación del reel.", ambiguous=True)
        return video_id

    def _call(self, url: str, headers: dict, data, timeout=(8, 45), ambiguous: bool = False) -> dict:
        try:
            response = self.session.post(url, headers=headers, data=data, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise FacebookPublishError(
                "Se perdió la conexión con Meta durante el reel" + ("; requiere conciliación manual." if ambiguous else "."),
                ambiguous=ambiguous,
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
            raise FacebookPublishError(
                f"Meta rechazó el reel (HTTP {response.status_code}" + (f", código {code}" if code is not None else "") + ").",
                ambiguous=ambiguous and response.status_code >= 500,
            )
        return payload if isinstance(payload, dict) else {}

    def publish_story(self, image_path: Path) -> str:
        """Page photo story: upload the image unpublished, then publish it as a story."""
        auth = {"Authorization": f"Bearer {self.page_token}"}
        base = f"https://graph.facebook.com/{self.api_version}/{self.page_id}"
        try:
            with image_path.open("rb") as handle:
                response = self.session.post(
                    f"{base}/photos", headers=auth, data={"published": "false"},
                    files={"source": (image_path.name, handle, "image/png")}, timeout=(8, 45),
                )
        except requests.RequestException as exc:
            raise FacebookPublishError("No se pudo subir la imagen de la historia.") from exc
        photo_id = str((response.json() if response.ok else {}).get("id") or "")
        if not photo_id:
            raise FacebookPublishError(f"Meta rechazó la imagen de la historia (HTTP {response.status_code}).")
        story = self._call(f"{base}/photo_stories", headers=auth, data={"photo_id": photo_id})
        return str(story.get("post_id") or photo_id)
