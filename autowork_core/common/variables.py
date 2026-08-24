from collections.abc import MutableMapping
from copy import deepcopy
import yaml
from autowork_core.common.compile import compile_locators
from config.paths import Paths
from autowork_core.utils.bus import normalize




class Stratify(MutableMapping):
    desktop_size = None
    variable = dict()

    def __init__(self, initial=None, ignore=(), caseless=True, spaceless=True):
        self._data = {}
        self._keys = {}

        self._public_data = {}
        self._normalize = lambda s: normalize(s, ignore, caseless, spaceless)
        if initial:
            self.add_initial(initial)

    def add_initial(self, initial):
        for k, v in initial.items():
            self._public_data[self._normalize(k)] = v

    def fork(self):
        layered = deepcopy(self)
        layered._data = {}
        layered._keys = {}
        return layered

    def load_data(self, file_name):
        test_dict = self._load_yml(Paths.DATA_DIR, file_name, resource_type="data")
        if test_dict:
            for k, v in test_dict.items():
                self[k] = v

    def load_locator(self, file_name):
        test_dict = self._load_yml(Paths.LOCATORS_DIR, file_name, resource_type="locator")
        if test_dict:
            compiled_map = compile_locators(test_dict, external_locators=self._locator_scope())
            for k, v in compiled_map.items():
                self[k] = v

    def _locator_scope(self):
        scope = {}
        scope.update(self._public_data)
        scope.update(self._data)
        return scope

    @property
    def public_data(self):
        return self._public_data

    @staticmethod
    def _load_yml(path, name, resource_type="resource"):
        name = name if name.endswith('.yaml') or name.endswith('.yml') else name + '.yaml'
        test_data_file = path / name
        if not test_data_file.exists():
            raise FileNotFoundError(f"声明的 {resource_type} 文件不存在: {test_data_file}")
        with open(test_data_file, 'r', encoding='UTF-8') as f:
            return yaml.safe_load(f) or {}

    def __getitem__(self, key):
        key = self._normalize(key)
        if key in self._data:
            return self._data[key]
        return self._public_data.get(key, None)


    def __setitem__(self, key, value):
        norm_key = self._normalize(key)
        self._data[norm_key] = value
        self._keys.setdefault(norm_key, key)

    def __delitem__(self, key):
        norm_key = self._normalize(key)
        if norm_key in self._data:
            del self._data[norm_key]
            del self._keys[norm_key]
        else:
            if norm_key in self._public_data:
                del self._public_data[norm_key]

    def __iter__(self):
        return (self._keys[norm_key] for norm_key in sorted(self._keys))

    def __len__(self):
        return len(self._data)

    def __str__(self):
        return '{%s}' % ', '.join('%r: %r' % (key, self[key]) for key in self)

    def __eq__(self, other):
        return self._data == other._data

    def __contains__(self, key):
        return self._normalize(key) in self._data

    def clear(self):
        self._data.clear()
        self._keys.clear()


