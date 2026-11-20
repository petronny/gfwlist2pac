/*
 * gfwlist2pac PAC runtime.
 *
 * Target engine: the PAC interpreter shipped with Windows (WinINet / WinHTTP,
 * JScript 5.8, roughly ES3). Keep this file free of ES5+ syntax and APIs:
 * no const/let/arrow/template literals, no JSON, no String#trim/#endsWith,
 * no Array#forEach/#map/#filter, no Object.keys.
 *
 * Structure (see tools/pacgen.py):
 *   HOSTMAPS[i] - suffix hash table, one lookup per host label
 *   RULES[i]    - URL regexps, each with an optional host suffix prefilter
 * Bucket order is the decision precedence:
 *   0 user/direct > 1 user/proxy > 2 gfwlist/direct > 3 gfwlist/proxy
 *
 * gfwlist.pac is generated - edit tools/ or pac/runtime.js instead.
 */
var CONF = __CONF__;

var proxy = CONF.proxy;
var DIRECT = 'DIRECT';
var DECISIONS = [DIRECT, proxy, DIRECT, proxy];

var HOSTMAPS = CONF.hosts;
var RULES = [];

(function () {
    var raw = CONF.rules;
    for (var i = 0; i < raw.length; i++) {
        var bucket = [];
        for (var j = 0; j < raw[i].length; j++) {
            var entry = raw[i][j];
            if (entry[0] === 1) {
                /* literal substring, equivalent to shExpMatch("*lit*") */
                bucket.push([1, entry[1], '']);
                continue;
            }
            var re;
            try {
                re = new RegExp(entry[1], entry[2]);
            } catch (e) {
                continue;
            }
            bucket.push([0, re, entry[3]]);
        }
        RULES.push(bucket);
    }
})();

/* True when host equals domain or ends with "." + domain. */
function endsWithDomain(host, domain) {
    var diff = host.length - domain.length;
    if (diff === 0) {
        return host === domain;
    }
    return diff > 0 && host.substring(diff - 1) === '.' + domain;
}

/* Probe the suffix table: "a.b.example.com" -> a.b.example.com, b.example.com, ... */
function hasSuffix(map, host) {
    var s = host;
    for (;;) {
        if (map[s] === 1) {
            return true;
        }
        var dot = s.indexOf('.');
        if (dot < 0) {
            return false;
        }
        s = s.substring(dot + 1);
    }
}

function matchUrl(list, url, host) {
    var lowUrl = null;
    for (var i = 0; i < list.length; i++) {
        var item = list[i];
        var filter = item[2];
        if (filter !== '' && !endsWithDomain(host, filter)) {
            continue;
        }
        if (item[0] === 1) {
            if (lowUrl === null) {
                lowUrl = url.toLowerCase();
            }
            if (lowUrl.indexOf(item[1]) >= 0) {
                return true;
            }
        } else if (item[1].test(url)) {
            return true;
        }
    }
    return false;
}

function FindProxyForURL(url, host) {
    for (var i = 0; i < 4; i++) {
        if (hasSuffix(HOSTMAPS[i], host) || matchUrl(RULES[i], url, host)) {
            return DECISIONS[i];
        }
    }
    return DIRECT;
}
