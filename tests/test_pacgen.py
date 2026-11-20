import base64
import tempfile
import unittest
from pathlib import Path

from tools import genpac_compat, pacgen
from tools.build_pac import (load_gfwlist, load_user_rules, sample_urls,
                             std_datetime, strict_urls)
from tools.verify_pac import lint_runtime

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / 'pac' / 'runtime.js'


class ClassifyRegexTest(unittest.TestCase):
    def test_domain_rule_becomes_host_rule(self):
        self.assertEqual(
            pacgen.classify_regex(pacgen.HOST_ANCHOR + r'github\.com'),
            ('host', 'github.com'))

    def test_path_rule_keeps_host_prefilter(self):
        self.assertEqual(
            pacgen.classify_regex(pacgen.HOST_ANCHOR + r'example\.com/foo'),
            ('url', 'example.com'))

    def test_boundary_rule_is_host_only(self):
        self.assertEqual(
            pacgen.classify_regex(pacgen.HOST_ANCHOR + r'example\.com' + pacgen.HOST_BOUNDARY),
            ('host', 'example.com'))

    def test_wildcard_host_has_no_prefilter(self):
        # `.*` 可以跨过 `/`，字面量未必落在主机结尾，不能当后缀过滤
        self.assertEqual(
            pacgen.classify_regex(pacgen.HOST_ANCHOR + r'boxun.*\.azurewebsites\.net'),
            ('url', ''))

    def test_unanchored_regex_has_no_prefilter(self):
        self.assertEqual(pacgen.classify_regex(r'^http://.*\.pimg\.tw/'), ('url', ''))

    def test_char_class_in_host_has_no_prefilter(self):
        pattern = pacgen.HOST_ANCHOR + r'[a-z0-9]+\.example\.com'
        self.assertEqual(pacgen.classify_regex(pattern), ('url', ''))


class ShellPatternTest(unittest.TestCase):
    def test_plain_substring(self):
        self.assertEqual(pacgen._shell_literal('*github*'), 'github')

    def test_question_mark_is_not_literal(self):
        self.assertIsNone(pacgen._shell_literal('*git?ub*'))

    def test_non_ascii_falls_back_to_regex(self):
        self.assertIsNone(pacgen._shell_literal('*中文*'))

    def test_shell_to_regex_escapes_metachars(self):
        self.assertEqual(pacgen.shell_to_regex('*a.b*'), r'.*a\.b.*')


class BucketTest(unittest.TestCase):
    def setUp(self):
        self.buckets = pacgen.build_buckets(
            ['@@||direct.example', '||proxy.example', '! comment'],
            ['||gfw.example', '@@||gfw-direct.example'])

    def test_precedence_order(self):
        self.assertIn('direct.example', self.buckets[0]['hosts'])
        self.assertIn('proxy.example', self.buckets[1]['hosts'])
        self.assertIn('gfw-direct.example', self.buckets[2]['hosts'])
        self.assertIn('gfw.example', self.buckets[3]['hosts'])

    def test_no_rule_is_dropped(self):
        hosts = sum(len(b['hosts']) for b in self.buckets)
        rules = sum(len(b['rules']) for b in self.buckets)
        self.assertEqual(hosts + rules, 4)

    def test_render_is_ascii_and_deterministic(self):
        runtime = RUNTIME.read_text(encoding='utf-8')
        first = pacgen.render_pac('PROXY 127.0.0.1:8080', self.buckets, runtime, ['test'])
        second = pacgen.render_pac('PROXY 127.0.0.1:8080', self.buckets, runtime, ['test'])
        self.assertEqual(first, second)
        first.encode('ascii')
        self.assertIn('function FindProxyForURL', first)


class GenpacCompatTest(unittest.TestCase):
    def test_caret_rule_parses_on_modern_python(self):
        # 这条规则会走到 genpac 里 re.sub 的替换模板分支，Python 3.12+ 会报 bad escape
        parsed = pacgen.parse_precise(['||example.com^'])
        self.assertTrue(parsed[2], 'proxy 正则槽位不应为空')
        self.assertIn('example', parsed[2][0])

    def test_shim_is_idempotent(self):
        genpac_compat.install()
        self.assertFalse(genpac_compat.install())


class GfwlistInputTest(unittest.TestCase):
    def test_base64_and_plain_text(self):
        body = '[AutoProxy 0.2.9]\n! Last Modified: Fri, 20 Nov 2026 20:39:38 +0800\n||a.example\n'
        with tempfile.TemporaryDirectory() as tmp:
            encoded = Path(tmp) / 'list.txt'
            encoded.write_bytes(base64.b64encode(body.encode()))
            lines, source, modified = load_gfwlist(str(encoded))
            self.assertEqual(lines, ['! Last Modified: Fri, 20 Nov 2026 20:39:38 +0800',
                                     '||a.example'])
            self.assertEqual(modified, 'Fri, 20 Nov 2026 20:39:38 +0800')
            self.assertEqual(source, str(encoded))

            plain = Path(tmp) / 'plain.txt'
            plain.write_text(body, encoding='utf-8')
            lines, _, _ = load_gfwlist(str(plain))
            self.assertIn('||a.example', lines)

    def test_std_datetime(self):
        self.assertRegex(std_datetime('Fri, 20 Nov 2026 20:39:38 +0800'),
                         r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')
        self.assertEqual(std_datetime('-'), '-')


class UserRulesTest(unittest.TestCase):
    def test_proxy_directive_is_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'custom.txt'
            path.write_text('! proxy: SOCKS5 1.2.3.4:1080; DIRECT;\n||a.example\n',
                            encoding='utf-8')
            lines, proxy = load_user_rules([str(path)])
            self.assertEqual(proxy, 'SOCKS5 1.2.3.4:1080; DIRECT;')
            self.assertIn('||a.example', lines)
            # 指令行本身留在规则行里，对 genpac 而言就是注释
            self.assertIn('! proxy: SOCKS5 1.2.3.4:1080; DIRECT;', lines)

    def test_first_proxy_directive_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / 'a.txt'
            first.write_text('! proxy: PROXY 10.0.0.1:8080\n', encoding='utf-8')
            second = Path(tmp) / 'b.txt'
            second.write_text('! proxy: PROXY 10.0.0.2:8080\n', encoding='utf-8')
            _, proxy = load_user_rules([str(first), str(second)])
            self.assertEqual(proxy, 'PROXY 10.0.0.1:8080')

    def test_repo_custom_file_has_proxy(self):
        _, proxy = load_user_rules([str(ROOT / 'custom.txt')])
        self.assertIn('PROXY', proxy)


class SampleUrlsTest(unittest.TestCase):
    def setUp(self):
        self.buckets = pacgen.build_buckets([], ['||blocked.example', '||direct.example'])

    def test_parity_urls_cover_rules(self):
        urls = sample_urls(self.buckets)
        self.assertIn('https://blocked.example/', urls)
        self.assertTrue(any(u.startswith('http://a.b.blocked.example:8080') for u in urls))

    def test_boundary_urls_append_foreign_domain(self):
        urls = strict_urls(self.buckets)
        self.assertTrue(urls)
        for url in urls:
            self.assertTrue(url.endswith('.example.net/'), url)


class RuntimeTemplateTest(unittest.TestCase):
    def test_runtime_is_es3_only(self):
        self.assertEqual(lint_runtime(RUNTIME), [])

    def test_runtime_has_conf_placeholder(self):
        self.assertIn('__CONF__', RUNTIME.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
