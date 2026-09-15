"""Stock UIAutomator tree parsing and shared selector behavior."""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .android_device import dump_root, run_adb_shell
from .android_ui_tree import (
    build_atomic_tree,
    is_scroll_node,
    is_visible,
    iter_nodes,
    node_fingerprint,
    node_kind,
    node_label,
    node_text,
    screen_rect,
    short_class,
    short_id,
    tree_to_text,
)
from .geometry_types import Rect, format_rect, parse_bounds


@dataclass(frozen=True)
class UISnapshot:
    xml: str
    root: ET.Element
    package: str
    activity: str
    elapsed_ms: float


_COMPONENT_RE = r"([A-Za-z0-9_.]+)/([A-Za-z0-9_.$/]+)"
_ACTIVITY_RECORD_COMPONENT_RE = r"ActivityRecord\{[^}]*?\s" + _COMPONENT_RE + r"(?:\s|})"
_CURRENT_APP_PATTERNS = (
    re.compile(r"(?:mResumedActivity|ResumedActivity):\s+" + _ACTIVITY_RECORD_COMPONENT_RE),
    re.compile(r"topResumedActivity=.*?" + _ACTIVITY_RECORD_COMPONENT_RE),
    re.compile(r"mFocusedApp=" + _ACTIVITY_RECORD_COMPONENT_RE),
    re.compile(r"mCurrentFocus=Window\{[^}]*?\s" + _COMPONENT_RE + r"(?:\s|})"),
)


def parse_current_app(text: str) -> dict[str, str] | None:
    for pattern in _CURRENT_APP_PATTERNS:
        match = pattern.search(text)
        if match:
            return {"package": match.group(1), "activity": match.group(2)}
    return None


def current_app(serial: str, timeout: float = 10.0) -> dict[str, str]:
    result = run_adb_shell(
        serial,
        ["dumpsys", "activity", "activities"],
        timeout=timeout,
        check=False,
    )
    app = parse_current_app(result.stdout)
    if app:
        return app
    result = run_adb_shell(serial, ["dumpsys", "window"], timeout=timeout, check=False)
    return parse_current_app(result.stdout) or {"package": "", "activity": ""}


def snapshot(
    serial: str,
    *,
    compressed: bool = True,
    timeout: float = 10.0,
    include_activity: bool = False,
) -> UISnapshot:
    started = time.perf_counter()
    xml, root = dump_root(serial, compressed=compressed, timeout=timeout)
    packages = [
        node.attrib.get("package") or ""
        for node in iter_nodes(root)
        if node.attrib.get("package")
    ]
    package = Counter(packages).most_common(1)[0][0] if packages else ""
    app = (
        current_app(serial, timeout=min(timeout, 5.0))
        if include_activity
        else {"package": package, "activity": ""}
    )
    return UISnapshot(
        xml=xml,
        root=root,
        package=package,
        activity=app["activity"] if app["package"] == package else "",
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def clean_tree_text(
    root: ET.Element,
    package_name: str | None,
    keep_id_regexes=(),
) -> str:
    screen = screen_rect(root)
    items = []
    for child in list(root):
        items.extend(
            build_atomic_tree(
                child,
                package_name=package_name,
                include_system=False,
                screen=screen,
                keep_id_regexes=keep_id_regexes,
            )
        )
    return tree_to_text(items, screen)


def _node_dump_values(node: ET.Element) -> list[str]:
    label = node_label(node)
    kind = node_kind(node)
    if not label or not kind or kind == "-":
        return []
    base = f"{label} [{kind}]"
    return [base, f"{base} {format_rect(node_rect(node))}"]


def _node_match_values(node: ET.Element) -> list[str]:
    resource_id = node.attrib.get("resource-id") or ""
    class_name = node.attrib.get("class") or ""
    return [
        node_text(node),
        node_label(node),
        node.attrib.get("content-desc") or "",
        resource_id,
        short_id(resource_id),
        class_name,
        short_class(class_name),
        *_node_dump_values(node),
    ]


def _matches(value: str, target: str, contains: bool) -> bool:
    return target in value if contains else target == value


def node_matches_target(node: ET.Element, target: str, contains: bool) -> bool:
    return any(
        value and _matches(value, target, contains)
        for value in _node_match_values(node)
    )


def _priority(node: ET.Element, target: str, contains: bool) -> int:
    resource_id = node.attrib.get("resource-id") or ""
    class_name = node.attrib.get("class") or ""
    groups = (
        (0, [node.attrib.get("text") or "", node.attrib.get("content-desc") or ""]),
        (1, [resource_id, short_id(resource_id)]),
        (2, [class_name, short_class(class_name)]),
        (3, _node_dump_values(node)),
    )
    for priority, values in groups:
        if any(value and _matches(value, target, contains) for value in values):
            return priority
    return 99


def node_rect(node: ET.Element) -> Rect:
    return parse_bounds(node.attrib.get("bounds"))


def _sort_key(node: ET.Element, screen: Rect) -> tuple[int, int, int, int]:
    rect = node_rect(node)
    clipped = int(
        rect.x < screen.x
        or rect.y < screen.y
        or rect.right > screen.right
        or rect.bottom > screen.bottom
    )
    return clipped, rect.y, rect.x, rect.w * rect.h


def find_target_nodes(
    root: ET.Element,
    target: str,
    *,
    package_name: str | None,
    contains: bool = False,
    scroll_only: bool = False,
) -> list[ET.Element]:
    screen = screen_rect(root)

    def collect(package_filter: str | None) -> list[ET.Element]:
        found = []
        for node in iter_nodes(root):
            if package_filter and node.attrib.get("package") != package_filter:
                continue
            if scroll_only and not is_scroll_node(node):
                continue
            if not is_visible(node, screen):
                continue
            if node_matches_target(node, target, contains):
                found.append(node)
        return found

    matches = collect(package_name)
    if not matches and package_name:
        matches = collect(None)
    return sorted(
        matches,
        key=lambda node: (_priority(node, target, contains), *_sort_key(node, screen)),
    )


def describe_node(node: ET.Element) -> str:
    rect = node_rect(node)
    bits = [f"label={node_label(node)!r}", f"bounds={format_rect(rect)}"]
    resource_id = short_id(node.attrib.get("resource-id"))
    class_name = short_class(node.attrib.get("class"))
    if resource_id:
        bits.append(f"id={resource_id}")
    if class_name:
        bits.append(f"class={class_name}")
    return " ".join(bits)


def node_payload(node: ET.Element) -> dict[str, object]:
    rect = node_rect(node)
    return {
        "label": node_label(node),
        "text": node_text(node),
        "contentDescription": node.attrib.get("content-desc") or "",
        "resourceId": node.attrib.get("resource-id") or "",
        "class": node.attrib.get("class") or "",
        "package": node.attrib.get("package") or "",
        "bounds": {
            "x": rect.x,
            "y": rect.y,
            "width": rect.w,
            "height": rect.h,
        },
        "center": {"x": rect.center[0], "y": rect.center[1]},
        "clickable": node.attrib.get("clickable") == "true",
        "enabled": node.attrib.get("enabled") != "false",
        "selected": node.attrib.get("selected") == "true",
        "checked": node.attrib.get("checked") == "true",
        "scrollable": is_scroll_node(node),
    }


def select_target(
    matches: list[ET.Element],
    target: str,
    *,
    match_index: int,
    role: str = "target",
) -> ET.Element:
    if match_index < 0:
        raise RuntimeError(f"{role} match index must be >= 0: {match_index}")
    if not matches:
        raise RuntimeError(f"{role} not found in current UI dump: {target!r}")
    if match_index >= len(matches):
        candidates = "; ".join(
            f"{index}: {describe_node(node)}"
            for index, node in enumerate(matches[:5])
        )
        raise RuntimeError(
            f"{role} match index {match_index} is out of range for {target!r}; "
            f"matched {len(matches)} node(s). candidates: {candidates or 'none'}"
        )
    return matches[match_index]


def visible_scroll_nodes(root: ET.Element, package_name: str | None) -> list[ET.Element]:
    screen = screen_rect(root)
    nodes = [
        node
        for node in iter_nodes(root)
        if is_scroll_node(node)
        and is_visible(node, screen)
        and (not package_name or node.attrib.get("package") == package_name)
    ]
    if not nodes and package_name:
        nodes = [
            node
            for node in iter_nodes(root)
            if is_scroll_node(node) and is_visible(node, screen)
        ]
    return sorted(nodes, key=lambda node: (-node_rect(node).w * node_rect(node).h, *_sort_key(node, screen)))


def select_scroll_node(
    root: ET.Element,
    package_name: str | None,
    selector: str | None,
    index: int,
) -> ET.Element:
    nodes = visible_scroll_nodes(root, package_name)
    if selector in ("horizontal", "vertical"):
        horizontal = selector == "horizontal"
        nodes = [node for node in nodes if (node_rect(node).w >= node_rect(node).h) == horizontal]
    elif selector:
        nodes = [
            node
            for node in nodes
            if node_matches_target(node, selector, contains=False)
        ]
    if not nodes:
        raise RuntimeError(f"no visible scroll container found for {selector or 'current page'}")
    if index < 0 or index >= len(nodes):
        raise RuntimeError(
            f"scroll container index {index} out of range; matched {len(nodes)} node(s)"
        )
    return nodes[index]


def swipe_points(direction: str, bounds: Rect) -> tuple[int, int, int, int]:
    cx, cy = bounds.center
    horizontal_padding = max(24, min(96, bounds.w // 14))
    left = bounds.x + min(bounds.w - 1, horizontal_padding)
    right = bounds.x + max(0, bounds.w - horizontal_padding)
    top = bounds.y + max(1, bounds.h // 4)
    bottom = bounds.y + bounds.h - max(1, bounds.h // 4)
    if direction in ("forth", "up"):
        return cx, bottom, cx, top
    if direction in ("back", "down"):
        return cx, top, cx, bottom
    if direction == "left":
        return right, cy, left, cy
    if direction == "right":
        return left, cy, right, cy
    raise ValueError(f"unsupported scroll direction: {direction}")


def relative_scroll_direction(direction: str, container: ET.Element) -> str:
    if direction not in ("forth", "back"):
        return direction
    horizontal = node_rect(container).w >= node_rect(container).h
    if direction == "forth":
        return "left" if horizontal else "up"
    return "right" if horizontal else "down"


def fingerprint(snapshot_value: UISnapshot) -> tuple[tuple[str, ...], ...]:
    return node_fingerprint(
        snapshot_value.root,
        snapshot_value.package,
        include_system=False,
    )
