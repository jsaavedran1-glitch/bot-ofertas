"""Sales events (Black Friday, Cyber Monday): countdown posts before, and a different tag, caption opener,
hashtags and more posts per hour during the event days. Dates are Bogotá days."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from app.image_renderer import POST_CANVAS, WHITE, YELLOW, OfferImageRenderer

BOGOTA = ZoneInfo("America/Bogota")
DEFAULT_TAG = "OFERTA DEL DÍA"
INK = "#0B0B10"  # Black Friday cards are black, with the brand yellow


@dataclass(frozen=True)
class SalesEvent:
    key: str
    name: str
    start: date
    end: date  # inclusive
    hashtags: tuple[str, ...]
    countdown_days: int = 0  # daily "faltan N días" posts before `start`

    @property
    def tag(self) -> str:
        return self.name.upper()


EVENTS = (
    SalesEvent("black-friday-2026", "Black Friday", date(2026, 11, 27), date(2026, 11, 29),
               ("#BlackFriday", "#BlackFridayColombia"), countdown_days=7),
    SalesEvent("cyber-monday-2026", "Cyber Monday", date(2026, 11, 30), date(2026, 11, 30),
               ("#CyberMonday", "#CyberMondayColombia")),
)

_DAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
           "noviembre", "diciembre")


def today_bogota() -> date:
    return datetime.now(BOGOTA).date()


def active_event(day: date | None = None) -> SalesEvent | None:
    day = day or today_bogota()
    return next((e for e in EVENTS if e.start <= day <= e.end), None)


def event_tag(day: date | None = None) -> str:
    event = active_event(day)
    return event.tag if event else DEFAULT_TAG


def countdown(day: date | None = None) -> tuple[SalesEvent, int] | None:
    """(event, days left) on countdown days and on the event's first day (0 = "hoy es")."""
    day = day or today_bogota()
    for e in EVENTS:
        left = (e.start - day).days
        if 0 <= left <= e.countdown_days and (left > 0 or e.start == day):
            return e, left
    return None


def long_date(day: date) -> str:
    return f"{_DAYS[day.weekday()]} {day.day} de {_MONTHS[day.month - 1]}"


def countdown_caption(event: SalesEvent, left: int, stores: str) -> str:
    tags = " ".join(("#OjoAlPrecio",) + event.hashtags)
    if left == 0:
        return (f"🖤 ¡Hoy es {event.name}!\n\n"
                f"Todo el día vamos a publicar las ofertas de {stores}, cada una con el precio de antes y el de "
                f"hoy. Si es el precio más bajo de los últimos 30 días, te lo decimos.\n\n"
                f"🔔 Activa las notificaciones de la página para verlas apenas salgan.\n"
                f"💬 ¿Qué vas a comprar hoy?\n\n{tags}")
    days = "día" if left == 1 else "días"
    return (f"🖤 Faltan {left} {days} para el {event.name} ({long_date(event.start)}).\n\n"
            f"Ese día publicamos las ofertas de {stores} con el precio de antes y el de hoy, y te avisamos "
            f"cuando es el más bajo de los últimos 30 días, para que no te vendan un descuento inflado.\n\n"
            f"🔔 Sigue la página y activa las notificaciones.\n"
            f"💬 ¿Qué estás esperando para comprar en {event.name}?\n\n{tags}")


class CountdownRenderer(OfferImageRenderer):
    def render_countdown(self, event: SalesEvent, left: int, name: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        W, H = POST_CANVAS
        canvas = Image.new("RGB", POST_CANVAS, INK)
        draw = ImageDraw.Draw(canvas)
        for y in range(H):  # a faint yellow glow behind the number
            glow = max(0.0, 1 - abs(y - 520) / 520) ** 2
            draw.line((0, y, W, y), fill=(11 + int(28 * glow), 11 + int(22 * glow), 16))
        self._brand(canvas, draw, 52, 92, size=72)

        if left == 0:
            draw.text((W // 2, 330), "HOY ES", anchor="mm", font=self._font(96, weight="Black"), fill=WHITE)
            title_font = self._fit_font(draw, event.tag, W - 104, 170, 90, weight="Black")
            draw.text((W // 2, 520), event.tag, anchor="mm", font=title_font, fill=YELLOW)
            sub = long_date(event.start).capitalize()
            draw.text((W // 2, 660), sub, anchor="mm", font=self._font(44, weight="Bold"), fill=WHITE)
        else:
            draw.text((W // 2, 230), "FALTAN", anchor="mm", font=self._font(64, weight="Black"), fill=WHITE)
            draw.text((W // 2, 450), str(left), anchor="mm", font=self._font(360, weight="Black"), fill=YELLOW)
            unit = "DÍA PARA EL" if left == 1 else "DÍAS PARA EL"
            draw.text((W // 2, 660), unit, anchor="mm", font=self._font(54, weight="Black"), fill=WHITE)
            title_font = self._fit_font(draw, event.tag, W - 104, 120, 70, weight="Black")
            draw.text((W // 2, 760), event.tag, anchor="mm", font=title_font, fill=YELLOW)

        points = ("Solo descuentos verificados", "Te avisamos si es el precio más bajo en 30 días",
                  "Activa las notificaciones")
        y = 860
        for text in points:
            draw.rounded_rectangle((52, y, W - 52, y + 86), radius=43, fill="#1A1A22")
            self._paste_emoji(canvas, (78, y + 21), "🔔" if text.startswith("Activa") else "✅", 44)
            font = self._fit_font(draw, text, W - 52 - 150 - 30, 36, 26, weight="Bold")
            draw.text((150, y + 43), text, anchor="lm", font=font, fill=WHITE)
            y += 100
        self._footer(canvas, draw, W, H)
        output = self.output_dir / f"{name}.png"
        canvas.save(output, format="PNG", optimize=True)
        return output


__all__ = ["EVENTS", "CountdownRenderer", "SalesEvent", "active_event", "countdown", "countdown_caption",
           "event_tag", "long_date"]
