// Appearance is independent of replay controls and recorded results.
export function initializeTheme(doc = document) {
  const root = doc.documentElement;
  const selector = doc.querySelector('#appearance');
  const themeColor = doc.querySelector('meta[name="theme-color"]');
  const view = doc.defaultView;

  function applyTheme(value) {
    const theme = value === 'light' ? 'light' : 'dark';
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    selector.value = theme;
    themeColor.content = view.getComputedStyle(root).backgroundColor;
  }

  // The head bootstrap restores the preference before the application loads.
  applyTheme(root.dataset.theme);
  selector.addEventListener('change', () => {
    applyTheme(selector.value);
    try {
      view.localStorage.setItem('meridian-theme', root.dataset.theme);
    } catch {
      // A denied or full store must not prevent changing the current appearance.
    }
  });
}
