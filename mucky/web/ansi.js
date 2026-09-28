// Render mucky output lines (raw ANSI plus highlight/link ranges) as DOM nodes.
//
// Offsets in a core Line's ``highlights`` and ``links`` index into ``plain``,
// the text Rich's ``Text.from_ansi`` leaves after stripping escape codes. The
// parser below strips exactly what Rich strips so those offsets line up.

// xterm's default 16-color palette; 16-255 are computed.
const BASE16 = [
  "#000000", "#cd0000", "#00cd00", "#cdcd00", "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
  "#7f7f7f", "#ff0000", "#00ff00", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff",
];

export function color256(n) {
  if (n < 16) return BASE16[n];
  if (n < 232) {
    n -= 16;
    const steps = [0, 95, 135, 175, 215, 255];
    return rgb(steps[Math.floor(n / 36)], steps[Math.floor(n / 6) % 6], steps[n % 6]);
  }
  const v = 8 + (n - 232) * 10;
  return rgb(v, v, v);
}

function rgb(r, g, b) {
  return "#" + [r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("");
}

// Rich's named colors (rich.color.ANSI_COLOR_NAMES), used by highlight styles.
const NAMED = {"black":0,"red":1,"green":2,"yellow":3,"blue":4,"magenta":5,"cyan":6,"white":7,"bright_black":8,"bright_red":9,"bright_green":10,"bright_yellow":11,"bright_blue":12,"bright_magenta":13,"bright_cyan":14,"bright_white":15,"grey0":16,"gray0":16,"navy_blue":17,"dark_blue":18,"blue3":20,"blue1":21,"dark_green":22,"deep_sky_blue4":25,"dodger_blue3":26,"dodger_blue2":27,"green4":28,"spring_green4":29,"turquoise4":30,"deep_sky_blue3":32,"dodger_blue1":33,"green3":40,"spring_green3":41,"dark_cyan":36,"light_sea_green":37,"deep_sky_blue2":38,"deep_sky_blue1":39,"spring_green2":47,"cyan3":43,"dark_turquoise":44,"turquoise2":45,"green1":46,"spring_green1":48,"medium_spring_green":49,"cyan2":50,"cyan1":51,"dark_red":88,"deep_pink4":125,"purple4":55,"purple3":56,"blue_violet":57,"orange4":94,"grey37":59,"gray37":59,"medium_purple4":60,"slate_blue3":62,"royal_blue1":63,"chartreuse4":64,"dark_sea_green4":71,"pale_turquoise4":66,"steel_blue":67,"steel_blue3":68,"cornflower_blue":69,"chartreuse3":76,"cadet_blue":73,"sky_blue3":74,"steel_blue1":81,"pale_green3":114,"sea_green3":78,"aquamarine3":79,"medium_turquoise":80,"chartreuse2":112,"sea_green2":83,"sea_green1":85,"aquamarine1":122,"dark_slate_gray2":87,"dark_magenta":91,"dark_violet":128,"purple":129,"light_pink4":95,"plum4":96,"medium_purple3":98,"slate_blue1":99,"yellow4":106,"wheat4":101,"grey53":102,"gray53":102,"light_slate_grey":103,"light_slate_gray":103,"medium_purple":104,"light_slate_blue":105,"dark_olive_green3":149,"dark_sea_green":108,"light_sky_blue3":110,"sky_blue2":111,"dark_sea_green3":150,"dark_slate_gray3":116,"sky_blue1":117,"chartreuse1":118,"light_green":120,"pale_green1":156,"dark_slate_gray1":123,"red3":160,"medium_violet_red":126,"magenta3":164,"dark_orange3":166,"indian_red":167,"hot_pink3":168,"medium_orchid3":133,"medium_orchid":134,"medium_purple2":140,"dark_goldenrod":136,"light_salmon3":173,"rosy_brown":138,"grey63":139,"gray63":139,"medium_purple1":141,"gold3":178,"dark_khaki":143,"navajo_white3":144,"grey69":145,"gray69":145,"light_steel_blue3":146,"light_steel_blue":147,"yellow3":184,"dark_sea_green2":157,"light_cyan3":152,"light_sky_blue1":153,"green_yellow":154,"dark_olive_green2":155,"dark_sea_green1":193,"pale_turquoise1":159,"deep_pink3":162,"magenta2":200,"hot_pink2":169,"orchid":170,"medium_orchid1":207,"orange3":172,"light_pink3":174,"pink3":175,"plum3":176,"violet":177,"light_goldenrod3":179,"tan":180,"misty_rose3":181,"thistle3":182,"plum2":183,"khaki3":185,"light_goldenrod2":222,"light_yellow3":187,"grey84":188,"gray84":188,"light_steel_blue1":189,"yellow2":190,"dark_olive_green1":192,"honeydew2":194,"light_cyan1":195,"red1":196,"deep_pink2":197,"deep_pink1":199,"magenta1":201,"orange_red1":202,"indian_red1":204,"hot_pink":206,"dark_orange":208,"salmon1":209,"light_coral":210,"pale_violet_red1":211,"orchid2":212,"orchid1":213,"orange1":214,"sandy_brown":215,"light_salmon1":216,"light_pink1":217,"pink1":218,"plum1":219,"gold1":220,"navajo_white1":223,"misty_rose1":224,"thistle1":225,"yellow1":226,"light_goldenrod1":227,"khaki1":228,"wheat1":229,"cornsilk1":230,"grey100":231,"gray100":231,"grey3":232,"gray3":232,"grey7":233,"gray7":233,"grey11":234,"gray11":234,"grey15":235,"gray15":235,"grey19":236,"gray19":236,"grey23":237,"gray23":237,"grey27":238,"gray27":238,"grey30":239,"gray30":239,"grey35":240,"gray35":240,"grey39":241,"gray39":241,"grey42":242,"gray42":242,"grey46":243,"gray46":243,"grey50":244,"gray50":244,"grey54":245,"gray54":245,"grey58":246,"gray58":246,"grey62":247,"gray62":247,"grey66":248,"gray66":248,"grey70":249,"gray70":249,"grey74":250,"gray74":250,"grey78":251,"gray78":251,"grey82":252,"gray82":252,"grey85":253,"gray85":253,"grey89":254,"gray89":254,"grey93":255,"gray93":255};

// Control characters Rich drops from plain text: BEL, BS, VT, FF, CR.
const STRIPPED = /[\x07\x08\x0b\x0c\r]/g;

// A port of rich.ansi.re_ansi, so escape codes are consumed exactly as the
// core consumed them: two-char escapes, OSC terminated by ST, then CSI.
const ESCAPE = /(?:\x1b[0-?])|(?:\x1b\](.*?)\x1b\\)|(?:\x1b([(@-Z\\-_]|\[[0-?]*[ -/]*[@-~]))/g;

// A style is a small plain object: {fg, bg, bold, dim, italic, underline,
// reverse, strike, link}. Missing keys mean "default".

function applySgr(style, params) {
  // Like Rich: split on ";" only, drop non-numeric parts, clamp to 255.
  const p = params
    .split(";")
    .filter((x) => x === "" || /^\d+$/.test(x))
    .map((x) => Math.min(255, x === "" ? 0 : Number(x)));
  const s = { ...style };
  for (let i = 0; i < p.length; i++) {
    const code = p[i];
    if (code === 0) {
      for (const k of Object.keys(s)) delete s[k];
    } else if (code === 1) s.bold = true;
    else if (code === 2) s.dim = true;
    else if (code === 3) s.italic = true;
    else if (code === 4) s.underline = true;
    else if (code === 7) s.reverse = true;
    else if (code === 9) s.strike = true;
    else if (code === 22) { delete s.bold; delete s.dim; }
    else if (code === 23) delete s.italic;
    else if (code === 24) delete s.underline;
    else if (code === 27) delete s.reverse;
    else if (code === 29) delete s.strike;
    else if (code >= 30 && code <= 37) s.fg = color256(code - 30);
    else if (code === 39) delete s.fg;
    else if (code >= 40 && code <= 47) s.bg = color256(code - 40);
    else if (code === 49) delete s.bg;
    else if (code >= 90 && code <= 97) s.fg = color256(code - 90 + 8);
    else if (code >= 100 && code <= 107) s.bg = color256(code - 100 + 8);
    else if (code === 38 || code === 48) {
      const key = code === 38 ? "fg" : "bg";
      if (p[i + 1] === 5 && i + 2 < p.length) {
        s[key] = color256(p[i + 2] & 255);
        i += 2;
      } else if (p[i + 1] === 2 && i + 4 < p.length) {
        s[key] = rgb(p[i + 2] & 255, p[i + 3] & 255, p[i + 4] & 255);
        i += 4;
      }
    }
  }
  return s;
}

// Parse one raw line into runs of [text, style] whose concatenated text
// equals the core's ``plain``.
export function parseAnsi(ansi) {
  // Like Rich, a carriage return mid-line keeps only what follows it.
  const cr = ansi.lastIndexOf("\r");
  if (cr >= 0) ansi = ansi.slice(cr + 1);
  const runs = [];
  let style = {};
  const push = (text) => {
    text = text.replace(STRIPPED, "");
    if (text) runs.push([text, style]);
  };
  let pos = 0;
  for (const m of ansi.matchAll(ESCAPE)) {
    if (m.index > pos) push(ansi.slice(pos, m.index));
    pos = m.index + m[0].length;
    const [, osc, seq] = m;
    if (seq === "(") {
      pos += 1; // "ESC ( X" selects a charset; Rich skips the X too
    } else if (seq && seq.endsWith("m")) {
      style = applySgr(style, seq.slice(1, -1));
    } else if (osc !== undefined && osc.startsWith("8;")) {
      // OSC 8 hyperlink: "8;params;url" (an empty url ends the link).
      const rest = osc.slice(2);
      const semi = rest.indexOf(";");
      if (semi >= 0) {
        style = { ...style };
        const url = rest.slice(semi + 1);
        if (url) style.link = url;
        else delete style.link;
      }
    }
  }
  if (pos < ansi.length) push(ansi.slice(pos));
  return runs;
}

// Parse a Rich style string such as "bold red on #202020" (as used by
// highlight entries in config.yaml). Unknown words are ignored.
export function parseRichStyle(str) {
  const s = {};
  const words = str.trim().toLowerCase().split(/\s+/);
  let target = "fg";
  for (let i = 0; i < words.length; i++) {
    const w = words[i];
    if (w === "on") { target = "bg"; continue; }
    if (w === "not") { i++; continue; }
    const attr = { b: "bold", bold: "bold", d: "dim", dim: "dim", i: "italic", italic: "italic",
      u: "underline", underline: "underline", r: "reverse", reverse: "reverse",
      s: "strike", strike: "strike" }[w];
    if (attr) { s[attr] = true; continue; }
    const col = richColor(w);
    if (col) { s[target] = col; target = "fg"; }
  }
  return s;
}

function richColor(w) {
  if (/^#[0-9a-f]{6}$/.test(w)) return w;
  let m = /^color\((\d+)\)$/.exec(w);
  if (m) return color256(Number(m[1]) & 255);
  m = /^rgb\((\d+),(\d+),(\d+)\)$/.exec(w);
  if (m) return rgb(+m[1] & 255, +m[2] & 255, +m[3] & 255);
  if (w in NAMED) return color256(NAMED[w]);
  return null;
}

// Split runs at the given offsets and merge ``extra`` into the style of the
// characters in [start, end). Offsets come from Python, so they count code
// points, not UTF-16 units; Array.from splits the same way.
function overlay(runs, start, end, extra) {
  const out = [];
  let pos = 0;
  for (const [text, style] of runs) {
    const chars = Array.from(text);
    const a = pos;
    const b = pos + chars.length;
    pos = b;
    const lo = Math.max(a, start);
    const hi = Math.min(b, end);
    if (lo >= hi) { out.push([text, style]); continue; }
    if (lo > a) out.push([chars.slice(0, lo - a).join(""), style]);
    out.push([chars.slice(lo - a, hi - a).join(""), { ...style, ...extra }]);
    if (hi < b) out.push([chars.slice(hi - a).join(""), style]);
  }
  return out;
}

function styleNode(text, style) {
  const node = style.link ? document.createElement("a") : document.createElement("span");
  node.textContent = text;
  if (style.link) {
    node.href = style.link;
    node.target = "_blank";
    node.rel = "noopener noreferrer";
  }
  let fg = style.fg;
  let bg = style.bg;
  if (style.reverse) {
    [fg, bg] = [bg || "var(--bg)", fg || "var(--fg)"];
  }
  if (fg) node.style.color = fg;
  if (bg) node.style.backgroundColor = bg;
  if (style.bold) node.style.fontWeight = "bold";
  if (style.dim) node.style.opacity = "0.6";
  if (style.italic) node.style.fontStyle = "italic";
  const deco = [style.underline && "underline", style.strike && "line-through"].filter(Boolean);
  if (deco.length) node.style.textDecoration = deco.join(" ");
  return node;
}

// Build the DOM for one core Line: {ansi, plain, highlights, links, status}.
export function renderLine(line) {
  const div = document.createElement("div");
  div.className = "line";
  if (line.status) {
    div.classList.add("status");
    div.textContent = line.plain;
    return div;
  }
  let runs = parseAnsi(line.ansi);
  for (const [start, end, url] of line.links || []) {
    runs = overlay(runs, start, end, { link: url });
  }
  for (const [start, end, style] of line.highlights || []) {
    runs = overlay(runs, start, end, parseRichStyle(style));
  }
  for (const [text, style] of runs) div.appendChild(styleNode(text, style));
  if (!div.firstChild) div.appendChild(document.createTextNode("​"));
  return div;
}
