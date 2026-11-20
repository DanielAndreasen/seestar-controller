"""Coordinate math for the catalog package — J2000 only, stdlib only.

Pure-Python, no GTK, no :mod:`seestar_controller.link` imports (module boundary
per ``.scratch/catalog/spec.md``).

Conventions (the one place they are defined):

- Every coordinate in this package is **J2000** (SIMBAD serves ICRS, which
  coincides with J2000 at the precision we need) and is stored **in degrees**.
- The canonical type is :class:`J2000Coord` with ``ra_deg`` in ``[0, 360)``
  (RA wraps at 0/360) and ``dec_deg`` in ``[-90, 90]``. ``ra_hours`` is the
  derived view in ``[0, 24)`` — the unit :meth:`link.Device.goto` takes.
- String parsing (:func:`parse_ra`, :func:`parse_dec`, :func:`parse_coords`,
  :meth:`J2000Coord.parse`) accepts:

  - sexagesimal RA ``'12h34m56.7s'`` / ``'12:34:56.7'`` / ``'12 34 56.7'``
    (hours/minutes/seconds; minutes and seconds must be < 60; RA carries no
    sign — it wraps at 24 h instead),
  - sexagesimal Dec with an optional leading sign:
    ``'-05d23m28s'`` / ``'-05:23:28'`` / ``'-5 23 28'`` / ``'-05°23′28″'``
    (degrees/arcminutes/arcseconds),
  - decimal RA in **hours** (``'5.5881'`` — the same unit ``goto`` takes;
    values wrap modulo 24 h) and decimal Dec in **degrees** (``'-5.39114'``).

  Invalid input raises :class:`CoordinateError` (a :class:`ValueError`) whose
  message names the offending value.

- :func:`angular_distance` is the great-circle (haversine) distance between two
  positions in degrees; it is correct across the RA 0/360 wraparound.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

#: Degrees of RA per hour of sidereal time.
DEG_PER_HOUR = 15.0

#: Hours of RA per full circle.
HOURS_PER_CIRCLE = 24.0


class CoordinateError(ValueError):
    """Invalid coordinate input — a :class:`ValueError` naming the value."""


# --- string parsing -----------------------------------------------------------

#: A plain decimal number (optional sign, optional fractional part).
_DECIMAL_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")

#: Sexagesimal with unit letters: ``12h34m56.7s`` (RA) / ``-05d23m28s`` (Dec).
#: The seconds part is only allowed after minutes; the first unit is required.
_SEXAGESIMAL_UNITS_TEMPLATE = (
    r"(?P<sign>[+-]?)\s*(?P<p0>\d+(?:\.\d+)?)\s*{first}"
    r"(?:\s*(?P<p1>\d+(?:\.\d+)?)\s*m(?:\s*(?P<p2>\d+(?:\.\d+)?)\s*s)?)?"
)
_RA_UNITS_RE = re.compile(_SEXAGESIMAL_UNITS_TEMPLATE.format(first=r"h"), re.IGNORECASE)
_DEC_UNITS_RE = re.compile(
    _SEXAGESIMAL_UNITS_TEMPLATE.format(first=r"(?:d|°|deg)"), re.IGNORECASE
)

#: One sexagesimal component: a number, optionally signed (sign only allowed on
#: the first component).
_COMPONENT_RE = re.compile(r"(?P<sign>[+-]?)(?P<num>\d+(?:\.\d*)?|\.\d+)")


def _components_from_split(
    parts: list[str], *, text: str, kind: str, allow_sign: bool
) -> tuple[float, list[float]]:
    """Validate colon/space-separated components; return (sign, [p0, p1, ...])."""
    if not 2 <= len(parts) <= 3:
        raise CoordinateError(
            f"invalid {kind} {text!r}: expected 2 or 3 components, got {len(parts)}"
        )
    sign = 1.0
    values: list[float] = []
    for i, part in enumerate(parts):
        match = _COMPONENT_RE.fullmatch(part.strip())
        if match is None:
            raise CoordinateError(f"invalid {kind} {text!r}: bad component {part!r}")
        if i > 0 and match.group("sign"):
            raise CoordinateError(
                f"invalid {kind} {text!r}: sign is only allowed on the first component ({part!r})"
            )
        if i == 0 and match.group("sign"):
            if not allow_sign:
                raise CoordinateError(_no_sign_message(kind, text))
            if match.group("sign") == "-":
                sign = -1.0
        values.append(float(match.group("num")))
    return sign, values


def _no_sign_message(kind: str, text: Any) -> str:
    return (
        f"invalid {kind} {text!r}: {kind} carries no sign (RA wraps at 24 h); "
        "use Dec for the sign"
    )


def _sexagesimal_value(sign: float, values: list[float], *, text: str, kind: str) -> float:
    """Combine [h/d, m, s] into the first-unit value; minutes/seconds must be < 60."""
    for v in values[1:]:
        if not 0.0 <= v < 60.0:
            raise CoordinateError(
                f"invalid {kind} {text!r}: minutes/seconds must be in [0, 60), got {v}"
            )
    value = values[0]
    if len(values) > 1:
        value += values[1] / 60.0
    if len(values) > 2:
        value += values[2] / 3600.0
    return sign * value


def _parse_angle(
    text: Any, *, kind: str, units_re: re.Pattern[str], unit_name: str, allow_sign: bool
) -> float:
    """Parse one angle string into its first-unit value (RA hours / Dec degrees).

    Shared by :func:`parse_ra` and :func:`parse_dec`; ``kind`` is ``'RA'`` or
    ``'Dec'`` and is used in error messages. ``allow_sign`` is False for RA
    (which wraps instead of carrying a sign) and True for Dec.
    """
    if isinstance(text, bool) or not isinstance(text, (str, int, float)):
        raise CoordinateError(f"invalid {kind}: expected a string or number, got {text!r}")
    if isinstance(text, (int, float)):
        # Numeric input is decimal: RA in hours, Dec in degrees.
        value = float(text)
        if not allow_sign and value < 0:
            raise CoordinateError(_no_sign_message(kind, text))
        return value
    raw = text
    s = text.strip()
    if not s:
        raise CoordinateError(f"invalid {kind}: empty value {raw!r}")
    # Unicode prime marks are just the m/s unit letters.
    s = s.replace("′", "m").replace("″", "s")

    match = units_re.fullmatch(s)
    if match is not None:
        sign_str = match.group("sign")
        if sign_str and not allow_sign:
            raise CoordinateError(_no_sign_message(kind, raw))
        values = [float(match.group("p0"))]
        if match.group("p1") is not None:
            values.append(float(match.group("p1")))
        if match.group("p2") is not None:
            values.append(float(match.group("p2")))
        sign = -1.0 if sign_str == "-" else 1.0
        return _sexagesimal_value(sign, values, text=raw, kind=kind)

    if ":" in s:
        sign, values = _components_from_split(
            s.split(":"), text=raw, kind=kind, allow_sign=allow_sign
        )
        return _sexagesimal_value(sign, values, text=raw, kind=kind)

    tokens = s.split()
    if len(tokens) > 1:
        sign, values = _components_from_split(
            tokens, text=raw, kind=kind, allow_sign=allow_sign
        )
        return _sexagesimal_value(sign, values, text=raw, kind=kind)

    if _DECIMAL_RE.fullmatch(s) is not None:
        if not allow_sign and s.startswith(("+", "-")):
            raise CoordinateError(_no_sign_message(kind, raw))
        return float(s)

    raise CoordinateError(
        f"invalid {kind} {raw!r}: not a decimal number or sexagesimal "
        f"({unit_name} separated by 'h'/'m'/'s', ':' or spaces)"
    )


def parse_ra(text: Any) -> float:
    """Parse an RA string into degrees in ``[0, 360)``.

    Accepts sexagesimal hours (``'12h34m56.7s'``, ``'12:34:56.7'``,
    ``'12 34 56.7'``) and decimal **hours** (``'12.5824'``, or a number —
    hours, matching :meth:`link.Device.goto`). RA wraps modulo 24 h and carries
    no sign. Raises :class:`CoordinateError` naming the offending value.
    """
    hours = _parse_angle(
        text, kind="RA", units_re=_RA_UNITS_RE, unit_name="h/m/s", allow_sign=False
    )
    return (hours % HOURS_PER_CIRCLE) * DEG_PER_HOUR


def parse_dec(text: Any) -> float:
    """Parse a Dec string into degrees in ``[-90, 90]``.

    Accepts sexagesimal degrees with an optional leading sign
    (``'-05d23m28s'``, ``'-05:23:28'``, ``'-5 23 28'``, ``'-05°23m28s'``) and
    decimal **degrees** (``'-5.39114'``, or a number). Raises
    :class:`CoordinateError` naming the offending value.
    """
    dec = _parse_angle(
        text, kind="Dec", units_re=_DEC_UNITS_RE, unit_name="d/m/s", allow_sign=True
    )
    if not -90.0 <= dec <= 90.0:
        raise CoordinateError(f"invalid Dec {text!r}: {dec} deg is outside [-90, 90]")
    return dec


def parse_coords(ra: Any, dec: Any) -> J2000Coord:
    """Parse an ``(ra, dec)`` pair of strings (or numbers) into a :class:`J2000Coord`."""
    return J2000Coord(ra_deg=parse_ra(ra), dec_deg=parse_dec(dec))


# --- the canonical coordinate type --------------------------------------------


@dataclass(frozen=True)
class J2000Coord:
    """A sky position, J2000, in degrees.

    ``ra_deg`` is normalized to ``[0, 360)`` (RA wraps at 0/360) and
    ``dec_deg`` must lie in ``[-90, 90]`` — both enforced in ``__post_init__``,
    the single place where raw values become a normalized coordinate. All
    parsing (:func:`parse_ra` / :func:`parse_dec` / :meth:`parse`) funnels
    through this type.
    """

    ra_deg: float
    dec_deg: float

    def __post_init__(self) -> None:
        try:
            ra = float(self.ra_deg)
            dec = float(self.dec_deg)
        except (TypeError, ValueError):
            raise CoordinateError(
                f"coordinates must be numbers, got ra={self.ra_deg!r} dec={self.dec_deg!r}"
            ) from None
        if not math.isfinite(ra) or not math.isfinite(dec):
            raise CoordinateError(
                f"coordinates must be finite, got ra={self.ra_deg!r} dec={self.dec_deg!r}"
            )
        if not -90.0 <= dec <= 90.0:
            raise CoordinateError(f"dec must be in [-90, 90] deg, got {dec!r}")
        object.__setattr__(self, "ra_deg", ra % 360.0)
        object.__setattr__(self, "dec_deg", dec)

    @classmethod
    def from_degrees(cls, ra_deg: float, dec_deg: float) -> J2000Coord:
        """Build from RA/Dec in degrees; RA wraps into ``[0, 360)``."""
        return cls(ra_deg=ra_deg, dec_deg=dec_deg)

    @classmethod
    def from_hours(cls, ra_hours: float, dec_deg: float) -> J2000Coord:
        """Build from RA in hours (the ``goto`` unit) and Dec in degrees."""
        return cls(ra_deg=float(ra_hours) * DEG_PER_HOUR, dec_deg=dec_deg)

    @classmethod
    def parse(cls, ra: Any, dec: Any) -> J2000Coord:
        """Parse an ``(ra, dec)`` pair of strings (or numbers); see :func:`parse_ra`."""
        return parse_coords(ra, dec)

    @property
    def ra_hours(self) -> float:
        """Right ascension in hours, ``[0, 24)`` — the unit ``link.Device.goto`` takes."""
        return self.ra_deg / DEG_PER_HOUR

    def as_degrees(self) -> tuple[float, float]:
        """``(ra_deg, dec_deg)`` — J2000 coordinates in degrees."""
        return self.ra_deg, self.dec_deg

    def separation(self, other: Any) -> float:
        """Great-circle distance to ``other`` in degrees; see :func:`angular_distance`."""
        return angular_distance(self, other)

    def __str__(self) -> str:
        return f"RA {self.ra_hours:.6f}h Dec {self.dec_deg:+.6f}deg"


# --- angular distance ----------------------------------------------------------


def _ra_dec(pos: Any) -> tuple[float, float]:
    """Extract ``(ra_deg, dec_deg)`` from a coord-like value.

    Accepts a :class:`J2000Coord`, any object with ``ra_deg``/``dec_deg``
    attributes (e.g. :class:`~.simbad.CatalogCandidate`), or a
    ``(ra_deg, dec_deg)`` pair.
    """
    if isinstance(pos, J2000Coord):
        return pos.ra_deg, pos.dec_deg
    if isinstance(pos, (tuple, list)) and len(pos) == 2:
        try:
            return float(pos[0]) % 360.0, float(pos[1])
        except (TypeError, ValueError):
            raise CoordinateError(f"invalid (ra, dec) pair: {pos!r}") from None
    ra = getattr(pos, "ra_deg", None)
    dec = getattr(pos, "dec_deg", None)
    if ra is not None and dec is not None:
        return float(ra) % 360.0, float(dec)
    raise CoordinateError(
        f"expected a J2000Coord, a (ra_deg, dec_deg) pair, or an object with "
        f"ra_deg/dec_deg, got {pos!r}"
    )


def angular_distance(a: Any, b: Any) -> float:
    """Great-circle distance between two sky positions, in degrees.

    Haversine on the sphere — numerically stable for small angles and correct
    across the RA 0/360 wraparound (``sin²(Δra/2)`` is invariant under
    ``Δra → Δra ± 360°``). Each argument is anything :func:`_ra_dec` accepts:
    a :class:`J2000Coord`, a ``(ra_deg, dec_deg)`` pair, or an object with
    ``ra_deg``/``dec_deg`` attributes (e.g. a SIMBAD candidate).
    """
    ra1, dec1 = _ra_dec(a)
    ra2, dec2 = _ra_dec(b)
    phi1 = math.radians(dec1)
    phi2 = math.radians(dec2)
    d_phi = math.radians(dec2 - dec1)
    d_lambda = math.radians(ra2 - ra1)
    h = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    # Clamp against floating-point drift outside [0, 1] before asin/sqrt.
    h = min(1.0, max(0.0, h))
    return math.degrees(2.0 * math.asin(math.sqrt(h)))
