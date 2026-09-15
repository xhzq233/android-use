"""Clean raw Android UI XML into compact nodes used by dump, tap, and verification."""

from __future__ import annotations

import re
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from .geometry_types import Rect, format_rect, parse_bounds


TEXT_CLASSES = {
    "android.widget.TextView": "Text",
    "android.widget.CheckedTextView": "Text",
    "com.lynx.tasm.behavior.ui.text.UIText": "Text",
    "com.lynx.tasm.behavior.ui.text.FlattenUIText": "Text",
}

IMAGE_CLASSES = {
    "android.widget.ImageView": "Image",
    "com.lynx.tasm.ui.image.UIImage": "Image",
    "com.lynx.tasm.ui.image.FlattenUIImage": "Image",
    "com.lynx.tasm.ui.image.UIFilterImage": "Image",
}

CONTROL_CLASSES = {
    "android.widget.Button": "Button",
    "android.widget.EditText": "Input",
    "android.widget.Switch": "Switch",
    "android.widget.CheckBox": "CheckBox",
    "android.widget.RadioButton": "RadioButton",
    "android.widget.SeekBar": "SeekBar",
    "android.widget.Spinner": "Spinner",
}

SCROLL_CLASSES = {
    "android.widget.ScrollView",
    "android.widget.HorizontalScrollView",
    "androidx.recyclerview.widget.RecyclerView",
    "android.widget.ListView",
    "android.widget.GridView",
    "com.lynx.tasm.behavior.ui.scroll.UIScrollView",
}

SCROLL_CLASS_NAMES = {
    "UIList2Hooker",
    "LynxFoldSlotDrag",
}


def short_id(resource_id: str | None) -> str:
    if not resource_id:
        return ""
    return resource_id.split("/")[-1]


def short_class(class_name: str | None) -> str:
    if not class_name:
        return ""
    return class_name.split(".")[-1]


def node_text(node: ET.Element) -> str:
    return node.attrib.get("text") or node.attrib.get("content-desc") or ""


def node_label(node: ET.Element) -> str:
    return (
        node_text(node)
        or short_id(node.attrib.get("resource-id"))
        or short_class(node.attrib.get("class"))
        or "node"
    )


def is_scroll_node(node: ET.Element) -> bool:
    cls = node.attrib.get("class", "")
    return (
        node.attrib.get("scrollable") == "true"
        or cls in SCROLL_CLASSES
        or short_class(cls) in SCROLL_CLASS_NAMES
    )


def is_custom_slider_node(node: ET.Element) -> bool:
    cls = node.attrib.get("class", "")
    rid = short_id(node.attrib.get("resource-id")).lower()
    return cls == "android.view.View" and bool(rid) and (
        "slider" in rid or "seekbar" in rid or rid.endswith("_levels_view")
    )


def custom_control_kind_by_id(node: ET.Element) -> str | None:
    rid = short_id(node.attrib.get("resource-id")).lower()
    if not rid:
        return None
    if "switch" in rid:
        return "Switch"
    if "checkbox" in rid or "check_box" in rid or "check-box" in rid:
        return "CheckBox"
    return None


def matches_keep_id_regex(node: ET.Element, patterns: Iterable[re.Pattern[str]]) -> bool:
    resource_id = node.attrib.get("resource-id") or ""
    return bool(resource_id) and any(pattern.search(resource_id) for pattern in patterns)


def node_kind(node: ET.Element) -> str:
    cls = node.attrib.get("class", "")
    if is_scroll_node(node):
        rect = parse_bounds(node.attrib.get("bounds"))
        direction = "horizontal" if rect.w >= rect.h else "vertical"
        return f"Scroll,{direction}"
    if cls == "android.widget.ImageView" and node.attrib.get("clickable") == "true":
        return "Button"
    if cls in ("com.lynx.tasm.behavior.ui.LynxFlattenUI", "com.lynx.tasm.behavior.ui.view.UIView") and node.attrib.get("clickable") == "true":
        return "Button"
    if cls in TEXT_CLASSES:
        return TEXT_CLASSES[cls]
    if cls in IMAGE_CLASSES:
        return IMAGE_CLASSES[cls]
    if cls in CONTROL_CLASSES:
        return CONTROL_CLASSES[cls]
    if is_custom_slider_node(node):
        return "Slider"
    custom_control_kind = custom_control_kind_by_id(node)
    if custom_control_kind:
        return custom_control_kind
    return "-"


def is_atomic_node(node: ET.Element) -> bool:
    cls = node.attrib.get("class", "")
    if is_scroll_node(node):
        return True
    if cls in TEXT_CLASSES:
        return bool(node_text(node) or node.attrib.get("resource-id"))
    if cls in IMAGE_CLASSES:
        return bool(
            node.attrib.get("content-desc")
            or node.attrib.get("resource-id")
            or node.attrib.get("clickable") == "true"
            or node.attrib.get("NAF") == "true"
        )
    if is_custom_slider_node(node):
        return True
    if custom_control_kind_by_id(node):
        return True
    if cls in ("com.lynx.tasm.behavior.ui.LynxFlattenUI", "com.lynx.tasm.behavior.ui.view.UIView") and node.attrib.get("clickable") == "true":
        return True
    return cls in CONTROL_CLASSES


def is_visible(node: ET.Element, screen: Rect) -> bool:
    if node.attrib.get("visible-to-user") == "false":
        return False
    rect = parse_bounds(node.attrib.get("bounds"))
    return rect.w > 0 and rect.h > 0 and rect.intersects(screen)


def iter_nodes(root: ET.Element) -> Iterable[ET.Element]:
    yield from root.iter("node")

def screen_rect(root: ET.Element) -> Rect:
    rects = [parse_bounds(node.attrib.get("bounds")) for node in iter_nodes(root)]
    if not rects:
        return Rect(0, 0, 0, 0)
    return max(rects, key=lambda rect: rect.w * rect.h)


def should_keep_package(node: ET.Element, package_name: str | None, include_system: bool) -> bool:
    if include_system or not package_name:
        return True
    return node.attrib.get("package") == package_name


def node_state(node: ET.Element) -> dict[str, bool]:
    state: dict[str, bool] = {}
    if node.attrib.get("selected") == "true":
        state["selected"] = True
    if node.attrib.get("checked") == "true":
        state["checked"] = True
    elif node.attrib.get("checkable") == "true":
        state["checked"] = False
    if node.attrib.get("enabled") == "false":
        state["enabled"] = False
    return state


def build_atomic_tree(
    node: ET.Element,
    *,
    package_name: str | None,
    include_system: bool,
    screen: Rect,
    keep_id_regexes: Iterable[re.Pattern[str]] = (),
) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for child in list(node):
        children.extend(
            build_atomic_tree(
                child,
                package_name=package_name,
                include_system=include_system,
                screen=screen,
                keep_id_regexes=keep_id_regexes,
            )
        )

    if not should_keep_package(node, package_name, include_system):
        return children
    kept_by_id = matches_keep_id_regex(node, keep_id_regexes)
    is_atomic = is_atomic_node(node) or kept_by_id
    if not is_atomic:
        return children

    kind = node_kind(node)
    if kept_by_id and kind == "-":
        kind = short_class(node.attrib.get("class")) or "View"
    tags = [kind]
    if kept_by_id:
        tags.append("kept")
    if not is_visible(node, screen):
        tags.append("invisible")
    state = node_state(node)
    if state.get("selected"):
        tags.append("selected")
    if "checked" in state:
        tags.append("checked" if state["checked"] else "unchecked")
    if state.get("enabled") is False:
        tags.append("disabled")
    item: dict[str, Any] = {
        "label": node_label(node),
        "kind": kind,
        "tags": tags,
        "bounds": parse_bounds(node.attrib.get("bounds")),
        "children": children,
        "state": state,
        "kept": kept_by_id,
    }
    return [item]


def tree_to_text(items: list[dict[str, Any]], screen: Rect) -> str:
    lines = [f"  root {format_rect(screen)}:"]

    def emit(item: dict[str, Any], depth: int) -> None:
        tags = ",".join(item["tags"])
        lines.append(
            "  " * depth
            + f"- {item['label']} [{tags}] {format_rect(item['bounds'])}"
        )
        for child in item.get("children", []):
            emit(child, depth + 1)

    for item in items:
        emit(item, 2)
    return "\n".join(lines) + "\n"


def node_fingerprint(root: ET.Element, package_name: str | None, include_system: bool) -> tuple[tuple[str, ...], ...]:
    attrs = (
        "text",
        "content-desc",
        "resource-id",
        "class",
        "bounds",
        "clickable",
        "scrollable",
        "enabled",
        "selected",
        "checked",
        "visible-to-user",
    )
    rows: list[tuple[str, ...]] = []
    for node in iter_nodes(root):
        if not should_keep_package(node, package_name, include_system):
            continue
        rows.append(tuple(node.attrib.get(attr, "") for attr in attrs))
    return tuple(rows)
