const $ = (id) => document.getElementById(id);
const fmt = (value, digits = 2) => value == null ? "—" : Number(value).toLocaleString("en-US", {minimumFractionDigits: digits, maximumFractionDigits: digits});
const fmtBtc = (value) => value == null ? "—" : `${Number(value).toFixed(8)} BTC`;
const priceHistory = [];
let latestPair = null;
let activePreview = null;
let activeConfirmation = "";

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
      latestPair = data.pair;
      $("expiry").textContent = data.pair.expiry;
      $("strike").textContent = `$${fmt(data.pair.strike, 0)}`;
      $("premium").textContent = fmt(data.pair.premium_btc, 6);
      renderQuote("call", data.pair.call);
      renderQuote("put", data.pair.put);
      $("trade-call-label").textContent = `${data.pair.call.name} · ask ${fmt(data.pair.call.ask, 6)} BTC`;
      $("trade-put-label").textContent = `${data.pair.put.name} · ask ${fmt(data.pair.put.ask, 6)} BTC`;
    } else {
      latestPair = null;
    }
    if (data.account) renderAccount(data.account);
    else {
      $("account-content").innerHTML = `<div class="account-error">${escapeHtml(data.account_error || "Account data unavailable.")}</div>`;
    }
    const tradeReady = Boolean(data.account && data.pair?.call?.ask && data.pair?.put?.ask);
    $("preview-orders").disabled = !tradeReady;
    if (!tradeReady) {
      $("trade-status").textContent = data.account_error || "Waiting for fresh call and put quotes.";
      $("trade-status").className = `trade-status ${data.account_error ? "error" : ""}`;
    } else if (!$('trade-status').classList.contains("success")) {
      $("trade-status").textContent = "Testnet credentials and live quotes ready. Select one or both legs to review.";
      $("trade-status").className = "trade-status";
    }
    setConnection("Connected to Deribit Testnet · market data is live", true);
  } catch (error) {
    latestPair = null;
    $("preview-orders").disabled = true;
    $("trade-status").textContent = error.message;
    $("trade-status").className = "trade-status error";
    setConnection(error.message, false);
  }
}

$("retry-button").addEventListener("click", refresh);
refresh();
setInterval(refresh, 3000);

function selectedLegs() {
  return [$("trade-call").checked ? "call" : null, $("trade-put").checked ? "put" : null].filter(Boolean);
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Request failed");
  return data;
}

$("preview-orders").addEventListener("click", async () => {
  const legs = selectedLegs();
  if (!legs.length) {
    $("trade-status").textContent = "Select Call, Put, or both first.";
    $("trade-status").className = "trade-status error";
    return;
  }
  const button = $("preview-orders");
  button.disabled = true;
  button.textContent = "Refreshing preview…";
  try {
    const preview = await postJson("/api/orders/preview", {legs, amount: $("trade-amount").value});
    activePreview = preview;
    activeConfirmation = legs.length === 2 ? "SELL CALL AND PUT" : `SELL ${legs[0].toUpperCase()}`;
    $("confirmation-phrase").textContent = activeConfirmation;
    $("trade-confirmation").value = "";
    $("submit-status").textContent = `Preview expires in ${preview.expires_in} seconds.`;
    $("submit-status").className = "trade-status";
    $("submit-orders").disabled = true;
    $("preview-orders").disabled = false;
    const rows = Object.entries(preview.legs).map(([leg, item]) => `<div class="preview-row"><span>${leg.toUpperCase()}</span><small>${escapeHtml(item.instrument_name)}</small><b>${escapeHtml(item.limit_price_btc)} BTC · ${escapeHtml(preview.amount)} BTC</b></div>`).join("");
    $("trade-preview-details").innerHTML = `<div class="preview-row"><span>PAIR</span><small>Strike ${fmt(preview.strike, 0)} · expiry ${escapeHtml(preview.expiry)}</small><b>${escapeHtml(preview.environment)}</b></div>${rows}<div class="trade-note">GTC limit orders. Verify each contract and price before confirming.</div>`;
    $("trade-dialog").showModal();
  } catch (error) {
    $("trade-status").textContent = error.message;
    $("trade-status").className = "trade-status error";
  } finally {
    button.disabled = !latestPair || !latestPair.call?.ask || !latestPair.put?.ask;
    button.textContent = "Review selected orders";
  }
});

$("trade-confirmation").addEventListener("input", () => {
  $("submit-orders").disabled = $("trade-confirmation").value.trim() !== activeConfirmation || !activePreview;
});

$("cancel-preview").addEventListener("click", () => $("trade-dialog").close());

$("submit-orders").addEventListener("click", async () => {
  if (!activePreview || $("trade-confirmation").value.trim() !== activeConfirmation) return;
  const button = $("submit-orders");
  button.disabled = true;
  button.textContent = "Submitting…";
  $("submit-status").textContent = "Sending confirmed orders to Deribit Testnet…";
  $("submit-status").className = "trade-status";
  try {
    const result = await postJson("/api/orders/submit", {
      preview_id: activePreview.preview_id,
      confirmation: activeConfirmation,
    });
    activePreview = null;
    $("submit-status").textContent = result.orders.map(order => `${order.leg.toUpperCase()} ${order.state || "accepted"} · ${order.order_id || order.instrument_name}`).join(" | ");
    $("submit-status").className = "trade-status success";
    $("trade-status").textContent = "Order response received. Check open orders and positions in Deribit Testnet.";
    $("trade-status").className = "trade-status success";
    refresh();
  } catch (error) {
    activePreview = null;
    $("submit-status").textContent = error.message;
    $("submit-status").className = "trade-status error";
  } finally {
    button.textContent = "Submit testnet orders";
  }
});
