/**
 * Clash Verge Rev Windows extension script.
 *
 * Scope: Windows, WSL, private networks, Tailscale, DNS safety, domain-level
 * advertising, and selected applications that have native Windows clients.
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
  "RULE-SET,Windows-Spotify,Windows-Spotify",
  "RULE-SET,Windows-Telegram-Domain,Windows-Telegram",
  "RULE-SET,Windows-Telegram-IP,Windows-Telegram,no-resolve",
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
  "Windows-Spotify": {
    type: "http",
    behavior: "domain",
    format: "mrs",
    url: "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo/geosite/spotify.mrs",
    path: "./ruleset/windows-spotify.mrs",
    interval: 86400,
  },
  "Windows-Telegram-Domain": {
    type: "http",
    behavior: "domain",
    format: "mrs",
    url: "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo/geosite/telegram.mrs",
    path: "./ruleset/windows-telegram-domain.mrs",
    interval: 86400,
  },
  "Windows-Telegram-IP": {
    type: "http",
    behavior: "ipcidr",
    format: "mrs",
    url: "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo/geoip/telegram.mrs",
    path: "./ruleset/windows-telegram-ip.mrs",
    interval: 86400,
  },
};

const managedGroups = [
  {
    name: "Windows-Auto",
    type: "url-test",
    "include-all": true,
    url: "https://www.gstatic.com/generate_204",
    interval: 600,
    tolerance: 50,
    lazy: true,
  },
  {
    name: "Windows-Spotify",
    type: "select",
    proxies: ["Windows-Auto", "DIRECT"],
    "include-all": true,
  },
  {
    name: "Windows-Telegram",
    type: "select",
    proxies: ["Windows-Auto", "DIRECT"],
    "include-all": true,
  },
];

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

function mergeGroups(original, additions) {
  const result = Array.isArray(original) ? original.slice() : [];
  for (const group of additions) {
    if (!result.some((item) => item && item.name === group.name)) {
      result.push(group);
    }
  }
  return result;
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

  config["proxy-groups"] = mergeGroups(config["proxy-groups"], managedGroups);
  config.rules = prependUnique(config.rules, managedRules);
  return config;
}

if (typeof module !== "undefined") {
  module.exports = {
    main,
    managedRules,
    managedProviders,
    managedGroups,
    fakeIpExclusions,
    tailscaleRouteExclusions,
  };
}
