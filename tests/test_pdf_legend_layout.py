import io
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import svg_to_paint_by_numbers_pdf as pdf_module


class RecordingCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.line_calls = []
        self.rect_calls = []
        self.text_calls = []
        self.current_dash = ()

    def setDash(self, array=[], phase=0):
        if isinstance(array, (int, float)):
            self.current_dash = (array, phase)
        else:
            self.current_dash = (tuple(array), phase)
        return super().setDash(array, phase)

    def line(self, x1, y1, x2, y2):
        self.line_calls.append((x1, y1, x2, y2, self.current_dash))
        return super().line(x1, y1, x2, y2)

    def rect(self, x, y, width, height, stroke=1, fill=0):
        self.rect_calls.append((x, y, width, height, stroke, fill))
        return super().rect(x, y, width, height, stroke=stroke, fill=fill)

    def drawString(self, x, y, text, mode=None, charSpace=0, direction=None, wordSpace=None):
        self.text_calls.append((x, y, text))
        return super().drawString(x, y, text, mode=mode, charSpace=charSpace, direction=direction, wordSpace=wordSpace)


class PdfLegendLayoutTests(unittest.TestCase):
    def test_legend_stays_bottom_aligned_without_title_or_separator(self):
        palette = [
            "#FF0000",
            "#00FF00",
            "#0000FF",
            "#FFFF00",
            "#FF00FF",
            "#00FFFF",
            "#C0C0C0",
            "#808080",
            "#800000",
            "#008000",
        ]
        color_to_label = {color: str(index + 1) for index, color in enumerate(palette)}
        legend_height = pdf_module.compute_legend_height(len(palette))
        page_width, _ = A4

        buffer = io.BytesIO()
        pdf = RecordingCanvas(buffer, pagesize=A4)

        with mock.patch.object(pdf_module, "FONT_NAME", "Helvetica"):
            pdf_module.draw_legend(
                pdf,
                palette=palette,
                color_to_label=color_to_label,
                page_width=page_width,
                legend_height=legend_height,
                show_hex=False,
                test_mode=False,
                image_bottom_y=108.0,
            )

        pdf.showPage()
        pdf.save()

        self.assertGreater(len(buffer.getvalue()), 0)
        self.assertEqual(len(pdf.line_calls), 0)
        self.assertNotIn("Leyenda de colores", [text for _, _, text in pdf.text_calls])

        swatch_rects = [call for call in pdf.rect_calls if call[2] == call[3] and call[5] == 1]
        self.assertEqual(len(swatch_rects), len(palette))

        row_positions = sorted({round(y, 2) for _, y, _, _, _, _ in swatch_rects})
        self.assertLessEqual(len(row_positions), 2)
        self.assertGreaterEqual(min(row_positions), 24.0)
        self.assertLessEqual(min(row_positions), 30.0)

        first_row = sorted((rect for rect in swatch_rects if round(rect[1], 2) == row_positions[0]), key=lambda rect: rect[0])
        gap = first_row[1][0] - (first_row[0][0] + first_row[0][2])
        self.assertAlmostEqual(gap, first_row[0][2] / 4.0, places=2)

    def test_test_mode_draws_dashed_layout_guides(self):
        palette = ["#FF0000", "#00FF00", "#0000FF"]
        color_to_label = {color: str(index + 1) for index, color in enumerate(palette)}
        legend_height = pdf_module.compute_legend_height(len(palette))
        page_width, _ = A4

        buffer = io.BytesIO()
        pdf = RecordingCanvas(buffer, pagesize=A4)

        with mock.patch.object(pdf_module, "FONT_NAME", "Helvetica"):
            pdf_module.draw_legend(
                pdf,
                palette=palette,
                color_to_label=color_to_label,
                page_width=page_width,
                legend_height=legend_height,
                show_hex=False,
                test_mode=True,
                image_bottom_y=108.0,
            )

        pdf.showPage()
        pdf.save()

        self.assertEqual(len(pdf.line_calls), 3)
        for _, _, _, _, dash in pdf.line_calls:
            self.assertEqual(dash, (4, 3))

        y_positions = sorted(round(call[1], 2) for call in pdf.line_calls)
        self.assertEqual(y_positions, [26.0, 94.0, 108.0])

    def test_legend_height_is_capped_to_two_rows(self):
        self.assertEqual(pdf_module.compute_legend_height(1), 72.0)
        self.assertEqual(pdf_module.compute_legend_height(24), 72.0)


class MysteryPatternSelectionTests(unittest.TestCase):
    def test_explicit_mystery_pattern_takes_priority(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            explicit_pattern = Path(tmpdir) / "explicit.svg"
            explicit_pattern.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
            args = pdf_module.argparse.Namespace(
                mystery_pattern=str(explicit_pattern),
                no_random_mystery_pattern=False,
            )

            resolved = pdf_module.resolve_mystery_pattern_for_run(Path("numbers.svg"), args)

            self.assertEqual(resolved, explicit_pattern.resolve())
            self.assertEqual(Path(args.mystery_pattern), explicit_pattern.resolve())

    def test_random_mystery_pattern_is_selected_from_patterns_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pattern_dir = Path(tmpdir)
            selected_pattern = pattern_dir / "selected.svg"
            selected_pattern.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
            (pattern_dir / "other.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
            args = pdf_module.argparse.Namespace(
                mystery_pattern=None,
                no_random_mystery_pattern=False,
            )

            with mock.patch.object(pdf_module, "get_default_patterns_dir", return_value=pattern_dir), \
                 mock.patch.object(pdf_module.random, "choice", return_value=selected_pattern):
                resolved = pdf_module.resolve_mystery_pattern_for_run(Path("numbers.svg"), args)

            self.assertEqual(resolved, selected_pattern)
            self.assertEqual(Path(args.mystery_pattern), selected_pattern)

    def test_random_mystery_pattern_can_be_disabled(self):
        args = pdf_module.argparse.Namespace(
            mystery_pattern=None,
            no_random_mystery_pattern=True,
        )

        resolved = pdf_module.resolve_mystery_pattern_for_run(Path("numbers.svg"), args)

        self.assertIsNone(resolved)
        self.assertIsNone(args.mystery_pattern)


if __name__ == "__main__":
    unittest.main()
