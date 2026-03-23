#!/usr/bin/env python3
"""Generate an A4 color-by-number PDF from a pure vector SVG.

The output PDF contains:
1) A black-and-white drawing (no original fill colors in the main artwork).
2) A number placed inside each detected colorable region.
3) A bottom legend that maps each number to the original exact color.

Notes:
- Pure black is always present in the legend.
- Pure black labels are omitted only when a zone requires fallback placement.
- Colors are sorted by HSV hue before assigning reference numbers.
- Color references use one-character labels (1-9, then A-Z).
"""

from __future__ import annotations

import argparse
import colorsys
import math
import re
import sys
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import polylabel
from svgpathtools import Arc, CubicBezier, Line, Path as SvgPath, QuadraticBezier, parse_path

try:
    from shapely.validation import make_valid as shapely_make_valid
except Exception:  # pragma: no cover - fallback for older Shapely
    shapely_make_valid = None


DRAWABLE_TAGS = {
    "path",
    "rect",
    "circle",
    "ellipse",
    "polygon",
    "polyline",
    "line",
}

STYLE_KEYS = {
    "fill",
    "stroke",
    "opacity",
    "fill-opacity",
    "stroke-opacity",
    "stroke-width",
    "fill-rule",
    "display",
    "visibility",
}

NUMBER_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
HEX_RE = re.compile(r"^#([0-9a-fA-F]{3,8})$")
RGB_RE = re.compile(r"^rgba?\((.+)\)$", re.IGNORECASE)

FONT_NAME = "Montserrat"
EXCLUDED_COLOR_HEX = "#000000"
DEFAULT_OUTLINE_GRAY = 0.68
DEFAULT_NUMBER_GRAY = 0.72
LABEL_PADDING_PDF = 0.8
LABEL_COLLISION_GAP_PDF = 0.6
DEFAULT_FONT_PATH = Path(__file__).resolve().parent / "fonts" / "Montserrat-Regular.ttf"
REFERENCE_SYMBOLS = "123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class SvgToPdfError(Exception):
    """Domain error for conversion failures."""


@dataclass
class SvgShape:
    """Parsed SVG shape with style information."""

    path: SvgPath
    fill_color: Optional[str]
    stroke_color: Optional[str]
    stroke_width: float
    fill_rule: str


@dataclass
class ColorZone:
    """Single region that should receive a number."""

    color_hex: str
    geometry: Polygon


@dataclass
class LayoutTransform:
    """Coordinate mapping from SVG space to PDF space."""

    svg_min_x: float
    svg_min_y: float
    svg_height: float
    scale: float
    draw_x: float
    draw_y: float
    offset_x: float
    offset_y: float
    scaled_height: float

    def map_xy(self, x: float, y: float) -> Tuple[float, float]:
        px = self.draw_x + self.offset_x + (x - self.svg_min_x) * self.scale
        local_y = (y - self.svg_min_y) * self.scale
        py = self.draw_y + self.offset_y + (self.scaled_height - local_y)
        return px, py


@dataclass
class LabelPlacement:
    """Resolved label placement guaranteed to fit its region."""

    point: Point
    font_size: float
    text_width_pdf: float
    ascent_pdf: float
    descent_pdf: float
    center_pdf_x: float
    center_pdf_y: float
    box_pdf: Tuple[float, float, float, float]
    used_fallback: bool


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def parse_style_attribute(style_attr: Optional[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not style_attr:
        return out
    for chunk in style_attr.split(";"):
        if ":" not in chunk:
            continue
        key, value = chunk.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key:
            out[key] = value
    return out


def parse_float(value: Optional[str], default: float = 0.0) -> float:
    if value is None:
        return default
    text = value.strip()
    if not text:
        return default
    try:
        if text.endswith("%"):
            return float(text[:-1]) / 100.0
        return float(text)
    except ValueError:
        return default


def parse_svg_length(value: Optional[str], default: float = 0.0) -> float:
    if value is None:
        return default
    text = value.strip()
    if not text:
        return default
    match = re.match(r"^\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)([a-zA-Z%]*)\s*$", text)
    if not match:
        return default
    number = float(match.group(1))
    unit = match.group(2).lower()
    if unit in {"", "px"}:
        return number
    if unit == "pt":
        return number
    if unit == "mm":
        return number * 72.0 / 25.4
    if unit == "cm":
        return number * 72.0 / 2.54
    if unit == "in":
        return number * 72.0
    return number


def register_montserrat_font(font_path: Path) -> None:
    if FONT_NAME in pdfmetrics.getRegisteredFontNames():
        return
    if not font_path.exists():
        raise SvgToPdfError(
            f"No se encontro la fuente requerida en {font_path}. "
            "Descarga Montserrat-Regular.ttf y colocala en fonts/."
        )

    try:
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(font_path)))
    except Exception as exc:
        raise SvgToPdfError(f"No se pudo registrar la fuente Montserrat: {exc}") from exc


def merge_style(parent: Dict[str, str], elem: ET.Element) -> Dict[str, str]:
    style = dict(parent)
    style.update(parse_style_attribute(elem.get("style")))
    for key in STYLE_KEYS:
        raw = elem.get(key)
        if raw is not None:
            style[key] = raw.strip()
    return style


def parse_css_number(value: str) -> float:
    value = value.strip()
    if value.endswith("%"):
        return max(0.0, min(1.0, float(value[:-1]) / 100.0))
    return float(value)


def parse_channel_value(token: str) -> int:
    token = token.strip()
    if token.endswith("%"):
        ratio = float(token[:-1]) / 100.0
        return max(0, min(255, int(round(ratio * 255.0))))
    value = float(token)
    return max(0, min(255, int(round(value))))


NAMED_COLORS: Dict[str, Tuple[int, int, int]] = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "green": (0, 128, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
}


def parse_color_value(value: Optional[str]) -> Optional[Tuple[int, int, int, float]]:
    if value is None:
        return None
    text = value.strip().lower()
    if not text or text in {"none", "transparent"}:
        return None
    if text.startswith("url("):
        return None

    if text in NAMED_COLORS:
        r, g, b = NAMED_COLORS[text]
        return r, g, b, 1.0

    hex_match = HEX_RE.match(text)
    if hex_match:
        payload = hex_match.group(1)
        if len(payload) == 3:
            r = int(payload[0] * 2, 16)
            g = int(payload[1] * 2, 16)
            b = int(payload[2] * 2, 16)
            return r, g, b, 1.0
        if len(payload) == 4:
            r = int(payload[0] * 2, 16)
            g = int(payload[1] * 2, 16)
            b = int(payload[2] * 2, 16)
            a = int(payload[3] * 2, 16) / 255.0
            return r, g, b, a
        if len(payload) == 6:
            r = int(payload[0:2], 16)
            g = int(payload[2:4], 16)
            b = int(payload[4:6], 16)
            return r, g, b, 1.0
        if len(payload) == 8:
            r = int(payload[0:2], 16)
            g = int(payload[2:4], 16)
            b = int(payload[4:6], 16)
            a = int(payload[6:8], 16) / 255.0
            return r, g, b, a

    rgb_match = RGB_RE.match(text)
    if rgb_match:
        body = rgb_match.group(1).replace("/", ",")
        parts = [p.strip() for p in re.split(r"[\s,]+", body) if p.strip()]
        if len(parts) not in {3, 4}:
            return None
        try:
            r = parse_channel_value(parts[0])
            g = parse_channel_value(parts[1])
            b = parse_channel_value(parts[2])
            a = 1.0
            if len(parts) == 4:
                a = parse_css_number(parts[3])
            return r, g, b, max(0.0, min(1.0, a))
        except ValueError:
            return None

    return None


def rgb_to_hex(rgb: Tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(rgb[0], rgb[1], rgb[2])


def resolve_color(style: Dict[str, str], channel: str) -> Optional[str]:
    raw = style.get(channel)
    rgba = parse_color_value(raw)
    if rgba is None:
        return None

    base_alpha = rgba[3]
    total_opacity = parse_float(style.get("opacity"), 1.0)
    if channel == "fill":
        channel_opacity = parse_float(style.get("fill-opacity"), 1.0)
    else:
        channel_opacity = parse_float(style.get("stroke-opacity"), 1.0)

    alpha = base_alpha * total_opacity * channel_opacity
    if alpha <= 0.01:
        return None

    return rgb_to_hex((rgba[0], rgba[1], rgba[2]))


def parse_points_list(points_text: Optional[str]) -> List[Tuple[float, float]]:
    if not points_text:
        return []
    numbers = [float(n) for n in NUMBER_RE.findall(points_text)]
    if len(numbers) < 2:
        return []
    if len(numbers) % 2 == 1:
        numbers = numbers[:-1]
    return [(numbers[i], numbers[i + 1]) for i in range(0, len(numbers), 2)]


def build_rect_path(elem: ET.Element) -> Optional[str]:
    x = parse_float(elem.get("x"), 0.0)
    y = parse_float(elem.get("y"), 0.0)
    width = parse_float(elem.get("width"), 0.0)
    height = parse_float(elem.get("height"), 0.0)
    if width <= 0 or height <= 0:
        return None

    rx = parse_float(elem.get("rx"), 0.0)
    ry = parse_float(elem.get("ry"), 0.0)
    if rx <= 0 and ry <= 0:
        return f"M {x} {y} L {x + width} {y} L {x + width} {y + height} L {x} {y + height} Z"

    if rx <= 0:
        rx = ry
    if ry <= 0:
        ry = rx
    rx = min(rx, width / 2.0)
    ry = min(ry, height / 2.0)

    return (
        f"M {x + rx} {y} "
        f"L {x + width - rx} {y} "
        f"A {rx} {ry} 0 0 1 {x + width} {y + ry} "
        f"L {x + width} {y + height - ry} "
        f"A {rx} {ry} 0 0 1 {x + width - rx} {y + height} "
        f"L {x + rx} {y + height} "
        f"A {rx} {ry} 0 0 1 {x} {y + height - ry} "
        f"L {x} {y + ry} "
        f"A {rx} {ry} 0 0 1 {x + rx} {y} Z"
    )


def build_circle_path(elem: ET.Element) -> Optional[str]:
    cx = parse_float(elem.get("cx"), 0.0)
    cy = parse_float(elem.get("cy"), 0.0)
    r = parse_float(elem.get("r"), 0.0)
    if r <= 0:
        return None
    return (
        f"M {cx - r} {cy} "
        f"A {r} {r} 0 1 0 {cx + r} {cy} "
        f"A {r} {r} 0 1 0 {cx - r} {cy} Z"
    )


def build_ellipse_path(elem: ET.Element) -> Optional[str]:
    cx = parse_float(elem.get("cx"), 0.0)
    cy = parse_float(elem.get("cy"), 0.0)
    rx = parse_float(elem.get("rx"), 0.0)
    ry = parse_float(elem.get("ry"), 0.0)
    if rx <= 0 or ry <= 0:
        return None
    return (
        f"M {cx - rx} {cy} "
        f"A {rx} {ry} 0 1 0 {cx + rx} {cy} "
        f"A {rx} {ry} 0 1 0 {cx - rx} {cy} Z"
    )


def build_polygon_path(elem: ET.Element, close: bool) -> Optional[str]:
    points = parse_points_list(elem.get("points"))
    if len(points) < 2:
        return None
    first = points[0]
    parts = [f"M {first[0]} {first[1]}"]
    for x, y in points[1:]:
        parts.append(f"L {x} {y}")
    if close:
        parts.append("Z")
    return " ".join(parts)


def build_line_path(elem: ET.Element) -> Optional[str]:
    x1 = parse_float(elem.get("x1"), 0.0)
    y1 = parse_float(elem.get("y1"), 0.0)
    x2 = parse_float(elem.get("x2"), 0.0)
    y2 = parse_float(elem.get("y2"), 0.0)
    if math.isclose(x1, x2) and math.isclose(y1, y2):
        return None
    return f"M {x1} {y1} L {x2} {y2}"


def element_to_path_data(elem: ET.Element, tag: str) -> Optional[str]:
    if tag == "path":
        return elem.get("d")
    if tag == "rect":
        return build_rect_path(elem)
    if tag == "circle":
        return build_circle_path(elem)
    if tag == "ellipse":
        return build_ellipse_path(elem)
    if tag == "polygon":
        return build_polygon_path(elem, close=True)
    if tag == "polyline":
        return build_polygon_path(elem, close=False)
    if tag == "line":
        return build_line_path(elem)
    return None


def collect_svg_shapes(root: ET.Element) -> List[SvgShape]:
    shapes: List[SvgShape] = []

    def walk(elem: ET.Element, parent_style: Dict[str, str]) -> None:
        style = merge_style(parent_style, elem)
        display = style.get("display", "").strip().lower()
        visibility = style.get("visibility", "").strip().lower()
        if display == "none" or visibility == "hidden":
            return

        tag = local_name(elem.tag)
        if tag in DRAWABLE_TAGS:
            d = element_to_path_data(elem, tag)
            if d:
                try:
                    path = parse_path(d)
                except Exception:
                    path = None
                if path is not None and len(path) > 0:
                    fill_color = resolve_color(style, "fill")
                    stroke_color = resolve_color(style, "stroke")
                    stroke_width = parse_float(style.get("stroke-width"), 1.0)
                    fill_rule = style.get("fill-rule", "nonzero").strip().lower() or "nonzero"
                    shapes.append(
                        SvgShape(
                            path=path,
                            fill_color=fill_color,
                            stroke_color=stroke_color,
                            stroke_width=stroke_width,
                            fill_rule=fill_rule,
                        )
                    )

        for child in list(elem):
            walk(child, style)

    walk(root, {})
    return shapes


def is_subpath_closed(path: SvgPath) -> bool:
    if len(path) == 0:
        return False
    try:
        if path.isclosed():
            return True
    except Exception:
        pass
    start = path[0].start
    end = path[-1].end
    return abs(start - end) < 1e-6


def sample_segment_points(segment, max_step: float) -> List[complex]:
    try:
        seg_length = max(segment.length(error=1e-4), 1e-6)
    except Exception:
        seg_length = max(abs(segment.end - segment.start), 1e-6)
    samples = max(2, int(math.ceil(seg_length / max_step)) + 1)
    return [segment.point(i / (samples - 1)) for i in range(samples)]


def cleaned_coords(points: Sequence[complex]) -> List[Tuple[float, float]]:
    coords: List[Tuple[float, float]] = []
    for p in points:
        coord = (float(p.real), float(p.imag))
        if not coords or coord != coords[-1]:
            coords.append(coord)
    if len(coords) >= 2 and coords[0] != coords[-1]:
        coords.append(coords[0])
    return coords


def safe_make_valid(geometry):
    if geometry.is_empty:
        return geometry
    if geometry.is_valid:
        return geometry
    if shapely_make_valid is not None:
        try:
            geometry = shapely_make_valid(geometry)
            if geometry.is_valid:
                return geometry
        except Exception:
            pass
    try:
        geometry = geometry.buffer(0)
    except Exception:
        pass
    return geometry


def iter_polygons(geometry) -> Iterator[Polygon]:
    if geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        yield geometry
        return
    if isinstance(geometry, MultiPolygon):
        for poly in geometry.geoms:
            if not poly.is_empty:
                yield poly
        return
    if isinstance(geometry, GeometryCollection):
        for item in geometry.geoms:
            yield from iter_polygons(item)


def path_to_fill_polygons(path: SvgPath, max_step: float, min_area: float) -> List[Polygon]:
    polygons: List[Polygon] = []
    for subpath in path.continuous_subpaths():
        if len(subpath) == 0 or not is_subpath_closed(subpath):
            continue

        sampled: List[complex] = []
        for segment in subpath:
            points = sample_segment_points(segment, max_step=max_step)
            if sampled:
                sampled.extend(points[1:])
            else:
                sampled.extend(points)

        coords = cleaned_coords(sampled)
        if len(coords) < 4:
            continue

        polygon = Polygon(coords)
        polygon = safe_make_valid(polygon)
        for poly in iter_polygons(polygon):
            if poly.area >= min_area:
                polygons.append(poly)

    return polygons


def path_to_stroke_polygons(path: SvgPath, stroke_width: float, max_step: float, min_area: float) -> List[Polygon]:
    if stroke_width <= 0:
        return []
    polygons: List[Polygon] = []
    for subpath in path.continuous_subpaths():
        if len(subpath) == 0:
            continue

        sampled: List[complex] = []
        for segment in subpath:
            points = sample_segment_points(segment, max_step=max_step)
            if sampled:
                sampled.extend(points[1:])
            else:
                sampled.extend(points)

        coords = cleaned_coords(sampled)
        if len(coords) < 2:
            continue
        if len(coords) >= 2 and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) < 2:
            continue

        line = LineString(coords)
        stroke_area = safe_make_valid(
            line.buffer(stroke_width / 2.0, cap_style=1, join_style=1, resolution=8)
        )
        for poly in iter_polygons(stroke_area):
            if poly.area >= min_area:
                polygons.append(poly)
    return polygons


def parse_view_box(root: ET.Element) -> Optional[Tuple[float, float, float, float]]:
    raw = root.get("viewBox")
    if not raw:
        return None
    values = [float(v) for v in NUMBER_RE.findall(raw)]
    if len(values) != 4:
        return None
    min_x, min_y, width, height = values
    if width <= 0 or height <= 0:
        return None
    return min_x, min_y, width, height


def compute_paths_bbox(shapes: Sequence[SvgShape]) -> Optional[Tuple[float, float, float, float]]:
    min_x = math.inf
    max_x = -math.inf
    min_y = math.inf
    max_y = -math.inf
    found = False

    for shape in shapes:
        try:
            sx_min, sx_max, sy_min, sy_max = shape.path.bbox()
        except Exception:
            continue
        min_x = min(min_x, sx_min)
        max_x = max(max_x, sx_max)
        min_y = min(min_y, sy_min)
        max_y = max(max_y, sy_max)
        found = True

    if not found:
        return None
    width = max_x - min_x
    height = max_y - min_y
    if width <= 0 or height <= 0:
        return None
    return min_x, min_y, width, height


def read_svg(svg_path: Path) -> Tuple[List[SvgShape], Tuple[float, float, float, float]]:
    try:
        tree = ET.parse(svg_path)
    except ET.ParseError as exc:
        raise SvgToPdfError(f"No se pudo parsear el SVG: {exc}") from exc

    root = tree.getroot()
    if local_name(root.tag) != "svg":
        raise SvgToPdfError("El archivo no contiene una raiz <svg> valida.")

    shapes = collect_svg_shapes(root)
    if not shapes:
        raise SvgToPdfError("No se encontraron elementos vectoriales compatibles.")

    view_box = parse_view_box(root)
    if view_box is None:
        width = parse_svg_length(root.get("width"), 0.0)
        height = parse_svg_length(root.get("height"), 0.0)
        if width > 0 and height > 0:
            view_box = (0.0, 0.0, width, height)
        else:
            bbox = compute_paths_bbox(shapes)
            if bbox is None:
                raise SvgToPdfError("No fue posible determinar el area del dibujo SVG.")
            view_box = bbox

    return shapes, view_box


def color_sort_key(color_hex: str) -> Tuple[float, float, float, str]:
    r = int(color_hex[1:3], 16) / 255.0
    g = int(color_hex[3:5], 16) / 255.0
    b = int(color_hex[5:7], 16) / 255.0
    h, s, v = colorsys.rgb_to_hsv(r, g, b)

    if s < 1e-3:
        hue_key = 2.0
    else:
        hue_key = h
    return hue_key, s, v, color_hex


def build_color_labels(palette: Sequence[str]) -> Dict[str, str]:
    if len(palette) > len(REFERENCE_SYMBOLS):
        raise SvgToPdfError(
            "Hay mas colores que referencias de un solo caracter disponibles "
            f"({len(palette)} > {len(REFERENCE_SYMBOLS)})."
        )
    return {color_hex: REFERENCE_SYMBOLS[idx] for idx, color_hex in enumerate(palette)}


def color_distance_to_black(color_hex: str) -> int:
    r = int(color_hex[1:3], 16)
    g = int(color_hex[3:5], 16)
    b = int(color_hex[5:7], 16)
    return (r * r) + (g * g) + (b * b)


def normalize_nearest_black(zones: Sequence[ColorZone]) -> List[ColorZone]:
    if not zones:
        return []

    unique_colors = sorted({zone.color_hex for zone in zones})
    if EXCLUDED_COLOR_HEX in unique_colors:
        return list(zones)

    nearest_black = min(unique_colors, key=color_distance_to_black)
    normalized: List[ColorZone] = []
    for zone in zones:
        if zone.color_hex == nearest_black:
            normalized.append(ColorZone(color_hex=EXCLUDED_COLOR_HEX, geometry=zone.geometry))
        else:
            normalized.append(zone)
    return normalized


def legend_text_color_for_background(color_hex: str) -> colors.Color:
    r = int(color_hex[1:3], 16)
    g = int(color_hex[3:5], 16)
    b = int(color_hex[5:7], 16)
    luma = (0.299 * r) + (0.587 * g) + (0.114 * b)
    if luma >= 150.0:
        return colors.black
    return colors.white


def build_zones(
    shapes: Sequence[SvgShape],
    include_strokes: bool,
    max_step: float,
    min_area: float,
) -> List[ColorZone]:
    zones: List[ColorZone] = []

    for shape in shapes:
        if shape.fill_color:
            fill_polys = path_to_fill_polygons(shape.path, max_step=max_step, min_area=min_area)
            for poly in fill_polys:
                zones.append(ColorZone(color_hex=shape.fill_color, geometry=poly))

        if (
            include_strokes
            and (not shape.fill_color)
            and shape.stroke_color
        ):
            stroke_polys = path_to_stroke_polygons(
                shape.path,
                stroke_width=max(shape.stroke_width, 0.01),
                max_step=max_step,
                min_area=min_area,
            )
            for poly in stroke_polys:
                zones.append(ColorZone(color_hex=shape.stroke_color, geometry=poly))

    return zones


def build_layout(
    page_width: float,
    page_height: float,
    view_box: Tuple[float, float, float, float],
    legend_height: float,
) -> LayoutTransform:
    margin = 24.0
    gap = 12.0
    draw_x = margin
    draw_y = margin + legend_height + gap
    draw_w = page_width - (2.0 * margin)
    draw_h = page_height - draw_y - margin
    if draw_w <= 0 or draw_h <= 0:
        raise SvgToPdfError("No hay espacio util para maquetar el dibujo en A4.")

    svg_min_x, svg_min_y, svg_w, svg_h = view_box
    scale = min(draw_w / svg_w, draw_h / svg_h)
    scaled_w = svg_w * scale
    scaled_h = svg_h * scale
    offset_x = (draw_w - scaled_w) / 2.0
    offset_y = (draw_h - scaled_h) / 2.0

    return LayoutTransform(
        svg_min_x=svg_min_x,
        svg_min_y=svg_min_y,
        svg_height=svg_h,
        scale=scale,
        draw_x=draw_x,
        draw_y=draw_y,
        offset_x=offset_x,
        offset_y=offset_y,
        scaled_height=scaled_h,
    )


def quadratic_to_cubic(segment: QuadraticBezier) -> Tuple[complex, complex]:
    start = segment.start
    control = segment.control
    end = segment.end
    c1 = start + (2.0 / 3.0) * (control - start)
    c2 = end + (2.0 / 3.0) * (control - end)
    return c1, c2


def draw_black_outline(
    pdf: canvas.Canvas,
    shape: SvgShape,
    transform: LayoutTransform,
    line_width: float,
    outline_gray: float,
) -> None:
    path_obj = pdf.beginPath()

    for subpath in shape.path.continuous_subpaths():
        if len(subpath) == 0:
            continue

        start = subpath[0].start
        sx, sy = transform.map_xy(start.real, start.imag)
        path_obj.moveTo(sx, sy)

        for segment in subpath:
            if isinstance(segment, Line):
                x, y = transform.map_xy(segment.end.real, segment.end.imag)
                path_obj.lineTo(x, y)
            elif isinstance(segment, CubicBezier):
                c1x, c1y = transform.map_xy(segment.control1.real, segment.control1.imag)
                c2x, c2y = transform.map_xy(segment.control2.real, segment.control2.imag)
                ex, ey = transform.map_xy(segment.end.real, segment.end.imag)
                path_obj.curveTo(c1x, c1y, c2x, c2y, ex, ey)
            elif isinstance(segment, QuadraticBezier):
                c1, c2 = quadratic_to_cubic(segment)
                c1x, c1y = transform.map_xy(c1.real, c1.imag)
                c2x, c2y = transform.map_xy(c2.real, c2.imag)
                ex, ey = transform.map_xy(segment.end.real, segment.end.imag)
                path_obj.curveTo(c1x, c1y, c2x, c2y, ex, ey)
            elif isinstance(segment, Arc):
                sampled = sample_segment_points(segment, max_step=2.0)
                for point in sampled[1:]:
                    x, y = transform.map_xy(point.real, point.imag)
                    path_obj.lineTo(x, y)
            else:
                sampled = sample_segment_points(segment, max_step=2.0)
                for point in sampled[1:]:
                    x, y = transform.map_xy(point.real, point.imag)
                    path_obj.lineTo(x, y)

        if is_subpath_closed(subpath):
            path_obj.close()

    pdf.setLineWidth(line_width)
    pdf.setStrokeGray(outline_gray)
    pdf.drawPath(path_obj, stroke=1, fill=0)


def pick_polygon_for_label(geometry) -> Optional[Polygon]:
    if isinstance(geometry, Polygon):
        return geometry
    candidates = list(iter_polygons(geometry))
    if not candidates:
        return None
    return max(candidates, key=lambda g: g.area)


def interior_point_for_polygon(polygon: Polygon) -> Optional[Point]:
    if polygon.is_empty:
        return None
    try:
        point = polylabel(polygon, tolerance=0.5)
        if point and polygon.contains(point):
            return point
    except Exception:
        pass
    point = polygon.representative_point()
    if point and polygon.contains(point):
        return point
    return None


def candidate_points_for_polygon(polygon: Polygon) -> List[Point]:
    points: List[Point] = []
    seen = set()

    def add_point(point: Optional[Point]) -> None:
        if point is None or point.is_empty:
            return
        if not polygon.contains(point):
            return
        key = (round(point.x, 3), round(point.y, 3))
        if key in seen:
            return
        seen.add(key)
        points.append(point)

    add_point(interior_point_for_polygon(polygon))

    centroid = polygon.centroid
    add_point(centroid)
    add_point(polygon.representative_point())

    min_x, min_y, max_x, max_y = polygon.bounds
    center = Point((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
    add_point(center)

    # First try two horizontal bands to reduce label collisions.
    y_bands = (0.40, 0.62, 0.50, 0.28, 0.74)
    x_positions = (0.20, 0.35, 0.50, 0.65, 0.80)
    span_x = max_x - min_x
    span_y = max_y - min_y
    if span_x > 0 and span_y > 0:
        for fy in y_bands:
            for fx in x_positions:
                candidate = Point(min_x + (span_x * fx), min_y + (span_y * fy))
                add_point(candidate)

    return points


def label_pdf_metrics(label: str, font_size: float) -> Tuple[float, float, float]:
    text_width = pdfmetrics.stringWidth(label, FONT_NAME, font_size)
    ascent, descent = pdfmetrics.getAscentDescent(FONT_NAME, font_size)
    if descent > 0:
        descent = -descent
    return text_width, ascent, descent


def label_box_in_svg(
    point: Point,
    text_width_pdf: float,
    ascent_pdf: float,
    descent_pdf: float,
    scale: float,
    padding_pdf: float,
) -> Polygon:
    width_svg = (text_width_pdf + (2.0 * padding_pdf)) / max(scale, 1e-9)
    height_pdf = (ascent_pdf - descent_pdf) + (2.0 * padding_pdf)
    height_svg = height_pdf / max(scale, 1e-9)
    half_width = width_svg / 2.0
    half_height = height_svg / 2.0
    return box(
        point.x - half_width,
        point.y - half_height,
        point.x + half_width,
        point.y + half_height,
    )


def label_fits_inside_polygon(
    polygon: Polygon,
    point: Point,
    text_width_pdf: float,
    ascent_pdf: float,
    descent_pdf: float,
    scale: float,
) -> bool:
    if not polygon.contains(point):
        return False

    label_rect = label_box_in_svg(
        point=point,
        text_width_pdf=text_width_pdf,
        ascent_pdf=ascent_pdf,
        descent_pdf=descent_pdf,
        scale=scale,
        padding_pdf=LABEL_PADDING_PDF,
    )

    containment_margin_svg = max(0.15 / max(scale, 1e-9), 1e-6)
    inner_polygon = safe_make_valid(polygon.buffer(-containment_margin_svg))
    target_geometry = inner_polygon if not inner_polygon.is_empty else polygon
    return bool(target_geometry.contains(label_rect))


def label_box_in_pdf(
    point: Point,
    text_width_pdf: float,
    ascent_pdf: float,
    descent_pdf: float,
    transform: LayoutTransform,
    padding_pdf: float,
) -> Tuple[float, float, float, float, float, float]:
    center_x, center_y = transform.map_xy(point.x, point.y)
    width_pdf = text_width_pdf + (2.0 * padding_pdf)
    height_pdf = (ascent_pdf - descent_pdf) + (2.0 * padding_pdf)
    half_w = width_pdf / 2.0
    half_h = height_pdf / 2.0
    return (
        center_x - half_w,
        center_y - half_h,
        center_x + half_w,
        center_y + half_h,
        center_x,
        center_y,
    )


def center_point_for_fallback(polygon: Polygon) -> Point:
    centroid = polygon.centroid
    if (
        centroid is not None
        and not centroid.is_empty
        and math.isfinite(centroid.x)
        and math.isfinite(centroid.y)
    ):
        return centroid

    rep = polygon.representative_point()
    if rep is not None and not rep.is_empty:
        return rep

    min_x, min_y, max_x, max_y = polygon.bounds
    return Point((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)


def boxes_overlap(
    box_a: Tuple[float, float, float, float],
    box_b: Tuple[float, float, float, float],
    gap: float,
) -> bool:
    ax0, ay0, ax1, ay1 = box_a
    bx0, by0, bx1, by1 = box_b
    if (ax1 + gap) <= bx0:
        return False
    if ax0 >= (bx1 + gap):
        return False
    if (ay1 + gap) <= by0:
        return False
    if ay0 >= (by1 + gap):
        return False
    return True


def collides_with_existing(
    box_pdf: Tuple[float, float, float, float],
    occupied_boxes_pdf: Sequence[Tuple[float, float, float, float]],
) -> bool:
    for existing in occupied_boxes_pdf:
        if boxes_overlap(box_pdf, existing, gap=LABEL_COLLISION_GAP_PDF):
            return True
    return False


def label_placement(
    geometry: Polygon,
    label: str,
    scale: float,
    transform: LayoutTransform,
    occupied_boxes_pdf: Sequence[Tuple[float, float, float, float]],
    min_font_size: float,
    max_font_size: float,
) -> Optional[LabelPlacement]:
    if geometry.is_empty:
        return None

    base_poly = pick_polygon_for_label(geometry)
    if base_poly is None:
        return None

    requested_max = max(min_font_size, max_font_size)
    requested_min = min(min_font_size, max_font_size)
    max_size = min(6.0, max(2.0, requested_max))
    min_size = max(2.0, min(requested_min, max_size))
    size = max_size
    step = 0.5
    base_candidates = candidate_points_for_polygon(base_poly)

    while size >= min_size - 1e-9:
        text_width_pdf, ascent_pdf, descent_pdf = label_pdf_metrics(label, size)

        for point in base_candidates:
            if label_fits_inside_polygon(
                polygon=base_poly,
                point=point,
                text_width_pdf=text_width_pdf,
                ascent_pdf=ascent_pdf,
                descent_pdf=descent_pdf,
                scale=scale,
            ):
                x0, y0, x1, y1, center_x, center_y = label_box_in_pdf(
                    point=point,
                    text_width_pdf=text_width_pdf,
                    ascent_pdf=ascent_pdf,
                    descent_pdf=descent_pdf,
                    transform=transform,
                    padding_pdf=LABEL_PADDING_PDF,
                )
                box_pdf = (x0, y0, x1, y1)
                if collides_with_existing(box_pdf, occupied_boxes_pdf):
                    continue
                return LabelPlacement(
                    point=point,
                    font_size=size,
                    text_width_pdf=text_width_pdf,
                    ascent_pdf=ascent_pdf,
                    descent_pdf=descent_pdf,
                    center_pdf_x=center_x,
                    center_pdf_y=center_y,
                    box_pdf=box_pdf,
                    used_fallback=False,
                )

        min_x, min_y, max_x, max_y = base_poly.bounds
        span_x = max_x - min_x
        span_y = max_y - min_y
        if span_x > 0 and span_y > 0:
            grid_steps = 5
            for gy in range(grid_steps):
                y = min_y + (span_y * ((gy + 0.5) / grid_steps))
                for gx in range(grid_steps):
                    x = min_x + (span_x * ((gx + 0.5) / grid_steps))
                    point = Point(x, y)
                    if not base_poly.contains(point):
                        continue
                    if label_fits_inside_polygon(
                        polygon=base_poly,
                        point=point,
                        text_width_pdf=text_width_pdf,
                        ascent_pdf=ascent_pdf,
                        descent_pdf=descent_pdf,
                        scale=scale,
                    ):
                        x0, y0, x1, y1, center_x, center_y = label_box_in_pdf(
                            point=point,
                            text_width_pdf=text_width_pdf,
                            ascent_pdf=ascent_pdf,
                            descent_pdf=descent_pdf,
                            transform=transform,
                            padding_pdf=LABEL_PADDING_PDF,
                        )
                        box_pdf = (x0, y0, x1, y1)
                        if collides_with_existing(box_pdf, occupied_boxes_pdf):
                            continue
                        return LabelPlacement(
                            point=point,
                            font_size=size,
                            text_width_pdf=text_width_pdf,
                            ascent_pdf=ascent_pdf,
                            descent_pdf=descent_pdf,
                            center_pdf_x=center_x,
                            center_pdf_y=center_y,
                            box_pdf=box_pdf,
                            used_fallback=False,
                        )

        size -= step

    # Mandatory fallback: place centered at minimum size even if outside bounds.
    fallback_size = min_size
    fallback_point = center_point_for_fallback(base_poly)
    text_width_pdf, ascent_pdf, descent_pdf = label_pdf_metrics(label, fallback_size)
    x0, y0, x1, y1, center_x, center_y = label_box_in_pdf(
        point=fallback_point,
        text_width_pdf=text_width_pdf,
        ascent_pdf=ascent_pdf,
        descent_pdf=descent_pdf,
        transform=transform,
        padding_pdf=LABEL_PADDING_PDF,
    )
    return LabelPlacement(
        point=fallback_point,
        font_size=fallback_size,
        text_width_pdf=text_width_pdf,
        ascent_pdf=ascent_pdf,
        descent_pdf=descent_pdf,
        center_pdf_x=center_x,
        center_pdf_y=center_y,
        box_pdf=(x0, y0, x1, y1),
        used_fallback=True,
    )


def draw_labels(
    pdf: canvas.Canvas,
    zones: Sequence[ColorZone],
    color_to_label: Dict[str, str],
    transform: LayoutTransform,
    min_font_size: float,
    max_font_size: float,
    number_gray: float,
) -> Tuple[int, int]:
    placed = 0
    skipped = 0
    occupied_boxes_pdf: List[Tuple[float, float, float, float]] = []

    ordered_zones = sorted(zones, key=lambda z: z.geometry.area)

    for zone in ordered_zones:
        label = color_to_label.get(zone.color_hex)
        if label is None:
            continue
        placement = label_placement(
            geometry=zone.geometry,
            label=label,
            scale=transform.scale,
            transform=transform,
            occupied_boxes_pdf=occupied_boxes_pdf,
            min_font_size=min_font_size,
            max_font_size=max_font_size,
        )
        if placement is None:
            skipped += 1
            continue

        if zone.color_hex == EXCLUDED_COLOR_HEX and placement.used_fallback:
            skipped += 1
            continue

        baseline_y = placement.center_pdf_y - ((placement.ascent_pdf + placement.descent_pdf) / 2.0)

        pdf.setFillGray(number_gray)
        pdf.setFont(FONT_NAME, placement.font_size)
        pdf.drawString(
            placement.center_pdf_x - (placement.text_width_pdf / 2.0),
            baseline_y,
            label,
        )
        occupied_boxes_pdf.append(placement.box_pdf)
        placed += 1

    return placed, skipped


def compute_legend_height(color_count: int) -> float:
    if color_count <= 0:
        return 110.0
    base = 70.0
    rows_estimate = max(1, math.ceil(color_count / 6.0))
    return min(220.0, base + (rows_estimate * 20.0))


def draw_legend(
    pdf: canvas.Canvas,
    palette: Sequence[str],
    color_to_label: Dict[str, str],
    page_width: float,
    legend_height: float,
    show_hex: bool,
) -> None:
    margin = 24.0
    bottom = margin
    top = margin + legend_height
    left = margin
    right = page_width - margin

    pdf.setStrokeColor(colors.black)
    pdf.setLineWidth(0.8)
    pdf.line(left, top, right, top)

    pdf.setFillColor(colors.black)
    pdf.setFont(FONT_NAME, 10)
    pdf.drawString(left, top - 14, "Leyenda de colores")

    if not palette:
        pdf.setFont(FONT_NAME, 9)
        pdf.drawString(left, top - 30, "No se detectaron colores numerables.")
        return

    available_width = right - left
    grid_top = top - 22.0
    grid_bottom = bottom + 6.0
    available_height = max(14.0, grid_top - grid_bottom)

    swatch_size = 22.0
    col_gap = 14.0
    row_gap = 10.0
    hex_width = 46.0 if show_hex else 0.0

    cols = 1
    rows = len(palette)
    row_height = swatch_size + row_gap
    item_width = swatch_size + hex_width

    while swatch_size >= 14.0:
        row_height = swatch_size + row_gap
        item_width = swatch_size + hex_width
        cols = max(1, min(len(palette), int((available_width + col_gap) // (item_width + col_gap))))
        rows = math.ceil(len(palette) / cols)
        if rows * row_height <= available_height:
            break
        swatch_size -= 1.0

    total_grid_width = (cols * item_width) + ((cols - 1) * col_gap)
    grid_left = left + max(0.0, (available_width - total_grid_width) / 2.0)

    for idx, color_hex in enumerate(palette):
        row = idx // cols
        col = idx % cols

        item_x = grid_left + (col * (item_width + col_gap))
        swatch_y = grid_top - ((row + 1) * row_height)
        swatch_x = item_x

        pdf.setFillColor(colors.HexColor(color_hex))
        pdf.setStrokeColor(colors.black)
        pdf.setLineWidth(0.8)
        pdf.rect(swatch_x, swatch_y, swatch_size, swatch_size, stroke=1, fill=1)

        label = color_to_label[color_hex]
        label_font_size = max(8.0, min(13.0, swatch_size * 0.58))
        text_width = pdfmetrics.stringWidth(label, FONT_NAME, label_font_size)
        ascent, descent = pdfmetrics.getAscentDescent(FONT_NAME, label_font_size)
        if descent > 0:
            descent = -descent

        center_x = swatch_x + (swatch_size / 2.0)
        center_y = swatch_y + (swatch_size / 2.0)
        baseline_y = center_y - ((ascent + descent) / 2.0)

        pdf.setFillColor(legend_text_color_for_background(color_hex))
        pdf.setFont(FONT_NAME, label_font_size)
        pdf.drawString(center_x - (text_width / 2.0), baseline_y, label)

        if show_hex:
            pdf.setFillColor(colors.black)
            pdf.setFont(FONT_NAME, 7.5)
            pdf.drawString(swatch_x + swatch_size + 5.0, swatch_y + (swatch_size * 0.35), color_hex.upper())


def render_pdf(
    output_pdf: Path,
    shapes: Sequence[SvgShape],
    zones: Sequence[ColorZone],
    view_box: Tuple[float, float, float, float],
    palette: Sequence[str],
    color_to_label: Dict[str, str],
    min_font_size: float,
    max_font_size: float,
    line_width: float,
    show_hex: bool,
    outline_gray: float,
    number_gray: float,
) -> Tuple[int, int]:
    page_width, page_height = A4
    legend_height = compute_legend_height(len(palette))
    transform = build_layout(page_width, page_height, view_box, legend_height)

    pdf = canvas.Canvas(str(output_pdf), pagesize=A4)
    pdf.setTitle("Color by Numbers")

    pdf.setFillColor(colors.white)
    pdf.rect(0, 0, page_width, page_height, stroke=0, fill=1)

    for shape in shapes:
        draw_black_outline(
            pdf,
            shape,
            transform,
            line_width=line_width,
            outline_gray=outline_gray,
        )

    placed, skipped = draw_labels(
        pdf,
        zones=zones,
        color_to_label=color_to_label,
        transform=transform,
        min_font_size=min_font_size,
        max_font_size=max_font_size,
        number_gray=number_gray,
    )

    draw_legend(
        pdf,
        palette=palette,
        color_to_label=color_to_label,
        page_width=page_width,
        legend_height=legend_height,
        show_hex=show_hex,
    )

    pdf.showPage()
    pdf.save()
    return placed, skipped


def convert(svg_path: Path, output_pdf: Path, args: argparse.Namespace) -> Tuple[int, int, int]:
    shapes, view_box = read_svg(svg_path)

    zones = build_zones(
        shapes,
        include_strokes=args.include_strokes,
        max_step=args.max_segment_step,
        min_area=args.min_area,
    )
    zones = normalize_nearest_black(zones)
    if not zones:
        raise SvgToPdfError(
            "No se detectaron zonas rellenables en el SVG."
        )

    palette = sorted({zone.color_hex for zone in zones}, key=color_sort_key)
    if not palette:
        raise SvgToPdfError("La paleta numerable quedo vacia.")

    color_to_label = build_color_labels(palette)

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    placed, skipped = render_pdf(
        output_pdf=output_pdf,
        shapes=shapes,
        zones=zones,
        view_box=view_box,
        palette=palette,
        color_to_label=color_to_label,
        min_font_size=args.min_font_size,
        max_font_size=args.max_font_size,
        line_width=args.line_width,
        show_hex=args.show_hex,
        outline_gray=args.outline_gray,
        number_gray=args.number_gray,
    )

    return len(palette), placed, skipped


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convierte un SVG vectorial (o una carpeta con SVGs) en PDF(s) A4 "
            "con formato de colorear por numeros."
        )
    )
    parser.add_argument(
        "input_path",
        help="Ruta de un archivo .svg o de una carpeta con archivos .svg.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help=(
            "Ruta del PDF de salida (solo modo archivo). "
            "Por defecto: output/<entrada>_paint_by_numbers.pdf"
        ),
    )
    parser.add_argument(
        "--font-path",
        default=str(DEFAULT_FONT_PATH),
        help="Ruta del archivo TTF de Montserrat.",
    )
    parser.add_argument(
        "--include-strokes",
        action="store_true",
        help=(
            "Incluye trazos sin relleno como zonas numerables (buffer geometrico por stroke-width)."
        ),
    )
    parser.add_argument(
        "--show-hex",
        action="store_true",
        help="Muestra tambien el codigo HEX en la leyenda.",
    )
    parser.add_argument(
        "--representation-grey",
        "--representation-gray",
        dest="representation_grey",
        nargs=2,
        type=float,
        metavar=("OUTLINE_GREY", "NUMBER_GREY"),
        help=(
            "Override de tonos grises para representacion del dibujo principal: "
            "primero contorno, luego numeros (rango 0..1)."
        ),
    )
    parser.add_argument(
        "--min-font-size",
        type=float,
        default=2.0,
        help="Tamano minimo de fuente para numeros (pt, minimo efectivo 2).",
    )
    parser.add_argument(
        "--max-font-size",
        type=float,
        default=6.0,
        help="Tamano maximo de fuente para numeros (pt, maximo efectivo 6).",
    )
    parser.add_argument(
        "--line-width",
        type=float,
        default=0.55,
        help="Grosor de linea del dibujo principal (pt).",
    )
    parser.add_argument(
        "--max-segment-step",
        type=float,
        default=2.2,
        help="Paso maximo de muestreo para arcos/curvas durante la geometria interna.",
    )
    parser.add_argument(
        "--min-area",
        type=float,
        default=0.0,
        help="Area minima de zona (unidades SVG^2) para etiquetado (0 incluye todas).",
    )
    return parser


def resolve_single_output_path(input_svg: Path, explicit_output: Optional[str]) -> Path:
    if explicit_output:
        return Path(explicit_output).expanduser().resolve()
    return input_svg.parent / "output" / f"{input_svg.stem}_paint_by_numbers.pdf"


def collect_svg_inputs(input_dir: Path) -> List[Path]:
    svg_files = [item for item in input_dir.iterdir() if item.is_file() and item.suffix.lower() == ".svg"]
    return sorted(svg_files, key=lambda p: p.name.lower())


def make_batch_output_dir(input_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = input_dir / f"pdf-output-{stamp}"

    suffix = 1
    while output_dir.exists():
        output_dir = input_dir / f"pdf-output-{stamp}-{suffix:02d}"
        suffix += 1

    output_dir.mkdir(parents=True, exist_ok=False)
    return output_dir


def run_single_file(input_svg: Path, args: argparse.Namespace) -> int:
    if input_svg.suffix.lower() != ".svg":
        print("Error: la entrada debe ser un archivo .svg", file=sys.stderr)
        return 1

    output_pdf = resolve_single_output_path(input_svg, args.output)

    try:
        palette_count, labels_placed, labels_skipped = convert(input_svg, output_pdf, args)
    except SvgToPdfError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive final fallback
        print(f"Error inesperado: {exc}", file=sys.stderr)
        return 3

    print(f"OK: PDF generado en {output_pdf}")
    print(f"- Colores numerables: {palette_count}")
    print(f"- Numeros colocados: {labels_placed}")
    print(f"- Zonas omitidas por falta de espacio: {labels_skipped}")
    return 0


def run_batch_directory(input_dir: Path, args: argparse.Namespace) -> int:
    if args.output:
        print(
            "Error: --output solo aplica al modo archivo. "
            "En modo carpeta se usa automaticamente pdf-output-{timestamp}.",
            file=sys.stderr,
        )
        return 1

    svg_files = collect_svg_inputs(input_dir)
    if not svg_files:
        print(
            f"Error: no se encontraron archivos .svg en la carpeta {input_dir}",
            file=sys.stderr,
        )
        return 1

    try:
        batch_output_dir = make_batch_output_dir(input_dir)
    except Exception as exc:
        print(f"Error: no se pudo crear la carpeta de salida batch: {exc}", file=sys.stderr)
        return 2

    ok_count = 0
    fail_count = 0

    print(f"Batch: {len(svg_files)} SVG(s) detectados en {input_dir}")
    print(f"Batch: salida en {batch_output_dir}")

    for svg_file in svg_files:
        output_pdf = batch_output_dir / f"{svg_file.stem}.pdf"
        try:
            palette_count, labels_placed, labels_skipped = convert(svg_file, output_pdf, args)
            print(
                f"[OK] {svg_file.name} -> {output_pdf.name} | "
                f"colores: {palette_count}, colocados: {labels_placed}, omitidos: {labels_skipped}"
            )
            ok_count += 1
        except SvgToPdfError as exc:
            print(f"[ERROR] {svg_file.name}: {exc}", file=sys.stderr)
            fail_count += 1
        except Exception as exc:  # pragma: no cover - defensive final fallback
            print(f"[ERROR] {svg_file.name}: error inesperado: {exc}", file=sys.stderr)
            fail_count += 1

    print("Batch finalizado")
    print(f"- SVG totales: {len(svg_files)}")
    print(f"- PDFs generados: {ok_count}")
    print(f"- Fallidos: {fail_count}")
    print(f"- Carpeta de salida: {batch_output_dir}")

    if fail_count > 0:
        return 4
    return 0


def validate_gray_value(value: float, label: str) -> float:
    if not math.isfinite(value):
        raise SvgToPdfError(f"{label} no es un numero valido.")
    if value < 0.0 or value > 1.0:
        raise SvgToPdfError(f"{label} debe estar entre 0 y 1.")
    return value


def resolve_representation_grays(
    override_pair: Optional[Sequence[float]],
) -> Tuple[float, float]:
    outline_gray = DEFAULT_OUTLINE_GRAY
    number_gray = DEFAULT_NUMBER_GRAY

    if override_pair is not None:
        if len(override_pair) != 2:
            raise SvgToPdfError(
                "--representation-grey requiere exactamente 2 valores: OUTLINE NUMBER"
            )
        outline_gray = float(override_pair[0])
        number_gray = float(override_pair[1])

    outline_gray = validate_gray_value(outline_gray, "Outline grey")
    number_gray = validate_gray_value(number_gray, "Number grey")
    return outline_gray, number_gray


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    input_path = Path(args.input_path).expanduser().resolve()
    if not input_path.exists():
        print(f"Error: no existe la ruta de entrada: {input_path}", file=sys.stderr)
        return 1

    try:
        outline_gray, number_gray = resolve_representation_grays(args.representation_grey)
    except SvgToPdfError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    args.outline_gray = outline_gray
    args.number_gray = number_gray

    font_path = Path(args.font_path).expanduser().resolve()
    try:
        register_montserrat_font(font_path)
    except SvgToPdfError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive final fallback
        print(f"Error inesperado registrando fuente: {exc}", file=sys.stderr)
        return 3

    if input_path.is_file():
        return run_single_file(input_path, args)
    if input_path.is_dir():
        return run_batch_directory(input_path, args)

    print("Error: la ruta de entrada debe ser archivo o carpeta", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
