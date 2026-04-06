(function () {
  // ── Config (auto-replaced by inject_widget.py) ──────────────────────────
  var CHATBOT_API = "https://shopify-ai-storefront.onrender.com";   // e.g. https://abc123.ngrok.io
  var STORE_NAME  = "myaistore";
  var ACCENT      = "#212121";
  var ACCENT_LITE = "#837E6B";

  // ── Inject styles ─────────────────────────────────────────────────────────
  var style = document.createElement("style");
  style.textContent = `
    #ai-chat-btn {
      position: fixed; bottom: 24px; right: 24px; z-index: 99999;
      width: 56px; height: 56px; border-radius: 50%;
      background: ${ACCENT}; color: #fff; border: none;
      font-size: 26px; cursor: pointer; box-shadow: 0 4px 16px rgba(0,0,0,0.25);
      display: flex; align-items: center; justify-content: center;
      transition: transform 0.2s;
    }
    #ai-chat-btn:hover { transform: scale(1.08); }

    #ai-chat-box {
      position: fixed; bottom: 92px; right: 24px; z-index: 99999;
      width: 340px; max-height: 520px;
      background: #fff; border-radius: 16px;
      box-shadow: 0 8px 32px rgba(0,0,0,0.18);
      display: none; flex-direction: column; overflow: hidden;
      font-family: Arial, sans-serif; font-size: 14px;
    }
    #ai-chat-box.open { display: flex; }

    #ai-chat-header {
      background: ${ACCENT}; color: #fff;
      padding: 14px 16px; font-weight: 700; font-size: 15px;
      display: flex; align-items: center; gap: 8px;
    }
    #ai-chat-header span.dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: #4caf50; display: inline-block;
    }
    #ai-chat-clear {
      margin-left: auto; background: none; border: 1px solid rgba(255,255,255,0.4);
      color: #fff; border-radius: 12px; padding: 3px 10px; font-size: 11px;
      cursor: pointer; opacity: 0.8;
    }
    #ai-chat-clear:hover { opacity: 1; }

    #ai-chat-messages {
      flex: 1; overflow-y: auto; padding: 14px;
      display: flex; flex-direction: column; gap: 10px;
      background: #f9f8f6;
    }

    .msg { max-width: 82%; padding: 9px 12px; border-radius: 12px; line-height: 1.45; }
    .msg.user {
      align-self: flex-end; background: ${ACCENT}; color: #fff;
      border-bottom-right-radius: 4px;
    }
    .msg.bot {
      align-self: flex-start; background: #fff; color: #212121;
      border: 1px solid #e8e5e0; border-bottom-left-radius: 4px;
    }
    .msg.bot.typing { color: ${ACCENT_LITE}; font-style: italic; }

    #ai-chat-footer {
      padding: 10px 12px; border-top: 1px solid #ece9e4;
      display: flex; gap: 8px; background: #fff;
    }
    #ai-chat-input {
      flex: 1; border: 1px solid #ddd; border-radius: 20px;
      padding: 8px 14px; font-size: 13px; outline: none;
      font-family: Arial, sans-serif;
    }
    #ai-chat-input:focus { border-color: ${ACCENT}; }
    #ai-chat-send {
      background: ${ACCENT}; color: #fff; border: none;
      border-radius: 20px; padding: 8px 16px; cursor: pointer;
      font-size: 13px; font-weight: 600;
    }
    #ai-chat-send:disabled { opacity: 0.5; cursor: default; }

    @media (max-width: 400px) {
      #ai-chat-box { width: calc(100vw - 32px); right: 16px; }
    }
  `;
  document.head.appendChild(style);

  // ── Build HTML ────────────────────────────────────────────────────────────
  var btn = document.createElement("button");
  btn.id = "ai-chat-btn";
  btn.title = "Chat with us";
  btn.innerHTML = "💬";

  var box = document.createElement("div");
  box.id = "ai-chat-box";
  box.innerHTML = `
    <div id="ai-chat-header">
      <span class="dot"></span> ${STORE_NAME} Assistant
      <button id="ai-chat-clear" title="Clear chat history">Clear</button>
    </div>
    <div id="ai-chat-messages"></div>
    <div id="ai-chat-footer">
      <input id="ai-chat-input" type="text" placeholder="Ask about our shoes..." />
      <button id="ai-chat-send">Send</button>
    </div>
  `;

  document.body.appendChild(btn);
  document.body.appendChild(box);

  // ── State ─────────────────────────────────────────────────────────────────
  var history = [];   // [{role, content}]
  var isOpen  = false;
  var isBusy  = false;

  var STORAGE_KEY = "ai_chat_" + STORE_NAME;
  var HISTORY_TTL = 24 * 60 * 60 * 1000; // 24 hours in ms

  // ── Persist history to localStorage ──────────────────────────────────────
  function saveHistory() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        ts: Date.now(),
        messages: history.slice(-40), // keep last 40 messages max
      }));
    } catch (_) {}
  }

  function loadHistory() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      var saved = JSON.parse(raw);
      if (Date.now() - saved.ts > HISTORY_TTL) {
        localStorage.removeItem(STORAGE_KEY);
        return;
      }
      history = saved.messages || [];
      // Replay messages into UI
      var msgs = document.getElementById("ai-chat-messages");
      history.forEach(function (m) {
        var div = document.createElement("div");
        div.className = "msg " + (m.role === "user" ? "user" : "bot");
        div.textContent = m.content;
        msgs.appendChild(div);
      });
      msgs.scrollTop = msgs.scrollHeight;
    } catch (_) {}
  }

  function clearHistory() {
    history = [];
    localStorage.removeItem(STORAGE_KEY);
    document.getElementById("ai-chat-messages").innerHTML = "";
    addBotMessage("Hi! 👋 I'm your shopping assistant. Ask me anything about our shoes, sizes, or prices!");
  }

  // ── Wake up server on page load ───────────────────────────────────────────
  fetch(CHATBOT_API + "/health").catch(function () {});

  // ── Restore history on load ───────────────────────────────────────────────
  loadHistory();

  // ── Toggle open / close ───────────────────────────────────────────────────
  btn.addEventListener("click", function () {
    isOpen = !isOpen;
    box.classList.toggle("open", isOpen);
    btn.innerHTML = isOpen ? "✕" : "💬";
    if (isOpen && history.length === 0) {
      addBotMessage("Hi! 👋 I'm your shopping assistant. Ask me anything about our shoes, sizes, or prices!");
    }
    if (isOpen) document.getElementById("ai-chat-input").focus();
  });

  document.getElementById("ai-chat-clear").addEventListener("click", clearHistory);

  // ── Send on Enter ─────────────────────────────────────────────────────────
  document.getElementById("ai-chat-input").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  document.getElementById("ai-chat-send").addEventListener("click", sendMessage);

  // ── Cart action ───────────────────────────────────────────────────────────
  var CART_TOKEN_RE = /\[ADD_TO_CART:(\d+):(\d+)\]/;

  function handleCartAction(msgDiv, fullText) {
    var match = fullText.match(CART_TOKEN_RE);
    if (!match) return;

    var variantId = parseInt(match[1], 10);
    var quantity  = parseInt(match[2], 10);

    // Strip the token from displayed text
    msgDiv.textContent = fullText.replace(CART_TOKEN_RE, "").trim();

    // Add to cart via Shopify AJAX API (same-origin, no auth needed)
    fetch("/cart/add.js", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: [{ id: variantId, quantity: quantity }] }),
    })
    .then(function (res) { return res.json(); })
    .then(function (data) {
      if (data.items) {
        var btn = document.createElement("a");
        btn.href = "/cart";
        btn.textContent = "🛒 View Cart →";
        btn.style.cssText = "display:inline-block;margin-top:8px;padding:6px 14px;" +
          "background:" + ACCENT + ";color:#fff;border-radius:20px;font-size:12px;" +
          "font-weight:600;text-decoration:none;";
        msgDiv.appendChild(document.createElement("br"));
        msgDiv.appendChild(btn);
      } else {
        msgDiv.textContent += "\n⚠ Could not add to cart: " + (data.description || "unknown error");
      }
    })
    .catch(function () {
      msgDiv.textContent += "\n⚠ Could not reach cart. Please add manually.";
    });
  }

  // ── Helpers ───────────────────────────────────────────────────────────────
  function addBotMessage(text, isTyping) {
    var msgs = document.getElementById("ai-chat-messages");
    var div = document.createElement("div");
    div.className = "msg bot" + (isTyping ? " typing" : "");
    div.textContent = text;
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
    return div;
  }

  function addUserMessage(text) {
    var msgs = document.getElementById("ai-chat-messages");
    var div = document.createElement("div");
    div.className = "msg user";
    div.textContent = text;
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
  }

  // ── Main send function ────────────────────────────────────────────────────
  function sendMessage() {
    if (isBusy) return;
    var input = document.getElementById("ai-chat-input");
    var send  = document.getElementById("ai-chat-send");
    var text  = input.value.trim();
    if (!text) return;

    input.value = "";
    addUserMessage(text);
    history.push({ role: "user", content: text });
    saveHistory();

    isBusy = true;
    send.disabled = true;

    // Typing indicator — shows "Connecting..." on cold start
    var typingDiv = addBotMessage("Connecting...", true);
    var accumulated = "";

    fetch(CHATBOT_API + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: history }),
    })
    .then(function (res) {
      var reader = res.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      var firstChunk = true;

      function read() {
        reader.read().then(function (result) {
          if (result.done) {
            handleCartAction(typingDiv, accumulated);
            history.push({ role: "assistant", content: accumulated.replace(CART_TOKEN_RE, "").trim() });
            saveHistory();
            isBusy = false;
            send.disabled = false;
            input.focus();
            return;
          }

          buffer += decoder.decode(result.value, { stream: true });
          var lines = buffer.split("\n");
          buffer = lines.pop();  // keep incomplete line in buffer

          lines.forEach(function (line) {
            if (!line.startsWith("data: ")) return;
            var data = line.slice(6).trim();
            if (data === "[DONE]") return;
            try {
              var chunk = JSON.parse(data).text;
              if (firstChunk) {
                typingDiv.classList.remove("typing");
                typingDiv.textContent = "";
                firstChunk = false;
              }
              accumulated += chunk;
              typingDiv.textContent = accumulated;
              document.getElementById("ai-chat-messages").scrollTop = 99999;
            } catch (_) {}
          });

          read();
        }).catch(function (readErr) {
          console.error("[chatbot] stream read error:", readErr);
          typingDiv.classList.remove("typing");
          typingDiv.textContent = "Stream error: " + readErr.message;
          isBusy = false;
          send.disabled = false;
        });
      }
      read();
    })
    .catch(function (err) {
      // Auto-retry once — handles Render free tier cold start (~30s wake-up)
      console.warn("[chatbot] first attempt failed:", err);
      typingDiv.textContent = "Waking up server, retrying in 30s…";
      setTimeout(function () {
        typingDiv.textContent = "Retrying…";
        fetch(CHATBOT_API + "/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ messages: history }),
        })
        .then(function (res) {
          typingDiv.textContent = "...";
          var reader = res.body.getReader();
          var decoder = new TextDecoder();
          var buffer = "";
          var firstChunk = true;
          function read() {
            reader.read().then(function (result) {
              if (result.done) {
                handleCartAction(typingDiv, accumulated);
                history.push({ role: "assistant", content: accumulated.replace(CART_TOKEN_RE, "").trim() });
                saveHistory();
                isBusy = false; send.disabled = false; input.focus(); return;
              }
              buffer += decoder.decode(result.value, { stream: true });
              var lines = buffer.split("\n"); buffer = lines.pop();
              lines.forEach(function (line) {
                if (!line.startsWith("data: ")) return;
                var data = line.slice(6).trim();
                if (data === "[DONE]") return;
                try {
                  var chunk = JSON.parse(data).text;
                  if (firstChunk) { typingDiv.classList.remove("typing"); typingDiv.textContent = ""; firstChunk = false; }
                  accumulated += chunk;
                  typingDiv.textContent = accumulated;
                  document.getElementById("ai-chat-messages").scrollTop = 99999;
                } catch (_) {}
              });
              read();
            }).catch(function (readErr) {
              console.error("[chatbot] stream read error (retry):", readErr);
              typingDiv.classList.remove("typing");
              typingDiv.textContent = "Stream error: " + readErr.message;
              isBusy = false; send.disabled = false;
            });
          }
          read();
        })
        .catch(function (err2) {
          console.error("[chatbot] retry failed:", err2);
          typingDiv.classList.remove("typing");
          typingDiv.textContent = "Connection failed: " + err2.message + ". Check browser console for details.";
          isBusy = false; send.disabled = false;
        });
      }, 30000);  // wait 30s — Render free tier cold start can take ~30s
    });
  }
})();
