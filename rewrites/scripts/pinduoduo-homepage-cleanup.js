/**
 * Pinduoduo homepage cleanup for Quantumult X.
 * Mirrors the response mutations observed from KeLee's Loon plugin.
 */

const allowedBottomLinks = new Set([
  "index.html",
  "chat_list.html",
  "personal.html",
]);

function cleanHomepage(payload) {
  const result = payload && payload.result;
  if (!result || typeof result !== "object") return payload;

  delete result.icon_set;
  delete result.search_bar_hot_query;
  if (result.dy_module && typeof result.dy_module === "object") {
    delete result.dy_module.irregular_banner_dy;
  }

  for (const key of ["bottom_tabs", "buffer_bottom_tabs"]) {
    if (Array.isArray(result[key])) {
      result[key] = result[key].filter((item) =>
        item && allowedBottomLinks.has(item.link)
      );
    }
  }

  if (Array.isArray(result.all_top_opts)) {
    for (const item of result.all_top_opts) {
      if (!item || typeof item !== "object") continue;
      delete item.selected_image;
      delete item.image;
      delete item.height;
      delete item.width;
    }
  }

  return payload;
}

function normalizeHeaders(source) {
  const headers = { ...(source || {}) };
  const removed = new Set([
    "connection",
    "content-encoding",
    "content-length",
    "content-type",
    "transfer-encoding",
  ]);

  for (const key of Object.keys(headers)) {
    if (removed.has(key.toLowerCase())) delete headers[key];
  }
  headers["Content-Type"] = "application/json";
  return headers;
}

if (typeof $response !== "undefined") {
  try {
    const payload = JSON.parse($response.body || "{}");
    $done({
      headers: normalizeHeaders($response.headers),
      body: JSON.stringify(cleanHomepage(payload)),
    });
  } catch (_) {
    $done({});
  }
}

if (typeof module !== "undefined") {
  module.exports = { cleanHomepage, normalizeHeaders };
}
