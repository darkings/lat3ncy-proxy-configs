"use strict";

const assert = require("node:assert/strict");
const { main, managedRules } = require("../clash-verge-windows.js");

const source = {
  dns: {
    "fake-ip-filter": ["existing.example"],
    "nameserver-policy": { "existing.example": "system" },
  },
  tun: {
    "route-exclude-address": ["192.0.2.0/24"],
  },
  "rule-providers": {
    Existing: { type: "file", behavior: "domain", path: "./existing.yaml" },
  },
  rules: ["DOMAIN,existing.example,DIRECT", "MATCH,Existing-Policy"],
};

const result = main(source);
assert.equal(result, source, "main should mutate and return the input config");
assert.ok(result.dns["fake-ip-filter"].includes("existing.example"));
assert.ok(result.dns["fake-ip-filter"].includes("+.ts.net"));
assert.equal(result.dns["nameserver-policy"]["existing.example"], "system");
assert.equal(result.dns["nameserver-policy"]["+.ts.net"], "system");
assert.ok(result.tun["route-exclude-address"].includes("192.0.2.0/24"));
assert.ok(result.tun["route-exclude-address"].includes("100.64.0.0/10"));
assert.ok(result["rule-providers"].Existing, "existing providers must remain");
assert.ok(result["rule-providers"]["Cats-Team-AdRules"]);
assert.ok(result["rule-providers"]["Windows-Private-Domain"]);
assert.ok(result["rule-providers"]["Windows-Private-IP"]);
assert.deepEqual(result.rules.slice(0, managedRules.length), managedRules);
assert.ok(result.rules.includes("DOMAIN,existing.example,DIRECT"));
assert.ok(result.rules.includes("MATCH,Existing-Policy"));

main(result);
for (const rule of managedRules) {
  assert.equal(result.rules.filter((item) => item === rule).length, 1, `duplicate rule: ${rule}`);
}
assert.equal(
  result.dns["fake-ip-filter"].filter((item) => item === "+.ts.net").length,
  1,
  "duplicate fake-IP exclusion"
);
assert.equal(
  result.tun["route-exclude-address"].filter((item) => item === "100.64.0.0/10").length,
  1,
  "duplicate TUN exclusion"
);

console.log("PASS: Windows Clash Verge extension script behavior validation");
