from __future__ import annotations

import sys
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from android_use.geometry_types import Rect
from android_use.ui_semantics import find_target_nodes, node_rect, select_scroll_node


PKG = "com.example.app"


def parse(xml: str) -> ET.Element:
    return ET.fromstring(xml)


class SelectorMatchingTest(unittest.TestCase):
    def test_scroll_container_can_match_dump_label_and_bounds(self) -> None:
        root = parse(
            f"""
            <hierarchy>
              <node package="{PKG}" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
                <node package="{PKG}" class="androidx.recyclerview.widget.RecyclerView"
                      resource-id="{PKG}:id/plugin_item_list"
                      bounds="[0,2100][1080,2300]" scrollable="true" />
              </node>
            </hierarchy>
            """
        )

        short_matches = find_target_nodes(
            root,
            "plugin_item_list [Scroll,horizontal]",
            package_name=PKG,
            scroll_only=True,
        )
        full_matches = find_target_nodes(
            root,
            "plugin_item_list [Scroll,horizontal] (0,2100,1080,200)",
            package_name=PKG,
            scroll_only=True,
        )

        self.assertEqual(len(short_matches), 1)
        self.assertEqual(len(full_matches), 1)

    def test_from_anchor_prefers_directional_scroll_container(self) -> None:
        root = parse(
            f"""
            <hierarchy>
              <node package="{PKG}" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
                <node package="{PKG}" class="android.widget.ScrollView"
                      resource-id="{PKG}:id/page_scroll"
                      bounds="[0,0][1080,2400]" scrollable="true">
                  <node package="{PKG}" class="androidx.recyclerview.widget.RecyclerView"
                        resource-id="{PKG}:id/plugin_item_list"
                        bounds="[0,2000][1080,2200]" scrollable="true">
                    <node package="{PKG}" class="android.widget.ScrollView"
                          resource-id="{PKG}:id/nested_vertical"
                          bounds="[10,2010][100,2190]" scrollable="true">
                      <node package="{PKG}" class="android.widget.TextView"
                            text="一键美化" bounds="[20,2020][80,2080]" />
                    </node>
                  </node>
                </node>
              </node>
            </hierarchy>
            """
        )
        selected = select_scroll_node(root, PKG, "horizontal", 0)

        self.assertEqual(node_rect(selected), Rect(0, 2000, 1080, 200))


if __name__ == "__main__":
    unittest.main()
