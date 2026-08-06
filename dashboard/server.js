// Pulse 本地仪表盘 —— 零依赖 Node 服务（端口 8766）
//
// 读取 data/monitor.db（scripts/search_ai.py 写入的 SQLite），提供：
//   GET /api/queries              所有监测对象（品牌）
//   GET /api/overview?query=X     概览卡片数据（最近运行/被提及/负面/变化）
//   GET /api/trends?query=X       各平台被提及时间序列（Chart.js 用）
//   GET /api/latest?query=X       各平台最近一次探测（快照表）
//   GET /api/health               存活检查
// 静态页面：dashboard/public/
//
// 设计取舍：Phase 2 MVP 用 Node 内置 http + node:sqlite，零安装即可跑；
// 后续若引入 Express，只需替换服务层，API 形态保持不变。
// 需要 Node >= 22.5；Node >= 24 开箱即用（本仓库已在此版本验证），
// 22.5–23.x 需 --experimental-sqlite flag；启动失败时请先升级 Node。

const http = require("http");
const fs = require("fs");
const path = require("path");
let DatabaseSync;
try {
  ({ DatabaseSync } = require("node:sqlite"));
} catch (err) {
  console.error("启动失败：需要 Node >= 22.5（node:sqlite 内置模块）。当前版本不支持，请升级 Node 后重试。");
  process.exit(1);
}

const PORT = Number(process.env.PULSE_PORT || 8766);
const HOST = "127.0.0.1";
const PROJECT_ROOT = path.join(__dirname, "..");
const PUBLIC_DIR = path.join(__dirname, "public");
const DB_PATH = process.env.PULSE_DB
  ? path.resolve(process.env.PULSE_DB)
  : path.join(PROJECT_ROOT, "data", "monitor.db");

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
};

const PLATFORM_LABELS = {
  deepseek: "DeepSeek",
  kimi: "Kimi（月之暗面）",
  doubao: "豆包（字节跳动）",
  yuanbao: "元宝（腾讯）",
};

const STATUS_LABELS = { ok: "正常", no_key: "未配置密钥", error: "失败" };
const SENTIMENT_LABELS = { positive: "正面", neutral: "中性", negative: "负面" };

function openDb() {
  if (!fs.existsSync(DB_PATH)) return null;
  try {
    return new DatabaseSync(DB_PATH, { readOnly: true });
  } catch (err) {
    console.error("打开 monitor.db 失败：", err.message);
    return null;
  }
}

function fmtRunAt(value) {
  return String(value || "").replace(/\.\d+/, "");
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

function apiQueries() {
  const conn = openDb();
  if (!conn) return [];
  try {
    return conn
      .prepare(
        "SELECT query, MAX(run_at) AS last_run, COUNT(DISTINCT run_at) AS total_runs" +
        " FROM probes GROUP BY query ORDER BY last_run DESC"
      )
      .all()
      .map((r) => ({
        query: r.query,
        last_run: fmtRunAt(r.last_run),
        total_runs: r.total_runs,
      }));
  } finally {
    conn.close();
  }
}

function distinctRunTimes(conn, query) {
  return conn
    .prepare("SELECT DISTINCT run_at FROM probes WHERE query=? ORDER BY run_at DESC")
    .all(query)
    .map((r) => r.run_at);
}

function apiOverview(query) {
  const conn = openDb();
  if (!conn) return null;
  try {
    const runs = distinctRunTimes(conn, query);
    if (!runs.length) return null;
    const lastRun = runs[0];
    const rows = conn
      .prepare("SELECT * FROM probes WHERE query=? AND run_at=?")
      .all(query, lastRun);
    const prevRows = runs.length > 1
      ? conn.prepare("SELECT * FROM probes WHERE query=? AND run_at=?").all(query, runs[1])
      : [];

    const okRows = rows.filter((r) => r.status === "ok");
    const cited = okRows.filter((r) => r.cited === 1).length;
    const negative = okRows.filter((r) => r.sentiment === "negative").length;

    // 变化量只在两次运行均有效（status=ok）的平台之间对比：
    // no_key/error 是「未知」而非「未被提及」，混入会把未知误算成引用下降/上升
    const okCurrentPlatforms = new Set(okRows.map((r) => r.platform));
    const okPrevPlatforms = new Set(
      prevRows.filter((r) => r.status === "ok").map((r) => r.platform)
    );
    const comparablePlatforms = [...okCurrentPlatforms].filter((p) => okPrevPlatforms.has(p));
    const prevCited = prevRows.filter(
      (r) => comparablePlatforms.includes(r.platform) && r.status === "ok" && r.cited === 1
    ).length;
    const citedSame = okRows.filter(
      (r) => comparablePlatforms.includes(r.platform) && r.cited === 1
    ).length;
    const citedDelta = !comparablePlatforms.length ? null : citedSame - prevCited;

    return {
      query,
      last_run: fmtRunAt(lastRun),
      has_previous: runs.length > 1,
      platforms: rows.length,
      ok_platforms: okRows.length,
      cited,
      cited_delta: citedDelta,
      negative,
      statuses: rows.map((r) => ({ platform: r.platform, status: r.status })),
    };
  } finally {
    conn.close();
  }
}

function apiTrends(query) {
  const conn = openDb();
  if (!conn) return { query, series: [] };
  try {
    const rows = conn
      .prepare(
        "SELECT platform, run_at, cited, status, sentiment FROM probes" +
        " WHERE query=? ORDER BY run_at ASC"
      )
      .all(query);
    const byPlatform = new Map();
    for (const r of rows) {
      if (!byPlatform.has(r.platform)) byPlatform.set(r.platform, []);
      byPlatform.get(r.platform).push({
        // 保留微秒精度：同一秒内的多次运行在趋势图上必须保持独立
        run_at: r.run_at,
        cited: r.cited === 1 ? true : r.cited === 0 ? false : null,
        status: r.status,
        sentiment: r.sentiment,
      });
    }
    return {
      query,
      series: [...byPlatform.entries()].map(([platform, points]) => ({
        platform,
        label: PLATFORM_LABELS[platform] || platform,
        points,
      })),
    };
  } finally {
    conn.close();
  }
}

function apiLatest(query) {
  const conn = openDb();
  if (!conn) return [];
  try {
    const rows = conn
      .prepare("SELECT * FROM probes WHERE query=? ORDER BY run_at DESC")
      .all(query);
    const seen = new Set();
    const latest = [];
    for (const r of rows) {
      if (seen.has(r.platform)) continue;
      seen.add(r.platform);
      latest.push({
        platform: r.platform,
        label: PLATFORM_LABELS[r.platform] || r.platform,
        run_at: fmtRunAt(r.run_at),
        status: r.status,
        status_label: STATUS_LABELS[r.status] || r.status,
        cited: r.cited === 1 ? true : r.cited === 0 ? false : null,
        sentiment: r.sentiment,
        sentiment_label: SENTIMENT_LABELS[r.sentiment] || "—",
        context: r.context || "",
        source: r.source,
        degraded: r.degraded === 1,
        error: r.error || "",
      });
    }
    return latest;
  } finally {
    conn.close();
  }
}

function serveStatic(req, res) {
  let urlPath;
  try {
    urlPath = decodeURIComponent(new URL(req.url, "http://localhost").pathname);
  } catch (err) {
    res.writeHead(400, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("Bad Request");
    return;
  }
  const rel = urlPath === "/" ? "index.html" : urlPath.replace(/^\/+/, "");
  const filePath = path.normalize(path.join(PUBLIC_DIR, rel));
  const relativePath = path.relative(PUBLIC_DIR, filePath);
  if (relativePath.startsWith("..") || path.isAbsolute(relativePath)) {
    res.writeHead(403);
    res.end("Forbidden");
    return;
  }
  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("Not Found");
      return;
    }
    res.writeHead(200, {
      "Content-Type": MIME[path.extname(filePath)] || "application/octet-stream",
      "Cache-Control": "no-cache",
    });
    res.end(data);
  });
}

const server = http.createServer((req, res) => {
  let url;
  try {
    url = new URL(req.url, `http://${HOST}:${PORT}`);
  } catch (err) {
    sendJson(res, 400, { error: "请求 URL 无效" });
    return;
  }
  const route = `${req.method} ${url.pathname}`;

  if (route === "GET /api/health") {
    sendJson(res, 200, { ok: true, db: fs.existsSync(DB_PATH) });
    return;
  }
  if (route === "GET /api/queries") {
    sendJson(res, 200, { queries: apiQueries() });
    return;
  }
  if (route === "GET /api/overview") {
    const query = url.searchParams.get("query");
    if (!query) {
      sendJson(res, 400, { error: "缺少 query 参数" });
      return;
    }
    const data = apiOverview(query);
    sendJson(res, data ? 200 : 404, data || { error: "没有该品牌的监测数据" });
    return;
  }
  if (route === "GET /api/trends") {
    const query = url.searchParams.get("query");
    if (!query) {
      sendJson(res, 400, { error: "缺少 query 参数" });
      return;
    }
    sendJson(res, 200, apiTrends(query));
    return;
  }
  if (route === "GET /api/latest") {
    const query = url.searchParams.get("query");
    if (!query) {
      sendJson(res, 400, { error: "缺少 query 参数" });
      return;
    }
    sendJson(res, 200, { items: apiLatest(query) });
    return;
  }
  if (url.pathname.startsWith("/api/")) {
    sendJson(res, 404, { error: "未知接口" });
    return;
  }
  serveStatic(req, res);
});

server.listen(PORT, HOST, () => {
  console.log(`Pulse 仪表盘已启动：http://${HOST}:${PORT}`);
  console.log(`数据源：${DB_PATH}${fs.existsSync(DB_PATH) ? "" : "（尚未生成，先运行 /pulse track）"}`);
});
