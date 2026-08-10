(function setupRepoScoutTheme() {
  "use strict";

  const THEME_STORAGE_KEY = "reposcout.theme";
  const DARK_THEME = "dark";
  const LIGHT_THEME = "light";
  const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");

  function isTheme(value) {
    return value === DARK_THEME || value === LIGHT_THEME;
  }

  function readExplicitTheme() {
    try {
      const value = window.localStorage.getItem(THEME_STORAGE_KEY);
      return isTheme(value) ? value : null;
    } catch {
      return null;
    }
  }

  function preferredSystemTheme() {
    return systemTheme.matches ? DARK_THEME : LIGHT_THEME;
  }

  function updateThemeControl(theme) {
    const toggle = document.querySelector("#theme-toggle");
    if (!(toggle instanceof HTMLButtonElement)) {
      return;
    }
    const targetTheme = theme === DARK_THEME ? LIGHT_THEME : DARK_THEME;
    const label = `Switch to ${targetTheme} theme`;
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
    const icon = toggle.querySelector("[data-theme-icon]");
    if (icon) {
      icon.textContent = targetTheme === LIGHT_THEME ? "☀" : "☾";
    }
  }

  function applyTheme(theme) {
    const resolvedTheme = isTheme(theme) ? theme : preferredSystemTheme();
    document.documentElement.dataset.theme = resolvedTheme;
    const themeColor = document.querySelector('meta[name="theme-color"]');
    if (themeColor) {
      themeColor.setAttribute("content", resolvedTheme === DARK_THEME ? "#07110f" : "#f4f8f7");
    }
    updateThemeControl(resolvedTheme);
  }

  function persistTheme(theme) {
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch {
      // The selected theme still applies for this page when storage is unavailable.
    }
  }

  applyTheme(readExplicitTheme() ?? preferredSystemTheme());

  document.addEventListener("DOMContentLoaded", () => {
    const toggle = document.querySelector("#theme-toggle");
    updateThemeControl(document.documentElement.dataset.theme);
    toggle?.addEventListener("click", () => {
      const nextTheme =
        document.documentElement.dataset.theme === DARK_THEME ? LIGHT_THEME : DARK_THEME;
      persistTheme(nextTheme);
      applyTheme(nextTheme);
    });
  });

  systemTheme.addEventListener("change", () => {
    if (readExplicitTheme() === null) {
      applyTheme(preferredSystemTheme());
    }
  });

  window.addEventListener("storage", (event) => {
    if (event.key === THEME_STORAGE_KEY || event.key === null) {
      applyTheme(readExplicitTheme() ?? preferredSystemTheme());
    }
  });
})();
