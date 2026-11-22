/* Для отдельного сервера задайте HTTPS URL API до загрузки app.js.
   Настройки не содержат учетных данных. */
window.KOMPOZA_CONFIG = Object.assign({ apiBase: '/api' }, window.KOMPOZA_CONFIG || {});
// GitHub Pages hosts the source entry; authenticated work stays on one HTTPS origin.
if (location.hostname === 'whsmotilek.github.io') {
  location.replace('https://compoza.95-163-227-180.sslip.io/app/');
}
