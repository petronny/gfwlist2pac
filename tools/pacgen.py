"""PAC 运行时后端：把 genpac 3.0.1 的精确（precise）规则重组为 Windows 友好的 PAC。

语义基线
--------
genpac ``--pac-precise`` 产出的是 2x4 的规则表::

    [ user, gfwlist ] x [ direct_regex, direct_wildcard, proxy_regex, proxy_wildcard ]

它的 FindProxyForURL 按 0..3 的顺序遍历每个来源的 4 个槽位，命中即返回；
其中槽位 0/1 返回 DIRECT、2/3 返回 proxy，user 组先于 gfwlist 组。
同一槽位内所有规则的返回值相同，所以「按 (来源, 决策) 分桶」是严格等价的：

    桶 0 user-direct > 桶 1 user-proxy > 桶 2 gfwlist-direct > 桶 3 gfwlist-proxy

桶内再拆成两类：

* 后缀哈希表：规则只依赖 host（如 `||github.com`）。查表时按 host 的各级后缀
  探测，命中即成立，复杂度只与标签数有关（通常 3~5 次），与规则总量无关。
* URL 正则：规则还带路径/端口/查询条件，仍需对整条 URL 测试。若能从 genpac 的
  `^[\\w\\-]+:\\/+(?!\\/)(?:[^\\/]+\\.)?<字面域名>` 前缀里取出完整的主机字面量，
  就把它作为**必要条件**先做后缀判断，未命中直接跳过（只会跳过不可能匹配的规则）。

Windows PAC 引擎约束（WinINet / WinHTTP 内置 JScript 5.8 ≈ ES3）
-----------------------------------------------------------------
* 只用 ES3 语法：无 const/let/箭头函数/模板串/JSON/Promise/Array#forEach；
* 不使用 String#trim / #endsWith / #includes 等 IE9+ 能力；
* 正则表达式在脚本加载阶段一次性编译，避免每次请求重新编译（旧模板的最大开销）；
* 输出为纯 ASCII（非 ASCII 字符全部转义），避免引擎按 ANSI 解码出现乱码。
"""

import json

# genpac 在 `||域名` 规则前拼的主机锚点（见 genpac/core.py::_parse_rule_precise）
HOST_ANCHOR = r'^[\w\-]+:\/+(?!\/)(?:[^\/]+\.)?'
# `^` 通配符（AdBlock 里的分隔符）在 genpac 里被替换成的边界组
HOST_BOUNDARY = r'(?:[^\w\-.%\u0080-\uFFFF]|$)'
# 主机部分之后可能出现的、可以证明「主机字面量已结束」的尾巴
PATH_TAILS = ('\\/', '/', ':', '\\?', '?', '\\#', '#')

_JS_REGEX_SPECIAL = set('\\^$.*+?()[]{}|/')

# 桶顺序即求值顺序，值即判定结果
BUCKET_LAYOUT = (
    ('user', 'direct'),
    ('user', 'proxy'),
    ('gfwlist', 'direct'),
    ('gfwlist', 'proxy'),
)


def _split_host_literal(rest):
    """从主机锚点之后的正则里切出连续的字面主机名。

    返回 ``(literal, tail)``：literal 为已反转义的字面量，tail 为剩余正则。
    遇到无法确定的部分（未转义的 `.`、`[`、`(`、`\\d` 等）立即停止。
    """
    buf = []
    i = 0
    n = len(rest)
    while i < n:
        ch = rest[i]
        if ch == '\\':
            nxt = rest[i + 1] if i + 1 < n else ''
            if nxt and nxt in '.-_':
                buf.append(nxt)
                i += 2
                continue
            break
        if ch.isalnum() or ch in '-_':
            buf.append(ch)
            i += 1
            continue
        break
    return ''.join(buf), rest[i:]


def classify_regex(pattern):
    """判断一条 genpac 正则规则能否化简。

    返回 ``(kind, value)``：

    * ``('host', domain)`` —— 判定只取决于 host，可进后缀表；
    * ``('url', domain)``  —— 仍需测 URL，domain 非空时可作后缀预过滤；
    * ``('url', '')``      —— 仍需测 URL，且无法提取可用的预过滤条件。

    只有能证明「字面量正好落在主机结尾」时才给预过滤。像
    ``boxun.*\\.azurewebsites\\.net`` 这种主机里带 ``.*`` 的规则，``.*`` 可以跨过
    ``/`` 吃到路径里，字面量未必属于主机，给后缀过滤会造成漏判，一律不做。
    """
    if not pattern.startswith(HOST_ANCHOR):
        return ('url', '')

    literal, tail = _split_host_literal(pattern[len(HOST_ANCHOR):])
    if len(literal) < 3 or literal.startswith('.') or literal.endswith('.'):
        return ('url', '')

    # 主机字面量后即结束 / 任意尾巴 / 主机边界：判定只与 host 有关
    if tail in ('', '.*', HOST_BOUNDARY):
        return ('host', literal)
    # 后面还挂着路径、端口、查询或 ^$：仍要测 URL，但主机字面量是必要条件
    if tail == '$' or tail.startswith(PATH_TAILS):
        return ('url', literal)
    # 主机部分还有正则成分（如 ||git[a-z]hub.com），字面量只是前缀，不能当后缀用
    return ('url', '')


def shell_to_regex(pattern):
    """等价复刻 PAC 内置 shExpMatch：`*` 任意串、`?` 单字符、大小写不敏感。"""
    out = []
    for ch in pattern:
        if ch == '*':
            out.append('.*')
        elif ch == '?':
            out.append('.')
        elif ch in _JS_REGEX_SPECIAL:
            out.append('\\' + ch)
        else:
            out.append(ch)
    return ''.join(out)


def _shell_literal(pattern):
    """``*字面量*`` 这种没有其它通配符的 shell 模式，返回其中的字面量。

    shExpMatch 大小写不敏感，等价于「整条 URL 含该子串」，可以直接用
    indexOf 判定，不必走正则。含非 ASCII 字符时返回 None（交给正则，
    避免 toLowerCase 与正则 `i` 语义在特殊语言下出现偏差）。
    """
    if len(pattern) < 2 or pattern[0] != '*' or pattern[-1] != '*':
        return None
    lit = pattern[1:-1]
    if not lit or '*' in lit or '?' in lit or not lit.isascii():
        return None
    return lit


def _split(parsed):
    """把一个来源的 4 槽位拆成 direct / proxy 两组（正则组, 通配组）。"""
    direct_re, direct_wild, proxy_re, proxy_wild = parsed
    return {'direct': (direct_re, direct_wild), 'proxy': (proxy_re, proxy_wild)}


def _build_bucket(regex_patterns, wildcard_patterns):
    """桶内规则统一成 ``[kind, value, flags, host_filter]``。

    kind 0 = 正则（value 为源码，flags 原样传给 RegExp）；
    kind 1 = 纯字面量子串（value 已小写，等价于 shExpMatch 的 ``*字面量*``）。
    """
    hosts = set()
    rules = []
    for pattern in regex_patterns:
        kind, value = classify_regex(pattern)
        if kind == 'host':
            hosts.add(value)
        else:
            rules.append((0, pattern, '', value))
    for pattern in wildcard_patterns:
        literal = _shell_literal(pattern)
        if literal is not None:
            rules.append((1, literal.lower(), '', ''))
        else:
            rules.append((0, shell_to_regex(pattern), 'i', ''))

    # 桶内规则同判定，排序只是为了让输出稳定、diff 可读
    return {
        'hosts': sorted(hosts),
        'rules': [list(r) for r in sorted(set(rules))],
    }


def parse_precise(lines):
    """用 genpac 3.0.1 的精确解析器解析规则行（保持上游语义不变）。"""
    from . import genpac_compat

    genpac_compat.install()
    from genpac import parse_rules

    return parse_rules(lines, True)


def build_buckets(user_lines, gfwlist_lines):
    """把两组规则行转换成 4 个桶（顺序见 BUCKET_LAYOUT）。"""
    return build_buckets_from_parsed(parse_precise(user_lines),
                                     parse_precise(gfwlist_lines))


def build_buckets_from_parsed(user_parsed, gfwlist_parsed):
    """同 build_buckets，但直接吃 genpac 的 precise 解析结果（便于测试）。"""
    parsed = {'user': _split(user_parsed), 'gfwlist': _split(gfwlist_parsed)}
    buckets = []
    for source, decision in BUCKET_LAYOUT:
        regex_patterns, wildcard_patterns = parsed[source][decision]
        buckets.append(_build_bucket(regex_patterns, wildcard_patterns))
    return buckets


def stats(buckets):
    """输出各桶规模，供日志与自检使用。"""
    out = []
    for (source, decision), bucket in zip(BUCKET_LAYOUT, buckets):
        out.append({
            'source': source,
            'decision': decision,
            'hosts': len(bucket['hosts']),
            'url_rules': len(bucket['rules']),
        })
    return out


def _fmt_host_map(hosts, per_line=6):
    if not hosts:
        return '{}'
    lines = []
    chunk = []
    for domain in hosts:
        chunk.append('{}:1'.format(json.dumps(domain)))
        if len(chunk) >= per_line:
            lines.append(','.join(chunk))
            chunk = []
    if chunk:
        lines.append(','.join(chunk))
    return '{\n' + ',\n'.join(lines) + '\n}'


def _fmt_conf(proxy, buckets):
    hosts_part = '[\n' + ',\n'.join(_fmt_host_map(b['hosts']) for b in buckets) + '\n]'
    rules_part = '[\n' + ',\n'.join(
        json.dumps(b['rules'], separators=(',', ':')) for b in buckets) + '\n]'
    return '{\n"proxy": %s,\n"hosts": %s,\n"rules": %s\n}' % (
        json.dumps(proxy), hosts_part, rules_part)


def render_runtime(runtime_js, conf):
    """把 CONF 注入运行时模板，返回完整 PAC 文本。"""
    if '__CONF__' not in runtime_js:
        raise ValueError('运行时模板缺少 __CONF__ 占位符')
    return runtime_js.replace('__CONF__', conf, 1)


def render_pac(proxy, buckets, runtime_js, header_lines=()):
    """生成 PAC 文本；header 只放确定性信息（不含构建时间），便于按内容 diff。"""
    conf = _fmt_conf(proxy, buckets)
    body = render_runtime(runtime_js, conf)
    header = ''.join('//! {}\n'.format(line) for line in header_lines)
    pac = header + body
    if not pac.endswith('\n'):
        pac += '\n'
    return pac
