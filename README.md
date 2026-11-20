# gfwlist2pac

每 6 小时把 [gfwlist](https://github.com/gfwlist/gfwlist) 编译成 PAC，并自动提交。

**在线地址**

- GitHub Raw: <https://raw.githubusercontent.com/bingqingpeng/gfwlist2pac/master/gfwlist.pac>
- jsDelivr: <https://cdn.jsdelivr.net/gh/bingqingpeng/gfwlist2pac@master/gfwlist.pac>

## 结构

```
gfwlist  ──►  genpac 3.0.1（只负责解析，语义基线）
                    │  precise 规则：user/gfwlist × direct/proxy
                    ▼
            tools/pacgen.py（规则重组：域名后缀表 + URL 正则）
                    │
                    ▼
            pac/runtime.js（PAC 运行时，面向 Windows 内置引擎）
                    │
                    ▼
                 gfwlist.pac
```

自定义规则写在 [`custom-proxy.txt`](custom-proxy.txt)：`@@||域名` 直连，`||域名` 走代理，
每 6 小时构建时合并，优先级高于 gfwlist。

## 相对旧版工作流的修复

升到 genpac 3.0.1 不是改个版本号的事，旧工作流在 3.0.1 下会**静默产出错误的 PAC**：

| # | 问题 | 旧行为 | 现在 |
|---|------|--------|------|
| 1 | 3.0.1 里 `--gfwlist-url -` 等于"禁用 gfwlist"（`fetch_gfwlist` 直接返回空） | 只输出 1.8 KB、仅含自定义规则的 PAC，无任何报错 | 不再传 `-`，直接读 `--gfwlist` 本地文件 |
| 2 | 3.0.1 必须显式指定格式 | `-f pac` 缺失时直接 FatalError | 走库接口，格式固定为 pac |
| 3 | 3.0.1 在 Python ≥3.12 上 `re.sub` 替换模板报 `bad escape \w` | 用 `sed` 改 genpac 源码，模板行数一变补丁就静默失效 | `tools/genpac_compat.py` 内存兼容层，带断言与单测 |
| 4 | 3.0.1 移除了 `setup.py`，且依赖钉死旧版本（`PyYAML==6.0.1`） | `python3 setup.py install` 直接失败 | `pip install -r requirements.txt` + `--no-deps` 装 genpac |
| 5 | 旧 `Update` 步骤只有 `set -x`，没有 `-e` | genpac 失败后 `git commit ... \|\| exit 0` 让 workflow 显示成功 | `set -euo pipefail`，先校验再提交 |
| 6 | 模板带 `Generated:` 时间戳 | 每 6 小时必然产生一次提交 | 输出不含构建时间，规则不变就不提交 |
| 7 | 用 `sed '1,7d'` 砍头注释 | 模板行数一变就会砍掉代码 | 生成器自己输出 4 行确定性头注释 |

`python -m tools.build_pac --engine genpac` 可以生成与原版 genpac 完全一致的 PAC（正文逐字节相同），
用于排查"到底是规则问题还是运行时问题"。

## PAC 后端优化

旧模板每次 `FindProxyForURL` 都会对 4000+ 条规则执行 `new RegExp(pattern).test(url)`——
**每次请求重新编译 4000 多个正则**，在 Windows 的 JScript 引擎上单次判定可达几十毫秒。

现在：

1. **分桶**：genpac 的 2×4 槽位按 `(来源, 决策)` 合成 4 个桶，桶内顺序无关（返回值相同），
   顺序即 `user-direct > user-proxy > gfwlist-direct > gfwlist-proxy`。
2. **域名后缀哈希表**：占 99% 的 `||域名` 规则变成一张表，查表只跟主机标签数有关（3~5 次），
   与规则总量无关。
3. **正则只在加载期编译一次**，并给能证明的规则加主机后缀预过滤（只跳过必然不匹配的规则）。
4. **通配符规则**：`*字面量*` 直接退化成 `indexOf`，不再走正则。
5. 输出 88 KB（原 296 KB），解析更快。

实测（对拍脚本，5398 条 URL，Node 参考值）：`2.38 ms/URL → 0.004 ms/URL`，约 **600 倍**。
Windows 的 JScript 比 V8 慢一个量级，绝对差距只会更大。

## Windows 引擎适配

WinINet / WinHTTP 的 PAC 解释器是 JScript 5.8（≈ ES3），很多现代写法直接报错：

- 只用 ES3：无 `const`/`let`/箭头函数/模板字符串/`JSON`/`Promise`；
- 不用 `String#trim`/`#endsWith`/`#includes`、`Array#forEach`/`#map`/`#filter`、`Object.keys`；
- 输出纯 ASCII（非 ASCII 全部 `\uXXXX` 转义），避免引擎按 ANSI 解码乱码；
- 判定只依赖 `url`/`host`，不含时间、随机、DNS，结果稳定可被引擎缓存；
- 正则在脚本加载时编译，请求时只做匹配。

`tools/verify_pac.py` 会扫描运行时模板，出现上述禁用写法直接判失败。

> 代理串沿用原配置 `SOCKS5 ...; PROXY ...; DIRECT;`。Windows 文档里 PAC 返回值只保证
> `DIRECT` / `PROXY` / `SOCKS`；若发现 Windows 侧没有走代理，把 workflow 里的
> `PAC_PROXY` 改成以 `PROXY 192.168.30.3:7890;` 开头即可。

## 与 genpac 基线的唯一行为差异（刻意收紧）

genpac 的 precise 正则 `^..(?:[^/]+\.)?<域名>` **末尾没有主机边界**，所以 `||12vpn.net`
连 `12vpn.net.example.net`、`||t.co` 连 `t.com` 都会命中——这比 gfwlist 里 `||` 的本意
（该域名及其子域）更宽。

本运行时按标签边界匹配（`host == domain` 或 `host` 以 `.domain` 结尾），因此：

- 不会比基线少放行：我们的匹配集合是基线的子集，不存在"该代理却没代理"的新增情况；
- `domain.other.tld` 这类主机不再被误判为走代理。

`tools/verify_pac.py --strict-urls` 会对这批 URL 断言必须 `DIRECT`；其余 5000+ 条 URL
要求与基线**逐条一致**。

## 本地构建

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install --no-deps "genpac==3.0.1"

curl -sL https://raw.githubusercontent.com/gfwlist/gfwlist/master/gfwlist.txt -o /tmp/gfwlist.txt

python -m tools.build_pac \
  --gfwlist /tmp/gfwlist.txt \
  --user-rule-from custom-proxy.txt \
  --proxy "SOCKS5 192.168.30.3:7891; PROXY 192.168.30.3:7890; DIRECT;" \
  -o gfwlist.pac \
  --baseline-out /tmp/genpac-baseline.pac \
  --sample-urls-out /tmp/parity-urls.txt \
  --strict-urls-out /tmp/boundary-urls.txt

python -m tools.verify_pac \
  --candidate gfwlist.pac \
  --baseline /tmp/genpac-baseline.pac \
  --urls /tmp/parity-urls.txt \
  --strict-urls /tmp/boundary-urls.txt

python -m unittest discover -s tests
```

## 目录

| 路径 | 说明 |
|------|------|
| `.github/workflows/update.yml` | 每 6 小时构建 → 对拍 → 提交 |
| `pac/runtime.js` | PAC 运行时模板（ES3，`__CONF__` 由生成器注入） |
| `tools/genpac_compat.py` | genpac 3.0.1 在新 Python 上的兼容层 |
| `tools/pacgen.py` | 规则重组：分桶、后缀表、正则预过滤、PAC 渲染 |
| `tools/build_pac.py` | 构建入口（`--engine optimized\|genpac`） |
| `tools/verify_pac.py` | 对拍 + 主机边界检查 + Windows 引擎语法检查 |
| `tests/test_pacgen.py` | 单元测试 |
| `custom-proxy.txt` | 自定义规则（直连/代理） |
| `gfwlist.pac` | 产物，由 workflow 自动更新，勿手改 |
