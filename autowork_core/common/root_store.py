from dataclasses import dataclass, field

from loguru import logger

from autowork_core.utils.bus import normalize


@dataclass
class RootEntry:
    name: str
    kind: str = "legacy"
    backend: str = "uia"
    criteria: dict = field(default_factory=dict)
    root: object = None
    handle: int = None
    process_id: int = None
    state: str = "cold"

    def is_hot_handle(self):
        return self.kind == "top" and self.handle and self.state == "hot"

    def mark_hot(self, root, handle, process_id=None):
        self.root = root
        self.handle = handle
        self.process_id = process_id
        self.state = "hot"

    def mark_cold(self, root=None):
        self.handle = None
        self.process_id = None
        self.state = "cold"
        if root is not None:
            self.root = root

    def mark_stale(self, root=None):
        # self.handle = None
        # self.process_id = None
        self.state = "stale"
        if root is not None:
            self.root = root


@dataclass
class RootResolveResult:
    root: object = None
    stale_entry: RootEntry = None

    def mark_stale_if_hot(self, root_factory=None):
        entry = self.stale_entry
        if entry is None or not entry.is_hot_handle():
            return False
        logger.debug(
            f"^^^^^^ 顶层 root 标记 stale -> {entry.name}, "
            f"handle={entry.handle}, process_id={entry.process_id}"
        )
        root = root_factory(entry) if root_factory is not None else None
        entry.mark_stale(root)
        return True


class RootStore(dict):
    LAST_ROOT_KEY = "last_root"

    @classmethod
    def _key(cls, name):
        if name is None:
            return name
        key = normalize(str(name))
        return cls.LAST_ROOT_KEY if key == normalize(cls.LAST_ROOT_KEY) else key

    def __init__(self):
        super().__init__()
        super().__setitem__(self.LAST_ROOT_KEY, None)

    def __setitem__(self, key, value):
        super().__setitem__(self._key(key), value)

    def __getitem__(self, key):
        return self._root_from_value(super().__getitem__(self._key(key)))

    def __contains__(self, key):
        return super().__contains__(self._key(key))

    def __delitem__(self, key):
        super().__delitem__(self._key(key))

    def get(self, key, default=None):
        return self._root_from_value(super().get(self._key(key), default))

    def get_entry(self, key, default=None):
        value = super().get(self._key(key), default)
        return value if isinstance(value, RootEntry) else default

    def pop(self, key, default=None):
        return self._root_from_value(super().pop(self._key(key), default))

    def clear(self):
        super().clear()
        super().__setitem__(self.LAST_ROOT_KEY, None)

    def set(self, name, root):
        if not name:
            raise ValueError("root 名称不能为空")
        if isinstance(root, RootEntry):
            entry = root
        else:
            entry = RootEntry(name=self._key(name), root=root)
        self[name] = entry
        return entry.root

    def set_entry(self, entry):
        if not entry.name:
            raise ValueError("root 名称不能为空")
        entry.name = self._key(entry.name)
        self[entry.name] = entry
        return entry.root

    def has(self, name):
        return name in self

    def set_last(self, root):
        super().__setitem__(self.LAST_ROOT_KEY, self._root_from_value(root))
        return self.last()

    def last(self):
        return self._root_from_value(super().get(self.LAST_ROOT_KEY))

    @staticmethod
    def _root_from_value(value):
        return value.root if isinstance(value, RootEntry) else value
