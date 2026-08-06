const state = { chart: null };
const errorBanner = document.getElementById("error-banner");

function showError(message) {
  if (!message) return;
  errorBanner.textContent = message;
  errorBanner.hidden = false;
}

function clearError() {
  errorBanner.hidden = true;
  errorBanner.textContent = "";
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function statusPill(status) {
  const map = { ok: "ok", no_key: "warn", error: "bad" };
  return `<span class="pill ${map[status] || ""}">${
    { ok: "正常", no_key: "未配置密钥", error: "失败" }[status] || status
  }</span>`;
}

function citedText(cited) {
  if (cited === true) return `<span class="pill ok">是</span>`;
  if (cited === false) return `<span class="pill warn">否</span>`;
  return `<span class="pill">未知</span>`;
}

async function loadQueries(preferred) {
  clearError();
  let queries;
  try {
    ({ queries } = await fetchJSON("/api/queries"));
  } catch (err) {
    showError(`加载品牌列表失败：${err.message}`);
    console.error(err);
    return;
  }
  const select = document.getElementById("query-select");
  select.innerHTML = "";
  if (!queries.length) {
    const opt = el("option", "", "（暂无监测数据，先运行 /pulse track）");
    select.appendChild(opt);
    renderEmpty();
    return;
  }
  const selected = preferred && queries.some((q) => q.query === preferred)
    ? preferred
    : queries[0].query;
  for (const q of queries) {
    const opt = el("option", "", `${q.query} · ${q.total_runs} 次`);
    opt.value = q.query;
    select.appendChild(opt);
  }
  select.value = selected;
  select.onchange = () => loadAll(select.value);
  await loadAll(selected);
}

function renderEmpty() {
  document.querySelector("#overview").innerHTML = "";
  document.querySelector("#latest-table tbody").innerHTML = "";
  if (state.chart) {
    state.chart.destroy();
    state.chart = null;
  }
}

async function loadOverview(query) {
  const box = document.querySelector("#overview");
  const data = await fetchJSON(`/api/overview?query=${encodeURIComponent(query)}`);
  if (!data) {
    box.innerHTML = `<div class="empty">该品牌暂无监测数据</div>`;
    return;
  }
  const deltaCls = data.cited_delta > 0 ? "up" : data.cited_delta < 0 ? "down" : "flat";
  const deltaTxt = data.cited_delta == null
    ? (data.has_previous ? "无重叠有效平台" : "首次快照")
    : data.cited_delta > 0 ? `较上次 +${data.cited_delta}` : `较上次 ${data.cited_delta}`;

  const cards = [
    { label: "监测对象", value: data.query, sub: `最近运行 ${data.last_run}` },
    { label: "平台", value: `${data.cited}/${data.ok_platforms}`, sub: "被提及 / 有效平台" },
    { label: "引用变化", value: deltaTxt, sub: "与上一次快照对比", cls: deltaCls },
    { label: "负面提及", value: data.negative, sub: "本次快照", cls: data.negative > 0 ? "down" : "" },
  ];
  box.innerHTML = "";
  for (const c of cards) {
    const card = el("div", "card");
    card.appendChild(el("div", "label", c.label));
    const value = el("div", "value", String(c.value));
    if (c.cls) value.className += ` ${c.cls}`;
    card.appendChild(value);
    card.appendChild(el("div", "delta", c.sub || ""));
    box.appendChild(card);
  }
}

async function loadTrends(query) {
  const { series } = await fetchJSON(`/api/trends?query=${encodeURIComponent(query)}`);
  const canvas = document.getElementById("trend-chart");
  if (state.chart) state.chart.destroy();
  if (!series.length) {
    state.chart = null;
    return;
  }
  const colors = { deepseek: "#4d6bfe", kimi: "#ff7a59", doubao: "#00a6a6", yuanbao: "#8a6ff0" };
  // ISO 时间戳全局排序：各平台首次运行时间不同时，x 轴顺序必须与时间一致
  const labels = [...new Set(series.flatMap((s) => s.points.map((p) => p.run_at)))].sort();
  const datasets = series.map((s) => ({
    label: s.label,
    data: labels.map((t) => {
      const p = s.points.find((x) => x.run_at === t);
      return p ? (p.cited === true ? 1 : p.cited === false ? 0 : null) : null;
    }),
    borderColor: colors[s.platform] || "#888",
    backgroundColor: colors[s.platform] || "#888",
    tension: 0.25,
    spanGaps: false,
    pointRadius: 4,
  }));
  state.chart = new Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: { min: -0.2, max: 1.2, ticks: { stepSize: 1, callback: (v) => (v === 1 ? "被提及" : v === 0 ? "未提及" : "") } },
        x: { ticks: { maxTicksLimit: 10, maxRotation: 0 } },
      },
      plugins: { legend: { position: "bottom" } },
    },
  });
}

async function loadLatest(query) {
  const { items } = await fetchJSON(`/api/latest?query=${encodeURIComponent(query)}`);
  const tbody = document.querySelector("#latest-table tbody");
  tbody.innerHTML = "";
  if (!items.length) {
    tbody.appendChild(el("tr", "", "")).appendChild(el("td", "", "暂无数据"));
    return;
  }
  for (const item of items) {
    const tr = el("tr");
    tr.appendChild(el("td", "", item.label));
    const tdStatus = el("td");
    tdStatus.innerHTML = statusPill(item.status);
    tr.appendChild(tdStatus);
    const tdCited = el("td");
    tdCited.innerHTML = citedText(item.cited);
    tr.appendChild(tdCited);
    const tdMine = el("td");
    tdMine.innerHTML = item.mine_cited == null
      ? `<span class="pill">—</span>`
      : citedText(item.mine_cited);
    tr.appendChild(tdMine);
    tr.appendChild(el("td", "", item.sentiment_label));
    const tdCtx = el("td", "context", item.context || item.error || "—");
    tdCtx.title = item.context || item.error || "";
    tr.appendChild(tdCtx);
    tbody.appendChild(tr);
  }
}

async function loadAll(query) {
  clearError();
  try {
    await Promise.all([loadOverview(query), loadTrends(query), loadLatest(query)]);
  } catch (err) {
    showError(`加载数据失败：${err.message}`);
    console.error(err);
  }
}

document.getElementById("refresh-btn").addEventListener("click", () => {
  const select = document.getElementById("query-select");
  // 重新拉取品牌列表（新 track 的品牌无需整页刷新），并保留当前选中项
  loadQueries(select.value);
});

loadQueries().catch((err) => {
  showError(`初始化失败：${err.message}`);
  console.error(err);
});
