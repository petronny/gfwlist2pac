"""PAC 出厂检验：对拍 + Windows 引擎兼容性检查。

对拍思路：同一份 gfwlist、同一份自定义规则，分别用 genpac 3.0.1 原生模板和本仓库
的优化运行时生成 PAC，在 Node 里对同一批 URL 求值，要求判定逐条一致。

URL 集里既有应命中的（每条 host 规则的裸访问、子域 + 端口 + 路径），也有专门用来
抓「过度匹配」的陷阱（把域名拼到别的域名前面/后面、域名只出现在查询串里）。

用法::

    python -m tools.verify_pac --candidate gfwlist.pac --baseline base.pac --urls urls.txt
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# 只允许 ES3：这些语法/内置对象在 WinINet/WinHTTP 的 JScript 5.8 里不存在
FORBIDDEN_JS = (
    (r'\bconst\s', 'ES6 const'),
    (r'\blet\s', 'ES6 let'),
    (r'=>', '箭头函数'),
    (r'`', '模板字符串'),
    (r'\bclass\s+\w+\s*\{', 'class'),
    (r'\.endsWith\(', 'String#endsWith'),
    (r'\.startsWith\(', 'String#startsWith'),
    (r'\.trim\(', 'String#trim'),
    (r'\.includes\(', 'String#includes'),
    (r'\.forEach\(', 'Array#forEach'),
    (r'\.map\(', 'Array#map'),
    (r'\.filter\(', 'Array#filter'),
    (r'Object\.keys', 'Object.keys'),
    (r'Array\.isArray', 'Array.isArray'),
    (r'\bJSON\.', 'JSON'),
    (r'\bPromise\b', 'Promise'),
    (r'\?\?|\?\.', '可选链 / 空值合并'),
)

NODE_HARNESS = r"""
'use strict';
var fs = require('fs');
var vm = require('vm');

var pacPath = process.argv[2];
var urlPath = process.argv[3];

var src = fs.readFileSync(pacPath, 'utf8');
// Harness-only: cache RegExp compilation so the genpac baseline can be sampled
// at full scale. Matching semantics are untouched (same pattern -> same object).
src = src.replace(/new RegExp\(/g, '__re(');

var reCache = {};
function v8re(pattern, flags) {
    var key = flags === undefined ? pattern : pattern + '\u0000' + flags;
    if (!Object.prototype.hasOwnProperty.call(reCache, key)) {
        reCache[key] = flags === undefined ? new RegExp(pattern) : new RegExp(pattern, flags);
    }
    return reCache[key];
}

function pacShExpMatch(str, shexp) {
    var re = '^' + shexp.replace(/[.+^${}()|[\]\\]/g, '\\$&')
        .replace(/\*/g, '.*').replace(/\?/g, '.') + '$';
    return new RegExp(re, 'i').test(str);
}

var sandbox = {
    __re: v8re,
    shExpMatch: pacShExpMatch,
    dnsDomainIs: function (host, domain) {
        return host.length >= domain.length &&
            host.substring(host.length - domain.length) === domain;
    },
    isPlainHostName: function (host) { return host.indexOf('.') < 0; },
    dnsResolve: function () { return '127.0.0.1'; },
    isResolvable: function () { return false; },
    isInNet: function () { return false; },
    myIpAddress: function () { return '127.0.0.1'; },
    alert: function () {}
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: pacPath });

var urls = fs.readFileSync(urlPath, 'utf8').split('\n').filter(function (l) {
    return l.length > 0;
});
var decisions = [];
var t0 = Date.now();
for (var i = 0; i < urls.length; i++) {
    var u = urls[i];
    var host = '';
    try { host = new URL(u).hostname; } catch (e) { host = ''; }
    try {
        decisions.push(sandbox.FindProxyForURL(u, host));
    } catch (e) {
        decisions.push('ERROR: ' + e.message);
    }
}
process.stdout.write(JSON.stringify({
    decisions: decisions,
    ms: Date.now() - t0,
    count: urls.length
}));
"""


def run_pac(pac_path, urls_path, harness_dir):
    harness = Path(harness_dir) / 'pac_harness.js'
    if not harness.exists():
        harness.write_text(NODE_HARNESS, encoding='utf-8')
    out = subprocess.run(['node', str(harness), str(pac_path), str(urls_path)],
                         capture_output=True, text=True, encoding='utf-8')
    if out.returncode != 0:
        raise RuntimeError('node 求值失败 ({}):\n{}'.format(pac_path, out.stderr.strip()))
    return json.loads(out.stdout)


def lint_runtime(path):
    """检查运行时模板里没有 ES5+ 语法（JScript 5.8 只认 ES3）。"""
    text = Path(path).read_text(encoding='utf-8')
    # 去掉块注释再扫，避免注释里的关键词造成误报
    code = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    problems = []
    for pattern, name in FORBIDDEN_JS:
        if re.search(pattern, code):
            problems.append(name)
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description='PAC 对拍与兼容性检查')
    ap.add_argument('--candidate', required=True, help='优化后的 PAC')
    ap.add_argument('--baseline', required=True, help='genpac 原生 PAC（语义基线）')
    ap.add_argument('--urls', required=True, help='对拍 URL 列表，一行一条')
    ap.add_argument('--strict-urls', help='genpac 会误命中的 URL：优化运行时必须全部 DIRECT')
    ap.add_argument('--runtime', default='pac/runtime.js', help='运行时模板，用于 ES3 检查')
    ap.add_argument('--min-hosts', type=int, default=1000,
                    help='候选 PAC 至少应包含的域名条数（防止空规则文件上线）')
    ap.add_argument('--max-report', type=int, default=10)
    args = ap.parse_args(argv)

    candidate = Path(args.candidate)
    baseline = Path(args.baseline)
    pac_text = candidate.read_text(encoding='utf-8')

    errors = []
    try:
        pac_text.encode('ascii')
    except UnicodeEncodeError as exc:
        errors.append('PAC 含非 ASCII 字符（Windows 引擎可能按 ANSI 解码）：{}'.format(exc))
    if 'FindProxyForURL' not in pac_text:
        errors.append('PAC 缺少 FindProxyForURL')
    if 'new Date(' in pac_text or 'Date.now' in pac_text:
        errors.append('PAC 里不应出现时间/随机相关调用（会导致每次请求结果不稳定）')

    host_count = pac_text.count(':1,') + pac_text.count(':1\n')
    if host_count < args.min_hosts:
        errors.append('候选 PAC 域名条数 {} 少于下限 {}，疑似规则未加载'.format(
            host_count, args.min_hosts))

    problems = lint_runtime(args.runtime) if Path(args.runtime).exists() else []
    for name in problems:
        errors.append('运行时模板使用了 Windows PAC 引擎不支持的语法：{}'.format(name))

    with tempfile.TemporaryDirectory() as tmp:
        cand = run_pac(candidate, args.urls, tmp)
        base = run_pac(baseline, args.urls, tmp)
        strict = None
        if args.strict_urls:
            strict = run_pac(candidate, args.strict_urls, tmp)

    urls = [l for l in Path(args.urls).read_text(encoding='utf-8').splitlines() if l]
    diffs = [(u, b, c) for u, b, c in zip(urls, base['decisions'], cand['decisions']) if b != c]

    print('samples: {} urls'.format(len(urls)))
    print('baseline: {:.1f} ms total ({:.3f} ms/url, genpac 模板)'.format(
        base['ms'], base['ms'] / max(base['count'], 1)))
    print('candidate: {:.1f} ms total ({:.3f} ms/url, 优化运行时)'.format(
        cand['ms'], cand['ms'] / max(cand['count'], 1)))
    if base['ms'] and cand['ms']:
        print('speedup: {:.0f}x (node 参考值，Windows JScript 上差距通常更明显)'.format(
            base['ms'] / max(cand['ms'], 1)))

    if strict is not None:
        strict_urls = [l for l in Path(args.strict_urls)
                       .read_text(encoding='utf-8').splitlines() if l]
        leaked = [u for u, d in zip(strict_urls, strict['decisions']) if d != 'DIRECT']
        print('boundary check: {} urls（阻塞域名 + 其它域名，必须 DIRECT）'.format(
            len(strict_urls)))
        if leaked:
            print('  泄漏 {} 条，前 {} 条：'.format(len(leaked), args.max_report))
            for u in leaked[:args.max_report]:
                print('    {}'.format(u))
            errors.append('主机边界检查失败：{} 条 URL 被误判为走代理'.format(len(leaked)))

    if diffs:
        print('\n判定不一致 {} 条，前 {} 条：'.format(len(diffs), args.max_report))
        for url, expected, got in diffs[:args.max_report]:
            print('  {}\n    genpac={} optimized={}'.format(url, expected, got))
        errors.append('对拍失败：{} 条 URL 判定与 genpac 基线不一致'.format(len(diffs)))

    if errors:
        print('\nFAILED:')
        for err in errors:
            print('  - {}'.format(err))
        return 1

    print('\nOK: 与 genpac 3.0.1 基线逐条一致，且未发现 Windows 引擎不兼容语法')
    return 0


if __name__ == '__main__':
    sys.exit(main())
