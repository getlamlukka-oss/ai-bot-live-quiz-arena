/**
 * AI Bot Live Quiz Arena — Frontend Controller (`app.js`)
 *
 * Responsibilities:
 *  1. JWT Login workflow (`POST /api/auth/login`): hides Login view on success
 *     and surfaces the active JWT Token in the top header ribbon.
 *  2. Question loading (`GET /api/questions/random`) with category filtering.
 *  3. Bolt Button dispatch (`POST /api/bot/answer`) with animated AI Thinking
 *     effect (orbital pulse + elapsed timer + option scanner).
 *  4. Real-time Log Console powered by WebSocket (`/ws/logs?token=<jwt>`) with
 *     automatic exponential reconnect and Server-Sent Events (`/api/events/stream`)
 *     fallback if WebSocket transport is unavailable.
 */

(() => {
  "use strict";

  const STORAGE_KEY_TOKEN = "quiz_arena_jwt";
  const STORAGE_KEY_USER = "quiz_arena_user";
  const STORAGE_KEY_EXP = "quiz_arena_exp";

  const state = {
    token: null,
    username: null,
    expiresAtMs: null,
    tokenExpanded: false,
    selectedCategory: "",
    currentQuestion: null,
    isThinking: false,
    autoRun: false,
    autoRunAbort: false,
    autoRunQueue: [],
    autoRunIndex: 0,
    autoRunAnswered: 0,
    autoRunCorrect: 0,
    autoRunTotal: 0,
    ws: null,
    sse: null,
    wsHeartbeatId: null,
    wsReconnectTimer: null,
    wsFailures: 0,
    ttlTimerId: null,
    thinkingIntervalId: null,
    scannerIntervalId: null,
    stats: {
      answered: 0,
      correct: 0,
      totalThinkSeconds: 0,
      logCount: 0,
    },
  };

  // DOM References
  const el = {
    loginView: document.getElementById("login-view"),
    arenaView: document.getElementById("arena-view"),
    loginForm: document.getElementById("login-form"),
    usernameInput: document.getElementById("username-input"),
    passwordInput: document.getElementById("password-input"),
    loginError: document.getElementById("login-error"),
    btnLogin: document.getElementById("btn-login"),
    loginBtnLabel: document.getElementById("login-btn-label"),

    operatorName: document.getElementById("operator-name"),
    dataSourceBadge: document.getElementById("data-source-badge"),
    jwtTokenDisplay: document.getElementById("jwt-token-display"),
    btnToggleToken: document.getElementById("btn-toggle-token"),
    btnCopyToken: document.getElementById("btn-copy-token"),
    realtimeBadge: document.getElementById("realtime-badge"),
    realtimeStatusText: document.getElementById("realtime-status-text"),
    btnLogout: document.getElementById("btn-logout"),

    statAnswered: document.getElementById("stat-answered"),
    statAccuracy: document.getElementById("stat-accuracy"),
    statAvgTime: document.getElementById("stat-avg-time"),
    statTokenTtl: document.getElementById("stat-token-ttl"),

    categoryPills: document.querySelectorAll(".pill[data-category]"),
    btnNextQuestion: document.getElementById("btn-next-question"),
    btnAutoRun: document.getElementById("btn-auto-run"),
    autoRunStatus: document.getElementById("auto-run-status"),
    autoRunStatusText: document.getElementById("auto-run-status-text"),
    autoRunScore: document.getElementById("auto-run-score"),
    autoRunBarFill: document.getElementById("auto-run-bar-fill"),
    questionCategoryBadge: document.getElementById("question-category-badge"),
    questionIdLabel: document.getElementById("question-id-label"),
    questionTitle: document.getElementById("question-title"),
    questionContent: document.getElementById("question-content"),
    questionText: document.getElementById("question-text"),
    optionCards: document.querySelectorAll(".option-card"),

    aiThinkingBanner: document.getElementById("ai-thinking-banner"),
    thinkingSubtext: document.getElementById("thinking-subtext"),
    thinkingTimer: document.getElementById("thinking-timer"),

    aiResultBanner: document.getElementById("ai-result-banner"),
    resultBadge: document.getElementById("result-badge"),
    resultMeta: document.getElementById("result-meta"),
    resultExplanation: document.getElementById("result-explanation"),

    btnBoltSend: document.getElementById("btn-bolt-send"),
    boltBtnLabel: document.getElementById("bolt-btn-label"),

    logConsole: document.getElementById("log-console"),
    btnClearLogs: document.getElementById("btn-clear-logs"),
    transportInfo: document.getElementById("transport-info"),
    logCounter: document.getElementById("log-counter"),
    toast: document.getElementById("toast"),
  };

  // =========================================================================
  // Utility Helpers
  // =========================================================================

  function showToast(message) {
    el.toast.textContent = message;
    el.toast.classList.remove("hidden");
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(() => el.toast.classList.add("hidden"), 2600);
  }

  function formatShortToken(token) {
    if (!token) return "--";
    if (token.length <= 42) return token;
    return `${token.slice(0, 22)}...${token.slice(-14)}`;
  }

  function formatTimeStamp(isoString) {
    const dt = isoString ? new Date(isoString) : new Date();
    return dt.toLocaleTimeString("th-TH", { hour12: false });
  }

  function setRealtimeStatus(mode, text) {
    el.realtimeBadge.classList.remove("status-online", "status-connecting", "status-offline");
    el.realtimeBadge.classList.add(`status-${mode}`);
    el.realtimeStatusText.textContent = text;
  }

  async function apiFetch(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (!headers.has("Content-Type") && options.body) {
      headers.set("Content-Type", "application/json");
    }
    if (state.token) {
      headers.set("Authorization", `Bearer ${state.token}`);
    }

    const response = await fetch(path, { ...options, headers });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : { detail: await response.text() };

    if (response.status === 401 && path !== "/api/auth/login") {
      appendConsoleLog({
        actor: "Error",
        level: "error",
        message: "JWT Token หมดอายุหรือไม่ถูกต้อง กำลังพากลับไปหน้า Login",
      });
      logout(false);
      throw new Error(payload.detail || "Unauthorized");
    }

    if (!response.ok) {
      let errMsg = `HTTP ${response.status}`;
      if (Array.isArray(payload.detail)) {
        errMsg = payload.detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
      } else if (typeof payload.detail === "string" && payload.detail) {
        errMsg = payload.detail;
      }
      throw new Error(errMsg);
    }
    return payload;
  }

  // =========================================================================
  // Real-Time Log Console (WebSocket Primary + SSE Fallback)
  // =========================================================================

  function appendConsoleLog({ actor = "System", message = "", level = "info", timestamp = null }) {
    const entry = document.createElement("div");
    entry.className = `log-entry level-${level}`;

    const timeEl = document.createElement("span");
    timeEl.className = "log-time";
    timeEl.textContent = formatTimeStamp(timestamp);

    const actorEl = document.createElement("span");
    actorEl.className = `log-actor actor-${actor}`;
    actorEl.textContent = `[${actor}]`;

    const msgEl = document.createElement("span");
    msgEl.className = "log-msg";
    msgEl.textContent = message;

    entry.append(timeEl, actorEl, msgEl);
    el.logConsole.appendChild(entry);
    el.logConsole.scrollTop = el.logConsole.scrollHeight;

    state.stats.logCount += 1;
    el.logCounter.textContent = `${state.stats.logCount} events`;
  }

  function disconnectRealtime() {
    clearInterval(state.wsHeartbeatId);
    clearTimeout(state.wsReconnectTimer);
    if (state.ws) {
      state.ws.onclose = null;
      state.ws.onerror = null;
      state.ws.close();
      state.ws = null;
    }
    if (state.sse) {
      state.sse.close();
      state.sse = null;
    }
  }

  function handleRealtimeEvent(data) {
    if (!data || data.type === "pong") return;
    appendConsoleLog({
      actor: data.actor || "System",
      message: data.message || "",
      level: data.level || "info",
      timestamp: data.timestamp,
    });

    if (data.type === "ai_analyzing" && state.isThinking) {
      el.thinkingSubtext.textContent = data.message;
    }
  }

  function connectSSEFallback() {
    if (!state.token || state.sse) return;
    const sseUrl = `/api/events/stream?token=${encodeURIComponent(state.token)}`;
    el.transportInfo.textContent = "Transport: SSE Fallback (/api/events/stream)";
    setRealtimeStatus("connecting", "เชื่อมต่อผ่าน SSE...");

    const sse = new EventSource(sseUrl);
    state.sse = sse;

    sse.onopen = () => {
      setRealtimeStatus("online", "SSE Live");
    };

    const onEvent = (evt) => {
      try {
        handleRealtimeEvent(JSON.parse(evt.data));
      } catch (_err) {
        // Ignore non-JSON keepalive frames
      }
    };

    [
      "log",
      "ws_connected",
      "sse_connected",
      "auth_success",
      "auth_failed",
      "question_loaded",
      "question_dispatched",
      "ai_thinking",
      "ai_analyzing",
      "ai_answered",
      "ai_error",
    ].forEach((eventName) => sse.addEventListener(eventName, onEvent));

    sse.onerror = () => {
      if (state.expiresAtMs && Date.now() >= state.expiresAtMs) {
        showToast("JWT Token หมดอายุ กรุณาเข้าสู่ระบบใหม่");
        logout(true);
        return;
      }
      setRealtimeStatus("offline", "SSE หลุดการเชื่อมต่อ");
    };
  }

  function connectWebSocket() {
    disconnectRealtime();
    if (!state.token) return;

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/ws/logs?token=${encodeURIComponent(state.token)}`;
    setRealtimeStatus("connecting", "กำลังเชื่อมต่อ WS...");
    el.transportInfo.textContent = "Transport: WebSocket (/ws/logs)";

    try {
      const ws = new WebSocket(wsUrl);
      state.ws = ws;

      ws.onopen = () => {
        state.wsFailures = 0;
        setRealtimeStatus("online", "WebSocket Live");
        state.wsHeartbeatId = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send("ping");
          }
        }, 25000);
      };

      ws.onmessage = (event) => {
        try {
          handleRealtimeEvent(JSON.parse(event.data));
        } catch (err) {
          console.warn("Malformed WebSocket frame:", err);
        }
      };

      ws.onerror = () => {
        state.wsFailures += 1;
      };

      ws.onclose = () => {
        clearInterval(state.wsHeartbeatId);
        if (!state.token) return;

        if (state.expiresAtMs && Date.now() >= state.expiresAtMs) {
          showToast("JWT Token หมดอายุ กรุณาเข้าสู่ระบบใหม่");
          logout(true);
          return;
        }

        if (state.wsFailures >= 2) {
          appendConsoleLog({
            actor: "System",
            level: "warning",
            message: "WebSocket ไม่ตอบสนอง สลับไปใช้ SSE Real-time อัตโนมัติ...",
          });
          connectSSEFallback();
          return;
        }

        setRealtimeStatus("connecting", "กำลังต่อ WS ใหม่...");
        state.wsReconnectTimer = setTimeout(connectWebSocket, 2500);
      };
    } catch (_err) {
      connectSSEFallback();
    }
  }

  // =========================================================================
  // Auth & View Switching
  // =========================================================================

  function renderTokenDisplay() {
    el.jwtTokenDisplay.textContent = state.tokenExpanded
      ? state.token
      : formatShortToken(state.token);
    el.jwtTokenDisplay.classList.toggle("expanded", state.tokenExpanded);
  }

  function startTokenCountdown() {
    clearInterval(state.ttlTimerId);
    const tick = () => {
      if (!state.expiresAtMs) {
        el.statTokenTtl.textContent = "--:--";
        return;
      }
      const remainingSec = Math.max(0, Math.floor((state.expiresAtMs - Date.now()) / 1000));
      const mins = String(Math.floor(remainingSec / 60)).padStart(2, "0");
      const secs = String(remainingSec % 60).padStart(2, "0");
      el.statTokenTtl.textContent = `${mins}:${secs}`;
      if (remainingSec === 0) {
        clearInterval(state.ttlTimerId);
        showToast("JWT Token หมดอายุ กรุณาเข้าสู่ระบบใหม่");
        logout(true);
      }
    };
    tick();
    state.ttlTimerId = setInterval(tick, 1000);
  }

  async function enterArena() {
    el.loginView.classList.add("hidden");
    el.arenaView.classList.remove("hidden");
    el.operatorName.textContent = state.username || "operator";
    renderTokenDisplay();
    startTokenCountdown();

    connectWebSocket();
    await loadRandomQuestion();
  }

  function logout(clearMessage = true) {
    disconnectRealtime();
    clearInterval(state.ttlTimerId);
    stopThinkingEffects();

    state.token = null;
    state.username = null;
    state.expiresAtMs = null;
    state.currentQuestion = null;
    state.wsFailures = 0;
    state.autoRunAbort = true;
    state.autoRun = false;
    state.autoRunQueue = [];
    state.autoRunIndex = 0;
    state.autoRunAnswered = 0;
    state.autoRunCorrect = 0;
    state.autoRunTotal = 0;
    if (el.btnAutoRun) {
      el.btnAutoRun.disabled = false;
      el.btnAutoRun.classList.remove("running");
      el.btnAutoRun.textContent = "▶ เริ่มตอบทั้งหมด";
    }
    if (el.autoRunStatus) el.autoRunStatus.classList.add("hidden");
    if (el.autoRunBarFill) el.autoRunBarFill.style.width = "0%";
    if (el.autoRunStatusText) el.autoRunStatusText.textContent = "กำลังดึงคลังคำถามทั้งหมด...";
    el.categoryPills.forEach((pill) => {
      pill.disabled = false;
    });
    el.btnNextQuestion.disabled = false;
    state.stats = {
      answered: 0,
      correct: 0,
      totalThinkSeconds: 0,
      logCount: 0,
    };

    el.statAnswered.textContent = "0";
    el.statAccuracy.textContent = "100%";
    el.statAvgTime.textContent = "0.0s";
    el.logConsole.innerHTML = "";
    el.logCounter.textContent = "0 events";

    sessionStorage.removeItem(STORAGE_KEY_TOKEN);
    sessionStorage.removeItem(STORAGE_KEY_USER);
    sessionStorage.removeItem(STORAGE_KEY_EXP);

    el.arenaView.classList.add("hidden");
    el.loginView.classList.remove("hidden");
    el.passwordInput.value = "";
    if (clearMessage) {
      el.loginError.classList.add("hidden");
    }
  }

  async function handleLoginSubmit(event) {
    event.preventDefault();
    const username = el.usernameInput.value.trim();
    const password = el.passwordInput.value;
    if (!username || !password) return;

    el.loginError.classList.add("hidden");
    el.btnLogin.disabled = true;
    el.loginBtnLabel.textContent = "กำลังตรวจสอบสิทธิ์...";

    try {
      const data = await apiFetch("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });

      state.token = data.access_token;
      state.username = data.username;
      state.expiresAtMs = Date.now() + Number(data.expires_in || 7200) * 1000;

      sessionStorage.setItem(STORAGE_KEY_TOKEN, state.token);
      sessionStorage.setItem(STORAGE_KEY_USER, state.username);
      sessionStorage.setItem(STORAGE_KEY_EXP, String(state.expiresAtMs));

      showToast("เข้าสู่ระบบสำเร็จ — ออก JWT Token แล้ว");
      await enterArena();
    } catch (err) {
      el.loginError.textContent = err.message || "เข้าสู่ระบบไม่สำเร็จ";
      el.loginError.classList.remove("hidden");
    } finally {
      el.btnLogin.disabled = false;
      el.loginBtnLabel.textContent = "เข้าสู่ระบบ & สร้าง JWT Token";
    }
  }

  // =========================================================================
  // Quiz Question & AI Bot Bolt Dispatch
  // =========================================================================

  function resetOptionStates() {
    el.optionCards.forEach((card) => {
      card.classList.remove("scanning", "ai-selected");
      const indicator = card.querySelector(".option-indicator");
      if (indicator) indicator.textContent = "";
    });
    el.aiResultBanner.classList.add("hidden");
  }

  function renderQuestion(question) {
    state.currentQuestion = question;
    resetOptionStates();

    el.dataSourceBadge.textContent = question.data_source || "supabase";
    el.questionCategoryBadge.textContent = question.category;
    el.questionIdLabel.textContent = `Question #${question.id}`;
    el.questionTitle.textContent = question.title;
    el.questionContent.textContent = question.content;
    el.questionText.textContent = question.question_text;

    ["A", "B", "C", "D"].forEach((key) => {
      const target = document.getElementById(`opt-text-${key}`);
      if (target) {
        target.textContent = (question.options && question.options[key]) || "--";
      }
    });

    el.btnBoltSend.disabled = state.autoRun;
    el.boltBtnLabel.textContent = state.autoRun
      ? "AI กำลังตอบอัตโนมัติ..."
      : "ส่งคำถามให้ AI บอทตอบ";
  }

  async function loadRandomQuestion() {
    if (state.isThinking || state.autoRun) return;
    el.btnNextQuestion.disabled = true;
    el.btnBoltSend.disabled = true;

    try {
      const params = new URLSearchParams();
      if (state.selectedCategory) {
        params.set("category", state.selectedCategory);
      }
      if (state.currentQuestion && state.currentQuestion.id) {
        params.set("exclude_id", String(state.currentQuestion.id));
      }
      const query = params.toString() ? `?${params.toString()}` : "";
      const question = await apiFetch(`/api/questions/random${query}`);
      renderQuestion(question);
    } catch (err) {
      appendConsoleLog({
        actor: "Error",
        level: "error",
        message: `ดึงคำถามไม่สำเร็จ: ${err.message}`,
      });
      showToast(`เกิดข้อผิดพลาด: ${err.message}`);
    } finally {
      el.btnNextQuestion.disabled = false;
      if (state.currentQuestion && !state.isThinking) {
        el.btnBoltSend.disabled = false;
      }
    }
  }

  function startThinkingEffects() {
    state.isThinking = true;
    resetOptionStates();
    el.btnBoltSend.disabled = true;
    el.btnNextQuestion.disabled = true;
    el.boltBtnLabel.textContent = "AI กำลังประมวลผล...";
    el.aiThinkingBanner.classList.remove("hidden");
    el.thinkingSubtext.textContent = "กำลังประมวลผลบริบท ROV Academy และเปรียบเทียบตัวเลือก A, B, C, D";

    const startTime = performance.now();
    el.thinkingTimer.textContent = "0.0s";
    state.thinkingIntervalId = setInterval(() => {
      const elapsed = ((performance.now() - startTime) / 1000).toFixed(1);
      el.thinkingTimer.textContent = `${elapsed}s`;
    }, 100);

    // Visual scanner cycling through options A -> B -> C -> D while AI thinks
    let scanIndex = 0;
    state.scannerIntervalId = setInterval(() => {
      el.optionCards.forEach((c) => c.classList.remove("scanning"));
      if (el.optionCards[scanIndex]) {
        el.optionCards[scanIndex].classList.add("scanning");
      }
      scanIndex = (scanIndex + 1) % el.optionCards.length;
    }, 320);
  }

  function stopThinkingEffects() {
    state.isThinking = false;
    clearInterval(state.thinkingIntervalId);
    clearInterval(state.scannerIntervalId);
    el.aiThinkingBanner.classList.add("hidden");
    el.optionCards.forEach((c) => c.classList.remove("scanning"));
  }

  function updateMetrics(result) {
    state.stats.answered += 1;
    if (result.is_correct) state.stats.correct += 1;
    state.stats.totalThinkSeconds += Number(result.thinking_seconds || 0);

    const accuracy = Math.round((state.stats.correct / state.stats.answered) * 100);
    const avgSec = (state.stats.totalThinkSeconds / state.stats.answered).toFixed(2);

    el.statAnswered.textContent = String(state.stats.answered);
    el.statAccuracy.textContent = `${accuracy}%`;
    el.statAvgTime.textContent = `${avgSec}s`;
  }

  function renderAnswerResult(result) {
    const chosenCard = document.querySelector(`.option-card[data-option="${result.selected_option}"]`);
    if (chosenCard) {
      chosenCard.classList.add("ai-selected");
      const indicator = chosenCard.querySelector(".option-indicator");
      if (indicator) indicator.textContent = "✅ AI เลือกข้อนี้";
    }

    el.resultBadge.textContent = result.is_correct
      ? `✅ AI ตอบข้อ ${result.selected_option} ถูกต้อง!`
      : `⚠️ AI ตอบข้อ ${result.selected_option}`;
    el.resultMeta.textContent = `ใช้เวลาคิด ${result.thinking_seconds.toFixed(2)}s · ความมั่นใจ ${Math.round(
      result.confidence * 100
    )}%`;
    el.resultExplanation.textContent = result.explanation;
    el.aiResultBanner.classList.remove("hidden");
  }

  async function answerCurrentQuestion({ auto = false } = {}) {
    if (!state.currentQuestion || state.isThinking) return null;

    startThinkingEffects();

    try {
      const result = await apiFetch("/api/bot/answer", {
        method: "POST",
        body: JSON.stringify({ question_id: state.currentQuestion.id }),
      });

      stopThinkingEffects();
      updateMetrics(result);
      renderAnswerResult(result);
      return result;
    } catch (err) {
      stopThinkingEffects();
      appendConsoleLog({
        actor: "Error",
        level: "error",
        message: `ส่งคำถามให้ AI ไม่สำเร็จ: ${err.message}`,
      });
      showToast(`เกิดข้อผิดพลาด: ${err.message}`);
      if (auto) state.autoRunAbort = true;
      return null;
    } finally {
      if (!state.autoRun) {
        el.btnBoltSend.disabled = false;
        el.btnNextQuestion.disabled = false;
      }
    }
  }

  async function handleBoltSend() {
    if (state.autoRun) return;
    const result = await answerCurrentQuestion();
    if (result) {
      el.boltBtnLabel.textContent = "ส่งคำถามนี้ให้ AI ตอบอีกครั้ง";
    }
  }

  // =========================================================================
  // Auto-Run: Pull the whole question bank and let the bot answer everything
  // =========================================================================

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function updateAutoRunProgress(done, total, correct) {
    state.autoRunAnswered = done;
    state.autoRunCorrect = correct;
    const percent = total ? Math.round((done / total) * 100) : 0;
    const accuracy = done ? Math.round((correct / done) * 100) : 100;
    el.autoRunScore.textContent = `${done}/${total} · ${accuracy}%`;
    el.autoRunBarFill.style.width = `${percent}%`;
  }

  function setAutoRunUI(running) {
    state.autoRun = running;
    el.btnAutoRun.classList.toggle("running", running);
    el.btnAutoRun.textContent = running ? "⏹ หยุดการตอบ" : "▶ เริ่มตอบทั้งหมด";
    el.categoryPills.forEach((pill) => {
      pill.disabled = running;
    });
    el.btnNextQuestion.disabled = running;
    if (running) {
      el.btnBoltSend.disabled = true;
      el.boltBtnLabel.textContent = "AI กำลังตอบอัตโนมัติ...";
    } else {
      el.btnBoltSend.disabled = !state.currentQuestion;
      el.boltBtnLabel.textContent = "ส่งคำถามให้ AI บอทตอบ";
    }
  }

  async function startAutoRun() {
    if (state.autoRun) {
      state.autoRunAbort = true;
      el.btnAutoRun.disabled = true;
      el.btnAutoRun.textContent = "⏹ กำลังหยุด...";
      return;
    }

    if (state.isThinking) {
      showToast("กรุณารอให้ AI ตอบข้อปัจจุบันก่อน");
      return;
    }

    if (!state.token) {
      showToast("กรุณาเข้าสู่ระบบก่อน");
      return;
    }

    setAutoRunUI(true);
    state.autoRunAbort = false;
    state.autoRunTotal = 0;
    state.autoRunQueue = [];
    el.autoRunStatus.classList.remove("hidden");
    el.autoRunStatusText.textContent = "กำลังดึงคลังคำถามทั้งหมดจาก Backend...";
    updateAutoRunProgress(0, 0, 0);

    let questions = [];
    try {
      const params = new URLSearchParams();
      if (state.selectedCategory) params.set("category", state.selectedCategory);
      const query = params.toString() ? `?${params.toString()}` : "";
      const payload = await apiFetch(`/api/questions${query}`);
      questions = payload.questions || [];
    } catch (err) {
      appendConsoleLog({
        actor: "Error",
        level: "error",
        message: `ดึงคลังคำถามทั้งหมดไม่สำเร็จ: ${err.message}`,
      });
      showToast(`ดึงคลังคำถามไม่สำเร็จ: ${err.message}`);
      setAutoRunUI(false);
      el.autoRunStatus.classList.add("hidden");
      return;
    }

    if (questions.length === 0) {
      showToast("ไม่พบคำถามในหมวดที่เลือก");
      setAutoRunUI(false);
      el.autoRunStatus.classList.add("hidden");
      return;
    }

    state.autoRunQueue = questions;
    state.autoRunTotal = questions.length;
    updateAutoRunProgress(0, questions.length, 0);
    el.autoRunStatusText.textContent = `พบ ${questions.length} คำถาม — เริ่มตอบอัตโนมัติ...`;

    let answered = 0;
    let correct = 0;

    for (let i = 0; i < questions.length; i += 1) {
      if (state.autoRunAbort || !state.token) break;

      const question = questions[i];
      state.autoRunIndex = i;
      renderQuestion(question);
      el.autoRunStatusText.textContent = `กำลังตอบข้อ ${i + 1}/${questions.length} — #${question.id} [${question.category}] ${question.title}`;
      updateAutoRunProgress(i, questions.length, correct);

      const result = await answerCurrentQuestion({ auto: true });
      if (result) {
        answered += 1;
        if (result.is_correct) correct += 1;
      }
      updateAutoRunProgress(answered, questions.length, correct);

      if (state.autoRunAbort) break;
      await sleep(350);
    }

    const aborted = state.autoRunAbort || !state.token;
    state.autoRunAbort = false;
    setAutoRunUI(false);
    el.btnAutoRun.disabled = false;

    if (!state.token) return;

    if (aborted) {
      el.autoRunStatusText.textContent = `หยุดการตอบแล้ว — ตอบไป ${answered}/${questions.length} ข้อ`;
      showToast(`หยุดการตอบแล้ว (${answered}/${questions.length} ข้อ)`);
      return;
    }

    const accuracy = answered ? Math.round((correct / answered) * 100) : 0;
    el.autoRunStatusText.textContent = `ตอบครบทั้งคลังแล้ว ${answered}/${questions.length} ข้อ · ความแม่นยำ ${accuracy}%`;
    el.autoRunScore.textContent = `${answered}/${questions.length} · ${accuracy}%`;
    appendConsoleLog({
      actor: "System",
      level: "success",
      message: `Auto-Run เสร็จสิ้น: ตอบ ${answered}/${questions.length} ข้อ ความแม่นยำ ${accuracy}%`,
    });
    showToast(`AI ตอบครบทั้ง ${answered} ข้อ (แม่นยำ ${accuracy}%)`);
  }

  // =========================================================================
  // Event Binding & Session Bootstrap
  // =========================================================================

  function bindEvents() {
    el.loginForm.addEventListener("submit", handleLoginSubmit);
    el.btnLogout.addEventListener("click", () => {
      logout(true);
      showToast("ออกจากระบบเรียบร้อยแล้ว");
    });

    el.btnToggleToken.addEventListener("click", () => {
      state.tokenExpanded = !state.tokenExpanded;
      renderTokenDisplay();
    });

    el.btnCopyToken.addEventListener("click", async () => {
      if (!state.token) return;
      try {
        await navigator.clipboard.writeText(state.token);
        showToast("คัดลอก JWT Token แล้ว");
      } catch (_err) {
        showToast("ไม่สามารถเข้าถึง Clipboard ได้");
      }
    });

    el.categoryPills.forEach((pill) => {
      pill.addEventListener("click", () => {
        if (state.isThinking || state.autoRun) return;
        el.categoryPills.forEach((p) => p.classList.remove("active"));
        pill.classList.add("active");
        state.selectedCategory = pill.dataset.category || "";
        loadRandomQuestion();
      });
    });

    el.btnNextQuestion.addEventListener("click", loadRandomQuestion);
    el.btnAutoRun.addEventListener("click", startAutoRun);
    el.btnBoltSend.addEventListener("click", handleBoltSend);
    el.btnClearLogs.addEventListener("click", () => {
      el.logConsole.innerHTML = "";
      state.stats.logCount = 0;
      el.logCounter.textContent = "0 events";
    });
  }

  async function bootstrap() {
    bindEvents();

    const savedToken = sessionStorage.getItem(STORAGE_KEY_TOKEN);
    const savedUser = sessionStorage.getItem(STORAGE_KEY_USER);
    const savedExp = Number(sessionStorage.getItem(STORAGE_KEY_EXP) || "0");

    if (savedToken && savedExp > Date.now()) {
      state.token = savedToken;
      state.username = savedUser || "operator";
      state.expiresAtMs = savedExp;
      try {
        await apiFetch("/api/auth/me");
        await enterArena();
      } catch (_err) {
        logout(true);
      }
    }
  }

  bootstrap();
})();
