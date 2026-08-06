const state = { chart: null };

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

async function loadQueries() {
  const { queries } = await fetchJSON("/api/queries");
  const select = document.getElementById("query-select");
  select.innerHTML = "";
  if (!queries.length) {
    const opt = el("option", "", "（暂无监测数据，先运行 /pulse track）");
    select.appendChild(opt);
    renderEmpty();
    return;
  }
  for (const q of queries) {
    const opt = el("option", "", `${q.query} · ${q.total_runs} 次`);
    opt.value = q.query;
    select.appendChild(opt);
  }
  select.onchange = () => loadAll(select.value);
  await loadAll(queries[0].query);
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
    ? "首次快照"
    : data.cited_delta > 0 ? `较上次 +${data.cited_delta}` : `较上次 ${data.cited_delta}`;

  const cards = [
    { label: "监测对象", value: data.query, sub: `最近运行 ${data.last_run}` },
    { label: "平台", value: `${data.cited}/${data.platforms}`, sub: "被提及 / 总平台" },
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
  const labels = [...new Set(series.flatMap((s) => s.points.map((p) => p.run_at)))];
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
    tr.appendChild(el("td", "", item.sentiment_label));
    const tdCtx = el("td", "context", item.context || item.error || "—");
    tdCtx.title = item.context || item.error || "";
    tr.appendChild(tdCtx);
    tbody.appendChild(tr);
  }
}

async function loadAll(query) {
  try {
    await Promise.all([loadOverview(query), loadTrends(query), loadLatest(query)]);
  } catch (err) {
    console.error(err);
  }
}

document.getElementById("refresh-btn").addEventListener("click", () => {
  const select = document.getElementById("query-select");
  if (select.value) loadAll(select.value);
});

loadQueries().catch((err) => console.error(err));
