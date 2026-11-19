/* PropDesk trading terminal. Market data: Delta Exchange (via /api/market). Account data: /api/account/... */
window.Terminal = { start(cfg) {
const { R, RISK_PCT, MAX_CONTRACTS, MAX_LEV, REQUIRE_SL, FEE, FIRM } = cfg;
const U = { ...cfg.U, mkt: (s, k, q = "") => `/api/market/${s}/${k}${q}` };
const $ = id => document.getElementById(id), $$ = (q, el = document) => [...el.querySelectorAll(q)];
const inr = (usd, sign) => { if (usd == null || isNaN(usd)) return "—"; const v = usd * R;
  return (sign && v > 0 ? "+" : v < 0 ? "−" : "") + "₹" + Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: 0 }); };
const num = (v, d = 2) => v == null || isNaN(v) ? "—" : Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const big = v => v == null ? "—" : v >= 1e9 ? (v / 1e9).toFixed(2) + "B" : v >= 1e6 ? (v / 1e6).toFixed(2) + "M" : v >= 1e3 ? (v / 1e3).toFixed(1) + "K" : num(v, 0);
const cls = v => v > 0 ? "pos" : v < 0 ? "neg" : "";
const tsOf = x => typeof x === "string" && isNaN(+x) ? new Date(x) : new Date(+x > 1e14 ? +x / 1000 : +x > 1e11 ? +x : +x * 1000);
const when = x => tsOf(x).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
const hhmm = d => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const trim = x => x.toFixed(4).replace(/\.?0+$/, "");

let SYM = cfg.SYM, RES = "15m", side = "buy", otype = "market_order", unit = "ct";
let S = null, T = null, ORD = [], priceTouched = false, lines = [], draftLine = null;
const dp = () => (T && T.last && T.last < 10 ? 4 : T && T.last < 1000 ? 2 : 1);
const coin = () => SYM.replace(/USD.*$/, "");

/* ---------- chart: TradingView Lightweight Charts, or a built-in canvas chart if the CDN is blocked ---------- */
function tvChart(el) {
  const chart = LightweightCharts.createChart(el, {
    layout: { background: { color: "transparent" }, textColor: "#8C98B6", fontFamily: "Manrope, system-ui, sans-serif" },
    grid: { vertLines: { color: "rgba(36,49,80,.45)" }, horzLines: { color: "rgba(36,49,80,.45)" } },
    rightPriceScale: { borderColor: "#243150" }, timeScale: { borderColor: "#243150", timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 }, autoSize: true,
    localization: { timeFormatter: t => new Date(t * 1000).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) },
  });
  const c = chart.addCandlestickSeries({ upColor: "#16C784", downColor: "#EA3943", borderVisible: false, wickUpColor: "#16C784", wickDownColor: "#EA3943" });
  const v = chart.addHistogramSeries({ priceScaleId: "", priceFormat: { type: "volume" } });
  v.priceScale().applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
  chart.subscribeCrosshairMove(p => showOhlc(p && p.seriesData && p.seriesData.get(c)));
  return { candles: c, vol: v, fit: () => chart.timeScale().scrollToRealTime() };
}
function canvasChart(el) {
  const cv = document.createElement("canvas"); el.appendChild(cv); const ctx = cv.getContext("2d");
  let data = [], vols = [], pl = [];
  function draw() {
    const W = el.clientWidth, H = el.clientHeight, dpr = devicePixelRatio || 1; cv.width = W * dpr; cv.height = H * dpr;
    cv.style.width = W + "px"; cv.style.height = H + "px"; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
    const axis = 76, cw = W - axis, n = Math.max(20, Math.floor(cw / 8)), d = data.slice(-n); if (!d.length) return;
    const near = pl.filter(p => p.title !== "Daily limit hit").map(p => p.price);   // don't let a far-off draft line squash the candles
    const ph = H * 0.82, lo = Math.min(...d.map(x => x.low), ...near), hi = Math.max(...d.map(x => x.high), ...near);
    const pad = (hi - lo) * 0.06 || 1, Y = p => 8 + (1 - (p - lo + pad) / (hi - lo + 2 * pad)) * (ph - 16), bw = cw / n;
    ctx.font = "11px Manrope, system-ui"; ctx.fillStyle = "#5E6A88"; ctx.strokeStyle = "rgba(36,49,80,.45)";
    for (let i = 0; i <= 5; i++) { const p = lo - pad + (hi - lo + 2 * pad) * i / 5, y = Y(p);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(cw, y); ctx.stroke(); ctx.fillText(num(p, dp()), cw + 6, y + 4); }
    const vmax = Math.max(...vols.slice(-n).map(x => x.value), 1);
    d.forEach((k, i) => { const x = i * bw + bw / 2, up = k.close >= k.open; ctx.fillStyle = ctx.strokeStyle = up ? "#16C784" : "#EA3943";
      const vv = (vols[vols.length - d.length + i] || {}).value || 0; ctx.globalAlpha = .28; ctx.fillRect(x - bw * .35, H - vv / vmax * H * .14, bw * .7, vv / vmax * H * .14);
      ctx.globalAlpha = 1; ctx.beginPath(); ctx.moveTo(x, Y(k.high)); ctx.lineTo(x, Y(k.low)); ctx.stroke();
      const y1 = Y(Math.max(k.open, k.close)), y2 = Y(Math.min(k.open, k.close)); ctx.fillRect(x - bw * .35, y1, bw * .7, Math.max(1, y2 - y1)); });
    pl.forEach(p => { const y = Y(p.price); if (y < 0 || y > ph) return; ctx.strokeStyle = ctx.fillStyle = p.color; ctx.setLineDash(p.lineStyle === 0 ? [] : [5, 4]);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(cw, y); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillRect(cw, y - 8, axis, 16); ctx.fillStyle = "#FFFFFF"; ctx.fillText(num(p.price, dp()), cw + 6, y + 4);
      if (p.title) { const w = ctx.measureText(p.title).width + 12; ctx.fillStyle = p.color; ctx.fillRect(cw - w - 6, y - 9, w, 18); ctx.fillStyle = "#0B1120"; ctx.fillText(p.title, cw - w, y + 4); } });
    const last = d[d.length - 1], y = Y(last.close); ctx.fillStyle = last.close >= last.open ? "#16C784" : "#EA3943";
    ctx.fillRect(cw, y - 8, axis, 16); ctx.fillStyle = "#0B1120"; ctx.fillText(num(last.close, dp()), cw + 6, y + 4);
    cv.onmousemove = e => showOhlc(d[Math.floor(e.offsetX / bw)]);
  }
  new ResizeObserver(draw).observe(el);
  return { candles: { setData: x => { data = x.slice(); draw(); }, update: k => { if (data.length && data[data.length - 1].time === k.time) data[data.length - 1] = k; else data.push(k); draw(); },
      createPriceLine: o => { pl.push(o); draw(); return o; }, removePriceLine: o => { pl = pl.filter(x => x !== o); draw(); } },
    vol: { setData: x => { vols = x; draw(); }, update: v => { if (vols.length && vols[vols.length - 1].time === v.time) vols[vols.length - 1] = v; else vols.push(v); } }, fit: () => {} };
}
function showOhlc(d) { $("ohlc").textContent = d ? `O ${num(d.open, dp())}   H ${num(d.high, dp())}   L ${num(d.low, dp())}   C ${num(d.close, dp())}` : ""; }
const CH = window.LightweightCharts ? tvChart($("chart")) : canvasChart($("chart"));
const candles = CH.candles, vol = CH.vol;
const UPV = "rgba(22,199,132,.35)", DNV = "rgba(234,57,67,.35)";
let lastCandle = null;

async function getJSON(url) { const r = await fetch(url); if (r.redirected || r.status === 302) location.reload(); return r.json(); }

/* Candles come from Binance USD-M futures (same coin, perpetual): REST for history, WebSocket for the live candle.
   If the browser can't reach Binance, the server fetches it; if that fails too, Delta's own candles are used. */
const binSym = () => SYM.toUpperCase().replace(/USD$/, "USDT");
let SRC = "binance", ws = null, wsKey = "";
const toCandle = k => ({ time: Math.floor(k[0] / 1000), open: +k[1], high: +k[2], low: +k[3], close: +k[4], volume: +k[5] });
async function fetchCandles() {
  try {
    const r = await fetch(`https://fapi.binance.com/fapi/v1/klines?symbol=${binSym()}&interval=${RES}&limit=500`);
    const rows = await r.json();
    if (Array.isArray(rows) && rows.length) return { src: "binance", data: rows.map(toCandle) };
  } catch (e) { /* blocked or offline: try the server */ }
  try {
    const j = await getJSON(U.mkt(SYM, "bcandles", "?res=" + RES));
    if (j.ok && j.data && j.data.length) return { src: "binance", data: j.data };
  } catch (e) {}
  const j = await getJSON(U.mkt(SYM, "candles", "?res=" + RES));
  return j.ok && j.data ? { src: "delta", data: j.data } : { src: "none", error: j.error };
}
function setCandles(data) {
  candles.setData(data.map(c => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close })));
  vol.setData(data.map(c => ({ time: c.time, value: c.volume, color: c.close >= c.open ? UPV : DNV })));
  lastCandle = data[data.length - 1] || null;
}
function liveCandle(c) {
  lastCandle = c;
  candles.update({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close });
  if (vol.update) vol.update({ time: c.time, value: c.volume, color: c.close >= c.open ? UPV : DNV });
}
function connectWS() {
  const key = binSym().toLowerCase() + "@kline_" + RES;
  if (ws && wsKey === key && ws.readyState <= 1) return;
  if (ws) { ws.onclose = null; ws.close(); }
  wsKey = key;
  try { ws = new WebSocket("wss://fstream.binance.com/market/ws/" + key); } catch (e) { return; }
  ws.onmessage = ev => { const m = JSON.parse(ev.data), k = m.k; if (!k || m.s !== binSym()) return;
    liveCandle({ time: Math.floor(k.t / 1000), open: +k.o, high: +k.h, low: +k.l, close: +k.c, volume: +k.v }); };
  ws.onclose = () => { if (wsKey === key) setTimeout(connectWS, 3000); };
}
function setSource(src) {
  SRC = src;
  const credit = document.querySelector(".credit");
  const lib = window.LightweightCharts ? ' · drawn with <a href="https://www.tradingview.com/" target="_blank" rel="noopener">TradingView Lightweight Charts</a>' : "";
  credit.innerHTML = (src === "binance" ? `Chart: Binance futures ${binSym()}` : src === "delta" ? `Chart: Delta Exchange ${SYM}` : "Chart unavailable") +
    ` · orders execute on Delta Exchange${lib}`;
}
async function loadCandles(fit) {
  const r = await fetchCandles();
  if (r.src === "none") { marketError(r.error); return; }
  setCandles(r.data); setSource(r.src);
  if (r.src === "binance") connectWS(); else if (ws) { ws.onclose = null; ws.close(); ws = null; }
  if (fit) CH.fit();
}
function drawLines() {
  lines.forEach(l => candles.removePriceLine(l)); lines = [];
  if (!S) return;
  const add = (price, color, title, style = 2) => price && lines.push(candles.createPriceLine({ price: +price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title }));
  S.positions.filter(p => p.symbol === SYM).forEach(p => {
    add(p.entry_price, "#8B7CF6", `${p.size > 0 ? "Long" : "Short"} ${Math.abs(p.size)}`, 0);
    if (p.liquidation_price) add(p.liquidation_price, "#EA3943", "Liquidation");
  });
  ORD.filter(o => o.symbol === SYM).forEach(o => {
    const t = o.type || "";
    if (t.includes("stop_loss")) add(o.stop, "#EA3943", "Stop-loss");
    else if (t.includes("take_profit")) add(o.stop, "#16C784", "Take-profit");
    else if (o.limit) add(o.limit, "#8C98B6", `${o.side === "buy" ? "Buy" : "Sell"} ${o.size}`);
  });
}
function drawDraft(price) {
  if (draftLine) { candles.removePriceLine(draftLine); draftLine = null; }
  if (price && price > 0) draftLine = candles.createPriceLine({ price, color: "#EA3943", lineWidth: 1, lineStyle: 1, axisLabelVisible: true, title: "Daily limit hit" });
}

/* ---------- market data ---------- */
function marketError(e) { if (e) { $("banner").innerHTML = `<div class="banner warn">Can't load prices from Delta: ${e}</div>`; setConn(false); } }
function setConn(ok) { $("conn").className = "conn " + (ok ? "ok" : "bad"); $("conn").lastChild.textContent = ok ? "Connected to Delta" : "Reconnecting"; }
async function loadTicker() {
  let j; try { j = await getJSON(U.mkt(SYM, "ticker")); } catch (e) { setConn(false); return; }
  if (!j.ok) { marketError(j.error); return; }
  T = j.data; setConn(true);
  $("mk-" + SYM).textContent = num(T.last, dp()); $("mk-" + SYM).className = cls(T.change_pct);
  $("t-chg").textContent = T.change_pct == null ? "—" : (T.change_pct >= 0 ? "+" : "") + T.change_pct.toFixed(2) + "%"; $("t-chg").className = cls(T.change_pct);
  $("t-mark").textContent = num(T.mark, dp()); $("t-hl").textContent = `${num(T.high, dp())} / ${num(T.low, dp())}`;
  $("t-vol").textContent = T.turnover_usd ? "$" + big(T.turnover_usd) : big(T.volume);
  $("t-fund").textContent = T.funding_rate == null ? "—" : (T.funding_rate * (Math.abs(T.funding_rate) < 0.01 ? 100 : 1)).toFixed(4) + "%";
  $("b-last").textContent = num(T.last, dp()); $("b-last").className = cls(T.change_pct);
  document.title = `${num(T.last, dp())} ${SYM} · ${FIRM}`;
  if (!priceTouched) $("price").value = (side === "buy" ? (T.best_bid || T.last) : (T.best_ask || T.last)) || "";
  if (SRC === "delta" && lastCandle && T.last) {
    lastCandle.close = T.last; lastCandle.high = Math.max(lastCandle.high, T.last); lastCandle.low = Math.min(lastCandle.low, T.last);
    candles.update({ time: lastCandle.time, open: lastCandle.open, high: lastCandle.high, low: lastCandle.low, close: lastCandle.close });
  }
  preview();
}
function bookRows(rows, el, sideCls, reverse) {
  let cum = 0; const withCum = rows.map(r => ({ ...r, cum: (cum += r.size) }));
  const max = cum || 1, list = reverse ? withCum.slice().reverse() : withCum;
  el.innerHTML = list.map(r => `<button type="button" class="brow ${sideCls}" data-price="${r.price}" style="--w:${(r.cum / max * 100).toFixed(1)}%">
    <span>${num(r.price, dp())}</span><span>${big(r.size)}</span><span>${big(r.cum)}</span></button>`).join("") || `<div class="empty">—</div>`;
  return cum;
}
async function loadBook() {
  let j; try { j = await getJSON(U.mkt(SYM, "book")); } catch (e) { return; }
  if (!j.ok) return;
  const asks = j.data.asks.slice(0, 14), bids = j.data.bids.slice(0, 14);
  const sa = bookRows(asks, $("asks"), "ask", true), sb = bookRows(bids, $("bids"), "bid", false);
  if (asks[0] && bids[0]) { const sp = asks[0].price - bids[0].price; $("b-spread").textContent = `spread ${num(sp, dp())} · ${(sp / asks[0].price * 100).toFixed(3)}%`; }
  const pb = sa + sb ? sb / (sa + sb) * 100 : 50;
  $("pr-b").style.width = pb + "%"; $("pr-s").style.width = (100 - pb) + "%";
  $("pr-bt").textContent = `B ${pb.toFixed(0)}%`; $("pr-st").textContent = `${(100 - pb).toFixed(0)}% S`;
}
async function loadTrades() {
  if ($("pane-trades").hidden) return;
  const j = await getJSON(U.mkt(SYM, "trades"));
  if (!j.ok) return;
  $("trades").innerHTML = j.data.map(t => `<div class="brow static"><span class="${t.side === "buy" ? "pos" : "neg"}">${num(t.price, dp())}</span><span>${big(t.size)}</span><span class="faint">${hhmm(tsOf(t.ts))}</span></div>`).join("");
}
async function loadMovers() {
  const j = await getJSON(U.movers);
  if (!j.ok || !j.data.length) return;
  const items = j.data.map(t => `<span class="tk"><b>${t.symbol}</b> ${num(t.last, t.last < 10 ? 4 : 2)} <em class="${cls(t.change_pct)}">${t.change_pct >= 0 ? "+" : ""}${t.change_pct.toFixed(2)}%</em></span>`).join("");
  $("tape").innerHTML = items + items;   // doubled so the scroll loops seamlessly
}

/* ---------- account ---------- */
const notionalOf = p => Math.abs(p.size) * (S.contract_values[p.symbol] || 0) * p.mark_price;
async function loadState() {
  try { S = await getJSON(U.state); } catch (e) { return; }
  const m = S.metrics, upl = S.positions.reduce((t, p) => t + p.unrealized_pnl, 0);
  $("a-eq").textContent = inr(m.equity); $("a-upl").textContent = inr(upl, 1); $("a-upl").className = cls(upl);
  $("p-eq").textContent = inr(m.equity); $("p-day").textContent = inr(m.daily_pnl, 1); $("p-day").className = cls(m.daily_pnl);
  const notional = S.positions.reduce((t, p) => t + notionalOf(p), 0);
  $("avail").textContent = inr(Math.max(0, m.equity - notional / MAX_LEV));
  const lo = m.dd_floor, hi = m.goal_equity || (m.starting_balance + (m.starting_balance - m.dd_floor));
  const at = x => Math.max(0, Math.min(100, (x - lo) / (hi - lo) * 100));
  $("run-me").style.left = Math.max(2, Math.min(98, at(m.equity))) + "%"; $("run-start").style.left = at(m.starting_balance) + "%";
  $("run-lo").textContent = inr(lo); $("run-hi").textContent = inr(hi);
  if ($("h-goal")) $("h-goal").innerHTML = `${inr(Math.max(0, m.total_pnl))} / ${inr(m.goal_equity - m.starting_balance)}`;
  const du = m.daily_loss_limit - m.daily_loss_left, ddt = m.starting_balance - m.dd_floor, ddu = ddt - m.dd_left;
  $("h-day").innerHTML = `<span class="${du > 0 ? "neg" : ""}">${inr(Math.max(0, du))}</span> / ${inr(m.daily_loss_limit)}`;
  $("h-dd").innerHTML = `<span class="${ddu > 0 ? "neg" : ""}">${inr(Math.max(0, ddu))}</span> / ${inr(ddt)}`;
  $("h-lev").textContent = (notional / (m.equity || 1)).toFixed(2) + "x";
  $("tt").textContent = S.trades_today;
  let ban = "";
  if (S.status === "failed" || S.status === "closed") ban = `<div class="banner">${S.reason || S.status_label}</div>`;
  else if (S.status !== "active") ban = `<div class="banner warn">${S.status_label}. ${S.reason || ""}</div>`;
  else if (!S.within_hours) ban = `<div class="banner warn">Outside trading hours.</div>`;
  if (S.last_error) ban += `<div class="banner warn">Can't reach your Delta account: ${S.last_error}</div>`;
  $("banner").innerHTML = ban;
  $("place").disabled = S.status !== "active";
  renderPositions(); preview();
}
function stopsFor(sym) {
  const sl = ORD.find(o => o.symbol === sym && (o.type || "").includes("stop_loss")), tp = ORD.find(o => o.symbol === sym && (o.type || "").includes("take_profit"));
  return `${sl ? `<span class="neg">${num(+sl.stop, dp())}</span>` : "—"} / ${tp ? `<span class="pos">${num(+tp.stop, dp())}</span>` : "—"}`;
}
function renderPositions() {
  if (!S) return;
  $("n-pos").textContent = S.positions.length || "";
  $("positions").innerHTML = S.positions.map(p => {
    const cv = (S.contract_values || {})[p.symbol] || 0, notional = Math.abs(p.size) * cv * p.entry_price, pct = notional ? p.unrealized_pnl / notional * 100 : 0;
    return `<tr><td><b>${p.symbol}</b> <span class="tag ${p.size > 0 ? "l" : "s"}">${p.size > 0 ? "Long" : "Short"}</span></td>
    <td class="num">${Math.abs(p.size)} <span class="faint small">${trim(Math.abs(p.size) * cv)} ${p.symbol.replace(/USD.*$/, "")}</span></td>
    <td class="num">${num(p.entry_price, dp())}</td><td class="num">${num(p.mark_price, dp())}</td>
    <td class="num faint">${p.liquidation_price ? num(+p.liquidation_price, dp()) : "—"}</td><td class="num">${stopsFor(p.symbol)}</td>
    <td class="num ${cls(p.unrealized_pnl)}"><b>${inr(p.unrealized_pnl, 1)}</b> <span class="small">${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%</span></td>
    <td class="num"><button class="small" data-close="${p.symbol}">Close</button></td></tr>`; }).join("")
    || `<tr><td colspan="8" class="empty">No open positions</td></tr>`;
  $("closeall").hidden = !S.positions.length;
  drawLines();
}
const label = t => ({ limit_order: "Limit", market_order: "Market", stop_loss_order: "Stop-loss", take_profit_order: "Take-profit" }[t] || (t || "").replace(/_/g, " "));
async function loadOrders() {
  const j = await getJSON(U.orders);
  ORD = j.orders || [];
  $("n-ord").textContent = ORD.length || "";
  $("orders").innerHTML = ORD.map(o => `<tr><td><b>${o.symbol}</b></td><td>${label(o.type)}${o.reduce_only ? ' <span class="faint small">reduce-only</span>' : ""}</td>
    <td class="${o.side === "buy" ? "pos" : "neg"}">${o.side === "buy" ? "Buy" : "Sell"}</td><td class="num">${o.size}</td>
    <td class="num">${num(+(o.limit || o.stop), dp())}</td>
    <td class="num"><button class="small" data-cancel="${o.id}" data-pid="${o.product_id}" data-type="${o.type}" data-sym="${o.symbol}">Cancel</button></td></tr>`).join("")
    || `<tr><td colspan="6" class="empty">No open orders</td></tr>`;
  $("fills").innerHTML = (j.fills || []).map(f => `<tr><td class="faint">${when(f.created_at)}</td>
    <td><b>${f.symbol || ""}</b></td><td class="${f.side === "buy" ? "pos" : "neg"}">${f.side === "buy" ? "Buy" : "Sell"}</td>
    <td class="num">${f.size}</td><td class="num">${num(+f.price, dp())}</td><td class="num faint">${inr(+f.fee)}</td></tr>`).join("")
    || `<tr><td colspan="6" class="empty">No fills yet</td></tr>`;
  renderPositions();
}
async function loadHistory() {
  const j = await getJSON(U.hist);
  $("closed").innerHTML = j.trades.map(t => `<tr><td><b>${t.symbol}</b></td><td><span class="tag ${t.side === "long" ? "l" : "s"}">${t.side === "long" ? "Long" : "Short"}</span></td>
    <td class="num">${Math.round(t.size)}</td><td class="num">${num(t.open_price, dp())}</td><td class="num">${num(t.close_price, dp())}</td>
    <td class="num faint">${inr(t.fees)}</td><td class="num ${cls(t.net)}"><b>${inr(t.net, 1)}</b></td>
    <td class="faint">${when(t.open_ts)}</td><td class="faint">${when(t.close_ts)}</td></tr>`).join("")
    || `<tr><td colspan="9" class="empty">Trades show here once a position is fully closed</td></tr>`;
  const act = o => o.kind === "entry" ? `<span class="${o.side === "buy" ? "pos" : "neg"}">${o.side === "buy" ? "Buy" : "Sell"}</span> ${o.symbol} · ${label(o.order_type)}${o.leverage ? " · " + o.leverage + "x" : ""}`
    : ({ flatten: "Auto-close", close: "Close", cancel: "Cancel" }[o.kind] || o.kind) + " " + (o.symbol && o.symbol !== "__all__" ? o.symbol : "all");
  $("logs").innerHTML = j.logs.map(o => `<tr><td class="faint">${when(o.ts)}</td><td>${act(o)}</td><td class="num">${o.size || ""}</td><td class="num">${o.stop_loss || ""}</td>
    <td class="small ${o.status === "rejected" || o.status === "error" ? "neg" : ""}" style="white-space:normal;max-width:440px">${o.status === "sent" && o.kind === "entry" ? "Placed" : (o.message || o.status)}</td></tr>`).join("")
    || `<tr><td colspan="5" class="empty">Nothing yet</td></tr>`;
}

/* ---------- order ticket ---------- */
const cvNow = () => S && S.contract_values[SYM];
function entryPrice() { return otype === "limit_order" ? parseFloat($("price").value) : (T && (side === "buy" ? (T.best_ask || T.last) : (T.best_bid || T.last))); }
function contracts() {
  const v = parseFloat($("qty").value) || 0, cv = cvNow(), e = entryPrice();
  if (!cv) return 0;
  if (unit === "ct") return Math.floor(v);
  if (unit === "coin") return Math.floor(v / cv + 1e-9);
  return e ? Math.floor(v / (cv * e) + 1e-9) : 0;
}
function fromContracts(c) {
  const cv = cvNow(), e = entryPrice();
  if (unit === "ct" || !cv) return c;
  if (unit === "coin") return +(c * cv).toFixed(6);
  return e ? +(c * cv * e).toFixed(2) : 0;
}
const slOn = () => REQUIRE_SL || $("tpsl").checked;
function maxQty() {
  if (!S || !T) return 0;
  const cv = cvNow(), e = entryPrice(), lev = +$("lev").value, sl = slOn() ? parseFloat($("sl").value) : NaN;
  if (!cv || !e) return 0;
  let q = Math.floor(S.metrics.equity * lev / (cv * e));
  if (sl) q = Math.min(q, Math.floor(S.metrics.starting_balance * RISK_PCT / 100 / (cv * Math.abs(e - sl))));
  return Math.max(0, Math.min(q, MAX_CONTRACTS));
}
function preview() {
  if (!S || !T) return;
  const cv = cvNow(), e = entryPrice(), q = contracts(), lev = +$("lev").value, mq = maxQty();
  const reduce = $("reduce").checked, sl = slOn() && !reduce ? parseFloat($("sl").value) : NaN, tp = slOn() && !reduce ? parseFloat($("tp").value) : NaN;
  $("unitcoin").textContent = coin();
  const val = q * cv * (e || 0);
  $("qtyhint").textContent = cv && e ? `${q} contract${q === 1 ? "" : "s"} · ${trim(q * cv)} ${coin()} · $${num(val)}` : "";
  $("pctmax").textContent = mq ? `${Math.min(999, Math.round(q / mq * 100))}% of max (${mq})` : "max —";
  if (document.activeElement !== $("slider")) $("slider").value = mq ? Math.min(100, q / mq * 100) : 0;
  $("pl1").textContent = `${reduce ? "Reduce · " : ""}${side === "buy" ? "Buy / Long" : "Sell / Short"} ${q ? trim(q * cv) : ""} ${coin()}`;
  $("pl2").textContent = q && e ? `${inr(val)} ${otype === "market_order" ? "at market" : "at " + num(e, dp())}` : "";
  if (!e || !q || !cv) { ["c-mar", "c-risk", "c-rew", "c-fee", "c-breach"].forEach(id => $(id).textContent = "—"); drawDraft(null); return; }
  const maxRisk = S.metrics.starting_balance * RISK_PCT / 100, fee = val * FEE;
  $("c-mar").textContent = reduce ? "—" : `${inr(val / lev)} · $${num(val / lev)}`;
  $("c-fee").textContent = inr(fee);
  if (sl) { const r = q * cv * Math.abs(e - sl) + fee; $("c-risk").innerHTML = `<span class="${r > maxRisk ? "neg" : ""}">${inr(r)}</span> <span class="faint">max ${inr(maxRisk)}</span>`; }
  else $("c-risk").innerHTML = REQUIRE_SL && !reduce ? `<span class="neg">set a stop-loss</span>` : "—";
  $("c-rew").innerHTML = tp ? `<span class="pos">${inr(q * cv * Math.abs(tp - e) - fee)}</span>` : "—";
  // price where today's loss limit would be hit, counting any open position in this market
  const pos = S.positions.find(p => p.symbol === SYM), net = (pos ? pos.size : 0) + (reduce ? 0 : (side === "buy" ? q : -q));
  const ref = pos && T.mark ? T.mark : e, room = S.metrics.daily_loss_left - fee;
  if (net && room > 0) {
    const bp = net > 0 ? ref - room / (Math.abs(net) * cv) : ref + room / (Math.abs(net) * cv);
    $("c-breach").textContent = bp > 0 ? `${num(bp, dp())} (${((bp / ref - 1) * 100).toFixed(2)}%)` : "not reachable"; drawDraft(bp > 0 ? bp : null);
  } else { $("c-breach").textContent = room <= 0 ? "limit reached" : "—"; drawDraft(null); }
}
function setSide(s) { side = s; $("sbuy").classList.toggle("on", s === "buy"); $("ssell").classList.toggle("on", s === "sell");
  $("place").className = "place " + s; priceTouched = false; $("sl").value = ""; $("tp").value = ""; loadTicker(); }
$("sbuy").onclick = () => setSide("buy"); $("ssell").onclick = () => setSide("sell");
$$(".ticketp .tabs button").forEach(b => b.onclick = () => { otype = b.dataset.otype; $$(".ticketp .tabs button").forEach(x => x.classList.toggle("on", x === b));
  $("pricerow").hidden = otype !== "limit_order"; preview(); });
$$(".seg button").forEach(b => b.onclick = () => { const c = contracts(); unit = b.dataset.unit; $$(".seg button").forEach(x => x.classList.toggle("on", x === b));
  $("qty").value = fromContracts(c); $("qty").step = unit === "ct" ? 1 : "any"; preview(); });
$("qminus").onclick = () => { $("qty").value = fromContracts(Math.max(1, contracts() - 1)); preview(); };
$("qplus").onclick = () => { $("qty").value = fromContracts(contracts() + 1); preview(); };
$("slider").oninput = () => { $("qty").value = fromContracts(Math.max(1, Math.floor(maxQty() * $("slider").value / 100))); preview(); };
$$("[data-pct]").forEach(b => b.onclick = () => { $("qty").value = fromContracts(Math.max(1, Math.floor(maxQty() * b.dataset.pct / 100))); preview(); });
$("price").addEventListener("input", () => { priceTouched = true; preview(); });
["qty", "sl", "tp", "lev"].forEach(id => $(id).addEventListener("input", preview));
$("tpsl").onchange = () => { $("tpslbox").hidden = !slOn(); preview(); };
$("reduce").onchange = preview;
$$("[data-sl]").forEach(b => b.onclick = () => { const e = entryPrice(); if (!e) return; const k = b.dataset.sl / 100;
  $("sl").value = (side === "buy" ? e * (1 - k) : e * (1 + k)).toFixed(dp()); preview(); });
$$("[data-tp]").forEach(b => b.onclick = () => { const e = entryPrice(); if (!e) return; const k = b.dataset.tp / 100;
  $("tp").value = (side === "buy" ? e * (1 + k) : e * (1 - k)).toFixed(dp()); preview(); });

async function post(url, data) { const fd = new FormData(); Object.entries(data).forEach(([k, v]) => fd.append(k, v ?? "")); fd.append("csrf", CSRF);
  return (await fetch(url, { method: "POST", body: fd })).json(); }
function say(j) { $("msg").className = "msg " + (j.ok ? "ok" : "err"); $("msg").textContent = j.message; }
const refreshAll = () => setTimeout(() => { loadState(); loadOrders(); loadHistory(); }, 600);
$("ticket").onsubmit = async e => {
  e.preventDefault(); $("place").disabled = true; $("msg").className = "msg"; $("msg").textContent = "Sending…";
  const reduce = $("reduce").checked;
  say(await post(U.order, { symbol: SYM, side, order_type: otype === "limit_order" ? "limit" : "market", size: contracts(),
    leverage: $("lev").value, limit_price: otype === "limit_order" ? $("price").value : "",
    stop_loss: slOn() && !reduce ? $("sl").value : "", take_profit: slOn() && !reduce ? $("tp").value : "", reduce_only: reduce ? "1" : "" }));
  refreshAll();
};
document.addEventListener("click", async e => {
  const r = e.target.closest(".brow[data-price]");
  if (r) { $$(".ticketp .tabs button")[1].click(); $("price").value = r.dataset.price; priceTouched = true; preview(); return; }
  const c = e.target.closest("[data-close]");
  if (c) { say(await post(U.close, { symbol: c.dataset.close })); refreshAll(); return; }
  const x = e.target.closest("[data-cancel]");
  if (x) { say(await post(U.cancel, { id: x.dataset.cancel, product_id: x.dataset.pid, type: x.dataset.type, symbol: x.dataset.sym })); setTimeout(loadOrders, 500); }
});
$("closeall").onclick = async () => { say(await post(U.close, { symbol: "__all__" })); refreshAll(); };

/* ---------- tabs, market switch, clock ---------- */
$$(".bookp .tabs button").forEach(b => b.onclick = () => { $$(".bookp .tabs button").forEach(x => x.classList.toggle("on", x === b));
  $("pane-book").hidden = b.dataset.pane !== "book"; $("pane-trades").hidden = b.dataset.pane !== "trades"; loadTrades(); });
$$(".bottomp .tabs [data-tab]").forEach(b => b.onclick = () => { $$(".bottomp .tabs [data-tab]").forEach(x => x.classList.toggle("on", x === b));
  ["pos", "ord", "closed", "fill", "log", "rules"].forEach(t => $("tab-" + t).hidden = t !== b.dataset.tab);
  if (["closed", "log"].includes(b.dataset.tab)) loadHistory(); });
$$(".res button").forEach(b => b.onclick = () => { RES = b.dataset.res; $$(".res button").forEach(x => x.classList.toggle("on", x === b)); loadCandles(true); });
$$(".mkt").forEach(b => b.onclick = () => { SYM = b.dataset.sym; $$(".mkt").forEach(x => x.classList.toggle("on", x === b));
  priceTouched = false; $("sl").value = $("tp").value = ""; T = null; loadTicker(); loadBook(); loadTrades(); loadCandles(true); drawLines(); });
function tick() { const left = Math.max(0, +$("reset").dataset.at - Date.now() / 1000);
  $("reset").textContent = [left / 3600, left % 3600 / 60, left % 60].map(v => String(Math.floor(v)).padStart(2, "0")).join(":"); }
async function otherPrices() {
  for (const b of $$(".mkt")) { if (b.dataset.sym === SYM) continue;
    const j = await getJSON(U.mkt(b.dataset.sym, "ticker")); if (!j.ok) continue;
    const el = $("mk-" + b.dataset.sym); el.textContent = num(j.data.last, j.data.last < 1000 ? 2 : 1); el.className = cls(j.data.change_pct); }
}

loadCandles(true); loadTicker(); loadBook(); loadState(); loadOrders(); loadHistory(); loadMovers(); tick(); otherPrices();
setInterval(loadTicker, 2000); setInterval(loadBook, 1500); setInterval(loadTrades, 2500); setInterval(tick, 1000);
setInterval(loadState, 3000); setInterval(loadOrders, 5000); setInterval(loadHistory, 15000); setInterval(loadMovers, 20000);
setInterval(otherPrices, 6000); setInterval(() => { if (SRC !== "binance" || !ws || ws.readyState !== 1) loadCandles(false); }, 30000);
} };
