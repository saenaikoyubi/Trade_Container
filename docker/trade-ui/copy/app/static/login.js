"use strict";

const form = document.querySelector("#login-form");
const password = document.querySelector("#ui-password");
const error = document.querySelector("#login-error");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.hidden = true;
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const response = await fetch("/auth/login", {
      method: "POST",
      headers: {"Content-Type": "application/json", "Accept": "application/json"},
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify({password: password.value}),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(typeof payload.detail === "string" ? payload.detail : "ログインに失敗しました");
    }
    password.value = "";
    window.location.assign("/");
  } catch (exc) {
    error.textContent = exc.message || "ログインに失敗しました";
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
});
