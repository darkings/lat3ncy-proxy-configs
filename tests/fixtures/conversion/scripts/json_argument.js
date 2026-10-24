// Synthetic offline fixture (NOT an upstream script): exercises the JSON-object
// $argument protocol implemented by the real Spotify/Bilibili scripts.
const params = JSON.parse($argument);
const body = $response.body;
$done({ body: JSON.stringify({ params: params, keyCount: Object.keys(params).length }) + String(body) });
