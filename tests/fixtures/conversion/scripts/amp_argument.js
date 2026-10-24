// Synthetic offline fixture: the WeatherKit response.bundle.js protocol splits
// the $argument string on "&" and then on "=" (amp-key-value).
const raw = typeof $argument === "string" ? $argument : "";
const pairs = raw.split("&").map((pair) => pair.split("="));
$done({ body: JSON.stringify(pairs) });
