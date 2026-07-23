/**
 * Pinduoduo homepage cleanup for Loon.
 * Removes server-delivered and buffered bottom tabs on every matched response.
 */

const allowedBottomLinks = new Set([
  "index.html",
  "chat_list.html",
  "personal.html",
]);

function linkPage(link) {
  if (typeof link !== "string") return "";
  return link.split(/[?#]/, 1)[0].split("/").pop();
}

function cleanResult(result) {
  if (!result || typeof result !== "object") return;

  delete result.icon_set;
  delete result.search_bar_hot_query;
  if (result.dy_module && typeof result.dy_module === "object") {
    delete result.dy_module.irregular_banner_dy;
  }

  for (const key of ["bottom_tabs", "buffer_bottom_tabs"]) {
    if (Array.isArray(result[key])) {
      result[key] = result[key].filter((item) =>
        item && allowedBottomLinks.has(linkPage(item.link))
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
}

function cleanHomepage(payload) {
  if (!payload || typeof payload !== "object") return payload;

  const candidates = [
    payload.result,
    payload.data && payload.data.result,
    payload.data,
  ];
  const visited = new Set();
  for (const result of candidates) {
    if (!result || typeof result !== "object" || visited.has(result)) continue;
    visited.add(result);
    cleanResult(result);
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
  headers["Content-Type"] = "application/json; charset=utf-8";
  return headers;
}

if (typeof $response !== "undefined") {
  try {
    const payload = JSON.parse($response.body || "{}");
    $done({
      headers: normalizeHeaders($response.headers),
      body: JSON.stringify(cleanHomepage(payload)),
    });
  } catch (error) {
    console.log(`Pinduoduo homepage cleanup skipped: ${error}`);
    $done({});
  }
}

if (typeof module !== "undefined") {
  module.exports = { cleanHomepage, cleanResult, normalizeHeaders };
}
