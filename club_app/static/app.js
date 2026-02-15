(() => {
  const tabsRoot = document.querySelector('.tabs');
  if (tabsRoot) {
    const tabButtons = Array.from(tabsRoot.querySelectorAll('button[data-tab]'));
    const panels = Array.from(document.querySelectorAll('.tab-panel'));

    const setActive = (tab) => {
      tabButtons.forEach((btn) => {
        btn.classList.toggle('active', btn.dataset.tab === tab);
      });
      panels.forEach((panel) => {
        panel.classList.toggle('active', panel.id === `tab-${tab}`);
      });

      const url = new URL(window.location.href);
      url.searchParams.set('tab', tab);
      window.history.replaceState({}, '', url);
    };

    tabButtons.forEach((btn) => {
      btn.addEventListener('click', () => setActive(btn.dataset.tab));
    });

    const startTab = tabsRoot.dataset.activeTab || 'squad';
    setActive(startTab);
  }

  const searchBox = document.getElementById('appointment-search');
  const hideFinished = document.getElementById('hide-finished');
  const cards = Array.from(document.querySelectorAll('.appointment-card'));

  const applyFilters = () => {
    const q = (searchBox?.value || '').toLowerCase().trim();
    const hide = Boolean(hideFinished?.checked);

    cards.forEach((card) => {
      const text = (card.dataset.search || '').toLowerCase();
      const status = (card.dataset.status || '').toLowerCase();
      const matchesSearch = !q || text.includes(q);
      const hiddenByStatus = hide && (status === 'completed' || status === 'canceled');
      card.style.display = matchesSearch && !hiddenByStatus ? '' : 'none';
    });
  };

  if (searchBox) {
    searchBox.addEventListener('input', applyFilters);
  }
  if (hideFinished) {
    hideFinished.addEventListener('change', applyFilters);
  }

  applyFilters();
})();
