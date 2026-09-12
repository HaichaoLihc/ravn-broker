"use strict";
// Explicit POST + navigation also works in embedded browsers that suppress
// native form redirects. This script is served only by the local simulator.
document.querySelector("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  data.set("decision", event.submitter?.value || "deny");
  const buttons = form.querySelectorAll("button");
  buttons.forEach((button) => (button.disabled = true));
  try {
    const response = await fetch("/consent", {
      method: "POST",
      credentials: "same-origin",
      redirect: "error",
      headers: { Accept: "application/json" },
      body: new URLSearchParams(data),
    });
    if (!response.ok)
      throw new Error(
        "Consent expired or was rejected. Return to Support Desk and start again.",
      );
    const value = await response.json();
    location.assign(value.redirect_url);
  } catch (error) {
    document.getElementById("consent-error").textContent = error.message;
    buttons.forEach((button) => (button.disabled = false));
  }
});
