class ElementPropertyReadError(RuntimeError):
    def __init__(self, property_name):
        self.property_name = property_name
        super().__init__(f"无法读取元素属性: {property_name}")


def read_accessible_name(element, *, element_info=None):
    info = element_info
    if info is None:
        try:
            info = element.element_info
        except AttributeError:
            info = element
        except Exception as error:
            raise ElementPropertyReadError("name") from error

    try:
        value = info.name
    except Exception as error:
        raise ElementPropertyReadError("name") from error
    return "" if value is None else value