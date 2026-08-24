"""桌面定位、视觉识别和操作录制调试工具。

Desktop locator, visual-recognition, and interaction-recording debug tools.
"""

__all__ = ["XPathDebuggerApp"]


def __getattr__(name):
    if name == "XPathDebuggerApp":
        from autowork_core.utils.debug_tools.app import XPathDebuggerApp

        return XPathDebuggerApp
    raise AttributeError(name)