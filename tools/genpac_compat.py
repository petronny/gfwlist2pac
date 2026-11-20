"""genpac 3.0.1 兼容层。

genpac 3.0.1 的代码仍是按 Python <= 3.11 写的，在 3.12+ 上会直接失败：

    re.error: bad escape \\w at position 5

触发点是 ``genpac/core.py::_parse_rule_precise`` 里的一行::

    line = re.sub(r'\\\\\\^', r'(?:[^\\w\\-.%\\u0080-\\uFFFF]|$)', line)

Python 3.12 起 re 模块不再容忍替换模板里的未知转义（``\\w``、``\\uXXXX``），
而这段模板在被替换进目标串之前没有任何需要解释的转义，语义上等价于「原样插入」。
上游 issue：https://github.com/JinnLynn/genpac/issues （3.0.1 未修复）。

这里不去改 site-packages，而是给 ``genpac.core`` 单独挂一个 re 代理对象：
先按原样调用，仅在抛 re.error 时退化成 callable 替换。这样

* 老版本 Python 行为完全不变（第一次调用就成功）；
* 含 ``\\1`` 反向引用的模板不会走到退化分支，语义不受影响；
* 不污染全局 re 模块，也不需要 CI 里 sed 打补丁。
"""

import re

import genpac.core as _core

_INSTALLED = False


class _ReProxy(object):
    """把 re 模块包一层，只接管 sub 的模板兼容性问题。"""

    def __init__(self, module):
        self._module = module

    def __getattr__(self, name):
        return getattr(self._module, name)

    def sub(self, pattern, repl, string, count=0, flags=0):
        if callable(repl):
            return self._module.sub(pattern, repl, string, count=count, flags=flags)
        try:
            return self._module.sub(pattern, repl, string, count=count, flags=flags)
        except re.error:
            # 模板里的 \w/\uXXXX 只是字面量，用 callable 原样返回即可。
            return self._module.sub(pattern, lambda _m: repl, string,
                                    count=count, flags=flags)


def install():
    """幂等地给 genpac.core 安装兼容层，返回是否本次生效。"""
    global _INSTALLED
    if _INSTALLED:
        return False
    if isinstance(_core.re, _ReProxy):
        _INSTALLED = True
        return False
    _core.re = _ReProxy(_core.re)
    _INSTALLED = True
    return True


def python_compat_note():
    """便于日志/README 引用的一句话说明。"""
    return (
        'compat: genpac 3.0.1 + Python {}.{}.{} '
        '(precise 规则的 re 替换模板已在内存中兼容)'.format(*__import__('sys').version_info[:3])
    )
