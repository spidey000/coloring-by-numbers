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
import functools
import math
import re
import sys
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from shapely.prepared import prep
from shapely import STRtree, affinity
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPolygon, Point, Polygon, box
from shapely.ops import polylabel, unary_union
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
DEFAULT_MYSTERY_BOUNDARY_GRAY = 0.74
DEFAULT_MYSTERY_BOUNDARY_WIDTH = 0.35
PROGRESS_RENDER_INTERVAL = 0.8
PROGRESS_CHECK_FLUSH_INTERVAL = 250


class SvgToPdfError(Exception):
    """Domain error for conversion failures."""


def format_elapsed(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes, rem = divmod(seconds, 60.0)
    hours, minutes = divmod(minutes, 60.0)
    if hours >= 1:
        return f"{int(hours)}h {int(minutes)}m {rem:04.1f}s"
    if minutes >= 1:
        return f"{int(minutes)}m {rem:04.1f}s"
    return f"{seconds:0.2f}s"


def log_step(message: str, args: Optional[argparse.Namespace] = None) -> None:
    prefix = "[paint-numbers]"
    if args is not None and hasattr(args, "command_started_at"):
        elapsed = format_elapsed(time.perf_counter() - args.command_started_at)
        print(f"{prefix} +{elapsed} {message}")
        return
    print(f"{prefix} {message}")


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


@dataclass
class MysteryPatternData:
    """Prepared pattern cells and drawable internal boundaries."""

    cells: List[Polygon]
    boundary_lines: Optional[object]
    cell_tree: Optional[STRtree] = None


@dataclass
class MysterySplitStats:
    """Diagnostics for mystery-pattern fragmentation."""

    zones_before: int = 0
    split_attempts: int = 0
    bbox_skips: int = 0
    fragments_generated: int = 0
    fragments_kept: int = 0
    rejected_small: int = 0
    rejected_ratio: int = 0
    zones_unsplit_too_few: int = 0
    zones_unsplit_over_limit: int = 0
    zones_split: int = 0
    zones_after: int = 0


@dataclass
class StageDiagnostics:
    """Accumulates per-stage timing data for a conversion."""

    timings: List[Tuple[str, float, Dict[str, object]]] = field(default_factory=list)

    def record(self, stage_name: str, elapsed: float, **metrics: object) -> None:
        self.timings.append((stage_name, elapsed, dict(metrics)))


@dataclass
class ProgressStep:
    key: str
    label: str


@dataclass
class ActiveProgress:
    key: str
    label: str
    started_at: float
    total_items: Optional[int] = None
    completed_items: int = 0
    unit_label: Optional[str] = None
    detail_label: Optional[str] = None
    detail_completed: int = 0
    last_percent_reported: int = -1


class CliProgressReporter:
    def __init__(
        self,
        *,
        steps: Sequence[ProgressStep],
        started_at: float,
        stage_estimates: Optional[Dict[str, float]] = None,
    ) -> None:
        self.steps = list(steps)
        self.started_at = started_at
        self.stage_estimates = dict(stage_estimates or {})
        self.step_index = {step.key: index for index, step in enumerate(self.steps)}
        self.completed_keys = set()
        self.active: Optional[ActiveProgress] = None
        self.last_render_at = 0.0

    def start_step(
        self,
        key: str,
        *,
        total_items: Optional[int] = None,
        unit_label: Optional[str] = None,
        detail_label: Optional[str] = None,
    ) -> None:
        step = self.steps[self.step_index[key]]
        self.active = ActiveProgress(
            key=key,
            label=step.label,
            started_at=time.perf_counter(),
            total_items=total_items,
            unit_label=unit_label,
            detail_label=detail_label,
        )
        self.render(force=True)

    def advance_items(self, count: int = 1) -> None:
        if self.active is None or count <= 0:
            return
        self.active.completed_items += count
        self.render()

    def advance_detail(self, count: int = 1) -> None:
        if self.active is None or count <= 0:
            return
        self.active.detail_completed += count
        self.render()

    def complete_step(self, key: str, actual_duration: Optional[float] = None) -> None:
        if actual_duration is not None:
            self.stage_estimates[key] = actual_duration
        self.completed_keys.add(key)
        if self.active is not None and self.active.key == key:
            self.active = None
        self.render(force=True)

    def render(self, force: bool = False) -> None:
        now = time.perf_counter()
        if not force and self.active is not None and self.active.total_items:
            percent = int((self.active.completed_items * 100) / max(self.active.total_items, 1))
            step_size = self._progress_percent_step(self.active)
            milestone = percent // step_size
            progressed = milestone > self.active.last_percent_reported
            if progressed:
                self.active.last_percent_reported = milestone
            if not progressed and (now - self.last_render_at) < PROGRESS_RENDER_INTERVAL:
                return
        elif not force and (now - self.last_render_at) < PROGRESS_RENDER_INTERVAL:
            return

        self.last_render_at = now
        parts = []
        active_key = self.active.key if self.active is not None else None
        for step in self.steps:
            if step.key in self.completed_keys:
                marker = "[x]"
            elif step.key == active_key:
                marker = "[>]"
            else:
                marker = "[ ]"
            parts.append(f"{marker} {step.label}")
        print(f"[paint-numbers][progress] {' | '.join(parts)}")

        if self.active is None:
            total_elapsed = format_elapsed(now - self.started_at)
            print(f"[paint-numbers][progress] transcurrido total: {total_elapsed}")
            return

        active = self.active
        step_elapsed = now - active.started_at
        total_elapsed = now - self.started_at
        summary = [f"paso: {active.label}"]
        if active.total_items is not None:
            percent = (active.completed_items / max(active.total_items, 1)) * 100.0
            summary.append(
                f"{active.completed_items}/{active.total_items} {active.unit_label or 'items'} ({percent:0.1f}%)"
            )
        if active.detail_label and active.detail_completed > 0:
            detail_text = f"{active.detail_label}: {active.detail_completed}"
            detail_total = self._estimate_detail_total(active)
            if detail_total is not None:
                detail_text += f"/~{detail_total}"
            summary.append(detail_text)

        summary.append(f"transcurrido: {format_elapsed(total_elapsed)}")
        summary.append(f"paso: {format_elapsed(step_elapsed)}")

        remaining = self._estimate_remaining_total(now)
        if remaining is not None:
            summary.append(f"ETA: {format_elapsed(remaining)}")

        print(f"[paint-numbers][progress] {' | '.join(summary)}")

    def _estimate_active_total(self, now: float) -> Optional[float]:
        if self.active is None:
            return None
        active = self.active
        elapsed = now - active.started_at
        if active.total_items is not None and active.completed_items > 0:
            return elapsed * (active.total_items / active.completed_items)
        historical = self.stage_estimates.get(active.key)
        if historical is not None and historical > 0:
            return historical
        return None

    def _progress_percent_step(self, active: ActiveProgress) -> int:
        if active.detail_label:
            return 2
        if active.unit_label == "formas":
            return 10
        return 5

    def _estimate_detail_total(self, active: ActiveProgress) -> Optional[int]:
        if active.total_items is None or active.completed_items <= 0 or active.detail_completed <= 0:
            return None
        estimated = (active.detail_completed / active.completed_items) * active.total_items
        return max(active.detail_completed, int(round(estimated)))

    def _estimate_remaining_total(self, now: float) -> Optional[float]:
        remaining = 0.0
        has_estimate = False

        active_total = self._estimate_active_total(now)
        if self.active is not None and active_total is not None:
            elapsed = now - self.active.started_at
            remaining += max(0.0, active_total - elapsed)
            has_estimate = True

        for step in self.steps:
            if step.key in self.completed_keys:
                continue
            if self.active is not None and step.key == self.active.key:
                continue
            estimate = self.stage_estimates.get(step.key)
            if estimate is None:
                continue
            remaining += estimate
            has_estimate = True

        if not has_estimate:
            return None
        return remaining


@dataclass
class LabelZoneProfile:
    """Detailed timing snapshot for a single zone label-placement attempt."""

    zone_index: int
    color_hex: str
    label: str
    area: float
    elapsed: float = 0.0
    result: str = "unknown"
    font_sizes_tried: int = 0
    base_candidates: int = 0
    direct_candidate_checks: int = 0
    grid_candidate_checks: int = 0
    collision_rejects: int = 0
    used_fallback: bool = False
    used_grid: bool = False


@dataclass
class LabelRenderDiagnostics:
    """Aggregated timings and counters for render-labels internals."""

    timings: Dict[str, float] = field(default_factory=dict)
    counters: Dict[str, int] = field(default_factory=dict)
    slowest_zones: List[LabelZoneProfile] = field(default_factory=list)
    max_slowest_zones: int = 15

    def add_time(self, name: str, elapsed: float) -> None:
        self.timings[name] = self.timings.get(name, 0.0) + elapsed

    def inc(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def add_zone_profile(self, profile: LabelZoneProfile) -> None:
        self.slowest_zones.append(profile)
        self.slowest_zones.sort(key=lambda item: item.elapsed, reverse=True)
        if len(self.slowest_zones) > self.max_slowest_zones:
            self.slowest_zones = self.slowest_zones[: self.max_slowest_zones]


@dataclass
class ConvertResult:
    """Final conversion summary and diagnostics."""

    palette_count: int
    labels_placed: int
    labels_skipped: int
    stage_diagnostics: StageDiagnostics
    label_diagnostics: Optional[LabelRenderDiagnostics] = None
    log_file_path: Optional[Path] = None


@dataclass
class LabelCollisionIndex:
    """Spatial hash for placed label boxes in PDF coordinates."""

    cell_size: float = 12.0
    buckets: Dict[Tuple[int, int], List[Tuple[float, float, float, float]]] = field(default_factory=dict)

    def _bucket_range(self, box_pdf: Tuple[float, float, float, float]) -> Tuple[int, int, int, int]:
        x0, y0, x1, y1 = box_pdf
        gap = LABEL_COLLISION_GAP_PDF
        min_x = math.floor((x0 - gap) / self.cell_size)
        max_x = math.floor((x1 + gap) / self.cell_size)
        min_y = math.floor((y0 - gap) / self.cell_size)
        max_y = math.floor((y1 + gap) / self.cell_size)
        return min_x, max_x, min_y, max_y

    def collides(self, box_pdf: Tuple[float, float, float, float]) -> bool:
        min_x, max_x, min_y, max_y = self._bucket_range(box_pdf)
        seen = set()
        for by in range(min_y, max_y + 1):
            for bx in range(min_x, max_x + 1):
                for existing in self.buckets.get((bx, by), []):
                    key = id(existing)
                    if key in seen:
                        continue
                    seen.add(key)
                    if boxes_overlap(box_pdf, existing, gap=LABEL_COLLISION_GAP_PDF):
                        return True
        return False

    def add(self, box_pdf: Tuple[float, float, float, float]) -> None:
        min_x, max_x, min_y, max_y = self._bucket_range(box_pdf)
        for by in range(min_y, max_y + 1):
            for bx in range(min_x, max_x + 1):
                self.buckets.setdefault((bx, by), []).append(box_pdf)


def log_stage_timing(
    stage_name: str,
    elapsed: float,
    args: Optional[argparse.Namespace] = None,
    **metrics: object,
) -> None:
    details = [f"{key}: {value}" for key, value in metrics.items() if value is not None]
    suffix = f" | {' | '.join(details)}" if details else ""
    log_step(f"Etapa {stage_name}: {format_elapsed(elapsed)}{suffix}", args)


def format_ratio(part: float, total: float) -> str:
    if total <= 0:
        return "0.0%"
    return f"{(part / total) * 100.0:0.1f}%"


def parse_elapsed_text(text: str) -> Optional[float]:
    payload = text.strip()
    if not payload:
        return None
    total = 0.0
    found = False
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([hms])", payload):
        value = float(amount)
        if unit == "h":
            total += value * 3600.0
        elif unit == "m":
            total += value * 60.0
        else:
            total += value
        found = True
    if found:
        return total
    return None


def load_stage_estimates_from_logs(output_pdf: Path) -> Dict[str, float]:
    log_pattern = f"{output_pdf.stem}_log_*.txt"
    candidates = sorted(output_pdf.parent.glob(log_pattern), key=lambda path: path.name, reverse=True)
    for log_path in candidates:
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue

        stage_estimates: Dict[str, float] = {}
        in_stage_section = False
        for line in lines:
            stripped = line.strip()
            if stripped == "stage_timings":
                in_stage_section = True
                continue
            if not in_stage_section:
                continue
            if not stripped:
                break
            match = re.match(r"^- ([^:]+): ([^|]+)", stripped)
            if not match:
                continue
            stage_name = match.group(1).strip()
            elapsed = parse_elapsed_text(match.group(2).strip())
            if elapsed is not None:
                stage_estimates[stage_name] = elapsed
        if stage_estimates:
            return stage_estimates
    return {}


def build_progress_steps(args: argparse.Namespace) -> List[ProgressStep]:
    steps = [
        ProgressStep("read-svg", "leer SVG"),
        ProgressStep("build-zones", "zonas"),
        ProgressStep("normalize-black", "normalizar negro"),
    ]
    if args.mystery_pattern:
        steps.extend([
            ProgressStep("load-mystery-pattern", "cargar patron"),
            ProgressStep("apply-mystery-pattern", "fragmentar"),
        ])
    steps.extend([
        ProgressStep("build-palette", "paleta"),
        ProgressStep("render-outline", "contornos"),
        ProgressStep("render-mystery-boundaries", "divisiones"),
        ProgressStep("render-labels", "labels"),
        ProgressStep("render-legend", "leyenda"),
        ProgressStep("render-save", "guardar"),
    ])
    return steps


def log_batch_progress(
    *,
    batch_started_at: float,
    files_total: int,
    files_completed: int,
    current_file: Optional[str] = None,
    current_file_index: Optional[int] = None,
) -> None:
    elapsed = time.perf_counter() - batch_started_at
    summary = [f"archivos: {files_completed}/{files_total}"]
    if files_total > 0:
        summary.append(f"{(files_completed / files_total) * 100.0:0.1f}%")
    if current_file is not None and current_file_index is not None:
        summary.append(f"actual: {current_file_index}/{files_total} {current_file}")
    summary.append(f"transcurrido: {format_elapsed(elapsed)}")
    if files_completed > 0 and files_total > files_completed:
        avg_per_file = elapsed / files_completed
        eta = avg_per_file * (files_total - files_completed)
        summary.append(f"ETA batch: {format_elapsed(eta)}")
    print(f"[paint-numbers][batch] {' | '.join(summary)}")


def make_test_log_path(output_pdf: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return output_pdf.with_name(f"{output_pdf.stem}_log_{stamp}.txt")


def build_test_log_text(
    *,
    input_svg: Path,
    output_pdf: Path,
    result: ConvertResult,
    args: argparse.Namespace,
) -> str:
    total_profiled = profiled_total_elapsed(result.stage_diagnostics)
    lines = [
        "paint-by-numbers profiling log",
        f"generated_at: {datetime.now().isoformat(timespec='seconds')}",
        f"input_svg: {input_svg}",
        f"output_pdf: {output_pdf}",
        f"test_mode: {bool(getattr(args, 'test', False))}",
        "",
        "summary",
        f"- palette_count: {result.palette_count}",
        f"- labels_placed: {result.labels_placed}",
        f"- labels_skipped: {result.labels_skipped}",
        f"- total_profiled: {format_elapsed(total_profiled)}",
        "",
        "stage_timings",
    ]

    for stage_name, elapsed, metrics in result.stage_diagnostics.timings:
        metric_text = " | ".join(f"{key}: {value}" for key, value in metrics.items())
        suffix = f" | {metric_text}" if metric_text else ""
        lines.append(
            f"- {stage_name}: {format_elapsed(elapsed)} ({format_ratio(elapsed, total_profiled)}){suffix}"
        )

    label_diag = result.label_diagnostics
    if label_diag is not None:
        label_total = label_diag.timings.get("render-labels-total", 0.0)
        lines.extend([
            "",
            "render_labels_breakdown",
            f"- total: {format_elapsed(label_total)}",
        ])

        for name in sorted(label_diag.timings):
            if name == "render-labels-total":
                continue
            elapsed = label_diag.timings[name]
            lines.append(f"- {name}: {format_elapsed(elapsed)} ({format_ratio(elapsed, label_total)})")

        lines.extend([
            "",
            "render_labels_counters",
        ])
        for name in sorted(label_diag.counters):
            lines.append(f"- {name}: {label_diag.counters[name]}")

        if label_diag.slowest_zones:
            lines.extend([
                "",
                "slowest_label_zones",
            ])
            for profile in label_diag.slowest_zones:
                lines.append(
                    "- "
                    f"zone={profile.zone_index} | label={profile.label} | color={profile.color_hex} | "
                    f"area={profile.area:0.2f} | elapsed={format_elapsed(profile.elapsed)} | "
                    f"result={profile.result} | font_sizes={profile.font_sizes_tried} | "
                    f"base_candidates={profile.base_candidates} | direct_checks={profile.direct_candidate_checks} | "
                    f"grid_checks={profile.grid_candidate_checks} | collision_rejects={profile.collision_rejects} | "
                    f"used_grid={profile.used_grid} | used_fallback={profile.used_fallback}"
                )

    return "\n".join(lines) + "\n"


def profiled_total_elapsed(stage_diagnostics: StageDiagnostics) -> float:
    nested_render_stages = {
        "render-outline",
        "render-mystery-boundaries",
        "render-labels",
        "render-legend",
        "render-save",
    }
    return sum(
        elapsed
        for stage_name, elapsed, _ in stage_diagnostics.timings
        if stage_name not in nested_render_stages
    )


def write_test_log(
    *,
    input_svg: Path,
    output_pdf: Path,
    result: ConvertResult,
    args: argparse.Namespace,
) -> Path:
    log_path = make_test_log_path(output_pdf)
    log_text = build_test_log_text(input_svg=input_svg, output_pdf=output_pdf, result=result, args=args)
    log_path.write_text(log_text, encoding="utf-8")
    return log_path


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


def bounds_overlap(
    bounds_a: Tuple[float, float, float, float],
    bounds_b: Tuple[float, float, float, float],
) -> bool:
    min_ax, min_ay, max_ax, max_ay = bounds_a
    min_bx, min_by, max_bx, max_by = bounds_b
    return not (
        max_ax < min_bx
        or max_bx < min_ax
        or max_ay < min_by
        or max_by < min_ay
    )


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


def transform_geometry_to_view_box(
    geometry,
    source_view_box: Tuple[float, float, float, float],
    target_view_box: Tuple[float, float, float, float],
    fit_mode: str,
):
    src_min_x, src_min_y, src_w, src_h = source_view_box
    dst_min_x, dst_min_y, dst_w, dst_h = target_view_box
    if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
        return geometry

    scale_x = dst_w / src_w
    scale_y = dst_h / src_h
    fit_mode = fit_mode.lower()
    if fit_mode == "stretch":
        sx = scale_x
        sy = scale_y
        offset_x = dst_min_x - (src_min_x * sx)
        offset_y = dst_min_y - (src_min_y * sy)
    else:
        uniform_scale = max(scale_x, scale_y) if fit_mode == "cover" else min(scale_x, scale_y)
        sx = uniform_scale
        sy = uniform_scale
        scaled_w = src_w * uniform_scale
        scaled_h = src_h * uniform_scale
        offset_x = dst_min_x + ((dst_w - scaled_w) / 2.0) - (src_min_x * uniform_scale)
        offset_y = dst_min_y + ((dst_h - scaled_h) / 2.0) - (src_min_y * uniform_scale)

    scaled = affinity.scale(geometry, xfact=sx, yfact=sy, origin=(0.0, 0.0))
    return affinity.translate(scaled, xoff=offset_x, yoff=offset_y)


def load_mystery_pattern(
    pattern_svg: Path,
    target_view_box: Tuple[float, float, float, float],
    max_step: float,
    fit_mode: str,
) -> MysteryPatternData:
    shapes, pattern_view_box = read_svg(pattern_svg)
    cells: List[Polygon] = []

    for shape in shapes:
        for poly in path_to_fill_polygons(shape.path, max_step=max_step, min_area=0.0):
            transformed = safe_make_valid(
                transform_geometry_to_view_box(poly, pattern_view_box, target_view_box, fit_mode)
            )
            for out_poly in iter_polygons(transformed):
                if out_poly.area > 0:
                    cells.append(out_poly)

    if not cells:
        raise SvgToPdfError("El pattern SVG no produjo celdas geometrizadas utilizables.")

    boundary_lines = unary_union([cell.boundary for cell in cells])
    cell_tree = STRtree(cells)
    return MysteryPatternData(cells=cells, boundary_lines=boundary_lines, cell_tree=cell_tree)


def fragment_zone_by_cells(
    zone: ColorZone,
    candidate_cells: Sequence[Polygon],
    min_fragment_area: float,
    min_fragment_ratio: float,
    max_fragments_per_zone: int,
    stats: Optional[MysterySplitStats] = None,
) -> List[ColorZone]:
    if not candidate_cells:
        return [zone]

    zone_area = max(zone.geometry.area, 1e-9)
    kept: List[ColorZone] = []
    if stats is not None:
        stats.fragments_generated += len(candidate_cells)

    for cell in candidate_cells:
        try:
            fragment = zone.geometry.intersection(cell)
        except Exception:
            continue

        for poly in iter_polygons(safe_make_valid(fragment)):
            if poly.area <= 0:
                continue
            if poly.area < min_fragment_area:
                if stats is not None:
                    stats.rejected_small += 1
                continue
            if (poly.area / zone_area) < min_fragment_ratio:
                if stats is not None:
                    stats.rejected_ratio += 1
                continue
            kept.append(ColorZone(color_hex=zone.color_hex, geometry=poly))

    if stats is not None:
        stats.fragments_kept += len(kept)

    if len(kept) < 2:
        if stats is not None:
            stats.zones_unsplit_too_few += 1
        return [zone]
    if max_fragments_per_zone > 0 and len(kept) > max_fragments_per_zone:
        if stats is not None:
            stats.zones_unsplit_over_limit += 1
        return [zone]
    if stats is not None:
        stats.zones_split += 1
    return kept


def apply_mystery_pattern(
    zones: Sequence[ColorZone],
    pattern_data: MysteryPatternData,
    min_fragment_area: float,
    min_fragment_ratio: float,
    max_fragments_per_zone: int,
) -> Tuple[List[ColorZone], Optional[object], MysterySplitStats]:
    stats = MysterySplitStats(zones_before=len(zones))
    pattern_lines = pattern_data.boundary_lines
    cell_tree = pattern_data.cell_tree
    if pattern_lines is None or getattr(pattern_lines, "is_empty", False) or cell_tree is None:
        stats.zones_after = len(zones)
        return list(zones), None, stats

    split_zones: List[ColorZone] = []
    union_geometries = []
    pattern_bounds = pattern_lines.bounds
    for zone in zones:
        if not bounds_overlap(zone.geometry.bounds, pattern_bounds):
            stats.bbox_skips += 1
            split_zones.append(zone)
            union_geometries.append(zone.geometry)
            continue

        stats.split_attempts += 1
        candidate_indices = cell_tree.query(zone.geometry, predicate="intersects")
        candidate_cells = [pattern_data.cells[int(idx)] for idx in candidate_indices]
        parts = fragment_zone_by_cells(
            zone=zone,
            candidate_cells=candidate_cells,
            min_fragment_area=min_fragment_area,
            min_fragment_ratio=min_fragment_ratio,
            max_fragments_per_zone=max_fragments_per_zone,
            stats=stats,
        )
        split_zones.extend(parts)
        union_geometries.extend([part.geometry for part in parts])

    if not union_geometries:
        stats.zones_after = len(zones)
        return list(zones), None, stats

    drawing_union = unary_union(union_geometries)
    clipped_boundaries = safe_make_valid(pattern_lines.intersection(drawing_union))
    stats.zones_after = len(split_zones)
    return split_zones, clipped_boundaries, stats


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


def iter_line_strings(geometry) -> Iterator[LineString]:
    if geometry is None:
        return
    if getattr(geometry, "is_empty", True):
        return
    if isinstance(geometry, LineString):
        yield geometry
        return
    if isinstance(geometry, MultiLineString):
        for line in geometry.geoms:
            if not line.is_empty:
                yield line
        return
    if isinstance(geometry, Polygon):
        yield geometry.exterior
        for interior in geometry.interiors:
            yield LineString(interior.coords)
        return
    if isinstance(geometry, MultiPolygon):
        for poly in geometry.geoms:
            yield from iter_line_strings(poly)
        return
    if isinstance(geometry, GeometryCollection):
        for item in geometry.geoms:
            yield from iter_line_strings(item)


def draw_line_geometry(
    pdf: canvas.Canvas,
    geometry,
    transform: LayoutTransform,
    line_width: float,
    stroke_gray: float,
) -> None:
    if geometry is None or getattr(geometry, "is_empty", True):
        return

    pdf.setLineWidth(line_width)
    pdf.setStrokeGray(stroke_gray)
    path_obj = pdf.beginPath()
    drawn = False

    for line in iter_line_strings(geometry):
        coords = list(line.coords)
        if len(coords) < 2:
            continue
        start_x, start_y = transform.map_xy(coords[0][0], coords[0][1])
        path_obj.moveTo(start_x, start_y)
        for x, y in coords[1:]:
            px, py = transform.map_xy(x, y)
            path_obj.lineTo(px, py)
        drawn = True

    if drawn:
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
    prepared_polygon = prep(polygon)

    def add_point(point: Optional[Point]) -> None:
        if point is None or point.is_empty:
            return
        if not prepared_polygon.contains(point):
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


@functools.lru_cache(maxsize=128)
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


def label_dimensions_in_svg(
    text_width_pdf: float,
    ascent_pdf: float,
    descent_pdf: float,
    scale: float,
    padding_pdf: float,
) -> Tuple[float, float]:
    width_svg = (text_width_pdf + (2.0 * padding_pdf)) / max(scale, 1e-9)
    height_pdf = (ascent_pdf - descent_pdf) + (2.0 * padding_pdf)
    height_svg = height_pdf / max(scale, 1e-9)
    return width_svg, height_svg


def label_bounds_around_point(point: Point, width_svg: float, height_svg: float) -> Tuple[float, float, float, float]:
    half_width = width_svg / 2.0
    half_height = height_svg / 2.0
    return (
        point.x - half_width,
        point.y - half_height,
        point.x + half_width,
        point.y + half_height,
    )


def bounds_can_contain_rect(
    outer_bounds: Tuple[float, float, float, float],
    rect_bounds: Tuple[float, float, float, float],
) -> bool:
    outer_x0, outer_y0, outer_x1, outer_y1 = outer_bounds
    rect_x0, rect_y0, rect_x1, rect_y1 = rect_bounds
    return (
        rect_x0 >= outer_x0
        and rect_y0 >= outer_y0
        and rect_x1 <= outer_x1
        and rect_y1 <= outer_y1
    )


def size_fits_within_bounds(width_svg: float, height_svg: float, outer_bounds: Tuple[float, float, float, float]) -> bool:
    outer_x0, outer_y0, outer_x1, outer_y1 = outer_bounds
    outer_width = outer_x1 - outer_x0
    outer_height = outer_y1 - outer_y0
    return width_svg <= outer_width and height_svg <= outer_height


def cap_font_size_by_bounds(
    label: str,
    requested_min_size: float,
    requested_max_size: float,
    bounds: Tuple[float, float, float, float],
    scale: float,
) -> Optional[float]:
    size = requested_max_size
    while size >= requested_min_size - 1e-9:
        text_width_pdf, ascent_pdf, descent_pdf = label_pdf_metrics(label, size)
        width_svg, height_svg = label_dimensions_in_svg(
            text_width_pdf=text_width_pdf,
            ascent_pdf=ascent_pdf,
            descent_pdf=descent_pdf,
            scale=scale,
            padding_pdf=LABEL_PADDING_PDF,
        )
        if size_fits_within_bounds(width_svg, height_svg, bounds):
            return size
        size -= 0.5
    return None


def label_fits_inside_polygon(
    target_geometry,
    prepared_target_geometry,
    point: Point,
    width_svg: float,
    height_svg: float,
    target_bounds: Tuple[float, float, float, float],
) -> bool:
    rect_bounds = label_bounds_around_point(point, width_svg, height_svg)
    if not bounds_can_contain_rect(target_bounds, rect_bounds):
        return False

    label_rect = box(*rect_bounds)
    if prepared_target_geometry is not None:
        return bool(prepared_target_geometry.contains(label_rect))
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
    collision_index: LabelCollisionIndex,
) -> bool:
    return collision_index.collides(box_pdf)


def label_placement(
    geometry: Polygon,
    label: str,
    color_hex: str,
    zone_index: int,
    scale: float,
    transform: LayoutTransform,
    collision_index: LabelCollisionIndex,
    min_font_size: float,
    max_font_size: float,
    diagnostics: Optional[LabelRenderDiagnostics] = None,
    progress: Optional[CliProgressReporter] = None,
) -> Optional[LabelPlacement]:
    zone_started_at = time.perf_counter()
    zone_profile = LabelZoneProfile(
        zone_index=zone_index,
        color_hex=color_hex,
        label=label,
        area=float(getattr(geometry, "area", 0.0)),
    )

    def flush_progress_checks() -> None:
        if progress is None:
            return
        pending_checks = (
            (zone_profile.direct_candidate_checks % PROGRESS_CHECK_FLUSH_INTERVAL)
            + (zone_profile.grid_candidate_checks % PROGRESS_CHECK_FLUSH_INTERVAL)
        )
        if pending_checks > 0:
            progress.advance_detail(pending_checks)

    if geometry.is_empty:
        zone_profile.result = "geometry-empty"
        zone_profile.elapsed = time.perf_counter() - zone_started_at
        if diagnostics is not None:
            diagnostics.add_time("placement-total", zone_profile.elapsed)
            diagnostics.inc("zones-geometry-empty")
            diagnostics.add_zone_profile(zone_profile)
        flush_progress_checks()
        return None

    step_started_at = time.perf_counter()
    base_poly = pick_polygon_for_label(geometry)
    pick_elapsed = time.perf_counter() - step_started_at
    if diagnostics is not None:
        diagnostics.add_time("pick-polygon", pick_elapsed)
    if base_poly is None:
        zone_profile.result = "no-base-polygon"
        zone_profile.elapsed = time.perf_counter() - zone_started_at
        if diagnostics is not None:
            diagnostics.add_time("placement-total", zone_profile.elapsed)
            diagnostics.inc("zones-no-base-polygon")
            diagnostics.add_zone_profile(zone_profile)
        flush_progress_checks()
        return None

    requested_max = max(min_font_size, max_font_size)
    requested_min = min(min_font_size, max_font_size)
    max_size = min(6.0, max(2.0, requested_max))
    min_size = max(2.0, min(requested_min, max_size))
    step = 0.5

    step_started_at = time.perf_counter()
    base_candidates = candidate_points_for_polygon(base_poly)
    candidate_elapsed = time.perf_counter() - step_started_at
    zone_profile.base_candidates = len(base_candidates)
    if diagnostics is not None:
        diagnostics.add_time("candidate-points", candidate_elapsed)
        diagnostics.inc("base-candidates-total", len(base_candidates))

    containment_margin_svg = max(0.15 / max(scale, 1e-9), 1e-6)

    step_started_at = time.perf_counter()
    inner_polygon = safe_make_valid(base_poly.buffer(-containment_margin_svg))
    buffer_elapsed = time.perf_counter() - step_started_at
    if diagnostics is not None:
        diagnostics.add_time("inner-buffer", buffer_elapsed)
    placement_geometry = inner_polygon if not inner_polygon.is_empty else base_poly
    placement_bounds = placement_geometry.bounds

    step_started_at = time.perf_counter()
    prepared_placement_geometry = prep(placement_geometry)
    prepared_elapsed = time.perf_counter() - step_started_at
    if diagnostics is not None:
        diagnostics.add_time("prepare-placement-geometry", prepared_elapsed)

    step_started_at = time.perf_counter()
    grid_candidates: List[Tuple[Point, bool]] = []
    min_x, min_y, max_x, max_y = base_poly.bounds
    span_x = max_x - min_x
    span_y = max_y - min_y
    if span_x > 0 and span_y > 0:
        grid_steps = 5
        prepared_base_poly = prep(base_poly)
        for gy in range(grid_steps):
            y = min_y + (span_y * ((gy + 0.5) / grid_steps))
            for gx in range(grid_steps):
                x = min_x + (span_x * ((gx + 0.5) / grid_steps))
                point = Point(x, y)
                grid_candidates.append((point, prepared_base_poly.contains(point)))
    precompute_grid_elapsed = time.perf_counter() - step_started_at
    if diagnostics is not None:
        diagnostics.add_time("precompute-grid", precompute_grid_elapsed)

    fallback_point = center_point_for_fallback(base_poly)

    step_started_at = time.perf_counter()
    min_text_width_pdf, min_ascent_pdf, min_descent_pdf = label_pdf_metrics(label, min_size)
    min_width_svg, min_height_svg = label_dimensions_in_svg(
        text_width_pdf=min_text_width_pdf,
        ascent_pdf=min_ascent_pdf,
        descent_pdf=min_descent_pdf,
        scale=scale,
        padding_pdf=LABEL_PADDING_PDF,
    )
    bounded_max_size = cap_font_size_by_bounds(
        label=label,
        requested_min_size=min_size,
        requested_max_size=max_size,
        bounds=placement_bounds,
        scale=scale,
    )
    bound_checks_elapsed = time.perf_counter() - step_started_at
    if diagnostics is not None:
        diagnostics.add_time("bounds-fit-pruning", bound_checks_elapsed)

    if (
        bounded_max_size is None
        or not size_fits_within_bounds(min_width_svg, min_height_svg, placement_bounds)
        or placement_geometry.area < (min_width_svg * min_height_svg)
    ):
        zone_profile.used_fallback = True
        zone_profile.result = "fallback-impossible-fit"
        zone_profile.elapsed = time.perf_counter() - zone_started_at
        if diagnostics is not None:
            diagnostics.inc("placements-impossible-fit")
        fallback_size = min_size
        text_width_pdf = min_text_width_pdf
        ascent_pdf = min_ascent_pdf
        descent_pdf = min_descent_pdf
        x0, y0, x1, y1, center_x, center_y = label_box_in_pdf(
            point=fallback_point,
            text_width_pdf=text_width_pdf,
            ascent_pdf=ascent_pdf,
            descent_pdf=descent_pdf,
            transform=transform,
            padding_pdf=LABEL_PADDING_PDF,
        )
        if diagnostics is not None:
            diagnostics.add_time("placement-total", zone_profile.elapsed)
            diagnostics.add_zone_profile(zone_profile)
        flush_progress_checks()
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

    size = bounded_max_size

    while size >= min_size - 1e-9:
        zone_profile.font_sizes_tried += 1
        if diagnostics is not None:
            diagnostics.inc("font-sizes-tried")

        step_started_at = time.perf_counter()
        text_width_pdf, ascent_pdf, descent_pdf = label_pdf_metrics(label, size)
        if diagnostics is not None:
            diagnostics.add_time("font-metrics", time.perf_counter() - step_started_at)
        width_svg, height_svg = label_dimensions_in_svg(
            text_width_pdf=text_width_pdf,
            ascent_pdf=ascent_pdf,
            descent_pdf=descent_pdf,
            scale=scale,
            padding_pdf=LABEL_PADDING_PDF,
        )

        for point in base_candidates:
            zone_profile.direct_candidate_checks += 1
            if diagnostics is not None:
                diagnostics.inc("direct-candidate-checks")
            if progress is not None and (zone_profile.direct_candidate_checks % PROGRESS_CHECK_FLUSH_INTERVAL) == 0:
                progress.advance_detail(PROGRESS_CHECK_FLUSH_INTERVAL)

            step_started_at = time.perf_counter()
            if label_fits_inside_polygon(
                target_geometry=placement_geometry,
                prepared_target_geometry=prepared_placement_geometry,
                point=point,
                width_svg=width_svg,
                height_svg=height_svg,
                target_bounds=placement_bounds,
            ):
                if diagnostics is not None:
                    diagnostics.add_time("fit-check-direct", time.perf_counter() - step_started_at)
                    diagnostics.inc("fit-success-direct")

                step_started_at = time.perf_counter()
                x0, y0, x1, y1, center_x, center_y = label_box_in_pdf(
                    point=point,
                    text_width_pdf=text_width_pdf,
                    ascent_pdf=ascent_pdf,
                    descent_pdf=descent_pdf,
                    transform=transform,
                    padding_pdf=LABEL_PADDING_PDF,
                )
                if diagnostics is not None:
                    diagnostics.add_time("pdf-box-direct", time.perf_counter() - step_started_at)
                box_pdf = (x0, y0, x1, y1)

                step_started_at = time.perf_counter()
                if collides_with_existing(box_pdf, collision_index):
                    if diagnostics is not None:
                        diagnostics.add_time("collision-check-direct", time.perf_counter() - step_started_at)
                        diagnostics.inc("collision-rejects")
                    zone_profile.collision_rejects += 1
                    continue
                if diagnostics is not None:
                    diagnostics.add_time("collision-check-direct", time.perf_counter() - step_started_at)
                    diagnostics.add_time("placement-total", time.perf_counter() - zone_started_at)
                    diagnostics.inc("placements-found")
                zone_profile.result = "direct-success"
                zone_profile.elapsed = time.perf_counter() - zone_started_at
                if diagnostics is not None:
                    diagnostics.add_zone_profile(zone_profile)
                flush_progress_checks()
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
            elif diagnostics is not None:
                diagnostics.add_time("fit-check-direct", time.perf_counter() - step_started_at)

        if grid_candidates:
            zone_profile.used_grid = True
            for point, point_is_inside in grid_candidates:
                if diagnostics is not None:
                    diagnostics.inc("grid-candidate-checks")
                zone_profile.grid_candidate_checks += 1
                if progress is not None and (zone_profile.grid_candidate_checks % PROGRESS_CHECK_FLUSH_INTERVAL) == 0:
                    progress.advance_detail(PROGRESS_CHECK_FLUSH_INTERVAL)

                if not point_is_inside:
                    if diagnostics is not None:
                        diagnostics.inc("grid-point-outside")
                    continue

                step_started_at = time.perf_counter()
                if label_fits_inside_polygon(
                    target_geometry=placement_geometry,
                    prepared_target_geometry=prepared_placement_geometry,
                    point=point,
                    width_svg=width_svg,
                    height_svg=height_svg,
                    target_bounds=placement_bounds,
                ):
                    if diagnostics is not None:
                        diagnostics.add_time("fit-check-grid", time.perf_counter() - step_started_at)
                        diagnostics.inc("fit-success-grid")

                    step_started_at = time.perf_counter()
                    x0, y0, x1, y1, center_x, center_y = label_box_in_pdf(
                        point=point,
                        text_width_pdf=text_width_pdf,
                        ascent_pdf=ascent_pdf,
                        descent_pdf=descent_pdf,
                        transform=transform,
                        padding_pdf=LABEL_PADDING_PDF,
                    )
                    if diagnostics is not None:
                        diagnostics.add_time("pdf-box-grid", time.perf_counter() - step_started_at)
                    box_pdf = (x0, y0, x1, y1)

                    step_started_at = time.perf_counter()
                    if collides_with_existing(box_pdf, collision_index):
                        if diagnostics is not None:
                            diagnostics.add_time("collision-check-grid", time.perf_counter() - step_started_at)
                            diagnostics.inc("collision-rejects")
                        zone_profile.collision_rejects += 1
                        continue
                    if diagnostics is not None:
                        diagnostics.add_time("collision-check-grid", time.perf_counter() - step_started_at)
                        diagnostics.add_time("placement-total", time.perf_counter() - zone_started_at)
                        diagnostics.inc("placements-found")
                    zone_profile.result = "grid-success"
                    zone_profile.elapsed = time.perf_counter() - zone_started_at
                    if diagnostics is not None:
                        diagnostics.add_zone_profile(zone_profile)
                    flush_progress_checks()
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
                elif diagnostics is not None:
                    diagnostics.add_time("fit-check-grid", time.perf_counter() - step_started_at)

        size -= step

    # Mandatory fallback: place centered at minimum size even if outside bounds.
    fallback_size = min_size

    step_started_at = time.perf_counter()
    text_width_pdf, ascent_pdf, descent_pdf = label_pdf_metrics(label, fallback_size)
    x0, y0, x1, y1, center_x, center_y = label_box_in_pdf(
        point=fallback_point,
        text_width_pdf=text_width_pdf,
        ascent_pdf=ascent_pdf,
        descent_pdf=descent_pdf,
        transform=transform,
        padding_pdf=LABEL_PADDING_PDF,
    )
    fallback_elapsed = time.perf_counter() - step_started_at
    zone_profile.used_fallback = True
    zone_profile.result = "fallback"
    zone_profile.elapsed = time.perf_counter() - zone_started_at
    if diagnostics is not None:
        diagnostics.add_time("fallback-placement", fallback_elapsed)
        diagnostics.add_time("placement-total", zone_profile.elapsed)
        diagnostics.inc("placements-fallback")
        diagnostics.add_zone_profile(zone_profile)
    flush_progress_checks()
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
    diagnostics: Optional[LabelRenderDiagnostics] = None,
    progress: Optional[CliProgressReporter] = None,
) -> Tuple[int, int]:
    started_at = time.perf_counter()
    placed = 0
    skipped = 0
    collision_index = LabelCollisionIndex()

    if diagnostics is not None:
        diagnostics.inc("zones-total", len(zones))

    sort_started_at = time.perf_counter()
    ordered_zones = sorted(zones, key=lambda z: z.geometry.area)
    if diagnostics is not None:
        diagnostics.add_time("sort-zones", time.perf_counter() - sort_started_at)

    for zone_index, zone in enumerate(ordered_zones):
        if diagnostics is not None:
            diagnostics.inc("zones-processed")
        label = color_to_label.get(zone.color_hex)
        if label is None:
            if diagnostics is not None:
                diagnostics.inc("zones-without-label")
            if progress is not None:
                progress.advance_items(1)
            continue
        placement = label_placement(
            geometry=zone.geometry,
            label=label,
            color_hex=zone.color_hex,
            zone_index=zone_index,
            scale=transform.scale,
            transform=transform,
            collision_index=collision_index,
            min_font_size=min_font_size,
            max_font_size=max_font_size,
            diagnostics=diagnostics,
            progress=progress,
        )
        if placement is None:
            skipped += 1
            if diagnostics is not None:
                diagnostics.inc("placements-none")
            if progress is not None:
                progress.advance_items(1)
            continue

        if zone.color_hex == EXCLUDED_COLOR_HEX and placement.used_fallback:
            skipped += 1
            if diagnostics is not None:
                diagnostics.inc("black-fallback-skipped")
            if progress is not None:
                progress.advance_items(1)
            continue

        baseline_y = placement.center_pdf_y - ((placement.ascent_pdf + placement.descent_pdf) / 2.0)

        draw_started_at = time.perf_counter()
        pdf.setFillGray(number_gray)
        pdf.setFont(FONT_NAME, placement.font_size)
        pdf.drawString(
            placement.center_pdf_x - (placement.text_width_pdf / 2.0),
            baseline_y,
            label,
        )
        if diagnostics is not None:
            diagnostics.add_time("draw-text", time.perf_counter() - draw_started_at)

        index_started_at = time.perf_counter()
        collision_index.add(placement.box_pdf)
        if diagnostics is not None:
            diagnostics.add_time("collision-index-add", time.perf_counter() - index_started_at)
            diagnostics.inc("labels-drawn")
        placed += 1
        if progress is not None:
            progress.advance_items(1)

    if diagnostics is not None:
        diagnostics.add_time("render-labels-total", time.perf_counter() - started_at)
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
    mystery_boundaries=None,
    mystery_boundary_gray: float = DEFAULT_MYSTERY_BOUNDARY_GRAY,
    mystery_boundary_width: float = DEFAULT_MYSTERY_BOUNDARY_WIDTH,
    args: Optional[argparse.Namespace] = None,
    diagnostics: Optional[StageDiagnostics] = None,
    label_diagnostics: Optional[LabelRenderDiagnostics] = None,
    progress: Optional[CliProgressReporter] = None,
) -> Tuple[int, int]:
    page_width, page_height = A4
    legend_height = compute_legend_height(len(palette))
    transform = build_layout(page_width, page_height, view_box, legend_height)

    pdf = canvas.Canvas(str(output_pdf), pagesize=A4)
    pdf.setTitle("Color by Numbers")

    pdf.setFillColor(colors.white)
    pdf.rect(0, 0, page_width, page_height, stroke=0, fill=1)

    stage_started_at = time.perf_counter()
    if progress is not None:
        progress.start_step("render-outline", total_items=len(shapes), unit_label="formas")
    for shape in shapes:
        draw_black_outline(
            pdf,
            shape,
            transform,
            line_width=line_width,
            outline_gray=outline_gray,
        )
        if progress is not None:
            progress.advance_items(1)
    elapsed = time.perf_counter() - stage_started_at
    if diagnostics is not None:
        diagnostics.record("render-outline", elapsed, shapes=len(shapes))
    if progress is not None:
        progress.complete_step("render-outline", elapsed)
    log_stage_timing("render-outline", elapsed, args, shapes=len(shapes))

    stage_started_at = time.perf_counter()
    if progress is not None:
        progress.start_step("render-mystery-boundaries")
    draw_line_geometry(
        pdf,
        geometry=mystery_boundaries,
        transform=transform,
        line_width=mystery_boundary_width,
        stroke_gray=mystery_boundary_gray,
    )
    elapsed = time.perf_counter() - stage_started_at
    if diagnostics is not None:
        diagnostics.record("render-mystery-boundaries", elapsed)
    if progress is not None:
        progress.complete_step("render-mystery-boundaries", elapsed)
    log_stage_timing("render-mystery-boundaries", elapsed, args)

    stage_started_at = time.perf_counter()
    if progress is not None:
        progress.start_step(
            "render-labels",
            total_items=len(zones),
            unit_label="zonas",
            detail_label="checks",
        )
    placed, skipped = draw_labels(
        pdf,
        zones=zones,
        color_to_label=color_to_label,
        transform=transform,
        min_font_size=min_font_size,
        max_font_size=max_font_size,
        number_gray=number_gray,
        diagnostics=label_diagnostics,
        progress=progress,
    )
    elapsed = time.perf_counter() - stage_started_at
    if diagnostics is not None:
        diagnostics.record(
            "render-labels",
            elapsed,
            zones=len(zones),
            labels_placed=placed,
            labels_skipped=skipped,
        )
    if progress is not None:
        progress.complete_step("render-labels", elapsed)
    log_stage_timing(
        "render-labels",
        elapsed,
        args,
        zones=len(zones),
        labels_placed=placed,
        labels_skipped=skipped,
    )

    stage_started_at = time.perf_counter()
    if progress is not None:
        progress.start_step("render-legend")
    draw_legend(
        pdf,
        palette=palette,
        color_to_label=color_to_label,
        page_width=page_width,
        legend_height=legend_height,
        show_hex=show_hex,
    )
    elapsed = time.perf_counter() - stage_started_at
    if diagnostics is not None:
        diagnostics.record("render-legend", elapsed, colors=len(palette))
    if progress is not None:
        progress.complete_step("render-legend", elapsed)
    log_stage_timing("render-legend", elapsed, args, colors=len(palette))

    stage_started_at = time.perf_counter()
    if progress is not None:
        progress.start_step("render-save")
    pdf.showPage()
    pdf.save()
    elapsed = time.perf_counter() - stage_started_at
    if diagnostics is not None:
        diagnostics.record("render-save", elapsed)
    if progress is not None:
        progress.complete_step("render-save", elapsed)
    log_stage_timing("render-save", elapsed, args)
    return placed, skipped


def convert(svg_path: Path, output_pdf: Path, args: argparse.Namespace) -> ConvertResult:
    diagnostics = StageDiagnostics()
    label_diagnostics = LabelRenderDiagnostics() if getattr(args, "test", False) else None
    progress = CliProgressReporter(
        steps=build_progress_steps(args),
        started_at=getattr(args, "command_started_at", time.perf_counter()),
        stage_estimates=load_stage_estimates_from_logs(output_pdf),
    )

    log_step(f"Leyendo SVG base: {svg_path.name}", args)
    stage_started_at = time.perf_counter()
    progress.start_step("read-svg")
    shapes, view_box = read_svg(svg_path)
    elapsed = time.perf_counter() - stage_started_at
    diagnostics.record("read-svg", elapsed, shapes=len(shapes))
    progress.complete_step("read-svg", elapsed)
    log_stage_timing("read-svg", elapsed, args, shapes=len(shapes))
    mystery_boundaries = None

    log_step("Extrayendo zonas coloreables", args)
    stage_started_at = time.perf_counter()
    progress.start_step("build-zones")
    zones = build_zones(
        shapes,
        include_strokes=args.include_strokes,
        max_step=args.max_segment_step,
        min_area=args.min_area,
    )
    elapsed = time.perf_counter() - stage_started_at
    diagnostics.record("build-zones", elapsed, zones=len(zones), max_step=args.max_segment_step)
    progress.complete_step("build-zones", elapsed)
    log_stage_timing("build-zones", elapsed, args, zones=len(zones), max_step=args.max_segment_step)
    log_step(f"Zonas iniciales detectadas: {len(zones)}", args)

    log_step("Normalizando color mas cercano al negro puro", args)
    stage_started_at = time.perf_counter()
    progress.start_step("normalize-black", total_items=len(zones), unit_label="zonas")
    zones = normalize_nearest_black(zones)
    elapsed = time.perf_counter() - stage_started_at
    diagnostics.record("normalize-black", elapsed, zones=len(zones))
    progress.advance_items(len(zones))
    progress.complete_step("normalize-black", elapsed)
    log_stage_timing("normalize-black", elapsed, args, zones=len(zones))

    if args.mystery_pattern:
        mystery_max_step = args.mystery_max_segment_step
        log_step(f"Cargando patron mystery: {Path(args.mystery_pattern).name}", args)
        stage_started_at = time.perf_counter()
        progress.start_step("load-mystery-pattern")
        pattern_data = load_mystery_pattern(
            pattern_svg=Path(args.mystery_pattern).expanduser().resolve(),
            target_view_box=view_box,
            max_step=mystery_max_step,
            fit_mode=args.mystery_fit,
        )
        elapsed = time.perf_counter() - stage_started_at
        diagnostics.record(
            "load-mystery-pattern",
            elapsed,
            cells=len(pattern_data.cells),
            max_step=mystery_max_step,
        )
        progress.complete_step("load-mystery-pattern", elapsed)
        log_stage_timing(
            "load-mystery-pattern",
            elapsed,
            args,
            cells=len(pattern_data.cells),
            max_step=mystery_max_step,
        )
        log_step(f"Patron preparado con {len(pattern_data.cells)} celdas", args)
        log_step("Fragmentando zonas con el patron", args)
        stage_started_at = time.perf_counter()
        progress.start_step("apply-mystery-pattern", total_items=len(zones), unit_label="zonas")
        zones, mystery_boundaries, mystery_stats = apply_mystery_pattern(
            zones=zones,
            pattern_data=pattern_data,
            min_fragment_area=args.mystery_min_fragment_area,
            min_fragment_ratio=args.mystery_min_fragment_ratio,
            max_fragments_per_zone=args.mystery_max_fragments_per_zone,
        )
        elapsed = time.perf_counter() - stage_started_at
        diagnostics.record(
            "apply-mystery-pattern",
            elapsed,
            zones_before=mystery_stats.zones_before,
            zones_after=mystery_stats.zones_after,
            split_attempts=mystery_stats.split_attempts,
            bbox_skips=mystery_stats.bbox_skips,
            fragments_generated=mystery_stats.fragments_generated,
            fragments_kept=mystery_stats.fragments_kept,
            zones_split=mystery_stats.zones_split,
            rejected_small=mystery_stats.rejected_small,
            rejected_ratio=mystery_stats.rejected_ratio,
            unsplit_too_few=mystery_stats.zones_unsplit_too_few,
            unsplit_over_limit=mystery_stats.zones_unsplit_over_limit,
        )
        progress.advance_items(mystery_stats.zones_before)
        progress.complete_step("apply-mystery-pattern", elapsed)
        log_stage_timing(
            "apply-mystery-pattern",
            elapsed,
            args,
            zones_before=mystery_stats.zones_before,
            zones_after=mystery_stats.zones_after,
            split_attempts=mystery_stats.split_attempts,
            bbox_skips=mystery_stats.bbox_skips,
            fragments_generated=mystery_stats.fragments_generated,
            fragments_kept=mystery_stats.fragments_kept,
            zones_split=mystery_stats.zones_split,
            rejected_small=mystery_stats.rejected_small,
            rejected_ratio=mystery_stats.rejected_ratio,
            unsplit_too_few=mystery_stats.zones_unsplit_too_few,
            unsplit_over_limit=mystery_stats.zones_unsplit_over_limit,
        )
        log_step(f"Zonas tras mystery pattern: {len(zones)}", args)

    if not zones:
        raise SvgToPdfError(
            "No se detectaron zonas rellenables en el SVG."
        )

    log_step("Construyendo paleta y referencias", args)
    stage_started_at = time.perf_counter()
    progress.start_step("build-palette", total_items=len(zones), unit_label="zonas")
    palette = sorted({zone.color_hex for zone in zones}, key=color_sort_key)
    if not palette:
        raise SvgToPdfError("La paleta numerable quedo vacia.")

    color_to_label = build_color_labels(palette)
    elapsed = time.perf_counter() - stage_started_at
    diagnostics.record("build-palette", elapsed, colors=len(palette))
    progress.advance_items(len(zones))
    progress.complete_step("build-palette", elapsed)
    log_stage_timing("build-palette", elapsed, args, colors=len(palette))

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    log_step(f"Renderizando PDF en {output_pdf}", args)
    stage_started_at = time.perf_counter()
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
        mystery_boundaries=mystery_boundaries,
        mystery_boundary_gray=args.mystery_boundary_gray,
        mystery_boundary_width=args.mystery_boundary_width,
        args=args,
        diagnostics=diagnostics,
        label_diagnostics=label_diagnostics,
        progress=progress,
    )
    elapsed = time.perf_counter() - stage_started_at
    diagnostics.record("render-pdf-total", elapsed)
    log_stage_timing("render-pdf-total", elapsed, args)

    total_stages = profiled_total_elapsed(diagnostics)
    log_stage_timing("convert-total-profiled", total_stages, args)

    log_step("PDF terminado", args)

    result = ConvertResult(
        palette_count=len(palette),
        labels_placed=placed,
        labels_skipped=skipped,
        stage_diagnostics=diagnostics,
        label_diagnostics=label_diagnostics,
    )
    if getattr(args, "test", False):
        result.log_file_path = write_test_log(
            input_svg=svg_path,
            output_pdf=output_pdf,
            result=result,
            args=args,
        )

    return result


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
        "--test",
        action="store_true",
        help=(
            "Genera un log detallado de profiling junto al PDF con nombre "
            "{filename}_log_{timestamp}.txt."
        ),
    )
    parser.add_argument(
        "--mystery-pattern",
        help="Ruta a un SVG patron para fragmentar geometricamente todo el dibujo.",
    )
    parser.add_argument(
        "--mystery-fit",
        choices=("contain", "cover", "stretch"),
        default="cover",
        help="Modo de ajuste del patron sobre el viewBox del dibujo.",
    )
    parser.add_argument(
        "--mystery-min-fragment-area",
        type=float,
        default=12.0,
        help="Area minima para conservar un fragmento generado por el patron.",
    )
    parser.add_argument(
        "--mystery-min-fragment-ratio",
        type=float,
        default=0.015,
        help="Proporcion minima respecto al area de la zona original para conservar un fragmento.",
    )
    parser.add_argument(
        "--mystery-max-fragments-per-zone",
        type=int,
        default=24,
        help="Limite de fragmentos por zona; si se supera, esa zona no se divide.",
    )
    parser.add_argument(
        "--mystery-boundary-grey",
        "--mystery-boundary-gray",
        dest="mystery_boundary_gray",
        type=float,
        default=DEFAULT_MYSTERY_BOUNDARY_GRAY,
        help="Tono gris para las divisiones internas del patron (0..1).",
    )
    parser.add_argument(
        "--mystery-boundary-width",
        type=float,
        default=DEFAULT_MYSTERY_BOUNDARY_WIDTH,
        help="Grosor de las divisiones internas del patron (pt).",
    )
    parser.add_argument(
        "--mystery-max-segment-step",
        type=float,
        default=4.0,
        help=(
            "Paso maximo de muestreo especifico para el patron mystery. "
            "Usa un valor mas alto para reducir CPU en la fragmentacion."
        ),
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
    log_step(f"Iniciando modo archivo para {input_svg.name}", args)
    log_step(f"Salida prevista: {output_pdf}", args)

    try:
        result = convert(input_svg, output_pdf, args)
    except SvgToPdfError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive final fallback
        print(f"Error inesperado: {exc}", file=sys.stderr)
        return 3

    total_elapsed = format_elapsed(time.perf_counter() - args.command_started_at)
    print(f"OK: PDF generado en {output_pdf}")
    print(f"- Colores numerables: {result.palette_count}")
    print(f"- Numeros colocados: {result.labels_placed}")
    print(f"- Zonas omitidas por falta de espacio: {result.labels_skipped}")
    if result.log_file_path is not None:
        print(f"- Log de test: {result.log_file_path}")
    print(f"- Tiempo total: {total_elapsed}")
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
    batch_started_at = time.perf_counter()

    log_step(f"Iniciando modo batch en {input_dir}", args)
    print(f"Batch: {len(svg_files)} SVG(s) detectados en {input_dir}")
    print(f"Batch: salida en {batch_output_dir}")
    log_batch_progress(
        batch_started_at=batch_started_at,
        files_total=len(svg_files),
        files_completed=0,
        current_file=svg_files[0].name if svg_files else None,
        current_file_index=1 if svg_files else None,
    )

    for file_index, svg_file in enumerate(svg_files, start=1):
        output_pdf = batch_output_dir / f"{svg_file.stem}.pdf"
        file_started_at = time.perf_counter()
        completed_before_current = ok_count + fail_count
        log_batch_progress(
            batch_started_at=batch_started_at,
            files_total=len(svg_files),
            files_completed=completed_before_current,
            current_file=svg_file.name,
            current_file_index=file_index,
        )
        log_step(f"Procesando archivo batch: {svg_file.name}", args)
        try:
            result = convert(svg_file, output_pdf, args)
            print(
                f"[OK] {svg_file.name} -> {output_pdf.name} | "
                f"colores: {result.palette_count}, colocados: {result.labels_placed}, omitidos: {result.labels_skipped}, "
                f"tiempo: {format_elapsed(time.perf_counter() - file_started_at)}"
            )
            if result.log_file_path is not None:
                print(f"      log: {result.log_file_path.name}")
            ok_count += 1
            log_batch_progress(
                batch_started_at=batch_started_at,
                files_total=len(svg_files),
                files_completed=ok_count + fail_count,
            )
        except SvgToPdfError as exc:
            print(f"[ERROR] {svg_file.name}: {exc}", file=sys.stderr)
            fail_count += 1
            log_batch_progress(
                batch_started_at=batch_started_at,
                files_total=len(svg_files),
                files_completed=ok_count + fail_count,
            )
        except Exception as exc:  # pragma: no cover - defensive final fallback
            print(f"[ERROR] {svg_file.name}: error inesperado: {exc}", file=sys.stderr)
            fail_count += 1
            log_batch_progress(
                batch_started_at=batch_started_at,
                files_total=len(svg_files),
                files_completed=ok_count + fail_count,
            )

    print("Batch finalizado")
    print(f"- SVG totales: {len(svg_files)}")
    print(f"- PDFs generados: {ok_count}")
    print(f"- Fallidos: {fail_count}")
    print(f"- Carpeta de salida: {batch_output_dir}")
    print(f"- Tiempo total: {format_elapsed(time.perf_counter() - args.command_started_at)}")

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
    args.command_started_at = time.perf_counter()

    input_path = Path(args.input_path).expanduser().resolve()
    if not input_path.exists():
        print(f"Error: no existe la ruta de entrada: {input_path}", file=sys.stderr)
        return 1

    try:
        outline_gray, number_gray = resolve_representation_grays(args.representation_grey)
        mystery_boundary_gray = validate_gray_value(
            args.mystery_boundary_gray,
            "Mystery boundary grey",
        )
    except SvgToPdfError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    args.outline_gray = outline_gray
    args.number_gray = number_gray
    args.mystery_boundary_gray = mystery_boundary_gray
    log_step("Argumentos validados", args)

    font_path = Path(args.font_path).expanduser().resolve()
    try:
        log_step(f"Registrando fuente desde {font_path}", args)
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
