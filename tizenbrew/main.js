(() => {
  const REMOTE = 'https://cdn.jsdelivr.net/npm/@foxreis/tizentube@1.15.0/dist/userScript.js';
  const CONFIG_KEY = 'ytaf-configuration';

  try {
    const config = JSON.parse(window.localStorage.getItem(CONFIG_KEY) || '{}');
    config.enableAdBlock = true;
    config.enableSponsorBlock = true;
    config.enableSponsorBlockToasts = false;
    window.localStorage.setItem(CONFIG_KEY, JSON.stringify(config));
  } catch (_) {}

  fetch(REMOTE, { cache: 'no-store' })
    .then((response) => {
      if (!response.ok) throw new Error('TizenTube bundle HTTP ' + response.status);
      return response.text();
    })
    .then((source) => { (0, eval)(source); })
    .catch((error) => { console.error('[Q60R AdFree] userscript load failed:', error); });
})();
