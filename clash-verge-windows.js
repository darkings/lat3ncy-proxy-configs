/**
 * Clash Verge Rev Windows extension script.
 *
 * Scope: Windows, WSL, private networks, Tailscale, DNS safety and domain-level
 * advertising. It intentionally adds no mobile-app or entertainment-app rules.
 */

const managedRules = [
  "DOMAIN-SUFFIX,ts.net,DIRECT",
  "DOMAIN-SUFFIX,tailscale.com,DIRECT",
  "IP-CIDR,100.64.0.0/10,DIRECT,no-resolve",
  "IP-CIDR6,fd7a:115c:a1e0::/48,DIRECT,no-resolve",
  "DOMAIN,dns.msftncsi.com,DIRECT",
  "DOMAIN,www.msftncsi.com,DIRECT",
  "DOMAIN,www.msftconnecttest.com,DIRECT",
  "DOMAIN,ipv6.msftconnecttest.com,DIRECT",
  "RULE-SET,Windows-Private-Domain,DIRECT",
  "RULE-SET,Windows-Private-IP,DIRECT,no-resolve",
  "RULE-SET,Cats-Team-AdRules,REJECT",
];

const managedProviders = {
  "Cats-Team-AdRules": {
    type: "http",
    behavior: "domain",
    format: "mrs",
    url: "https://raw.githubusercontent.com/Cats-Team/AdRules/main/adrules-mihomo.mrs",
    path: "./ruleset/cats-team-adrules.mrs",
    interval: 21600,
  },
  "Windows-Private-Domain": {
    type: "http",
    behavior: "domain",
    format: "mrs",
    url: "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo/geosite/private.mrs",
    path: "./ruleset/windows-private-domain.mrs",
    interval: 86400,
  },
  "Windows-Private-IP": {
    type: "http",
    behavior: "ipcidr",
    format: "mrs",
    url: "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo/geoip/private.mrs",
    path: "./ruleset/windows-private-ip.mrs",
    interval: 86400,
  },
};

const fakeIpExclusions = [
  "*.lan",
  "*.local",
  "*.arpa",
  "time.*.com",
  "ntp.*.com",
  "localhost.ptlogin2.qq.com",
  "*.msftncsi.com",
  "www.msftconnecttest.com",
  "+.ts.net",
  "+.tailscale.com",
];

const tailscaleRouteExclusions = [
  "100.64.0.0/10",
  "fd7a:115c:a1e0::/48",
];

function mergeUnique(original, additions) {
  const result = Array.isArray(original) ? original.slice() : [];
  for (const value of additions) {
    if (!result.includes(value)) result.push(value);
  }
  return result;
}

function prependUnique(original, additions) {
  const additionSet = new Set(additions);
  const remainder = Array.isArray(original)
    ? original.filter((value) => !additionSet.has(value))
    : [];
  return additions.concat(remainder);
}

function main(config) {
  if (!config || typeof config !== "object") return config;

  config.dns = config.dns || {};
  config.dns["fake-ip-filter"] = mergeUnique(
    config.dns["fake-ip-filter"],
    fakeIpExclusions
  );
  config.dns["nameserver-policy"] = config.dns["nameserver-policy"] || {};
  config.dns["nameserver-policy"]["+.ts.net"] = "system";
  config.dns["nameserver-policy"]["+.tailscale.com"] = "system";

  config.tun = config.tun || {};
  config.tun["route-exclude-address"] = mergeUnique(
    config.tun["route-exclude-address"],
    tailscaleRouteExclusions
  );

  config["rule-providers"] = config["rule-providers"] || {};
  for (const name of Object.keys(managedProviders)) {
    config["rule-providers"][name] = managedProviders[name];
  }

  config.rules = prependUnique(config.rules, managedRules);
  return config;
}

if (typeof module !== "undefined") {
  module.exports = {
    main,
    managedRules,
    managedProviders,
    fakeIpExclusions,
    tailscaleRouteExclusions,
  };
}
