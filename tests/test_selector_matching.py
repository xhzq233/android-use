from __future__ import annotations

import sys
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from android_use.geometry_types import Rect
from android_use.ui_semantics import (
    clean_tree_text,
    find_target_nodes,
    node_rect,
    select_scroll_node,
)


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

    def test_clean_tree_preserves_id_center_and_supports_filter_and_id_desc_selectors(self) -> None:
        root = parse(
            f"""
            <hierarchy>
              <node package="{PKG}" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
                <node package="{PKG}" class="android.widget.TextView"
                      text="冰透粉" resource-id="{PKG}:id/tv_name"
                      bounds="[351,2038][510,2086]" clickable="true" />
                <node package="{PKG}" class="android.widget.ImageView"
                      content-desc="关闭面板" resource-id="{PKG}:id/iv_close"
                      bounds="[20,100][100,180]" clickable="true" />
                <node package="{PKG}" class="android.widget.TextView"
                      text="其他项" resource-id="{PKG}:id/tv_other"
                      bounds="[100,500][300,600]" />
              </node>
            </hierarchy>
            """
        )
        full_tree = clean_tree_text(root, PKG)
        self.assertIn("- 冰透粉 #tv_name [Text] (351,2038,159,48) @430,2062", full_tree)
        self.assertIn("- 关闭面板 #iv_close [Button] (20,100,80,80) @60,140", full_tree)

        filtered = clean_tree_text(root, PKG, filter_query="iv_close")
        self.assertIn("#iv_close", filtered)
        self.assertNotIn("冰透粉", filtered)

        by_id = find_target_nodes(root, "iv_close", package_name=PKG, field="id")
        self.assertEqual(len(by_id), 1)
        self.assertEqual(node_rect(by_id[0]).center, (60, 140))

        by_desc = find_target_nodes(root, "关闭", package_name=PKG, contains=True, field="desc")
        self.assertEqual(len(by_desc), 1)
        self.assertEqual(node_rect(by_desc[0]).center, (60, 140))


if __name__ == "__main__":
    unittest.main()
