# gfwlist2pac

Compiles [gfwlist](https://github.com/gfwlist/gfwlist) into a PAC file every 6 hours and commits the result.

**Endpoints**

- GitHub Raw: <https://raw.githubusercontent.com/bingqingpeng/gfwlist2pac/master/gfwlist.pac>
- jsDelivr: <https://cdn.jsdelivr.net/gh/bingqingpeng/gfwlist2pac@master/gfwlist.pac>

## Customize (single file)

Everything you normally need to change lives in [`custom.txt`](custom.txt): the proxy string and your own rules.

```
! proxy: SOCKS5 192.168.30.3:7891; PROXY 192.168.30.3:7890; DIRECT;

@@||example.cn          ! bypass the proxy
||github.com            ! always use the proxy
```

- `! proxy: ...` — the value returned by `FindProxyForURL` when a rule matches. Change this line to
  replicate the setup with your own proxy; `--proxy` on the command line overrides it.
- `||example.com` — proxy for the domain and all its subdomains.
- `@@||example.com` — bypass the proxy for the domain and all its subdomains.
- `! ...` — comment (also valid gfwlist syntax, so genpac can parse the file as-is).

User rules are evaluated before gfwlist rules. Push a change to `custom.txt` and the workflow reruns
immediately; it does **not** shift the schedule below.

## Schedule

- Cron: `0 4,10,16,22 * * *` — 04:00, 10:00, 16:00, 22:00 **UTC** (12:00, 18:00, 00:00, 06:00 in UTC+8).
- `push` touching `custom.txt` on `master` triggers an extra run.
- Manual runs: `workflow_dispatch`.

Runs are serialized by a concurrency group, so a push-triggered run never overlaps a scheduled one.

## Pipeline

```
gfwlist ──► genpac 3.0.1 (parsing only, the semantic baseline)
                │  precise rules: user/gfwlist × direct/proxy
                ▼
         tools/pacgen.py (rebucket: host suffix table + URL regexps)
                │
                ▼
         pac/runtime.js (PAC runtime for the Windows built-in engine)
                │
                ▼
              gfwlist.pac
```

## Fixes required by the genpac 3.0.1 upgrade

Bumping the version was not enough — the old workflow produced a **silently broken** PAC on 3.0.1:

| # | Problem | Old behaviour | Now |
|---|---------|---------------|-----|
| 1 | In 3.0.1 `--gfwlist-url -` means "disable gfwlist" (`fetch_gfwlist` returns early) | Emitted a 1.8 KB PAC with only the custom rules, **no error** | No `-`; the local gfwlist file is read directly |
| 2 | 3.0.1 requires an explicit output format | Missing `-f pac` → FatalError | Library API, format is fixed |
| 3 | 3.0.1 raises `bad escape \w` from an `re.sub` replacement template on Python ≥ 3.12 | A `sed` patch edited genpac's source; it silently stopped matching when the template changed | `tools/genpac_compat.py`, an in-memory shim that falls back only on that error, covered by unit tests |
| 4 | 3.0.1 has no `setup.py` and pins `PyYAML==6.0.1` | `python3 setup.py install` failed | `pip install -r requirements.txt` + `--no-deps` for genpac |
| 5 | The update step used `set -x` without `-e` | genpac failed, `git commit ... \|\| exit 0` still reported success | `set -euo pipefail`, verify before commit |
| 6 | Template contained a `Generated:` timestamp | A commit every 6 hours even with identical rules | No build timestamp in the output; empty runs commit nothing |
| 7 | `sed '1,7d'` stripped the header | Any template change silently cut real code | The generator writes its own deterministic 4-line header |

`python -m tools.build_pac --engine genpac` renders the original genpac template from the same parsed
rules; its body is byte-identical to the genpac 3.0.1 CLI output, which makes it easy to tell "bad rules"
from "bad runtime".

## Why the PAC runtime was rewritten

The old template ran `new RegExp(pattern).test(url)` for every one of the 4000+ rules on **every**
`FindProxyForURL` call — thousands of regex compilations per request. On Windows' JScript engine a single
lookup could take tens of milliseconds.

The new runtime:

1. **Rebucketing** — genpac's 2×4 slots collapse into four `(source, decision)` buckets. Order inside a
   bucket is irrelevant (same return value), and the bucket order preserves the original precedence:
   `user-direct > user-proxy > gfwlist-direct > gfwlist-proxy`.
2. **Host suffix hash table** — the `||domain` rules (99% of gfwlist) become one lookup per host label
   (typically 3-5 lookups), independent of the rule count.
3. **Regexps compiled once at load**, each guarded by a host suffix prefilter when (and only when) it can
   be proven to be a necessary condition.
4. **Wildcard rules** degrade to `indexOf`.
5. Artifact size: 296 KB → 88 KB.

Measured with the parity harness (5398 URLs, Node reference): `2.28 ms/URL → 0.004 ms/URL`, roughly
**600×**. JScript is an order of magnitude slower than V8, so the absolute gap is larger.

## Windows PAC engine notes

WinINet / WinHTTP run PAC in JScript 5.8 (≈ ES3):

- ES3 only: no `const`/`let`/arrow functions/template literals/`JSON`/`Promise`.
- No `String#trim`/`#endsWith`/`#includes`, no `Array#forEach`/`#map`/`#filter`, no `Object.keys`.
- Pure ASCII output (non-ASCII escaped as `\uXXXX`) so an ANSI decode cannot corrupt it.
- Decisions depend only on `url`/`host` — no time, randomness or DNS — so results are stable and cacheable.
- Regexps are compiled at load time; requests only match.

`tools/verify_pac.py` fails the build if the runtime template uses a forbidden construct.

> The proxy string keeps the original `SOCKS5 ...; PROXY ...; DIRECT;` order. Windows documents only
> `DIRECT` / `PROXY` / `SOCKS` as PAC return values; if Windows ignores the first token, start the line in
> `custom.txt` with `PROXY 192.168.30.3:7890;`.

## The only intentional behaviour change

genpac's precise regex is `^..(?:[^/]+\.)?<domain>` with **no host boundary at the end**, so `||12vpn.net`
also matches `12vpn.net.example.net` and `||t.co` also matches `t.com` — wider than what `||` means in
gfwlist (the domain and its subdomains).

This runtime matches on label boundaries (`host == domain` or `host` ends with `.domain`), therefore:

- it never proxies less than the baseline intends: its match set is a subset of the baseline's, so no
  newly broken site can appear;
- hosts such as `domain.other.tld` are no longer sent to the proxy by accident.

`tools/verify_pac.py --strict-urls` asserts `DIRECT` for those hosts; the other 5000+ URLs must match the
baseline **exactly**. Use `--engine genpac` if you need the old behaviour verbatim.

## Local build

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install --no-deps "genpac==3.0.1"

curl -sL https://raw.githubusercontent.com/gfwlist/gfwlist/master/gfwlist.txt -o /tmp/gfwlist.txt

python -m tools.build_pac \
  --gfwlist /tmp/gfwlist.txt \
  --user-rule-from custom.txt \
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

## Layout

| Path | Purpose |
|------|---------|
| `.github/workflows/update.yml` | Build → verify → commit, on schedule and on `custom.txt` changes |
| `custom.txt` | Proxy string + your own rules (the only file you usually edit) |
| `pac/runtime.js` | PAC runtime template (ES3, `__CONF__` injected by the generator) |
| `tools/genpac_compat.py` | Compatibility shim for genpac 3.0.1 on modern Python |
| `tools/pacgen.py` | Rule rebucketing: suffix tables, regexp prefilters, PAC rendering |
| `tools/build_pac.py` | Build entry point (`--engine optimized\|genpac`) |
| `tools/verify_pac.py` | Parity run, host-boundary check, Windows engine lint |
| `tests/test_pacgen.py` | Unit tests |
| `gfwlist.pac` | Generated artifact — do not edit by hand |
