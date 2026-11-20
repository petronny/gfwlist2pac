"""生成 gfwlist.pac。

两种引擎，规则解析都来自 genpac 3.0.1：

* ``optimized``（默认）—— 本仓库的 PAC 运行时（pac/runtime.js + tools/pacgen.py）
* ``genpac``          —— 原样渲染 genpac 的 ``--pac-precise`` 模板，作为语义基线，
                         仅用于对拍 / 排查问题

用法::

    python -m tools.build_pac \
        --gfwlist gfwlist/gfwlist.txt \
        --user-rule-from custom-proxy.txt \
        --proxy "SOCKS5 192.168.30.3:7891; PROXY 192.168.30.3:7890; DIRECT;" \
        -o gfwlist.pac
"""

import argparse
import base64
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import genpac_compat, pacgen

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_JS = ROOT / 'pac' / 'runtime.js'


def load_gfwlist(path=None, url=None):
    """返回 (解码后的规则行, 来源描述, Last-Modified)。

    与 genpac 一致：丢掉第一行（gfwlist 自身的头），其余逐行作为规则；
    gfwlist 为 base64 文本，也接受已经解码的明文，便于本地调试。
    """
    if path:
        raw = Path(path).read_bytes()
        source = str(path)
    elif url:
        import urllib.request
        with urllib.request.urlopen(url, timeout=60) as resp:
            raw = resp.read()
        source = 'online[{}]'.format(url)
    else:
        raise SystemExit('必须提供 --gfwlist 或 --gfwlist-url')

    try:
        if len(raw) > 100 and re.fullmatch(rb'[A-Za-z0-9+/=\s]+', raw):
            text = base64.b64decode(raw).decode('utf-8')
        else:
            raise ValueError
    except Exception:
        text = raw.decode('utf-8', 'replace')
        print('warning: {} 不是 base64 编码的 gfwlist，按明文处理'.format(source),
              file=sys.stderr)

    lines = text.splitlines()[1:]
    modified = '-'
    for line in lines:
        if line.startswith('! Last Modified:'):
            modified = line.split(':', 1)[1].strip()
            break
    return lines, source, modified


def load_user_rules(paths):
    lines = []
    for p in paths:
        lines.extend(Path(p).read_text(encoding='utf-8').splitlines())
    return lines


def _genpac_version():
    try:
        from importlib import metadata
        return metadata.version('genpac')
    except Exception:
        return '3.0.1'


def render_genpac_pac(proxy, user_parsed, gfwlist_parsed, meta):
    """等价于 genpac 3.0.1 的 ``FmtPAC.generate``（--pac-precise，不压缩）。"""
    from genpac.format.pac import _TPL_PAC_PRECISE
    from genpac.util import replace_all, get_version, get_project_url

    rules = json.dumps([user_parsed, gfwlist_parsed], indent=4)
    tpl = str(_TPL_PAC_PRECISE).strip('\n') + '\n'
    version = get_version() or _genpac_version()
    return replace_all(tpl, {
        '__GENPAC__': 'genpac {} {}'.format(version, get_project_url()),
        '__GENERATED__': meta['generated'],
        '__MODIFIED__': meta['modified'],
        '__GFWLIST_FROM__': meta['source'],
        '__PROXY__': proxy,
        '__RULES__': rules,
    })


def header_lines(proxy, buckets, meta):
    sig = []
    for (source, decision), bucket in zip(pacgen.BUCKET_LAYOUT, buckets):
        sig.append('{}/{}:{}h+{}r'.format(source, decision,
                                          len(bucket['hosts']),
                                          len(bucket['rules'])))
    return [
        'gfwlist2pac | genpac {} precise rules | PAC runtime for Windows (JScript 5.8)'.format(_genpac_version()),
        'GFWList: {} [{}]'.format(meta['modified'], meta['source']),
        'buckets: {}'.format(' '.join(sig)),
        'proxy: {}'.format(proxy),
    ]


def sample_urls(buckets, trap_step=17):
    """对拍 URL：每一条都要求优化运行时与 genpac 基线判定完全一致。"""
    urls = []
    seen = set()

    def add(u):
        if u not in seen:
            seen.add(u)
            urls.append(u)

    hosts = []
    for bucket in buckets:
        hosts.extend(bucket['hosts'])

    for host in hosts:
        add('https://{}/'.format(host))

    for idx, host in enumerate(hosts):
        if idx % 5 == 0:
            add('http://a.b.{}:8080/x/y?z=1'.format(host))
        if idx % trap_step == 0:
            add('https://x{}/'.format(host))                          # 前缀粘连
        if idx % (trap_step * 2) == 0:
            add('https://example.org/?u=https://{}/'.format(host))    # 域名只出现在查询串

    for fixed in ('http://localhost/', 'http://127.0.0.1:8080/',
                  'https://example.com/', 'https://www.baidu.com/',
                  'https://github.com/', 'https://raw.githubusercontent.com/a/b',
                  'https://pbq.cc.cd/', 'https://notpbq.cc.cd/'):
        add(fixed)

    # 通配/子串规则（gfwlist 里那些不带 || 的裸域名行）要单独造样本，
    # 否则只能靠陷阱 URL 碰运气覆盖到其中一小部分。
    for bucket in buckets:
        for kind, value, _flags, _filter in bucket['rules']:
            if kind != 1:
                continue
            add('https://example.org/p/{}/x'.format(value))
            add('https://example.org/p/{}/x'.format(value.upper()))
            if len(value) > 4:
                add('https://example.org/p/{}/x'.format(value[:-1]))
    return urls


def strict_urls(buckets, trap_step=17):
    """genpac 会误命中的 URL：阻塞域名后面还挂着别的域名。

    genpac 的 precise 正则 ``^..(?:[^/]+\\.)?<domain>`` 没有主机结尾边界，
    因此 ``12vpn.net.example.net`` 也会命中。gfwlist 里 ``||domain`` 的本意是
    「该域名及其子域」，本运行时按标签边界判定，这类 URL 必须 DIRECT。
    """
    urls = []
    for idx, host in enumerate(
            h for bucket in buckets for h in bucket['hosts']):
        if idx % trap_step == 0:
            urls.append('https://{}.example.net/'.format(host))
    return urls


def std_datetime(modified_datestr):
    """gfwlist 头部的 RFC 时间 -> 本地时间，等价于 genpac 3.0.1 的同名实现。

    上游用的是 ``datetime.utcfromtimestamp``（3.12 起弃用），这里换成
    timezone-aware 的写法，结果一致且不再触发弃用告警。
    """
    try:
        naive, _, offset = modified_datestr.rpartition(' ')
        dt = datetime.strptime(naive, '%a, %d %b %Y %H:%M:%S')
        minutes = int(offset[-4:-2]) * 60 + int(offset[-2:])
        if offset[0] == '-':
            minutes = -minutes
        utc = dt - timedelta(minutes=minutes)
        return utc.replace(tzinfo=timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return modified_datestr


def main(argv=None):
    ap = argparse.ArgumentParser(description='生成 gfwlist PAC（genpac 3.0.1 规则 + 优化运行时）')
    src = ap.add_mutually_exclusive_group()
    src.add_argument('--gfwlist', help='本地 gfwlist 文件（base64，或已解码明文）')
    src.add_argument('--gfwlist-url', help='在线 gfwlist 地址')
    ap.add_argument('--user-rule-from', action='append', default=[],
                    metavar='FILE', help='自定义规则文件，可重复')
    ap.add_argument('--proxy', required=True, help='PAC 返回的代理串')
    ap.add_argument('-o', '--output', required=True, help='输出 PAC 路径')
    ap.add_argument('--engine', choices=('optimized', 'genpac'), default='optimized')
    ap.add_argument('--runtime', default=str(RUNTIME_JS), help='运行时模板路径')
    ap.add_argument('--baseline-out', help='额外输出 genpac 原生 PAC（对拍用）')
    ap.add_argument('--sample-urls-out', help='输出对拍 URL 列表')
    ap.add_argument('--strict-urls-out', help='输出"应当 DIRECT"的 URL 列表（genpac 会误命中）')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args(argv)

    gfwlist_lines, source, modified = load_gfwlist(args.gfwlist, args.gfwlist_url)
    user_lines = load_user_rules(args.user_rule_from)

    genpac_compat.install()
    user_parsed = pacgen.parse_precise(user_lines)
    gfwlist_parsed = pacgen.parse_precise(gfwlist_lines)

    buckets = pacgen.build_buckets_from_parsed(user_parsed, gfwlist_parsed)
    meta = {
        'modified': std_datetime(modified),
        'source': source,
        'generated': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
    }

    if args.engine == 'genpac':
        pac = render_genpac_pac(args.proxy, user_parsed, gfwlist_parsed, meta)
    else:
        runtime_js = Path(args.runtime).read_text(encoding='utf-8')
        pac = pacgen.render_pac(args.proxy, buckets, runtime_js,
                                header_lines(args.proxy, buckets, meta))

    Path(args.output).write_text(pac, encoding='utf-8', newline='\n')

    if args.baseline_out:
        Path(args.baseline_out).write_text(
            render_genpac_pac(args.proxy, user_parsed, gfwlist_parsed, meta),
            encoding='utf-8', newline='\n')

    if args.sample_urls_out:
        Path(args.sample_urls_out).write_text(
            '\n'.join(sample_urls(buckets)) + '\n', encoding='utf-8', newline='\n')

    if args.strict_urls_out:
        Path(args.strict_urls_out).write_text(
            '\n'.join(strict_urls(buckets)) + '\n', encoding='utf-8', newline='\n')

    if not args.quiet:
        print('input: gfwlist lines={} user lines={}'.format(
            len(gfwlist_lines), len(user_lines)))
        for item in pacgen.stats(buckets):
            print('  {source:>8}/{decision:<6} hosts={hosts:<5} url-rules={url_rules}'
                  .format(**item))
        print('output: {} ({:,} bytes, engine={})'.format(
            args.output, len(pac.encode('utf-8')), args.engine))
    return 0


if __name__ == '__main__':
    sys.exit(main())
