(() => {
  const PROXY =
    'http://127.0.0.1:8081/module/npm%2F%40foxreis%2Ftizentube/dist/userScript.js';
  const CONFIG_KEY = 'ytaf-configuration';

  try {
    const config = JSON.parse(window.localStorage.getItem(CONFIG_KEY) || '{}');
    config.enableAdBlock = true;
    config.enableSponsorBlock = true;
    config.enableSponsorBlockToasts = false;
    window.localStorage.setItem(CONFIG_KEY, JSON.stringify(config));
  } catch (_) {}

  fetch(PROXY, { cache: 'no-store' })
    .then((response) => {
      if (!response.ok) throw new Error('TizenTube bundle HTTP ' + response.status);
      return response.text();
    })
    .then((source) => { (0, eval)(source); })
    .catch((error) => {
      console.error('[yt.adfree] userscript load failed:', error);
    });
})();
