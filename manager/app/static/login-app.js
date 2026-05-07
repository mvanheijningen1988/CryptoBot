/**
 * CryptoBot Manager – Login Page Logic
 *
 * Handles credential submission, JWT storage, forced password change
 * on first login, language switching, and auto-redirect when a valid
 * session already exists.
 */

/** Active UI language code ("en" | "nl"). */
let lang = localStorage.getItem("cryptobot_lang") || "en";

/** JWT token received after successful login (kept in memory for the
 *  password-change step before it is persisted to localStorage). */
let authToken = null;

/**
 * Translate a key using the I18N dictionary loaded from i18n.js.
 * Falls back to the English value, then to the raw key.
 *
 * @param {string} key - Translation key.
 * @returns {string} Translated string.
 */
function t(key) {
  return (I18N[lang] && I18N[lang][key]) || (I18N.en && I18N.en[key]) || key;
}

/**
 * Apply the current language to every translatable element on the
 * login page (title, labels, buttons, overlay texts) and highlight
 * the active language link.
 */
function applyLang() {
  document.getElementById("login_title").textContent = t("login_title");
  document.getElementById("lbl_user").textContent = t("lbl_username");
  document.getElementById("lbl_pass").textContent = t("lbl_password");
  document.getElementById("btn_login").textContent = t("btn_login");
  document.getElementById("pw_title").textContent = t("change_pw_title");
  document.getElementById("pw_msg").textContent = t("change_pw_msg");
  document.getElementById("lbl_new_pw").textContent = t("lbl_new_password");
  document.getElementById("lbl_confirm_pw").textContent = t("lbl_confirm_password");
  document.getElementById("btn_change_pw").textContent = t("btn_change_pw");

  // Password rules text
  document.querySelectorAll("[data-i18n-rule]").forEach((el) => {
    el.textContent = t(el.dataset.i18nRule);
  });

  // Highlight the active language link
  document.querySelectorAll(".lang-switch a").forEach((a) => {
    a.classList.toggle("active", a.dataset.lang === lang);
  });
}

// ──────────────────────────────────────────────────────────────
// Language switcher
// ──────────────────────────────────────────────────────────────

document.querySelectorAll(".lang-switch a").forEach((a) => {
  a.onclick = (e) => {
    e.preventDefault();
    lang = a.dataset.lang;
    localStorage.setItem("cryptobot_lang", lang);
    applyLang();
  };
});

// ──────────────────────────────────────────────────────────────
// Login form submission
// ──────────────────────────────────────────────────────────────

document.getElementById("btn_login").onclick = async () => {
  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value;
  const errEl = document.getElementById("error_msg");
  errEl.style.display = "none";

  try {
    const res = await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });

    if (!res.ok) {
      errEl.textContent = t("login_error");
      errEl.style.display = "block";
      return;
    }

    const data = await res.json();
    authToken = data.token;
    localStorage.setItem("cryptobot_token", data.token);

    // Store session timing for auto-logout
    if (data.session_max_seconds) {
      localStorage.setItem("cryptobot_session_start", String(Date.now()));
      localStorage.setItem("cryptobot_session_max", String(data.session_max_seconds));
    }

    // Adopt server-side locale if it differs from the current selection
    if (data.user.locale && data.user.locale !== lang) {
      lang = data.user.locale;
      localStorage.setItem("cryptobot_lang", lang);
    }

    // If the account requires a password change, show the overlay
    if (data.user.must_change_password) {
      document.getElementById("pw_overlay").style.display = "flex";
      applyLang();
    } else {
      window.location.href = "/";
    }
  } catch (err) {
    errEl.textContent = t("login_error");
    errEl.style.display = "block";
  }
};

/** Allow pressing Enter in the password field to submit. */
document.getElementById("password").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("btn_login").click();
});

// ──────────────────────────────────────────────────────────────
// Password change overlay
// ──────────────────────────────────────────────────────────────

/** Validate password rules and update the checklist UI. Returns true if all pass. */
function validatePasswordRules(pw) {
  const rules = [
    { id: "pw_rule_length", pass: pw.length >= 8 },
    { id: "pw_rule_digit", pass: /\d/.test(pw) },
    { id: "pw_rule_special", pass: /[^A-Za-z0-9]/.test(pw) },
  ];
  let allPass = true;
  for (const r of rules) {
    const li = document.getElementById(r.id);
    if (!li) continue;
    li.className = r.pass ? "rule-pass" : "rule-fail";
    li.querySelector(".pw-rule-icon").textContent = r.pass ? "✓" : "✗";
    if (!r.pass) allPass = false;
  }
  return allPass;
}

document.getElementById("new_password").addEventListener("input", (e) => {
  validatePasswordRules(e.target.value);
});

document.getElementById("btn_change_pw").onclick = async () => {
  const newPw = document.getElementById("new_password").value;
  const confirmPw = document.getElementById("confirm_password").value;
  const errEl = document.getElementById("pw_error");
  errEl.style.display = "none";

  // Client-side validation
  if (!validatePasswordRules(newPw)) {
    errEl.textContent = t("pw_requirements_not_met");
    errEl.style.display = "block";
    return;
  }
  if (newPw !== confirmPw) {
    errEl.textContent = t("pw_mismatch");
    errEl.style.display = "block";
    return;
  }

  try {
    const res = await fetch("/api/v1/auth/change-password", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + authToken,
      },
      body: JSON.stringify({ new_password: newPw }),
    });

    if (!res.ok) {
      const txt = await res.text();
      errEl.textContent = txt || "Error";
      errEl.style.display = "block";
      return;
    }

    // Password changed successfully → proceed to dashboard
    window.location.href = "/";
  } catch (err) {
    errEl.textContent = "Error";
    errEl.style.display = "block";
  }
};

/** Allow pressing Enter in the confirm-password field to submit. */
document.getElementById("confirm_password").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("btn_change_pw").click();
});

// ──────────────────────────────────────────────────────────────
// Auto-redirect check
// ──────────────────────────────────────────────────────────────

/**
 * On page load, check whether a valid session already exists.
 * If the stored token is still valid and the user does NOT need
 * to change their password, redirect straight to the dashboard.
 */
(async () => {
  const token = localStorage.getItem("cryptobot_token");
  if (token) {
    try {
      const res = await fetch("/api/v1/auth/me", {
        headers: { Authorization: "Bearer " + token },
      });
      if (res.ok) {
        const user = await res.json();
        if (!user.must_change_password) {
          window.location.href = "/";
          return;
        }
      }
    } catch (e) {
      // Token invalid or expired – stay on login page
    }
  }
  applyLang();
})();
