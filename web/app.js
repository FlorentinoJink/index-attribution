"use strict";
const $ = (id) => document.getElementById(id);
const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const fmt = (x, n = 2) =>
  x == null
    ? "—"
    : x.toLocaleString("zh-CN", {
        minimumFractionDigits: n,
        maximumFractionDigits: n,
      });
const signed = (x, suffix = "%") =>
  x == null ? "—" : (x > 0 ? "+" : "") + fmt(x) + suffix;
const tone = (x) => (x == null ? "" : x > 0 ? "pos" : x < 0 ? "neg" : "");
let meta,
  report,
  ordered = [],
  page = 0,
  target = "csi500",
  tab = "analysis",
  latestReq = 0,
  hover = -1,
  plot = null;
const presets = {
  analysis: ["指数收益归因", "按交易日拆解指数涨跌，核对因子贡献与模型残差。"],
  events: [
    "政策与市场事件",
    "按公告时间定位事件，比较同一指数的不同观察窗口。",
  ],
  sources: ["研究数据目录", "核对数据提供方、实际覆盖区间与快照版本。"],
};
const info = (msg) => {
  $("feedback").textContent = msg;
  $("feedback").hidden = !msg;
};
async function api(path, options) {
  const r = await fetch(path, options),
    v = await r.json();
  if (!r.ok) throw new Error(v.error || "读取失败");
  return v;
}
const opts = (value, label) =>
  '<option value="' +
  escapeHTML(value) +
  '">' +
  escapeHTML(label) +
  "</option>";
function setTab(value) {
  tab = value;
  $("title").textContent = presets[tab][0];
  $("subtitle").textContent = presets[tab][1];
  document
    .querySelectorAll("[data-tab]")
    .forEach((b) =>
      b.setAttribute("aria-selected", String(b.dataset.tab === tab)),
    );
  $("research").hidden = tab === "sources";
  $("sources").hidden = tab !== "sources";
  $("analysis").hidden = tab !== "analysis";
  $("events").hidden = tab !== "events";
  $("regimes").hidden = tab !== "events";
  if (tab === "analysis") requestAnimationFrame(draw);
}
function setDates() {
  const s = meta.series[target],
    val = $("range").value,
    end = s.last;
  if (val === "custom") return;
  let start = s.first,
    finish = end;
  if (val.startsWith("plan-")) {
    const year = Number(val.slice(5));
    start = year + "-01-01";
    finish = year + 4 + "-12-31";
  } else if (val !== "all") {
    const t = new Date(end + "T00:00:00Z");
    t.setUTCMonth(t.getUTCMonth() - { "1y": 12, "3m": 3, "3y": 36 }[val]);
    start = t.toISOString().slice(0, 10);
  }
  $("start").value = start;
  $("end").value = finish > end ? end : finish;
}
function params() {
  return new URLSearchParams({
    run: meta.run.id,
    target,
    model: $("model").value,
    window: $("window").value,
    timing: $("timing").value,
    backfill: $("backfill").checked ? 1 : 0,
    start: $("start").value,
    end: $("end").value,
  });
}
function showTargets() {
  $("targets").innerHTML = meta.targets
    .map(
      (k) =>
        '<button data-target="' +
        k +
        '" aria-pressed="' +
        (k === target) +
        '">' +
        meta.names[k] +
        "<small>" +
        meta.series[k].code +
        "</small></button>",
    )
    .join("");
}
async function metadata(run) {
  const path = "/api/meta" + (run ? "?run=" + run : "");
  let next = await api(path);
  while (!next.run && next.status.running) {
    info("正在首次采集指数行情…");
    await new Promise((resolve) => setTimeout(resolve, 1200));
    next = await api(path);
  }
  if (!next.run) throw new Error("指数数据尚未就绪，请点击「更新数据」重试。" + (next.status.error || ""));
  meta = next;
  $("snapshot").innerHTML = meta.snapshots
    .map((s) =>
      opts(
        s.id,
        "#" + s.id + " · " + s.collected.slice(0, 16).replace("T", " "),
      ),
    )
    .join("");
  $("snapshot").value = meta.run.id;
  $("periods").innerHTML = meta.plans
    .map(([n, a, b]) => opts("plan-" + a, n + " · " + a + "–" + b))
    .join("");
  $("source-time").textContent =
    "采集于 " + meta.run.collected.slice(0, 19).replace("T", " ");
  $("source-rows").innerHTML = [...meta.targets, "market", "bond", "dxy", "yen"]
    .map((key) => {
      const s = meta.series[key],
        q = new URLSearchParams({ run: meta.run.id, key });
      return (
        "<tr><td>" +
        s.name +
        '<span class="source-code">' +
        s.code +
        '</span></td><td><a href="' +
        escapeHTML(s.url) +
        '" target="_blank" rel="noopener">' +
        s.provider +
        " ↗</a></td><td>" +
        s.first +
        "</td><td>" +
        s.last +
        "</td><td>" +
        fmt(s.n, 0) +
        '</td><td class="source-links"><a href="/api/series.csv?' +
        q +
        '">CSV</a><a href="/api/raw?' +
        q +
        '">原始文件</a><span class="source-code source-hash" title="' +
        s.hash +
        '">SHA-256 ' +
        s.hash.slice(0, 12) +
        "…</span></td></tr>"
      );
    })
    .join("");
  showTargets();
}
function val(r, key) {
  return r[report.cols.indexOf(key)];
}
async function load() {
  const request = ++latestReq;
  const exported = $("export");
  exported.removeAttribute("href");
  if (
    !$("start").value ||
    !$("end").value ||
    $("start").value > $("end").value
  ) {
    report = null;
    $("content").hidden = true;
    info("当前指数在所选日期区间没有观测，请调整区间。");
    return;
  }
  info("正在计算交易日归因…");
  $("content").hidden = true;
  try {
    const q = params(),
      data = await api("/api/report?" + q);
    if (request !== latestReq) return;
    report = data;
    hover = -1;
    plot = null;
    page = 0;
    exported.href = "/api/report.csv?" + q;
    $("content").hidden = false;
    info(meta.status.error ? "最近采集失败，当前显示上一次完整快照。" : "");
    const retB =
      (val(report.rows.at(-1), "benchmark") / report.base_benchmark - 1) * 100;
    for (const [id, v] of [
      ["k-total", report.total],
      ["k-market", retB],
    ]) {
      $(id).textContent = signed(v);
      $(id).className = tone(v);
    }
    $("k-dates").textContent = report.first + " — " + report.last;
    $("k-r2").textContent = fmt(report.r2, 3);
    $("k-cover").textContent =
      fmt(report.modeled, 0) + " / " + fmt(report.count, 0);
    $("k-last").textContent = "最后有效日 " + (report.fitted_last || "—");
    $("legend-name").textContent = meta.names[target];
    renderBars();
    renderComparison();
    sortDays();
    renderEvents();
    requestAnimationFrame(draw);
    const loc = new URLSearchParams(q);
    loc.set("range", $("range").value);
    loc.set("tab", tab);
    history.replaceState(null, "", "?" + loc);
  } catch (e) {
    if (request === latestReq) {
      report = null;
      info(e.message);
    }
  }
}
function renderBars() {
  const max = Math.max(...Object.values(report.contrib).map(Math.abs), 0.01);
  $("bars").innerHTML = report.components
    .map((k) => {
      const v = report.contrib[k],
        width = (48 * Math.abs(v)) / max,
        color =
          k === "unmodeled" || k === "residual"
            ? "#a6b2bf"
            : v >= 0
              ? "#6bcab4"
              : "#dc9291";
      return (
        '<div class="bar-row"><span>' +
        meta.names[k] +
        '</span><span class="track"><i style="background:' +
        color +
        ";width:" +
        width +
        "%;left:" +
        (v >= 0 ? 50 : 50 - width) +
        '%"></i></span><b class="' +
        tone(v) +
        '">' +
        signed(v, "") +
        "</b></div>"
      );
    })
    .join("");
  $("reconcile").textContent =
    "贡献合计 " +
    signed(
      Object.values(report.contrib).reduce((a, b) => a + b, 0),
      " 个百分点",
    );
}
function sortDays() {
  if (!report) return;
  ordered = report.dates.map((_, i) => i).reverse();
  if ($("sort").value === "residual")
    ordered.sort(
      (a, b) =>
        Math.abs(val(report.rows[b], "residual") ?? 0) -
        Math.abs(val(report.rows[a], "residual") ?? 0),
    );
  page = 0;
  renderDays();
}
function renderDays() {
  const indices = ordered.slice(page * 12, page * 12 + 12);
  $("days").innerHTML = indices
    .map((i) => {
      const row = report.rows[i],
        ret = val(row, "ret"),
        fit = val(row, "fit"),
        res = val(row, "residual");
      return (
        "<tr><td>" +
        report.dates[i] +
        "</td><td>" +
        fmt(val(row, "level")) +
        '</td><td class="' +
        tone(ret) +
        '">' +
        signed(ret) +
        "</td><td>" +
        signed(fit) +
        "</td><td>" +
        signed(res) +
        "</td><td>" +
        (fit == null ? "未建模" : "已归因") +
        '</td><td><button data-day="' +
        i +
        '">查看</button></td></tr>'
      );
    })
    .join("");
  $("prev").disabled = page === 0;
  $("next").disabled = (page + 1) * 12 >= ordered.length;
  $("page-info").textContent =
    "第 " +
    (page + 1) +
    " / " +
    Math.ceil(ordered.length / 12) +
    " 页 · " +
    fmt(ordered.length, 0) +
    " 个交易日";
}
function renderEvents() {
  $("regime-rows").innerHTML = report.regimes
    .map(
      (g) =>
        '<tr><td><button data-period="' +
        g.year +
        '">' +
        g.name +
        "</button></td><td>" +
        g.first +
        " — " +
        g.last +
        "</td><td>" +
        g.count +
        '</td><td class="' +
        tone(g.total) +
        '">' +
        signed(g.total) +
        "</td><td>" +
        fmt(g.volatility) +
        "%</td><td>" +
        signed(g.drawdown) +
        "</td></tr>",
    )
    .join("");
  $("no-events").hidden = report.events.length > 0;
  $("event-rows").innerHTML = report.events
    .map((ev) => {
      const i = report.dates.indexOf(ev.anchor);
      return (
        "<tr><td>" +
        escapeHTML(ev.title) +
        '<span class="source-code">' +
        ev.date +
        " · " +
        ev.group +
        "</span></td><td>" +
        (i >= 0
          ? '<button data-day="' + i + '">' + ev.anchor + "</button>"
          : ev.anchor) +
        "</td>" +
        ev.moves
          .map((v) => '<td class="' + tone(v) + '">' + signed(v) + "</td>")
          .join("") +
        "<td>" +
        signed(ev.residual5, " pp") +
        '</td><td><a href="' +
        escapeHTML(ev.source) +
        '" target="_blank" rel="noopener">查阅 ↗</a></td></tr>'
      );
    })
    .join("");
}
function renderComparison() {
  const names = {
    combined: "市场与宏观 · 全部四项",
    domestic: "境内股债 · 沪深300与国债",
    macro: "宏观因子 · 国债、美元与日元",
  };
  const { n, metrics } = report.comparison;
  $("common-days").textContent = n + " 个共同测试日";
  $("compare-rows").innerHTML = metrics
    .map(
      (m) =>
        "<tr><td>" +
        names[m.model] +
        "</td><td>" +
        fmt(m.r2, 3) +
        "</td><td>" +
        fmt(m.rmse, 3) +
        "</td></tr>",
    )
    .join("");
  const full = metrics.find((m) => m.model === "combined").r2,
    local = metrics.find((m) => m.model === "domestic").r2;
  $("increment").textContent =
    full == null || local == null
      ? "共同样本不足，暂不比较增量解释度。"
      : "加入美元与日元因子后，共同样本的 R² 变化 " +
        signed((full - local) * 100, " 个百分点") +
        "。三组模型使用相同测试日与均值基准。";
}
$("regime-rows").onclick = (e) => {
  const b = e.target.closest("[data-period]");
  if (b) {
    $("range").value = "plan-" + b.dataset.period;
    setDates();
    load();
  }
};
function dayDetail(i) {
  if (!report || i < 0 || i >= report.rows.length) return;
  const row = report.rows[i],
    valid = val(row, "fit") != null;
  const missing = report.factors.filter(k => val(row, "x_"+k) == null);
  const reason = missing.length
    ? "缺少对齐后的因子收益：" + missing.map(k => meta.names[k] + "（源数据截至 " + meta.series[k].last + "）").join("、")
    : "发布期或历史训练窗口尚未满足估计条件";
  $("detail-title").textContent = meta.names[target] + " · " + report.dates[i];
  const items = report.factors
    .map(
      (k) =>
        "<tr><td>" +
        meta.names[k] +
        "</td><td>" +
        signed(val(row, "x_" + k)) +
        "</td><td>" +
        fmt(val(row, "beta_" + k), 4) +
        "</td><td>" +
        signed(val(row, k), " pp") +
        "</td></tr>",
    )
    .join("");
  $("detail-body").innerHTML =
    '<p class="hint">指数涨跌 ' +
    signed(val(row, "ret")) +
    "　收盘 " +
    fmt(val(row, "level")) +
    "　" +
    (valid
      ? "模型解释 " + signed(val(row, "fit"))
      : "未建模 · " + reason) +
    '</p><div class="scroll"><table><thead><tr><th>因子</th><th>对齐后收益</th><th>历史系数 β</th><th>当日贡献</th></tr></thead><tbody>' +
    items +
    '</tbody></table></div><p class="hint">截距 ' +
    signed(val(row, "alpha"), " pp") +
    "　残差 " +
    signed(val(row, "residual"), " pp") +
    "　未建模 " +
    signed(val(row, "unmodeled"), " pp") +
    "</p>";
  $("detail").showModal();
}
function draw() {
  if (!report || tab !== "analysis" || $("content").hidden) return;
  const canvas = $("chart"),
    w = canvas.clientWidth,
    h = canvas.clientHeight;
  if (w < 50) return;
  const ctx = canvas.getContext("2d"),
    s = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * s);
  canvas.height = Math.round(h * s);
  ctx.scale(s, s);
  const a = report.rows.map((r) => 100 * (r[0] / report.base_level - 1)),
    b = report.rows.map((r) => 100 * (r[1] / report.base_benchmark - 1));
  const L = 58,
    R = w - 23,
    T = 13,
    B = h - 27;
  let lo = Math.min(...a, ...b, 0),
    hi = Math.max(...a, ...b, 0);
  const pad = Math.max(0.2, (hi - lo) * 0.07);
  lo -= pad;
  hi += pad;
  const X = (i) => L + (i * (R - L)) / Math.max(1, a.length - 1),
    Y = (v) => B - ((v - lo) / (hi - lo)) * (B - T);
  ctx.font = "11px Consolas,monospace";
  ctx.fillStyle = "#a2b5bc";
  ctx.lineWidth = 1;
  for (let k = 0; k <= 4; k++) {
    const v = lo + ((hi - lo) * k) / 4,
      y = Y(v);
    ctx.strokeStyle = "#273842";
    ctx.beginPath();
    ctx.moveTo(L, y);
    ctx.lineTo(R, y);
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(fmt(v, Math.abs(hi - lo) < 3 ? 2 : 1) + "%", L - 7, y + 4);
  }
  for (const [arr, color] of [
    [b, "#d8af71"],
    [a, "#71d7bf"],
  ]) {
    ctx.beginPath();
    arr.forEach((v, i) => {
      if (i === 0) ctx.moveTo(X(i), Y(v));
      else ctx.lineTo(X(i), Y(v));
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.8;
    ctx.stroke();
    if (arr.length === 1) {
      ctx.fillStyle = color;
      ctx.fillRect(X(0) - 2, Y(arr[0]) - 2, 4, 4);
    }
  }
  const ticks = w < 420 ? 3 : 5;
  ctx.fillStyle = "#a2b5bc";
  for (let j = 0; j < ticks; j++) {
    const i = Math.round(((a.length - 1) * j) / (ticks - 1));
    ctx.textAlign = j === 0 ? "left" : j === ticks - 1 ? "right" : "center";
    ctx.fillText(report.dates[i].slice(2), X(i), h - 7);
  }
  plot = { L, R, n: a.length };
  if (hover >= 0 && hover < a.length) {
    const x = X(hover);
    ctx.beginPath();
    ctx.setLineDash([3, 4]);
    ctx.moveTo(x, T);
    ctx.lineTo(x, B);
    ctx.strokeStyle = "#a4b7bd";
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.setLineDash([]);
    $("tooltip").textContent =
      report.dates[hover] +
      "\n" +
      meta.names[target] +
      "　" +
      signed(a[hover]) +
      "\n沪深300　" +
      signed(b[hover]);
    $("tooltip").hidden = false;
    $("tooltip").style.left =
      Math.max(0, Math.min(w - $("tooltip").offsetWidth - 4, x + 8)) + "px";
    $("tooltip").style.top = "10px";
  } else $("tooltip").hidden = true;
}
$("chart").onpointermove = (e) => {
  if (!plot) return;
  const x = e.clientX - e.currentTarget.getBoundingClientRect().left;
  hover = Math.max(
    0,
    Math.min(
      plot.n - 1,
      Math.round(((x - plot.L) / (plot.R - plot.L)) * (plot.n - 1)),
    ),
  );
  draw();
};
$("chart").onpointerleave = () => {
  hover = -1;
  draw();
};
$("chart").onclick = () => dayDetail(hover);
$("chart").onkeydown = (e) => {
  if (!report) return;
  if (e.key === "Enter") dayDetail(hover);
  if (["ArrowLeft", "ArrowRight"].includes(e.key)) {
    e.preventDefault();
    hover = Math.min(
      report.count - 1,
      Math.max(0, hover + (e.key === "ArrowRight" ? 1 : -1)),
    );
    draw();
  }
};
new ResizeObserver(draw).observe($("chart-box"));
document.querySelector("nav").onclick = (e) => {
  const b = e.target.closest("[data-tab]");
  if (b) {
    setTab(b.dataset.tab);
    const u = new URL(location.href);
    u.searchParams.set("tab", tab);
    history.replaceState(null, "", u);
  }
};
$("targets").onclick = (e) => {
  const b = e.target.closest("[data-target]");
  if (b) {
    target = b.dataset.target;
    showTargets();
    if ($("range").value !== "custom") setDates();
    load();
  }
};
$("range").onchange = () => {
  setDates();
  load();
};
for (const id of ["start", "end"])
  $(id).onchange = () => {
    $("range").value = "custom";
    load();
  };
for (const id of ["window", "model", "timing", "backfill"])
  $(id).onchange = load;
$("sort").onchange = sortDays;
$("prev").onclick = () => {
  page--;
  renderDays();
};
$("next").onclick = () => {
  page++;
  renderDays();
};
document.querySelector("main").addEventListener("click", (e) => {
  const b = e.target.closest("[data-day]");
  if (b) dayDetail(Number(b.dataset.day));
});
$("method").onclick = () => $("methods").showModal();
document
  .querySelectorAll("dialog .close")
  .forEach((b) => (b.onclick = () => b.closest("dialog").close()));
async function switchSnapshot(run) {
  const range = $("range").value;
  await metadata(run);
  $("range").value = range;
  setDates();
  await load();
}
$("snapshot").onchange = () =>
  switchSnapshot($("snapshot").value).catch((e) => info(e.message));
$("new-event").onclick = () => {
  $("event-error").textContent = "";
  $("event-form").showModal();
};
$("annotation").onsubmit = async (e) => {
  e.preventDefault();
  const form = e.currentTarget,
    data = new FormData(form),
    btn = form.querySelector("button");
  const day = data.get("day");
  btn.disabled = true;
  try {
    await api("/api/event", {
      method: "POST",
      headers: {
        "X-Research-Action": "event",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        day,
        title: data.get("title"),
        url: data.get("url"),
        same_session: data.get("session") === "same",
      }),
    });
    form.reset();
    $("event-form").close();
    if (day < $("start").value || day > $("end").value) {
      $("range").value = "all";
      setDates();
    }
    await load();
    info("事件已加入本机目录。覆盖该日期的指数可查看观察窗口。");
  } catch (err) {
    $("event-error").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
};
$("refresh").onclick = async () => {
  $("refresh").disabled = true;
  info("正在向数据源采集最新快照…");
  try {
    await api("/api/refresh", {
      method: "POST",
      headers: { "X-Research-Action": "refresh" },
    });
    let state;
    do {
      await new Promise((r) => setTimeout(r, 1200));
      state = await api("/api/meta");
    } while (state.status.running);
    if (state.status.error)
      throw new Error("更新失败，保留原快照。" + state.status.error);
    if (!meta) return location.reload();
    await switchSnapshot(state.run.id);
    if (state.status.support_error)
      info("指数数据已更新。ETF 数据更新失败，承接观察仍使用上次快照。");
  } catch (e) {
    info(e.message);
  } finally {
    $("refresh").disabled = false;
  }
};
(async () => {
  const q = new URLSearchParams(location.search);
  await metadata(q.get("run"));
  if (meta.targets.includes(q.get("target"))) target = q.get("target");
  for (const id of ["range", "window", "model", "timing"]) {
    if ([...$(id).options].some((o) => o.value === q.get(id)))
      $(id).value = q.get(id);
  }
  $("backfill").checked = q.get("backfill") === "1";
  setDates();
  if ($("range").value === "custom") {
    $("start").value = q.get("start") || meta.series[target].first;
    $("end").value = q.get("end") || meta.series[target].last;
  }
  showTargets();
  setTab(Object.hasOwn(presets, q.get("tab")) ? q.get("tab") : "analysis");
  await load();
})().catch((e) => info(e.message));
