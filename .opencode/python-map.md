# Python Map

## Module inventory

### `svg_to_paint_by_numbers_pdf.py`

Current role: single-module application containing the complete CLI and conversion pipeline.

## Core data models

### `SvgShape`
- Represents one parsed SVG drawable with normalized path and style information.
- Expected fields: `path`, `fill_color`, `stroke_color`, `stroke_width`, `fill_rule`.
- Used as the bridge between XML parsing and downstream geometry generation.

### `ColorZone`
- Represents one polygonal region that should receive a paint-by-numbers label.
- Expected fields: `color_hex`, `geometry`.

### `LayoutTransform`
- Represents the mapping from SVG coordinates into A4 PDF coordinates.
- Expected to handle rotation for landscape-oriented art and translate text metrics between PDF and SVG spaces.

### `LabelPlacement`
- Represents a resolved label position, font size, and collision box.
- Used by `label_placement()` and `draw_labels()`.

### `MysteryPatternData`
- Holds prepared mystery pattern cells, merged boundary geometry, and a spatial tree for intersection queries.

### `MysterySplitStats`
- Tracks how mystery fragmentation behaved for diagnostics and logs.

### `StageDiagnostics`
- Stores per-stage timing snapshots and optional metrics for a conversion run.

### `CliProgressReporter`
- Emits live progress lines for major pipeline stages and some item-count-based steps.

### `LabelRenderDiagnostics`
- Aggregates detailed timing and counters for expensive label rendering internals when test mode is enabled.

### `ConvertResult`
- Summarizes one completed conversion: palette count, placed labels, skipped labels, diagnostics, and optional log path.

### `BatchJobResult`
- Summarizes one worker result in batch mode, including success/failure and printable stats.

### `LabelCollisionIndex`
- Spatial hash used to detect label box collisions efficiently in PDF coordinate space.

## Function groups and expectations

## 1. CLI and orchestration

### `build_arg_parser()`
- Builds the full CLI contract for the application.
- Must stay in sync with `README.md` and `runbook.md`.
- Expected to define single-file mode, batch mode, mystery options, grayscale options, geometry options, and profiling options.

### `resolve_single_output_path(input_svg, explicit_output)`
- Resolves the target PDF path for single-file mode.
- Defaults to `output/<stem>_paint_by_numbers.pdf` when no explicit path is provided.

### `collect_svg_inputs(input_dir)`
- Enumerates `.svg` files in a batch input directory.
- Expected to return a stable sorted list.

### `resolve_batch_worker_count(requested_workers, svg_count)`
- Chooses the effective number of worker processes for batch runs.
- Must reject values below 1 and keep the count bounded by the amount of work and available CPU.

### `build_batch_worker_args(args)`
- Converts CLI args into a worker-safe dictionary for multiprocessing.
- Expected to resolve paths before dispatch and suppress noisy worker output.

### `run_batch_job(svg_path_str, output_pdf_str, worker_args)`
- Worker entrypoint for one batch item.
- Must register fonts, call `convert()`, and turn exceptions into `BatchJobResult` instead of crashing the pool.

### `make_batch_output_dir(input_dir)`
- Creates a unique timestamped `pdf-output-*` directory under the batch input folder.
- Expected to avoid collisions by appending numeric suffixes if needed.

### `run_single_file(input_svg, args)`
- Single-file CLI execution path.
- Validates file extension, registers font, invokes `convert()`, and prints a human-readable summary.

### `run_batch_directory(input_dir, args)`
- Batch CLI execution path.
- Validates directory content, creates the output folder, chooses serial or parallel mode, and prints progress plus per-file results.

### `main(argv=None)`
- Top-level process entrypoint.
- Resolves input path, validates grayscale args, then dispatches to file or directory mode.

## 2. Helpers, parsing, and style normalization

### `format_elapsed(seconds)`
- Formats elapsed time into readable CLI text.

### `log_step(message, args=None)`
- Emits timestamped user-facing progress lines unless quiet worker mode is enabled.

### `cli_help_hint()`, `cli_input_examples()`, `build_help_epilog()`
- Provide reusable CLI help snippets.
- Expected to stay aligned with the actual parser and supported examples.

### `local_name(tag)`
- Extracts the local XML name from namespaced SVG tags.

### `parse_style_attribute(style_attr)`
- Parses inline CSS style strings into a normalized dict.

### `parse_float(value, default=0.0)`
- Safe numeric parsing helper for SVG attributes.

### `parse_svg_length(value, default=0.0)`
- Parses SVG length text into float values.
- Expected to tolerate unit suffixes used by exported SVG files.

### `register_montserrat_font(font_path)`
- Registers the font used for label rendering with ReportLab.
- Must fail clearly if the TTF file is missing or invalid.

### `merge_style(parent, elem)`
- Merges parent style information with the current XML element.
- Important for inherited fill/stroke values.

### `parse_css_number(value)`
- Parses CSS numeric tokens, including percentage-like values where relevant.

### `parse_channel_value(token)`
- Parses a single color channel from CSS color text.

### `parse_color_value(value)`
- Parses SVG/CSS color values into RGBA-like components.
- Expected to reject unsupported or empty color values safely.

### `rgb_to_hex(rgb)`
- Converts integer RGB tuples into normalized `#RRGGBB` strings.

### `resolve_color(style, channel)`
- Resolves `fill` or `stroke` color from merged style information.
- Expected to honor opacity and visibility rules used by the script.

### `parse_points_list(points_text)`
- Parses polygon/polyline point strings into coordinate tuples.

## 3. SVG geometry extraction

### `build_rect_path(elem)`
- Converts SVG `rect` into path data.

### `build_circle_path(elem)`
- Converts SVG `circle` into path data.

### `build_ellipse_path(elem)`
- Converts SVG `ellipse` into path data.

### `build_polygon_path(elem, close)`
- Converts SVG `polygon` or `polyline` into path data.

### `build_line_path(elem)`
- Converts SVG `line` into path data.

### `element_to_path_data(elem, tag)`
- Dispatches supported SVG element types into a unified path-data representation.

### `collect_svg_shapes(root)`
- Walks the SVG tree, filters supported drawable nodes, merges style, and produces `SvgShape` objects.
- Expected to ignore non-drawable, invisible, or unsupported elements.

### `parse_view_box(root)`
- Reads the SVG viewBox when present.

### `compute_paths_bbox(shapes)`
- Computes a geometry-derived bounding box when the source SVG lacks a usable view box.

### `read_svg(svg_path)`
- Main SVG loading function.
- Must parse XML, validate the root element, collect shapes, and resolve a usable view box.
- Expected failure modes: malformed XML, missing drawable shapes, or missing dimensions.

## 4. Path sampling and polygon construction

### `is_subpath_closed(path)`
- Detects whether a sampled path should be treated as a closed region.

### `sample_segment_points(segment, max_step)`
- Samples SVG line, bezier, and arc segments into points suitable for polygon construction.

### `cleaned_coords(points)`
- Removes degenerate or duplicate coordinate samples before polygon creation.

### `safe_make_valid(geometry)`
- Repairs invalid Shapely geometry when possible.
- Important defensive layer before intersections and unions.

### `iter_polygons(geometry)`
- Iterates polygon outputs from single or multi-part Shapely geometry.

### `path_to_fill_polygons(path, max_step, min_area)`
- Converts closed SVG path geometry into fill polygons.
- Expected to discard empty or too-small polygons.

### `path_to_stroke_polygons(path, stroke_width, max_step, min_area)`
- Converts stroke-only geometry into buffered polygons when `--include-strokes` is enabled.

### `bounds_overlap(...)`
- Fast bounding-box precheck used to skip expensive geometry work.

### `build_zones(shapes, include_strokes, max_step, min_area)`
- Builds the list of `ColorZone` objects from parsed shapes.
- Expected to include fill regions by default and stroke-only regions only when requested.

## 5. Color handling and palette generation

### `color_sort_key(color_hex)`
- Produces a hue-based sort key so the legend is chromatically ordered.

### `build_color_labels(palette)`
- Assigns one-character symbols to each palette color.
- Must fail when the number of colors exceeds the available symbol set.

### `color_distance_to_black(color_hex)`
- Computes a simple distance metric used to identify the nearest black-like color.

### `normalize_nearest_black(zones)`
- Ensures the palette includes pure black by rewriting the nearest dark color to `#000000` if necessary.

### `legend_text_color_for_background(color_hex)`
- Chooses black or white legend text for contrast against the swatch background.

## 6. Mystery pattern and obfuscation flow

### `transform_geometry_to_view_box(geometry, source_view_box, target_view_box, fit_mode)`
- Fits mystery pattern geometry from its own view box into the drawing view box.
- Expected fit modes: `contain`, `cover`, `stretch`.

### `load_mystery_pattern(pattern_svg, target_view_box, max_step, fit_mode)`
- Reads the pattern SVG, converts fill shapes into polygons, transforms them into the target drawing space, and builds a spatial index.
- Must fail clearly if the pattern yields no usable cells.

### `fragment_zone_by_cells(zone, candidate_cells, min_fragment_area, min_fragment_ratio, max_fragments_per_zone, stats=None)`
- Intersects one color zone against candidate pattern cells.
- Expected to reject tiny fragments and back out to the original zone when splitting is not worthwhile.

### `apply_mystery_pattern(zones, pattern_data, min_fragment_area, min_fragment_ratio, max_fragments_per_zone)`
- Applies mystery fragmentation across all zones and returns split zones plus drawable boundary geometry.
- Expected to use bounding-box and STRtree filtering to keep intersection cost manageable.

## 7. Layout and PDF drawing helpers

### `build_layout(page_width, page_height, view_box, legend_height)`
- Computes a `LayoutTransform` that fits the drawing into printable A4 space above the legend.
- Expected to rotate wide art clockwise for a better fit.

### `quadratic_to_cubic(segment)`
- Converts a quadratic bezier to cubic control points for ReportLab path drawing.

### `draw_black_outline(pdf, shape, transform, line_width, outline_gray)`
- Draws the source artwork as grayscale line work in PDF space.

### `iter_line_strings(geometry)`
- Iterates Shapely line-like geometry segments.

### `draw_line_geometry(pdf, geometry, transform, line_width, stroke_gray)`
- Draws Shapely line geometry, used mainly for mystery boundaries.

### `compute_legend_height(color_count)`
- Estimates how much vertical space the legend needs.

### `draw_legend(pdf, palette, color_to_label, page_width, legend_height, show_hex)`
- Draws the bottom legend with swatches, labels, and optional HEX values.

### `render_pdf(...)`
- Full rendering orchestrator for one output PDF.
- Expected to render outline, optional mystery boundaries, labels, legend, and save the PDF.
- Returns `(placed, skipped)` label counts.

## 8. Label placement internals

### `pick_polygon_for_label(geometry)`
- Chooses the polygon component that should receive a label.

### `interior_point_for_polygon(polygon)`
- Computes a stable interior point, typically using `polylabel` with fallback behavior.

### `candidate_points_for_polygon(polygon)`
- Produces candidate points for label placement beyond the primary interior point.

### `label_pdf_metrics(label, font_size)`
- Computes text metrics in PDF space for one label at one font size.

### `label_box_in_svg(...)`
- Converts a text box into SVG-space bounds for containment tests.

### `label_dimensions_in_svg(...)`
- Computes SVG-space label dimensions from PDF metrics.

### `label_bounds_around_point(point, width_svg, height_svg)`
- Builds a rectangle around a candidate point for containment checks.

### `bounds_can_contain_rect(...)`
- Fast bounding-box rejection before more expensive polygon checks.

### `size_fits_within_bounds(...)`
- Checks whether a label size can fit inside a target bounding box.

### `cap_font_size_by_bounds(...)`
- Limits the chosen font size according to the available geometry.

### `label_fits_inside_polygon(...)`
- Verifies that a candidate label rectangle remains inside the chosen region.

### `label_box_in_pdf(...)`
- Converts a resolved placement into PDF-space collision bounds.

### `center_point_for_fallback(polygon)`
- Generates a final fallback center when strict containment fails.

### `boxes_overlap(...)`
- Collision helper used by `LabelCollisionIndex`.

### `collides_with_existing(...)`
- Checks a candidate box against already placed labels.

### `label_placement(...)`
- Main algorithm for finding a label point and font size for one zone.
- Expected behavior: try interior candidates first, avoid collisions, probe alternatives, and finally fall back to a centered tiny label if necessary.

### `draw_labels(...)`
- Iterates zones, resolves label placement, draws labels, updates diagnostics and progress, and returns placed/skipped counts.

## 9. Diagnostics and profiling

### `log_stage_timing(...)`
- Prints stage timing summaries in a consistent format.

### `format_ratio(part, total)`
- Formats ratios for logs.

### `parse_elapsed_text(text)`
- Parses elapsed-time text back into numeric seconds.

### `load_stage_estimates_from_logs(output_pdf)`
- Loads historical stage durations from prior test logs to improve ETA estimates.

### `build_progress_steps(args)`
- Produces the ordered list of progress steps for the current run configuration.

### `log_batch_progress(...)`
- Prints overall batch progress and ETA information.

### `make_test_log_path(output_pdf)`
- Computes the profiling log path written in `--test` mode.

### `build_test_log_text(...)`
- Generates the text body for profiling log files.

### `profiled_total_elapsed(stage_diagnostics)`
- Sums recorded stage durations into a profiled total.

### `write_test_log(input_svg, output_pdf, result, args)`
- Writes a human-readable diagnostic log adjacent to the generated PDF.

## 10. High-level conversion contract

### `convert(svg_path, output_pdf, args)`
- Main pure conversion orchestrator for one source SVG.
- Expected order:
  1. read SVG
  2. build zones
  3. normalize black
  4. optional mystery load/apply
  5. build palette
  6. render PDF
  7. optionally write test log
- Returns `ConvertResult`.

## Behavioral invariants to preserve

- The output remains a single-page A4 PDF.
- Color references stay single-character labels.
- Pure black should always be present in the palette after normalization.
- Fallback label placement may allow the glyph to leave the zone rather than dropping the reference entirely.
- Batch mode should keep working in both serial and multiprocessing modes.
- User-visible help and error messages should remain Spanish unless the project intentionally changes language.

## AST Inventory

This section is generated automatically from the Python AST so the symbol inventory stays fresh even if the hand-written explanations lag behind.
Use `python scripts/generate_python_map.py --write` after adding, removing, or renaming top-level classes/functions.

<!-- BEGIN AUTOGENERATED: python-map -->
_Generated from `svg_to_paint_by_numbers_pdf.py` by `scripts/generate_python_map.py`. Do not edit this block by hand._

- Top-level classes: `18`
- Top-level functions: `98`

### Classes
- `CleanHelpFormatter` (`svg_to_paint_by_numbers_pdf.py:95`)
  - doc: -
- `SvgToPdfError` (`svg_to_paint_by_numbers_pdf.py:102`)
  - doc: Domain error for conversion failures.
- `SvgShape` (`svg_to_paint_by_numbers_pdf.py:159`) | decorators: dataclass
  - doc: Parsed SVG shape with style information.
  - fields: `path`, `fill_color`, `stroke_color`, `stroke_width`, `fill_rule`
- `ColorZone` (`svg_to_paint_by_numbers_pdf.py:170`) | decorators: dataclass
  - doc: Single region that should receive a number.
  - fields: `color_hex`, `geometry`
- `LayoutTransform` (`svg_to_paint_by_numbers_pdf.py:178`) | decorators: dataclass
  - doc: Coordinate mapping from SVG space to PDF space.
  - fields: `svg_min_x`, `svg_min_y`, `svg_width`, `svg_height`, `scale`, `draw_x`, `draw_y`, `offset_x`, `offset_y`, `scaled_width`, `scaled_height`, `rotate_clockwise`
  - methods: `effective_svg_dimensions()`, `label_dimensions_in_svg()`, `map_xy()`
- `LabelPlacement` (`svg_to_paint_by_numbers_pdf.py:230`) | decorators: dataclass
  - doc: Resolved label placement for a zone label.
  - fields: `point`, `font_size`, `text_width_pdf`, `ascent_pdf`, `descent_pdf`, `center_pdf_x`, `center_pdf_y`, `box_pdf`, `used_fallback`, `fits_inside_region`
- `MysteryPatternData` (`svg_to_paint_by_numbers_pdf.py:246`) | decorators: dataclass
  - doc: Prepared pattern cells and drawable internal boundaries.
  - fields: `cells`, `boundary_lines`, `cell_tree`
- `MysterySplitStats` (`svg_to_paint_by_numbers_pdf.py:255`) | decorators: dataclass
  - doc: Diagnostics for mystery-pattern fragmentation.
  - fields: `zones_before`, `split_attempts`, `bbox_skips`, `fragments_generated`, `fragments_kept`, `rejected_small`, `rejected_ratio`, `zones_unsplit_too_few`, `zones_unsplit_over_limit`, `zones_split`, `zones_after`
- `DynamicObfuscationStats` (`svg_to_paint_by_numbers_pdf.py:272`) | decorators: dataclass
  - doc: Diagnostics for SVG-derived obfuscation overlays.
  - fields: `source_lines`, `fragments_attempted`, `fragments_kept`, `variants_kept`
- `StageDiagnostics` (`svg_to_paint_by_numbers_pdf.py:282`) | decorators: dataclass
  - doc: Accumulates per-stage timing data for a conversion.
  - fields: `timings`
  - methods: `record()`
- `ProgressStep` (`svg_to_paint_by_numbers_pdf.py:292`) | decorators: dataclass
  - doc: -
  - fields: `key`, `label`
- `ActiveProgress` (`svg_to_paint_by_numbers_pdf.py:298`) | decorators: dataclass
  - doc: -
  - fields: `key`, `label`, `started_at`, `total_items`, `completed_items`, `unit_label`, `detail_label`, `detail_completed`, `last_percent_reported`
- `CliProgressReporter` (`svg_to_paint_by_numbers_pdf.py:310`)
  - doc: -
  - methods: `__init__()`, `start_step()`, `advance_items()`, `advance_detail()`, `complete_step()`, `render()`, ...
- `LabelZoneProfile` (`svg_to_paint_by_numbers_pdf.py:478`) | decorators: dataclass
  - doc: Detailed timing snapshot for a single zone label-placement attempt.
  - fields: `zone_index`, `color_hex`, `label`, `area`, `elapsed`, `result`, `font_sizes_tried`, `base_candidates`, `direct_candidate_checks`, `grid_candidate_checks`, `collision_rejects`, `used_fallback`, `used_grid`
- `LabelRenderDiagnostics` (`svg_to_paint_by_numbers_pdf.py:497`) | decorators: dataclass
  - doc: Aggregated timings and counters for render-labels internals.
  - fields: `timings`, `counters`, `slowest_zones`, `max_slowest_zones`
  - methods: `add_time()`, `inc()`, `add_zone_profile()`
- `ConvertResult` (`svg_to_paint_by_numbers_pdf.py:519`) | decorators: dataclass
  - doc: Final conversion summary and diagnostics.
  - fields: `palette_count`, `labels_placed`, `labels_skipped`, `stage_diagnostics`, `label_diagnostics`, `log_file_path`
- `BatchJobResult` (`svg_to_paint_by_numbers_pdf.py:531`) | decorators: dataclass
  - doc: -
  - fields: `svg_name`, `output_pdf_name`, `elapsed_text`, `ok`, `palette_count`, `labels_placed`, `labels_skipped`, `log_file_name`, `error_message`
- `LabelCollisionIndex` (`svg_to_paint_by_numbers_pdf.py:544`) | decorators: dataclass
  - doc: Spatial hash for placed label boxes in PDF coordinates.
  - fields: `cell_size`, `buckets`
  - methods: `_bucket_range()`, `collides()`, `add()`

### Functions
- `format_elapsed(seconds: float) -> str` (`svg_to_paint_by_numbers_pdf.py:106`)
  - doc: -
- `log_step(message: str, args: Optional[argparse.Namespace] = None) -> None` (`svg_to_paint_by_numbers_pdf.py:117`)
  - doc: -
- `cli_help_hint() -> str` (`svg_to_paint_by_numbers_pdf.py:128`)
  - doc: -
- `cli_input_examples() -> str` (`svg_to_paint_by_numbers_pdf.py:132`)
  - doc: -
- `build_help_epilog() -> str` (`svg_to_paint_by_numbers_pdf.py:139`)
  - doc: -
- `log_stage_timing(stage_name: str, elapsed: float, args: Optional[argparse.Namespace] = None, **metrics: object) -> None` (`svg_to_paint_by_numbers_pdf.py:580`)
  - doc: -
- `format_ratio(part: float, total: float) -> str` (`svg_to_paint_by_numbers_pdf.py:591`)
  - doc: -
- `parse_elapsed_text(text: str) -> Optional[float]` (`svg_to_paint_by_numbers_pdf.py:597`)
  - doc: -
- `load_stage_estimates_from_logs(output_pdf: Path) -> Dict[str, float]` (`svg_to_paint_by_numbers_pdf.py:617`)
  - doc: -
- `build_progress_steps(args: argparse.Namespace) -> List[ProgressStep]` (`svg_to_paint_by_numbers_pdf.py:649`)
  - doc: -
- `log_batch_progress(*, batch_started_at: float, files_total: int, files_completed: int, current_file: Optional[str] = None, current_file_index: Optional[int] = None) -> None` (`svg_to_paint_by_numbers_pdf.py:675`)
  - doc: -
- `make_test_log_path(output_pdf: Path) -> Path` (`svg_to_paint_by_numbers_pdf.py:697`)
  - doc: -
- `build_test_log_text(*, input_svg: Path, output_pdf: Path, result: ConvertResult, args: argparse.Namespace) -> str` (`svg_to_paint_by_numbers_pdf.py:707`)
  - doc: -
- `profiled_total_elapsed(stage_diagnostics: StageDiagnostics) -> float` (`svg_to_paint_by_numbers_pdf.py:779`)
  - doc: -
- `write_test_log(*, input_svg: Path, output_pdf: Path, result: ConvertResult, args: argparse.Namespace) -> Path` (`svg_to_paint_by_numbers_pdf.py:794`)
  - doc: -
- `local_name(tag: str) -> str` (`svg_to_paint_by_numbers_pdf.py:807`)
  - doc: -
- `parse_style_attribute(style_attr: Optional[str]) -> Dict[str, str]` (`svg_to_paint_by_numbers_pdf.py:813`)
  - doc: -
- `parse_float(value: Optional[str], default: float = 0.0) -> float` (`svg_to_paint_by_numbers_pdf.py:828`)
  - doc: -
- `parse_svg_length(value: Optional[str], default: float = 0.0) -> float` (`svg_to_paint_by_numbers_pdf.py:842`)
  - doc: -
- `register_montserrat_font(font_path: Path) -> None` (`svg_to_paint_by_numbers_pdf.py:866`)
  - doc: -
- `merge_style(parent: Dict[str, str], elem: ET.Element) -> Dict[str, str]` (`svg_to_paint_by_numbers_pdf.py:885`)
  - doc: -
- `parse_css_number(value: str) -> float` (`svg_to_paint_by_numbers_pdf.py:895`)
  - doc: -
- `parse_channel_value(token: str) -> int` (`svg_to_paint_by_numbers_pdf.py:902`)
  - doc: -
- `parse_color_value(value: Optional[str]) -> Optional[Tuple[int, int, int, float]]` (`svg_to_paint_by_numbers_pdf.py:925`)
  - doc: -
- `rgb_to_hex(rgb: Tuple[int, int, int]) -> str` (`svg_to_paint_by_numbers_pdf.py:984`)
  - doc: -
- `resolve_color(style: Dict[str, str], channel: str) -> Optional[str]` (`svg_to_paint_by_numbers_pdf.py:988`)
  - doc: -
- `parse_points_list(points_text: Optional[str]) -> List[Tuple[float, float]]` (`svg_to_paint_by_numbers_pdf.py:1008`)
  - doc: -
- `build_rect_path(elem: ET.Element) -> Optional[str]` (`svg_to_paint_by_numbers_pdf.py:1019`)
  - doc: -
- `build_circle_path(elem: ET.Element) -> Optional[str]` (`svg_to_paint_by_numbers_pdf.py:1052`)
  - doc: -
- `build_ellipse_path(elem: ET.Element) -> Optional[str]` (`svg_to_paint_by_numbers_pdf.py:1065`)
  - doc: -
- `build_polygon_path(elem: ET.Element, close: bool) -> Optional[str]` (`svg_to_paint_by_numbers_pdf.py:1079`)
  - doc: -
- `build_line_path(elem: ET.Element) -> Optional[str]` (`svg_to_paint_by_numbers_pdf.py:1092`)
  - doc: -
- `element_to_path_data(elem: ET.Element, tag: str) -> Optional[str]` (`svg_to_paint_by_numbers_pdf.py:1102`)
  - doc: -
- `collect_svg_shapes(root: ET.Element) -> List[SvgShape]` (`svg_to_paint_by_numbers_pdf.py:1120`)
  - doc: -
- `is_subpath_closed(path: SvgPath) -> bool` (`svg_to_paint_by_numbers_pdf.py:1160`)
  - doc: -
- `sample_segment_points(segment, max_step: float) -> List[complex]` (`svg_to_paint_by_numbers_pdf.py:1173`)
  - doc: -
- `cleaned_coords(points: Sequence[complex]) -> List[Tuple[float, float]]` (`svg_to_paint_by_numbers_pdf.py:1182`)
  - doc: -
- `safe_make_valid(geometry)` (`svg_to_paint_by_numbers_pdf.py:1193`)
  - doc: -
- `iter_polygons(geometry) -> Iterator[Polygon]` (`svg_to_paint_by_numbers_pdf.py:1212`)
  - doc: -
- `path_to_fill_polygons(path: SvgPath, max_step: float, min_area: float) -> List[Polygon]` (`svg_to_paint_by_numbers_pdf.py:1228`)
  - doc: -
- `path_to_stroke_polygons(path: SvgPath, stroke_width: float, max_step: float, min_area: float) -> List[Polygon]` (`svg_to_paint_by_numbers_pdf.py:1255`)
  - doc: -
- `subpath_to_line(subpath: SvgPath, max_step: float) -> Optional[LineString]` (`svg_to_paint_by_numbers_pdf.py:1289`)
  - doc: -
- `stable_unit_value(*parts: object) -> float` (`svg_to_paint_by_numbers_pdf.py:1313`)
  - doc: -
- `normalize_line_like_geometry(geometry) -> List[LineString]` (`svg_to_paint_by_numbers_pdf.py:1319`)
  - doc: -
- `build_dynamic_obfuscation(shapes: Sequence[SvgShape], zones: Sequence[ColorZone], max_step: float, spacing: float, offset: float, density: float, min_length: float) -> Tuple[Optional[object], DynamicObfuscationStats]` (`svg_to_paint_by_numbers_pdf.py:1332`)
  - doc: -
- `bounds_overlap(bounds_a: Tuple[float, float, float, float], bounds_b: Tuple[float, float, float, float]) -> bool` (`svg_to_paint_by_numbers_pdf.py:1435`)
  - doc: -
- `parse_view_box(root: ET.Element) -> Optional[Tuple[float, float, float, float]]` (`svg_to_paint_by_numbers_pdf.py:1449`)
  - doc: -
- `compute_paths_bbox(shapes: Sequence[SvgShape]) -> Optional[Tuple[float, float, float, float]]` (`svg_to_paint_by_numbers_pdf.py:1462`)
  - doc: -
- `read_svg(svg_path: Path) -> Tuple[List[SvgShape], Tuple[float, float, float, float]]` (`svg_to_paint_by_numbers_pdf.py:1489`)
  - doc: -
- `color_sort_key(color_hex: str) -> Tuple[float, float, float, str]` (`svg_to_paint_by_numbers_pdf.py:1529`)
  - doc: -
- `build_color_labels(palette: Sequence[str]) -> Dict[str, str]` (`svg_to_paint_by_numbers_pdf.py:1542`)
  - doc: -
- `color_distance_to_black(color_hex: str) -> int` (`svg_to_paint_by_numbers_pdf.py:1551`)
  - doc: -
- `normalize_nearest_black(zones: Sequence[ColorZone]) -> List[ColorZone]` (`svg_to_paint_by_numbers_pdf.py:1558`)
  - doc: -
- `legend_text_color_for_background(color_hex: str) -> colors.Color` (`svg_to_paint_by_numbers_pdf.py:1576`)
  - doc: -
- `build_zones(shapes: Sequence[SvgShape], include_strokes: bool, max_step: float, min_area: float) -> List[ColorZone]` (`svg_to_paint_by_numbers_pdf.py:1586`)
  - doc: -
- `transform_geometry_to_view_box(geometry, source_view_box: Tuple[float, float, float, float], target_view_box: Tuple[float, float, float, float], fit_mode: str)` (`svg_to_paint_by_numbers_pdf.py:1617`)
  - doc: -
- `load_mystery_pattern(pattern_svg: Path, target_view_box: Tuple[float, float, float, float], max_step: float, fit_mode: str) -> MysteryPatternData` (`svg_to_paint_by_numbers_pdf.py:1649`)
  - doc: -
- `fragment_zone_by_cells(zone: ColorZone, candidate_cells: Sequence[Polygon], min_fragment_area: float, min_fragment_ratio: float, max_fragments_per_zone: int, stats: Optional[MysterySplitStats] = None) -> List[ColorZone]` (`svg_to_paint_by_numbers_pdf.py:1678`)
  - doc: -
- `apply_mystery_pattern(zones: Sequence[ColorZone], pattern_data: MysteryPatternData, min_fragment_area: float, min_fragment_ratio: float, max_fragments_per_zone: int) -> Tuple[List[ColorZone], Optional[object], MysterySplitStats]` (`svg_to_paint_by_numbers_pdf.py:1729`)
  - doc: -
- `build_layout(page_width: float, page_height: float, view_box: Tuple[float, float, float, float], legend_height: float) -> LayoutTransform` (`svg_to_paint_by_numbers_pdf.py:1777`)
  - doc: -
- `quadratic_to_cubic(segment: QuadraticBezier) -> Tuple[complex, complex]` (`svg_to_paint_by_numbers_pdf.py:1820`)
  - doc: -
- `draw_black_outline(pdf: canvas.Canvas, shape: SvgShape, transform: LayoutTransform, line_width: float, outline_gray: float) -> None` (`svg_to_paint_by_numbers_pdf.py:1829`)
  - doc: -
- `iter_line_strings(geometry) -> Iterator[LineString]` (`svg_to_paint_by_numbers_pdf.py:1880`)
  - doc: -
- `draw_line_geometry(pdf: canvas.Canvas, geometry, transform: LayoutTransform, line_width: float, stroke_gray: float) -> None` (`svg_to_paint_by_numbers_pdf.py:1907`)
  - doc: -
- `pick_polygon_for_label(geometry) -> Optional[Polygon]` (`svg_to_paint_by_numbers_pdf.py:1937`)
  - doc: -
- `interior_point_for_polygon(polygon: Polygon) -> Optional[Point]` (`svg_to_paint_by_numbers_pdf.py:1946`)
  - doc: -
- `candidate_points_for_polygon(polygon: Polygon) -> List[Point]` (`svg_to_paint_by_numbers_pdf.py:1961`)
  - doc: -
- `label_pdf_metrics(label: str, font_size: float) -> Tuple[float, float, float]` (`svg_to_paint_by_numbers_pdf.py:2011`)
  - doc: -
- `label_box_in_svg(point: Point, text_width_pdf: float, ascent_pdf: float, descent_pdf: float, transform: LayoutTransform, padding_pdf: float) -> Polygon` (`svg_to_paint_by_numbers_pdf.py:2019`)
  - doc: -
- `label_dimensions_in_svg(text_width_pdf: float, ascent_pdf: float, descent_pdf: float, transform: LayoutTransform, padding_pdf: float) -> Tuple[float, float]` (`svg_to_paint_by_numbers_pdf.py:2043`)
  - doc: -
- `label_bounds_around_point(point: Point, width_svg: float, height_svg: float) -> Tuple[float, float, float, float]` (`svg_to_paint_by_numbers_pdf.py:2058`)
  - doc: -
- `bounds_can_contain_rect(outer_bounds: Tuple[float, float, float, float], rect_bounds: Tuple[float, float, float, float]) -> bool` (`svg_to_paint_by_numbers_pdf.py:2069`)
  - doc: -
- `size_fits_within_bounds(width_svg: float, height_svg: float, outer_bounds: Tuple[float, float, float, float]) -> bool` (`svg_to_paint_by_numbers_pdf.py:2083`)
  - doc: -
- `cap_font_size_by_bounds(label: str, requested_min_size: float, requested_max_size: float, bounds: Tuple[float, float, float, float], transform: LayoutTransform) -> Optional[float]` (`svg_to_paint_by_numbers_pdf.py:2090`)
  - doc: -
- `label_fits_inside_polygon(target_geometry, prepared_target_geometry, point: Point, width_svg: float, height_svg: float, target_bounds: Tuple[float, float, float, float]) -> bool` (`svg_to_paint_by_numbers_pdf.py:2113`)
  - doc: -
- `label_box_in_pdf(point: Point, text_width_pdf: float, ascent_pdf: float, descent_pdf: float, transform: LayoutTransform, padding_pdf: float) -> Tuple[float, float, float, float, float, float]` (`svg_to_paint_by_numbers_pdf.py:2131`)
  - doc: -
- `center_point_for_fallback(polygon: Polygon) -> Point` (`svg_to_paint_by_numbers_pdf.py:2154`)
  - doc: -
- `boxes_overlap(box_a: Tuple[float, float, float, float], box_b: Tuple[float, float, float, float], gap: float) -> bool` (`svg_to_paint_by_numbers_pdf.py:2172`)
  - doc: -
- `collides_with_existing(box_pdf: Tuple[float, float, float, float], collision_index: LabelCollisionIndex) -> bool` (`svg_to_paint_by_numbers_pdf.py:2190`)
  - doc: -
- `label_placement(geometry: Polygon, label: str, color_hex: str, zone_index: int, transform: LayoutTransform, collision_index: LabelCollisionIndex, min_font_size: float, max_font_size: float, diagnostics: Optional[LabelRenderDiagnostics] = None, progress: Optional[CliProgressReporter] = None) -> Optional[LabelPlacement]` (`svg_to_paint_by_numbers_pdf.py:2197`)
  - doc: -
- `draw_labels(pdf: canvas.Canvas, zones: Sequence[ColorZone], color_to_label: Dict[str, str], transform: LayoutTransform, min_font_size: float, max_font_size: float, number_gray: float, diagnostics: Optional[LabelRenderDiagnostics] = None, progress: Optional[CliProgressReporter] = None) -> Tuple[int, int]` (`svg_to_paint_by_numbers_pdf.py:2528`)
  - doc: -
- `compute_legend_height(color_count: int) -> float` (`svg_to_paint_by_numbers_pdf.py:2617`)
  - doc: -
- `draw_legend(pdf: canvas.Canvas, palette: Sequence[str], color_to_label: Dict[str, str], page_width: float, legend_height: float, show_hex: bool) -> None` (`svg_to_paint_by_numbers_pdf.py:2625`)
  - doc: -
- `render_pdf(output_pdf: Path, shapes: Sequence[SvgShape], zones: Sequence[ColorZone], view_box: Tuple[float, float, float, float], palette: Sequence[str], color_to_label: Dict[str, str], min_font_size: float, max_font_size: float, line_width: float, show_hex: bool, outline_gray: float, number_gray: float, mystery_boundaries = None, mystery_boundary_gray: float = DEFAULT_MYSTERY_BOUNDARY_GRAY, mystery_boundary_width: float = DEFAULT_MYSTERY_BOUNDARY_WIDTH, dynamic_obfuscation_lines = None, dynamic_obfuscation_gray: float = DEFAULT_DYNAMIC_OBFUSCATION_GRAY, dynamic_obfuscation_width: float = DEFAULT_DYNAMIC_OBFUSCATION_WIDTH, args: Optional[argparse.Namespace] = None, diagnostics: Optional[StageDiagnostics] = None, label_diagnostics: Optional[LabelRenderDiagnostics] = None, progress: Optional[CliProgressReporter] = None) -> Tuple[int, int]` (`svg_to_paint_by_numbers_pdf.py:2713`)
  - doc: -
- `convert(svg_path: Path, output_pdf: Path, args: argparse.Namespace) -> ConvertResult` (`svg_to_paint_by_numbers_pdf.py:2873`)
  - doc: -
- `build_arg_parser() -> argparse.ArgumentParser` (`svg_to_paint_by_numbers_pdf.py:3105`)
  - doc: -
- `resolve_single_output_path(input_svg: Path, explicit_output: Optional[str]) -> Path` (`svg_to_paint_by_numbers_pdf.py:3322`)
  - doc: -
- `collect_svg_inputs(input_dir: Path) -> List[Path]` (`svg_to_paint_by_numbers_pdf.py:3328`)
  - doc: -
- `resolve_batch_worker_count(requested_workers: Optional[int], svg_count: int) -> int` (`svg_to_paint_by_numbers_pdf.py:3333`)
  - doc: -
- `build_batch_worker_args(args: argparse.Namespace) -> Dict[str, object]` (`svg_to_paint_by_numbers_pdf.py:3345`)
  - doc: -
- `run_batch_job(svg_path_str: str, output_pdf_str: str, worker_args: Dict[str, object]) -> BatchJobResult` (`svg_to_paint_by_numbers_pdf.py:3355`)
  - doc: -
- `make_batch_output_dir(input_dir: Path) -> Path` (`svg_to_paint_by_numbers_pdf.py:3393`)
  - doc: -
- `run_single_file(input_svg: Path, args: argparse.Namespace) -> int` (`svg_to_paint_by_numbers_pdf.py:3406`)
  - doc: -
- `run_batch_directory(input_dir: Path, args: argparse.Namespace) -> int` (`svg_to_paint_by_numbers_pdf.py:3450`)
  - doc: -
- `validate_gray_value(value: float, label: str) -> float` (`svg_to_paint_by_numbers_pdf.py:3615`)
  - doc: -
- `validate_positive_value(value: float, label: str, *, allow_zero: bool = False) -> float` (`svg_to_paint_by_numbers_pdf.py:3627`)
  - doc: -
- `resolve_representation_grays(override_pair: Optional[Sequence[float]]) -> Tuple[float, float]` (`svg_to_paint_by_numbers_pdf.py:3640`)
  - doc: -
- `main(argv: Optional[Sequence[str]] = None) -> int` (`svg_to_paint_by_numbers_pdf.py:3659`)
  - doc: -
<!-- END AUTOGENERATED: python-map -->
