"""Account rows and paged overview must fit within terminal height."""
import unittest

from agy_cli_manager.cli import _overview_layout


class OverviewLayoutTests(unittest.TestCase):
    def test_wide_short_terminal_still_uses_two_columns(self):
        layout = _overview_layout(24, 120, 7, 14)
        self.assertEqual(layout['columns'], 2)
        self.assertGreaterEqual(layout['list_rows'], 3)
        self.assertLessEqual(layout['rows'], layout['list_rows'])

    def test_many_pools_never_grow_overview_into_list_or_footer(self):
        for height, width, header in ((24, 80, 9), (24, 120, 7), (40, 160, 6), (18, 80, 8)):
            for count in (4, 14, 100):
                with self.subTest(height=height, width=width, count=count):
                    layout = _overview_layout(height, width, header, count)
                    self.assertGreaterEqual(layout['list_rows'], 1)
                    list_last = header + 2 + layout['list_rows'] - 1
                    self.assertLess(list_last, layout['top'] - 1)
                    self.assertLess(layout['top'] + layout['rows'], height - 2)
                    self.assertLessEqual(layout['rows'], layout['list_rows'])

    def test_pagination_reaches_every_detail_and_clamps_after_resize(self):
        count = 37
        initial = _overview_layout(24, 120, 7, count)
        seen = []
        for page in range(initial['pages']):
            layout = _overview_layout(24, 120, 7, count, page)
            seen.extend(range(layout['start'], min(count, layout['start'] + layout['capacity'])))
        self.assertEqual(seen, list(range(count)))
        layout = _overview_layout(40, 160, 5, 3, 99)
        self.assertEqual(layout['page'], 0)

    def test_tiny_terminal_does_not_allocate_negative_space(self):
        layout = _overview_layout(10, 40, 8, 20)
        self.assertEqual(layout['capacity'], 0)
        self.assertEqual(layout['list_rows'], 0)


if __name__ == '__main__':
    unittest.main()
