import unittest

from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

import svg_to_paint_by_numbers_pdf as pdf_module


class MysteryPatternBoundaryRenderingTests(unittest.TestCase):
    def test_rejected_split_does_not_draw_pattern_boundaries(self):
        zone = pdf_module.ColorZone(
            color_hex="#FF0000",
            geometry=Polygon([(0, 0), (12, 0), (12, 12), (0, 12)]),
        )
        cells = [
            Polygon([(0, 0), (4, 0), (4, 12), (0, 12)]),
            Polygon([(4, 0), (8, 0), (8, 12), (4, 12)]),
            Polygon([(8, 0), (12, 0), (12, 12), (8, 12)]),
        ]
        pattern_data = pdf_module.MysteryPatternData(
            cells=cells,
            boundary_lines=unary_union([cell.boundary for cell in cells]),
            cell_tree=STRtree(cells),
        )

        zones, mystery_boundaries, stats = pdf_module.apply_mystery_pattern(
            zones=[zone],
            pattern_data=pattern_data,
            min_fragment_area=1.0,
            min_fragment_ratio=0.0,
            max_fragments_per_zone=2,
        )

        self.assertEqual(len(zones), 1)
        self.assertTrue(zones[0].geometry.equals(zone.geometry))
        self.assertIsNone(mystery_boundaries)
        self.assertEqual(stats.zones_unsplit_over_limit, 1)

    def test_accepted_split_keeps_pattern_boundaries_for_split_zone(self):
        zone = pdf_module.ColorZone(
            color_hex="#FF0000",
            geometry=Polygon([(0, 0), (12, 0), (12, 12), (0, 12)]),
        )
        cells = [
            Polygon([(0, 0), (6, 0), (6, 12), (0, 12)]),
            Polygon([(6, 0), (12, 0), (12, 12), (6, 12)]),
        ]
        pattern_data = pdf_module.MysteryPatternData(
            cells=cells,
            boundary_lines=unary_union([cell.boundary for cell in cells]),
            cell_tree=STRtree(cells),
        )

        zones, mystery_boundaries, stats = pdf_module.apply_mystery_pattern(
            zones=[zone],
            pattern_data=pattern_data,
            min_fragment_area=1.0,
            min_fragment_ratio=0.0,
            max_fragments_per_zone=4,
        )

        self.assertEqual(len(zones), 2)
        self.assertIsNotNone(mystery_boundaries)
        self.assertFalse(mystery_boundaries.is_empty)
        self.assertEqual(stats.zones_split, 1)


if __name__ == "__main__":
    unittest.main()
