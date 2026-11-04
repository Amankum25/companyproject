const $ = (id) => document.getElementById(id);
const fmt = (value, digits = 2) => value == null ? "—" : Number(value).toLocaleString("en-US", {minimumFractionDigits: digits, maximumFractionDigits: digits});
const fmtBtc = (value) => value == null ? "—" : `${Number(value).toFixed(8)} BTC`;
const priceHistory = [];

function drawPriceHistory(price) {
  priceHistory.push(price);
  if (priceHistory.length > 36) priceHistory.shift();
  if (priceHistory.length < 2) return;
  const low = Math.min(...priceHistory), high = Math.max(...priceHistory), span = high - low || 1;
  const points = priceHistory.map((value, index) => `${(index / (priceHistory.length - 1)) * 220},${43 - ((value - low) / span) * 38}`);
  const line = `M${points.join(" L")}`;
  $("sparkline-line").setAttribute("d", line);
  $("sparkline-fill").setAttribute("d", `${line} L220,48 L0,48 Z`);
}

function setConnection(message, ok) {
  $("connection-message").textContent = message;
  $("connection-alert").classList.toggle("offline", !ok);
  const liveTag = document.querySelector(".live-tag");
  liveTag.classList.toggle("stale", !ok);
  liveTag.innerHTML = `<i></i> ${ok ? "LIVE" : "STALE"}`;
  const source = document.querySelector(".source-pill");
  source.innerHTML = `<span class="live-dot"></span> ${ok ? "EXCHANGE DATA" : "STALE DATA"}`;
  $("last-updated").textContent = ok ? `Updated ${new Date().toLocaleTimeString()}` : "Waiting for connection";
}

function renderQuote(prefix, quote) {
  $(`${prefix}-name`).textContent = quote?.name ?? "—";
  $(`${prefix}-bid`).textContent = fmt(quote?.bid, 6);
  $(`${prefix}-ask`).textContent = fmt(quote?.ask, 6);
  $(`${prefix}-mark`).textContent = fmt(quote?.mark, 6);
  $(`${prefix}-iv`).textContent = quote?.iv == null ? "—" : `${fmt(quote.iv, 1)}%`;
}

function renderAccount(data) {
  if (!data) {
    $("account-content").innerHTML = `<div class="account-error">${escapeHtml($('account-error-text')?.textContent || "Account data unavailable.")}</div>`;
    return;
  }
  $("account-content").innerHTML = `<div class="account-grid"><div class="account-stat"><span>ACCOUNT EQUITY</span><b>${fmtBtc(data.equity_btc)}</b></div><div class="account-stat"><span>AVAILABLE FUNDS</span><b>${fmtBtc(data.available_btc)}</b></div><div class="account-stat"><span>INITIAL MARGIN</span><b>${fmtBtc(data.initial_margin_btc)}</b></div><div class="account-stat"><span>ENVIRONMENT</span><b>Testnet</b></div></div>`;
}

function escapeHtml(value) { return String(value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

async function refresh() {
  try {
    const response = await fetch("/api/dashboard", {cache: "no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Exchange connection unavailable");
    $("btc-price").textContent = `$${fmt(data.btc_index, 2)}`;
    drawPriceHistory(data.btc_index);
    $("instrument-count").textContent = `${Number(data.option_count).toLocaleString()} option instruments`;
    if (data.pair) {
      $("expiry").textContent = data.pair.expiry;
      $("strike").textContent = `$${fmt(data.pair.strike, 0)}`;
      $("premium").textContent = fmt(data.pair.premium_btc, 6);
      renderQuote("call", data.pair.call);
      renderQuote("put", data.pair.put);
    }
    if (data.account) renderAccount(data.account);
    else {
      $("account-content").innerHTML = `<div class="account-error">${escapeHtml(data.account_error || "Account data unavailable.")}</div>`;
    }
    setConnection("Connected to Deribit Testnet · market data is live", true);
  } catch (error) {
    setConnection(error.message, false);
  }
}

$("retry-button").addEventListener("click", refresh);
refresh();
setInterval(refresh, 3000);
