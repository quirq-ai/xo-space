"use strict";
(async () => {
  const status = document.getElementById("status");
  try {
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    const hex = value => Array.from(value, b => b.toString(16).padStart(2, "0")).join("");
    const secret = hex(bytes);
    const hash = hex(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(secret))));
    document.getElementById("command").textContent = `venv/bin/python scripts/founder_credential.py ${hash}`;
    const button = document.getElementById("connect");
    button.disabled = false;
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const response = await fetch("/api/founder/session", {
          method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({secret})
        });
        if (!response.ok) {
          const error = await response.json();
          throw new Error(error.detail?.message || "Connection failed. Check the server enrollment command.");
        }
        status.textContent = "Connected. Return to your Space; this browser can now send messages and decisions.";
      } catch (error) {
        status.textContent = error.message;
        button.disabled = false;
      }
    });
  } catch (error) {
    status.textContent = "Open this page over HTTPS (or localhost) to connect your browser.";
  }
})();
