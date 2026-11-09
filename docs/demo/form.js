// Inline validation for the Bol OS demo forms, like real bank and government sites do it:
// errors appear under the field (red text, aria-invalid, aria-describedby) after the field loses focus or
// when Submit is pressed, plus a summary at the top. Messages come from data attributes so the English and
// Hindi pages share this file.
(function () {
  "use strict";
  var form = document.querySelector("form[data-demo]");
  if (!form) return;
  var page = document.body.dataset;

  function errorBox(el) {
    return document.getElementById((el.type === "radio" ? el.name : el.id) + "-error");
  }

  function message(el) {
    if (el.type === "radio") {
      var group = form.querySelectorAll('input[name="' + el.name + '"]');
      var picked = Array.prototype.some.call(group, function (r) { return r.checked; });
      return el.required && !picked ? (el.dataset.msgRequired || page.msgRequired) : "";
    }
    if (el.type === "checkbox") {
      return el.required && !el.checked ? (el.dataset.msgRequired || page.msgRequired) : "";
    }
    var value = (el.value || "").trim();
    if (!value) return el.required ? (el.dataset.msgRequired || page.msgRequired) : "";
    if (el.dataset.pattern && !new RegExp("^(?:" + el.dataset.pattern + ")$").test(value)) {
      return el.dataset.msgPattern || page.msgPattern;
    }
    if (el.type === "date" && el.max && value > el.max) return el.dataset.msgPattern || page.msgPattern;
    return "";
  }

  function show(el) {
    var box = errorBox(el);
    if (!box) return true;
    var text = message(el);
    box.textContent = text;
    var group = el.type === "radio" ? form.querySelectorAll('input[name="' + el.name + '"]') : [el];
    Array.prototype.forEach.call(group, function (g) {
      if (text) g.setAttribute("aria-invalid", "true"); else g.removeAttribute("aria-invalid");
    });
    return !text;
  }

  var fields = form.querySelectorAll("input, select, textarea");
  Array.prototype.forEach.call(fields, function (el) {
    if (el.type === "file") return;
    el.addEventListener(el.type === "radio" || el.type === "checkbox" ? "change" : "blur", function () { show(el); });
  });

  form.addEventListener("submit", function (event) {
    var bad = 0;
    var seen = {};
    Array.prototype.forEach.call(fields, function (el) {
      if (el.type === "radio") {
        if (seen[el.name]) return;
        seen[el.name] = true;
      }
      if (!show(el)) bad += 1;
    });
    var summary = document.getElementById("summary");
    if (bad) {
      event.preventDefault();
      summary.textContent = (bad === 1 ? page.msgSummaryOne : page.msgSummary.replace("{n}", bad));
      summary.hidden = false;
      summary.scrollIntoView({ block: "center" });
    } else {
      summary.hidden = true;
    }
  });

  var otpButton = document.getElementById("send-otp");
  if (otpButton) {
    otpButton.addEventListener("click", function () {
      document.getElementById("otp-status").textContent = page.msgOtpDemo;
    });
  }
})();
