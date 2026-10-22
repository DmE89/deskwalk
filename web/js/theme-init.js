// Runs before the first paint so the page does not flash in the wrong theme.
(function () {
  try {
    var theme = localStorage.getItem("dw-theme");
    if (theme === "system" || theme === "dark" || theme === "light") {
      document.documentElement.setAttribute("data-theme", theme);
    }
  } catch (e) {
    // storage blocked: keep the system theme
  }
})();
