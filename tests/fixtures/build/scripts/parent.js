// Fixture parent script (synthetic, not upstream code).
// It references a runtime chunk by absolute URL: the resource resolver must
// mirror the chunk and substitute the content-addressed URL before hashing the
// parent, so a chunk-only change changes the parent asset too.
const CHUNK_URL = "https://kelee.one/Resource/JavaScript/Demo/chunk-abc123def456.js";

module.exports = { chunk: CHUNK_URL, marker: "parent-v1" };
