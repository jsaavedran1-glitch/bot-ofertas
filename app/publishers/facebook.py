from __future__ import annotations

import json
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
        self.last_photo_id = ""

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
        self.last_photo_id = str(payload.get("id") or "")
        return str(post_id)

    def photo_source(self, photo_id: str) -> str:
        """Public URL (Facebook's CDN, JPEG) of the largest version of an uploaded photo, for Instagram."""
        try:
            response = self.session.get(f"https://graph.facebook.com/{self.api_version}/{photo_id}",
                                        params={"fields": "images"},
                                        headers={"Authorization": f"Bearer {self.page_token}"}, timeout=(8, 20))
            images = (response.json() if response.ok else {}).get("images") or []
        except (requests.RequestException, ValueError) as exc:
            raise FacebookPublishError("No se pudo leer la foto publicada.") from exc
        best = max(images, key=lambda i: i.get("width", 0) * i.get("height", 0), default={})
        if not best.get("source"):
            raise FacebookPublishError("Meta no devolvió la URL de la foto publicada.")
        return str(best["source"])

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
        video_id, finished = self._upload_video(
            "video_reels", video_path, {"video_state": "PUBLISHED", "description": description}
        )
        if not finished.get("success"):
            raise FacebookPublishError("Meta no confirmó la publicación del reel.", ambiguous=True)
        return video_id

    def publish_video_story(self, video_path: Path) -> str:
        video_id, finished = self._upload_video("video_stories", video_path, {})
        if not finished.get("success"):
            raise FacebookPublishError("Meta no confirmó la historia de video.", ambiguous=True)
        return str(finished.get("post_id") or video_id)

    def _upload_video(self, edge: str, video_path: Path, finish_data: dict) -> tuple[str, dict]:
        """Resumable upload shared by reels and video stories: start -> rupload -> finish."""
        if not video_path.is_file():
            raise FacebookPublishError("No existe el video que se intentó publicar.")
        endpoint = f"https://graph.facebook.com/{self.api_version}/{self.page_id}/{edge}"
        auth = {"Authorization": f"Bearer {self.page_token}"}
        started = self._call(endpoint, headers=auth, data={"upload_phase": "start"})
        video_id = str(started.get("video_id") or "")
        if not video_id:
            raise FacebookPublishError("Meta no devolvió video_id al iniciar la subida del video.")
        self._call(
            f"https://rupload.facebook.com/video-upload/{self.api_version}/{video_id}",
            headers={"Authorization": f"OAuth {self.page_token}", "offset": "0", "file_size": str(video_path.stat().st_size)},
            data=video_path.read_bytes(),
            timeout=(8, 120),
        )
        # Only the finish call can leave a published video behind, so only it is ambiguous on timeout.
        finished = self._call(
            endpoint, headers=auth, data={"upload_phase": "finish", "video_id": video_id, **finish_data}, ambiguous=True,
        )
        return video_id, finished

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

    def publish_carousel(self, image_paths: list[Path], message: str) -> str:
        """Multi-photo feed post: upload each image unpublished, then attach them all to one post."""
        auth = {"Authorization": f"Bearer {self.page_token}"}
        base = f"https://graph.facebook.com/{self.api_version}/{self.page_id}"
        media_ids = []
        for path in image_paths:
            try:
                with path.open("rb") as handle:
                    response = self.session.post(
                        f"{base}/photos", headers=auth, data={"published": "false"},
                        files={"source": (path.name, handle, "image/png")}, timeout=(8, 45),
                    )
            except requests.RequestException as exc:
                raise FacebookPublishError("No se pudo subir una imagen del carrusel.") from exc
            photo_id = str((response.json() if response.ok else {}).get("id") or "")
            if not photo_id:
                raise FacebookPublishError(f"Meta rechazó una imagen del carrusel (HTTP {response.status_code}).")
            media_ids.append(photo_id)
        data = {"message": message, "published": "true"}
        data.update({f"attached_media[{i}]": json.dumps({"media_fbid": m}) for i, m in enumerate(media_ids)})
        post = self._call(f"{base}/feed", headers=auth, data=data, ambiguous=True)
        post_id = str(post.get("id") or "")
        if not post_id:
            raise FacebookPublishError("Meta respondió sin identificador del carrusel.", ambiguous=True)
        return post_id
