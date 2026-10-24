"use strict";
// Generic sandbox runner for Stash/Loon scripts used by the conversion tests.
//
// Usage: node node_runner.cjs <scriptPath> <contextJsonPath> <resultJsonPath>
//
// The context JSON may contain:
//   request            - object exposed as $request (default {})
//   response           - object exposed as $response (default {})
//   responseBodyBase64 - base64 bytes exposed as $response.body (Uint8Array)
//   argument           - value exposed as $argument; when the property is
//                        absent $argument stays `undefined` (Stash: no argument)
//   persistentStore    - object with read/write stubs (optional)
//
// The runner executes the script synchronously in a Node `vm` context, captures
// console output / $notify calls and writes a JSON result:
//   { done: { called, value, thrown }, logs: [{level, args}] }
// Binary values returned through $done (e.g. {body: Uint8Array}) are serialized
// as {__bytes: "base64", data, length}.
const fs = require("fs");
const vm = require("vm");

const [scriptPath, contextPath, resultPath] = process.argv.slice(2);
const spec = JSON.parse(fs.readFileSync(contextPath, "utf8"));
const logs = [];

let doneValue;
let doneCalled = false;

const ctx = {
  $request: spec.request || {},
  $response: spec.response || {},
  $done(value) {
    doneValue = value;
    doneCalled = true;
  },
  $notify(...args) {
    logs.push({ level: "notify", args: args.map(String) });
  },
  $persistentStore: spec.persistentStore || { read: () => null, write: () => true },
  $prefs: { valueForKey: () => null, setValueForKey: () => true },
  console: {
    log: (...args) => logs.push({ level: "log", args: args.map(String) }),
    warn: (...args) => logs.push({ level: "warn", args: args.map(String) }),
    error: (...args) => logs.push({ level: "error", args: args.map(String) }),
    info: (...args) => logs.push({ level: "info", args: args.map(String) }),
  },
  setTimeout,
  clearTimeout,
  Uint8Array,
  ArrayBuffer,
  DataView,
  TextDecoder,
  TextEncoder,
  JSON,
  Math,
  Date,
  RegExp,
  String,
  Number,
  Boolean,
  Object,
  Array,
  Promise,
};

if (Object.prototype.hasOwnProperty.call(spec, "argument")) {
  ctx.$argument = spec.argument;
}
if (Object.prototype.hasOwnProperty.call(spec, "responseBodyBase64")) {
  ctx.$response.body = Buffer.from(spec.responseBodyBase64, "base64");
}

let thrown = null;
try {
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(scriptPath, "utf8"), ctx, {
    filename: scriptPath,
    timeout: 5000,
  });
} catch (error) {
  thrown = String((error && error.stack) || error);
}

function serializeDone(value) {
  if (!doneCalled) return { called: false, thrown };
  if (value === undefined) return { called: true, value: null, thrown };
  if (value === null || typeof value !== "object") return { called: true, value, thrown };
  const out = {};
  for (const [key, item] of Object.entries(value)) {
    if (item instanceof Uint8Array || Buffer.isBuffer(item)) {
      out[key] = {
        __bytes: "base64",
        data: Buffer.from(item).toString("base64"),
        length: item.length,
      };
    } else {
      out[key] = item;
    }
  }
  return { called: true, value: out, thrown };
}

fs.writeFileSync(resultPath, JSON.stringify({ done: serializeDone(doneValue), logs }), "utf8");
