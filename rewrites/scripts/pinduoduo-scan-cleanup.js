/*
 * 拼多多扫码取件页净化
 * Upstream: KeLee / ZenmoFeiShi, https://hub.kelee.one
 * Audited: 2026-07-19
 * Official chunk SHA-256: 225f289a66783c891f44a79d23929f3efb22803e83cf583cb9741212cb670c4d
 * KeLee upstream chunk SHA-256: e8e4f0e81525d314a7bc6af0fa45600b7d49b098f71f53ab36155d9ebd327ba7
 * Repository chunk SHA-256 (one final LF): 717a457110a7dde0d4a74d32114e97ecb7642eca133a9f3eb11e402fa936ef0f
 */
let body = $response.body || "";

const oldChunk = "https://pfile.pddpic.com/mdkd/mdkd/_next/static/chunks/9410-b8806e870a26db7d.js";
const newChunk = "https://cdn.jsdelivr.net/gh/darkings/lat3ncy-quantumultx-config@93955a63afe561b665d6dab49c9dcc4ea257ceb5/rewrites/vendor/pinduoduo/9410-b8806e870a26db7d.js";

function replaceAllText(text, from, to) {
  let pos = text.indexOf(from);
  while (pos !== -1) {
    text = text.slice(0, pos) + to + text.slice(pos + from.length);
    pos = text.indexOf(from, pos + to.length);
  }
  return text;
}

function removeGifContainer(html) {
  const marker = "index_gif-container";
  let pos = html.indexOf(marker);

  while (pos !== -1) {
    const open = html.lastIndexOf("<div", pos);
    if (open === -1) break;

    let i = open;
    let depth = 0;
    let end = -1;

    while (i < html.length) {
      const nextOpen = html.indexOf("<div", i);
      const nextClose = html.indexOf("</div>", i);
      if (nextClose === -1) break;

      if (nextOpen !== -1 && nextOpen < nextClose) {
        depth++;
        i = nextOpen + 4;
      } else {
        depth--;
        i = nextClose + 6;
        if (depth === 0) {
          end = i;
          break;
        }
      }
    }

    if (end === -1) break;

    html = html.slice(0, open) + html.slice(end);
    pos = html.indexOf(marker, open);
  }

  return html;
}

function trimNextData(html) {
  const idNeedle = 'id="__NEXT_DATA__"';
  const idPos = html.indexOf(idNeedle);
  if (idPos === -1) return html;

  const tagStart = html.lastIndexOf("<script", idPos);
  if (tagStart === -1) return html;

  const contentStart = html.indexOf(">", tagStart);
  if (contentStart === -1) return html;

  const tagEnd = html.indexOf("</script>", contentStart);
  if (tagEnd === -1) return html;

  const jsonText = html.slice(contentStart + 1, tagEnd);

  try {
    const data = JSON.parse(jsonText);
    const serverData = data &&
      data.props &&
      data.props.pageProps &&
      data.props.pageProps.serverData;

    if (Array.isArray(serverData)) {
      data.props.pageProps.serverData = serverData.filter(item =>
        item &&
        (item.key === "fastBindCMobilePreCheck" ||
         item.key === "queryStationPackageInfo")
      );
    }

    return html.slice(0, contentStart + 1) +
      JSON.stringify(data) +
      html.slice(tagEnd);
  } catch (e) {
    return html;
  }
}

body = replaceAllText(body, oldChunk, newChunk);
body = removeGifContainer(body);
body = trimNextData(body);

$done({ body });
