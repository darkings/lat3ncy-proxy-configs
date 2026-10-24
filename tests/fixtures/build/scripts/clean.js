// Fixture hosted script (synthetic, not upstream code).
// Used by tests/fixtures/build/DemoScript.lpx.
async function clean(payload) {
  return { ok: true, marker: "clean-v1", payload };
}

module.exports = { clean };
