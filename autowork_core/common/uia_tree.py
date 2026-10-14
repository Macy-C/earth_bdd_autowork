def iter_descendants_via_children(root, *, max_depth=None, unique=True):
    depth_limit = _normalize_depth(max_depth)
    if depth_limit is not None and depth_limit < 1:
        return

    seen = set()

    stack = [
        (child, 1)
        for child in reversed(list(root.children() or []))
    ]

    while stack:
        element, depth = stack.pop()
        if unique:
            key = element_identity_key(element)
            if key in seen:
                continue
            seen.add(key)

        yield element

        if depth_limit is not None and depth >= depth_limit:
            continue

        children = list(element.children() or [])
        for child in reversed(children):
            stack.append((child, depth + 1))


def _normalize_depth(value):
    if value is None:
        return None
    return int(value)


def iter_self_and_descendants_via_children(root, *, max_depth=None, unique=True):
    yield root
    yield from iter_descendants_via_children(
        root,
        max_depth=max_depth,
        unique=unique,
    )


def element_identity_key(element):
    info = getattr(element, "element_info", element)

    handle = getattr(info, "handle", None)
    if handle:
        return "handle", handle

    runtime_id = getattr(info, "runtime_id", None)
    if runtime_id:
        return "runtime_id", tuple(runtime_id)

    control_type = _safe_attr(info, "control_type")
    name = _safe_attr(info, "name")
    automation_id = _safe_attr(info, "automation_id")
    rectangle = _rectangle_tuple(element, info)
    if not any(value is not None for value in (control_type, name, automation_id, rectangle)):
        return "object", id(element)

    return (
        "semantic",
        control_type,
        name,
        automation_id,
        rectangle,
    )


def _safe_attr(source, name):
    try:
        return getattr(source, name, None)
    except Exception:
        return None


def _rectangle_tuple(element, info):
    for source in (element, info):
        rectangle = getattr(source, "rectangle", None)
        try:
            if callable(rectangle):
                rectangle = rectangle()
            if rectangle is not None:
                return (
                    getattr(rectangle, "left", None),
                    getattr(rectangle, "top", None),
                    getattr(rectangle, "right", None),
                    getattr(rectangle, "bottom", None),
                )
        except Exception:
            continue
    return None